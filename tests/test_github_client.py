import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from repka.config import ConfigError, GithubConfig
from repka.github.client import (
    AuthError,
    GitHubClient,
    GitHubError,
    RateLimited,
)

FIX = Path(__file__).parent / "fixtures" / "github"
TOKEN = "ghp_SECRETTOKEN123"


class Recorder:
    """A MockTransport handler that replays queued responses and records requests."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class DictCache:
    def __init__(self):
        self.data: dict[str, tuple[str, str]] = {}

    def get(self, url):
        return self.data.get(url)

    def put(self, url, etag, body):
        self.data[url] = (etag, body)


def make(*responses, cfg=None, token_source="env", cache=None):
    rec = Recorder(*responses)
    fake = FakeTime()
    client = GitHubClient(
        TOKEN,
        cfg or GithubConfig(),
        transport=httpx.MockTransport(rec),
        clock=fake.clock,
        sleep=fake.sleep,
        cache=cache,
        token_source=token_source,
    )
    return client, rec, fake


def ok(payload=None, **kw):
    return httpx.Response(200, json=payload if payload is not None else {}, **kw)


def search_ok():
    return httpx.Response(200, json={"total_count": 1, "incomplete_results": False, "items": []})


def test_headers_and_auth():
    client, rec, _ = make(search_ok())
    client.search_repos("topic:mcp", page=2, per_page=30)
    req = rec.requests[0]
    assert req.url.host == "api.github.com" and req.url.path == "/search/repositories"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["accept"] == "application/vnd.github+json"
    assert req.headers["x-github-api-version"] == "2022-11-28"
    assert req.headers["user-agent"] == "repka-cli"
    q = dict(req.url.params)
    assert q["q"] == "topic:mcp" and q["page"] == "2" and q["per_page"] == "30" and q["sort"] == "stars"


def test_search_parses_page_fixture():
    client, _, _ = make(httpx.Response(200, content=(FIX / "search_page.json").read_bytes()))
    page = client.search_repos("x")
    assert page.total_count == 2 and not page.incomplete and [i["id"] for i in page.items] == [1, 2]


def test_search_throttle_sleeps_after_limit():
    client, _, fake = make(search_ok(), cfg=GithubConfig(search_per_minute=2))
    for _ in range(3):
        client.search_total_count("q")
    assert len(fake.sleeps) == 1 and fake.sleeps[0] == pytest.approx(60)


def test_secondary_limit_raises_rate_limited():
    resp = httpx.Response(
        403,
        json={"message": "You have exceeded a secondary rate limit"},
        headers={"Retry-After": "120"},
    )
    client, rec, fake = make(resp)
    with pytest.raises(RateLimited) as exc:
        client.search_total_count("q")
    assert exc.value.retry_at is not None
    assert len(rec.requests) == 1 and fake.sleeps == []


def test_secondary_body_with_short_retry_after_still_raises():
    resp = httpx.Response(
        403, json={"message": "You have exceeded a secondary rate limit"}, headers={"Retry-After": "30"}
    )
    client, rec, fake = make(resp)
    with pytest.raises(RateLimited):
        client.search_total_count("q")
    assert len(rec.requests) == 1 and fake.sleeps == []


def test_secondary_body_without_retry_after_raises():
    client, _, _ = make(httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}))
    with pytest.raises(RateLimited):
        client.search_total_count("q")


def test_long_retry_after_alone_raises_without_sleep():
    client, rec, fake = make(httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimited):
        client.search_total_count("q")
    assert len(rec.requests) == 1 and fake.sleeps == []


def test_short_retry_after_sleeps_and_retries():
    client, rec, fake = make(httpx.Response(429, headers={"Retry-After": "5"}), search_ok())
    assert client.search_total_count("q") == 1
    assert fake.sleeps == [5] and len(rec.requests) == 2


def test_short_retry_after_twice_raises():
    client, rec, _ = make(httpx.Response(429, headers={"Retry-After": "5"}))
    with pytest.raises(RateLimited):
        client.search_total_count("q")
    assert len(rec.requests) == 2  # exactly one retry


def test_primary_limit_remaining_zero_raises():
    resp = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1900000000"},
    )
    client, _, _ = make(resp)
    with pytest.raises(RateLimited) as exc:
        client.contributors_count("a/b")
    assert exc.value.retry_at == 1900000000.0


def test_5xx_retries_then_github_error():
    client, rec, fake = make(httpx.Response(502, text="bad gateway"))
    with pytest.raises(GitHubError) as exc:
        client.search_total_count("q")
    assert not isinstance(exc.value, (RateLimited, AuthError))
    assert fake.sleeps == [1, 2, 4] and len(rec.requests) == 4


def test_transport_error_retries_then_succeeds():
    client, rec, fake = make(httpx.ConnectError("boom"), search_ok())
    assert client.search_total_count("q") == 1
    assert fake.sleeps == [1] and len(rec.requests) == 2


def test_401_is_auth_error_with_env_fix():
    client, rec, fake = make(httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(AuthError) as exc:
        client.check_auth()
    assert isinstance(exc.value, ConfigError)
    assert "REPKA_GITHUB_TOKEN" in str(exc.value) and "settings/tokens" in str(exc.value)
    assert len(rec.requests) == 1 and fake.sleeps == []


def test_401_is_auth_error_with_gh_fix():
    client, _, _ = make(httpx.Response(401), token_source="gh")
    with pytest.raises(AuthError, match="gh auth login"):
        client.check_auth()


def test_check_auth_reads_login_and_expiry():
    resp = ok({"login": "octocat"}, headers={"github-authentication-token-expiration": "2026-11-20 10:00:00 UTC"})
    client, _, _ = make(resp)
    info = client.check_auth()
    assert info.login == "octocat"
    assert info.expires_at == datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def test_check_auth_without_expiry_header():
    client, _, _ = make(ok({"login": "octocat"}))
    assert client.check_auth().expires_at is None


def test_contributors_count_from_link_header():
    headers = json.loads((FIX / "contributors_link.headers.json").read_text())
    client, rec, _ = make(httpx.Response(200, content=(FIX / "contributors_link.json").read_bytes(), headers=headers))
    assert client.contributors_count("octo/repo") == 137
    params = dict(rec.requests[0].url.params)
    assert params == {"per_page": "1", "anon": "true"}


def test_contributors_without_link_counts_items():
    client, _, _ = make(ok([{"login": "a"}, {"login": "b"}]))
    assert client.contributors_count("o/r") == 2


def test_contributors_too_large_returns_none():
    resp = httpx.Response(403, json={"message": "The history or contributor list is too large to list contributors"})
    client, _, _ = make(resp)
    assert client.contributors_count("o/r") is None


def test_participation_returns_series():
    client, _, _ = make(httpx.Response(200, content=(FIX / "participation.json").read_bytes()))
    series = client.participation("o/r")
    assert series is not None and len(series) == 52


def test_participation_202_returns_none():
    client, _, _ = make(httpx.Response(202, json={}))
    assert client.participation("o/r") is None


def test_readme_returns_raw_text_and_404_returns_empty():
    client, rec, _ = make(httpx.Response(200, content=(FIX / "readme.md").read_bytes()))
    assert client.readme("o/r").startswith("# Demo repo")
    assert rec.requests[0].headers["accept"] == "application/vnd.github.raw+json"
    client, _, _ = make(httpx.Response(404, json={"message": "Not Found"}))
    assert client.readme("o/r") == ""


def test_etag_304_returns_cached_body():
    cache = DictCache()
    first = httpx.Response(200, content=b"# hello", headers={"ETag": '"abc"'})
    client, rec, _ = make(first, httpx.Response(304), cache=cache)
    assert client.readme("o/r") == "# hello"
    assert cache.data and next(iter(cache.data.values())) == ('"abc"', "# hello")
    assert client.readme("o/r") == "# hello"
    assert "if-none-match" not in rec.requests[0].headers
    assert rec.requests[1].headers["if-none-match"] == '"abc"'


def test_etag_304_for_contributors_keeps_count():
    cache = DictCache()
    headers = json.loads((FIX / "contributors_link.headers.json").read_text())
    first = httpx.Response(200, json=[{"login": "a"}], headers=headers)
    client, _, _ = make(first, httpx.Response(304), cache=cache)
    assert client.contributors_count("o/r") == 137
    assert client.contributors_count("o/r") == 137  # the 304 carries no Link header


def test_token_not_in_error_messages(caplog):
    caplog.set_level("DEBUG")
    cases = [
        (httpx.ConnectError(f"cannot reach with {TOKEN}"), GitHubError),
        (httpx.Response(500, text=f"oops {TOKEN}"), GitHubError),
        (httpx.Response(403, text=f"forbidden {TOKEN}"), GitHubError),
        (httpx.Response(401, text=TOKEN), AuthError),
    ]
    for item, expected in cases:
        client, _, _ = make(item)
        with pytest.raises(expected) as exc:
            client.check_auth()
        assert TOKEN not in str(exc.value)
    assert TOKEN not in caplog.text


def test_redirect_301_is_followed():
    moved = httpx.Response(301, headers={"Location": "https://api.github.com/repositories/9/readme"})
    client, rec, _ = make(moved, httpx.Response(200, text="moved readme"))
    assert client.readme("old/name") == "moved readme"
    assert rec.requests[1].url.path == "/repositories/9/readme"


def test_graphql_tolerates_not_found_node_errors():
    payload = {
        "data": {"nodes": [{"id": "R_1"}, None]},
        "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a node", "path": ["nodes", 1]}],
    }
    client, rec, _ = make(ok(payload))
    assert client.graphql("query($ids:[ID!]!){nodes(ids:$ids){id}}", {"ids": ["R_1", "R_2"]}) == payload["data"]
    assert json.loads(rec.requests[0].content)["variables"] == {"ids": ["R_1", "R_2"]}


def test_graphql_without_data_raises():
    client, _, _ = make(ok({"errors": [{"type": "INTERNAL", "message": "boom"}]}))
    with pytest.raises(GitHubError, match="boom"):
        client.graphql("query{x}", {})


def test_graphql_rate_limited_error_raises_rate_limited():
    client, _, _ = make(ok({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}))
    with pytest.raises(RateLimited):
        client.graphql("query{x}", {})


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("REPKA_LIVE"), reason="needs REPKA_LIVE=1 and a token")
def test_live_search_total_count():
    import subprocess

    token = os.environ.get("REPKA_GITHUB_TOKEN") or subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    client = GitHubClient(token, GithubConfig(), token_source="gh")
    try:
        assert client.search_total_count("topic:mcp") > 0
    finally:
        client.close()


def test_single_attempt_client_fails_fast_when_offline():
    rec = Recorder(httpx.ConnectError("down"))
    fake = FakeTime()
    client = GitHubClient(TOKEN, GithubConfig(), transport=httpx.MockTransport(rec), sleep=fake.sleep, max_server_retries=0)
    with pytest.raises(GitHubError):
        client.check_auth()
    assert len(rec.requests) == 1 and fake.sleeps == []
