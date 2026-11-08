"""The one GitHub HTTP client: auth, search throttle, rate limits, retries, redirects, ETags.

Nothing else in repka constructs an `httpx.Client` for GitHub. Errors:
- `AuthError` (a ConfigError, exit 2): 401, never retried, message carries the fix for the token source.
- `RateLimited`: secondary/primary limit we will not wait out; callers stop GitHub work cleanly.
- `GitHubError`: everything else (5xx after retries, transport errors, unexpected statuses).
The token never appears in an exception message or a log line.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from repka.config import ConfigError, GithubConfig

log = logging.getLogger(__name__)

BASE_URL = "https://api.github.com"
MAX_SHORT_RETRY_AFTER = 60  # a Retry-After up to this many seconds is slept out, longer means stop
SERVER_RETRY_DELAYS = (1, 2, 4)  # 5xx / transport errors: 3 retries
SEARCH_WINDOW_S = 60.0

FIX_ENV = (
    "create a new token at https://github.com/settings/tokens and replace REPKA_GITHUB_TOKEN "
    "in .env (or empty it to use the gh login)"
)
FIX_GH = "run `gh auth login`"


class GitHubError(RuntimeError):
    """Any GitHub failure that is not a user-fixable credential problem."""


class RateLimited(GitHubError):
    """GitHub told us to stop. `retry_at` is a wall-clock epoch (time.time() scale) or None if unknown."""

    def __init__(self, message: str, retry_at: float | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class AuthError(ConfigError):
    """Bad credentials (HTTP 401): expired, revoked or invalid token."""


@dataclass(frozen=True)
class AuthInfo:
    login: str
    expires_at: datetime | None  # aware UTC; None = the token does not expire (e.g. gh OAuth token)


class EtagCache(Protocol):
    def get(self, url: str) -> tuple[str, str] | None: ...

    def put(self, url: str, etag: str, body: str) -> None: ...


@dataclass
class SearchPage:
    total_count: int
    incomplete: bool
    items: list[dict]


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP-date form: treat as unknown


def _page_of(url: str) -> int | None:
    pages = parse_qs(urlparse(url).query).get("page")
    return int(pages[0]) if pages and pages[0].isdigit() else None


class GitHubClient:
    def __init__(
        self,
        token: str,
        cfg: GithubConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        cache: EtagCache | None = None,
        token_source: Literal["env", "gh"] = "env",
        max_server_retries: int | None = None,  # None: retry per SERVER_RETRY_DELAYS; 0: a single attempt
    ) -> None:
        self._max_server_retries = len(SERVER_RETRY_DELAYS) if max_server_retries is None else max_server_retries
        self._token = token
        self._cfg = cfg
        self._clock = clock
        self._sleep = sleep
        self._cache = cache
        self._token_source = token_source
        self._search_calls: deque[float] = deque()
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "repka-cli",
            },
            follow_redirects=True,
            timeout=cfg.timeout_s,
            transport=transport,
        )

    # ---- public API -------------------------------------------------------------------------

    def check_auth(self) -> AuthInfo:
        resp = self._request("GET", "/user")
        self._expect_ok(resp, "GET /user")
        raw = resp.headers.get("github-authentication-token-expiration")
        return AuthInfo(login=resp.json()["login"], expires_at=_parse_expiry(raw))

    def search_repos(
        self, q: str, page: int = 1, per_page: int | None = None, sort: str = "stars", order: str = "desc"
    ) -> SearchPage:
        params = {
            "q": q,
            "sort": sort,
            "order": order,
            "per_page": per_page or self._cfg.per_page,
            "page": page,
        }
        resp = self._request("GET", "/search/repositories", params=params, search=True)
        self._expect_ok(resp, "GET /search/repositories")
        body = resp.json()
        return SearchPage(
            total_count=int(body["total_count"]),
            incomplete=bool(body.get("incomplete_results", False)),
            items=list(body.get("items", [])),
        )

    def search_total_count(self, q: str) -> int:
        return self.search_repos(q, per_page=1).total_count

    def graphql(self, query: str, variables: dict) -> dict:
        resp = self._request("POST", "/graphql", json_body={"query": query, "variables": variables})
        self._expect_ok(resp, "POST /graphql")
        try:
            body = resp.json()
        except ValueError:  # empty or non-JSON 2xx body: a GitHub failure for this call, not a crash
            raise GitHubError(f"GraphQL returned a non-JSON body (HTTP {resp.status_code})") from None
        data = body.get("data")
        errors = body.get("errors") or []
        if data is None:
            if any(e.get("type") == "RATE_LIMITED" for e in errors):
                raise RateLimited("GitHub GraphQL rate limit exceeded", self._reset_epoch(resp))
            raise GitHubError(self._redact(f"GraphQL returned no data: {self._error_text(errors)}"))
        unexpected = [e for e in errors if e.get("type") != "NOT_FOUND"]
        if unexpected:  # NOT_FOUND = deleted/private node, handled by the caller via a null node
            log.warning("graphql returned errors: %s", self._redact(self._error_text(unexpected)))
        return data

    def contributors_count(self, full_name: str) -> int | None:
        def derive(resp: httpx.Response) -> str:
            last = resp.links.get("last")
            page = _page_of(last["url"]) if last else None
            if page is not None:
                return str(page)
            return str(len(resp.json()))

        resp, value = self._cached_get(
            f"/repos/{full_name}/contributors", {"per_page": 1, "anon": "true"}, derive=derive
        )
        if resp.status_code == 403 and "too large" in resp.text.lower():
            return None
        if resp.status_code == 204:  # empty repository
            return 0
        if value is None:
            self._expect_ok(resp, f"GET contributors of {full_name}")
            raise GitHubError(f"unexpected response status {resp.status_code} for contributors")
        return int(value)

    def participation(self, full_name: str) -> list[int] | None:
        def derive(resp: httpx.Response) -> str:
            series = resp.json().get("all")
            return json.dumps(series) if series is not None else "null"

        resp, value = self._cached_get(f"/repos/{full_name}/stats/participation", None, derive=derive)
        if resp.status_code == 202:  # GitHub is still computing the statistics
            return None
        if value is None:
            self._expect_ok(resp, f"GET participation of {full_name}")
            raise GitHubError(f"unexpected response status {resp.status_code} for participation")
        return json.loads(value)

    def readme(self, full_name: str) -> str:
        resp, value = self._cached_get(
            f"/repos/{full_name}/readme",
            None,
            accept="application/vnd.github.raw+json",
            derive=lambda r: r.text,
        )
        if resp.status_code == 404:
            return ""
        if value is None:
            self._expect_ok(resp, f"GET readme of {full_name}")
            raise GitHubError(f"unexpected response status {resp.status_code} for readme")
        return value

    def close(self) -> None:
        self._http.close()

    # ---- internals --------------------------------------------------------------------------

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    @staticmethod
    def _error_text(errors: list[dict]) -> str:
        return "; ".join(str(e.get("message", e)) for e in errors)[:300]

    def _expect_ok(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code >= 400:
            raise GitHubError(self._redact(f"{what} failed: HTTP {resp.status_code} {resp.text[:200]}"))

    def _throttle(self) -> None:
        """Sliding 60 s window: at most cfg.search_per_minute search requests."""
        limit = self._cfg.search_per_minute
        calls = self._search_calls
        now = self._clock()
        while True:
            while calls and calls[0] <= now - SEARCH_WINDOW_S:
                calls.popleft()
            if len(calls) < limit:
                break
            wait = calls[0] + SEARCH_WINDOW_S - now
            log.debug("search throttle: sleeping %.1fs", wait)
            self._sleep(wait)
            now = max(self._clock(), calls[0] + SEARCH_WINDOW_S)
        calls.append(now)

    def _reset_epoch(self, resp: httpx.Response) -> float | None:
        reset = resp.headers.get("x-ratelimit-reset")
        try:
            return float(reset) if reset is not None else None
        except ValueError:
            return None

    def _auth_error(self) -> AuthError:
        fix = FIX_GH if self._token_source == "gh" else FIX_ENV
        return AuthError(f"GitHub rejected the token (expired, revoked or invalid): {fix}")

    def _rate_limit_check(self, resp: httpx.Response) -> tuple[bool, float | None]:
        """For a 403/429: (is_rate_limit, retry_after_seconds_if_short_enough_to_wait)."""
        retry_after = _retry_after(resp)
        remaining = resp.headers.get("x-ratelimit-remaining")
        text = resp.text.lower()
        if ("secondary" in text and "rate limit" in text) or remaining == "0":
            return True, None  # stop the run cleanly (RF1), even if Retry-After is short
        if retry_after is not None:
            return True, retry_after if retry_after <= MAX_SHORT_RETRY_AFTER else None
        return resp.status_code == 429, None

    def _limited(self, resp: httpx.Response) -> RateLimited:
        retry_after = _retry_after(resp)
        retry_at = time.time() + retry_after if retry_after is not None else self._reset_epoch(resp)
        kind = "secondary" if "secondary" in resp.text.lower() else "primary/other"
        return RateLimited(f"GitHub rate limit hit ({kind}, HTTP {resp.status_code})", retry_at)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        search: bool = False,
    ) -> httpx.Response:
        server_retries = 0
        rate_retried = False
        while True:
            if search:
                self._throttle()
            try:
                resp = self._http.request(method, url, params=params, headers=headers, json=json_body)
            except httpx.TransportError as exc:
                failure = self._redact(f"{type(exc).__name__}: {exc}")
            else:
                if resp.status_code == 401:
                    raise self._auth_error()
                if resp.status_code in (403, 429):
                    limited, wait = self._rate_limit_check(resp)
                    if limited:
                        if wait is not None and not rate_retried:
                            rate_retried = True
                            log.warning("rate limited, sleeping %.0fs before one retry", wait)
                            self._sleep(wait)
                            continue
                        raise self._limited(resp)
                    return resp  # a plain 403 (e.g. contributors "too large"): the caller decides
                if resp.status_code < 500:
                    return resp
                failure = f"HTTP {resp.status_code}"
            if server_retries >= self._max_server_retries:
                raise GitHubError(f"{method} {url} failed after {server_retries} retries: {failure}")
            delay = SERVER_RETRY_DELAYS[server_retries]
            server_retries += 1
            log.warning("%s %s failed (%s); retry %d in %ds", method, url, failure, server_retries, delay)
            self._sleep(delay)

    def _cached_get(
        self,
        path: str,
        params: dict[str, Any] | None,
        *,
        derive: Callable[[httpx.Response], str],
        accept: str | None = None,
    ) -> tuple[httpx.Response, str | None]:
        """GET with If-None-Match. Returns (response, derived value or None).

        The cache stores the *derived* string (a count, a JSON series, README text), so a 304 needs no
        headers from the original response. Non-200/304 responses give value None.
        """
        key = str(self._http.build_request("GET", path, params=params).url)
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        cached = self._cache.get(key) if self._cache else None
        if cached:
            headers["If-None-Match"] = cached[0]
        resp = self._request("GET", path, params=params, headers=headers)
        if resp.status_code == 304 and cached:
            return resp, cached[1]
        if resp.status_code != 200:
            return resp, None
        value = derive(resp)
        etag = resp.headers.get("etag")
        if self._cache and etag:
            self._cache.put(key, etag, value)
        return resp, value


def _parse_expiry(raw: str | None) -> datetime | None:
    """'2026-11-20 10:00:00 UTC' -> aware UTC datetime; anything unparseable -> None."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.removesuffix(" UTC").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
