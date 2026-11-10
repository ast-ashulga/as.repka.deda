"""End-to-end pipeline test: run_pass with the real stages, driven by fixtures and fakes.

Real: GitHubClient (on httpx.MockTransport serving tests/fixtures/e2e), collect, triage, embed, digest
and the store. Fake: the GitHub servers, the decider backend, the embedder, the Telegram sender.
Local time is Asia/Tbilisi (UTC+4); slots 08:30 and 19:30; the domain is scanned every 6 hours.
"""

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from repka.collect import collect_domains, due_domains, iso
from repka.config import AppConfig, DigestConfig, EmbedConfig, GithubConfig, NotifyConfig, RunConfig, TriageConfig
from repka.digest.releases import run_releases
from repka.digest.run import run_digest
from repka.domain.schema import Domain, SearchSpec
from repka.embed.embed import embed_domain
from repka.github.client import GitHubClient
from repka.notify.telegram_user import SendResult
from repka.pipeline import Boundaries, run_pass
from repka.store import repo
from repka.store.db import connect, migrate
from repka.triage.deciders import load_questions
from repka.triage.triage import triage_domain

ROOT = Path(__file__).resolve().parents[1]
ITEMS = json.loads((Path(__file__).parent / "fixtures" / "e2e" / "search_items.json").read_text())["items"]
TZ = ZoneInfo("Asia/Tbilisi")
CFG = AppConfig(
    triage=TriageConfig(enabled=True),
    embed=EmbedConfig(enabled=True),
    notify=NotifyConfig(enabled=True),
    run=RunConfig(send=True),
    digest=DigestConfig(slots=["08:30", "19:30"]),
)
DOMAIN = Domain(
    id="mcp", name="MCP servers", enabled=True, scan_every=timedelta(hours=6), profile="MCP servers and SDKs",
    search=SearchSpec(keywords=(), topics=("mcp",), languages=(), exclude=()),
)
RELEVANT = {10: 0.95, 11: 0.92, 12: 0.88, 13: 0.10}  # decider answers by repo id; 13 is rejected


def local(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).astimezone(UTC)


class World:
    """What the fake GitHub serves; tests mutate it between passes."""

    def __init__(self):
        self.visible = [10]  # repo ids the search returns
        self.releases: dict[int, list[dict]] = {}
        self.rate_limited = False
        self.requests: list[str] = []

    def node(self, item: dict) -> dict:
        return {
            "id": item["node_id"], "databaseId": item["id"], "stargazerCount": item["stargazers_count"],
            "forkCount": 3, "issues": {"totalCount": 1}, "watchers": {"totalCount": 5},
            "defaultBranchRef": {"target": {"c7": {"totalCount": 4}, "c30": {"totalCount": 12}}},
            "releases": {"nodes": self.releases.get(item["id"], [])},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        if path == "/search/repositories":
            if self.rate_limited:
                return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, text="API rate limit exceeded")
            items = [i for i in ITEMS if i["id"] in self.visible]
            return httpx.Response(200, json={"total_count": len(items), "incomplete_results": False, "items": items})
        if path == "/graphql":
            ids = json.loads(request.content)["variables"]["ids"]
            by_node = {i["node_id"]: i for i in ITEMS}
            return httpx.Response(200, json={"data": {"nodes": [self.node(by_node[n]) for n in ids]}})
        if path.endswith("/readme"):
            return httpx.Response(200, text=f"# {path.split('/')[2]}\nreadme text")
        if path.endswith("/contributors"):
            return httpx.Response(200, json=[{"login": "a"}, {"login": "b"}])
        if path.endswith("/stats/participation"):
            return httpx.Response(200, json={"all": [1] * 52})
        return httpx.Response(404, json={"message": "unexpected request"})


class FakeBackend:
    def decide(self, items, q):
        out = {}
        for i in items:
            p = RELEVANT[i.repo_id]
            out[f"{i.domain_id}:{i.repo_id}"] = {
                "relevant": {"type": "noul", "p": p, "pred": p > 0.5, "conf": p},
                "substance": {"type": "score", "pred": 3, "score": 3.0, "conf": 0.6, "probs": {}},
                "kind": {"type": "choice", "pred": "tool", "conf": 0.7, "probs": {}},
            }
        return out, {}


class FacetBackend(FakeBackend):
    """FakeBackend plus a domain facet answer: repo 11 is a `gateway` (p 0.9), repo 12 has no facets."""

    def decide(self, items, q):
        out, extra = super().decide(items, q)
        for i in items:
            if i.repo_id == 11:
                out[f"{i.domain_id}:{i.repo_id}"]["gateway"] = {"type": "noul", "p": 0.9, "pred": True, "conf": 0.9}
        return out, extra


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self):
        self.texts: list[str] = []

    def embed(self, texts):
        self.texts += texts
        return [[b / 255 + 0.01 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts]


class FakeSender:
    def __init__(self):
        self.calls: list[list[str]] = []

    def send(self, parts):
        self.calls.append(list(parts))
        return SendResult(ok=True, sent=len(parts))


class Rig:
    def __init__(self, conn, cfg: AppConfig = CFG, backend=None):
        self.conn = conn
        self.cfg = cfg
        self.backend = backend or FakeBackend()
        self.world = World()
        self.embedder = FakeEmbedder()
        self.sender = FakeSender()
        self.client = GitHubClient(
            "ghp_x", GithubConfig(), transport=httpx.MockTransport(self.world.handle), sleep=lambda s: None
        )
        self.questions = load_questions(ROOT / "deciders" / "questions.yaml")

    def boundaries(self) -> Boundaries:
        conn, cfg = self.conn, self.cfg

        def collect(ds, now):
            states = {d.id: repo.get_domain_state(conn, d.id) for d in ds}
            return collect_domains(self.client, conn, due_domains(ds, states, now), cfg, now)

        return Boundaries(
            collect=collect,
            triage=lambda d, now: triage_domain(
                conn, d, self.backend, self.questions, "fake", cfg.triage.thresholds, self.client.readme,
                cfg.triage.readme_chars, iso(now),
            ),
            embed=lambda d, now: embed_domain(conn, d.id, self.embedder, cfg.embed.batch, iso(now)),
            digest=lambda now, send: run_digest(conn, [DOMAIN], cfg, self.sender, now, send, tz=TZ),
            releases=lambda now, send: run_releases(conn, [DOMAIN], cfg, self.sender, now, send, tz=TZ),
        )

    def run(self, when: datetime, send: bool = True):
        return run_pass([DOMAIN], self.cfg, self.boundaries(), when, send)


@pytest.fixture
def rig(tmp_path):
    conn = connect(tmp_path / "e2e.db")
    migrate(conn)
    yield Rig(conn)
    conn.close()


def baseline(rig):
    """Pass 0 (day before): the first scan is a silent baseline, so nothing is digested."""
    report = rig.run(local(6, 8, 45))
    assert report.status == "ok" and report.digest.status == "empty" and rig.sender.calls == []
    return report


def accepted_names(conn) -> set[str]:
    rows = conn.execute("SELECT r.full_name FROM verdicts v JOIN repos r USING (repo_id) WHERE v.accepted = 1")
    return {r["full_name"] for r in rows}


def test_full_pass_produces_one_digest(rig):
    baseline(rig)
    assert accepted_names(rig.conn) == {"old/mcp-veteran"} and len(rig.embedder.texts) == 1

    rig.world.visible = [10, 11, 12, 13]  # three new repos appear
    report = rig.run(local(7, 8, 45))

    assert report.status == "ok", report.errors
    assert report.collect.domains[0].search.new == 3
    stats = report.triage["mcp"]
    assert (stats.decided, stats.accepted) == (3, 2)
    assert report.embed["mcp"].embedded == 2  # the veteran was embedded in pass 0
    assert report.digest.status == "sent" and report.digest.item_count == 2
    assert len(rig.sender.calls) == 1
    text = "\n".join(rig.sender.calls[0])
    assert "acme/mcp-github" in text and "zed/mcp-sdk-py" in text
    assert "troll/mcp-blog" not in text and "old/mcp-veteran" not in text  # rejected / baseline absent
    row = rig.conn.execute("SELECT slot, local_date, status FROM digests WHERE status = 'sent'").fetchone()
    assert (row["slot"], row["local_date"]) == ("08:30", "2026-11-07")


def test_second_pass_same_slot_sends_nothing(rig):
    baseline(rig)
    rig.world.visible = [10, 11, 12, 13]
    rig.run(local(7, 8, 45))
    calls_before = len(rig.world.requests)
    report = rig.run(local(7, 9, 15))  # the domain is not due again (6h), the slot is already sent
    assert report.status == "ok" and report.digest.status == "not_due"
    assert len(rig.sender.calls) == 1
    assert len(rig.world.requests) == calls_before  # nothing was fetched either


def test_next_slot_reports_only_new_events(rig):
    baseline(rig)
    rig.world.visible = [10, 11, 12, 13]
    rig.run(local(7, 8, 45))
    rig.world.releases[11] = [{
        "tagName": "v1.0.0", "name": "First stable", "publishedAt": "2026-11-07T10:00:00Z",
        "createdAt": "2026-11-07T09:55:00Z", "url": "https://github.com/acme/mcp-github/releases/tag/v1.0.0",
    }]
    report = rig.run(local(7, 19, 45))
    assert report.status == "ok", report.errors
    assert report.digest.status == "sent" and report.digest.item_count == 1
    text = "\n".join(rig.sender.calls[1])
    assert "v1.0.0" in text and "acme/mcp-github" in text
    assert "zed/mcp-sdk-py" not in text  # already reported in the morning


def test_rate_limit_mid_collect_still_sends_digest_of_known_items(rig):
    baseline(rig)
    rig.world.visible = [10, 11, 12, 13]
    assert rig.run(local(7, 8, 45), send=False).digest.status == "preview"  # known now, but not delivered
    assert rig.sender.calls == []
    rig.world.rate_limited = True
    report = rig.run(local(7, 19, 45))  # the next scan hits GitHub's limit
    assert report.status == "partial" and report.collect.rate_limited
    assert any("rate limit" in e for e in report.errors)
    assert report.digest.status == "sent" and report.digest.item_count == 2
    text = "\n".join(rig.sender.calls[0])
    assert "acme/mcp-github" in text and "zed/mcp-sdk-py" in text


def test_pass_with_insights_and_releases_message(tmp_path):
    cfg = dataclasses.replace(CFG, digest=DigestConfig(slots=["08:30", "19:30"], insights=True, releases_message=True))
    conn = connect(tmp_path / "insights.db")
    migrate(conn)
    rig = Rig(conn, cfg, FacetBackend())
    try:
        assert rig.run(local(6, 8, 45)).status == "ok"  # baseline: the veteran, silent
        rig.world.visible = [10, 11, 12, 13]
        rig.world.releases[11] = [{
            "tagName": "v1.0.0", "name": "First stable", "publishedAt": "2026-11-05T10:00:00Z",
            "createdAt": "2026-11-05T09:55:00Z", "url": "https://github.com/acme/mcp-github/releases/tag/v1.0.0",
        }]

        # 08:45: the main slot is due, the 09:00 releases slot is not yet
        first = rig.run(local(7, 8, 45))
        assert first.status == "ok", first.errors
        assert first.digest.status == "sent" and first.digest.item_count == 2
        assert first.releases.status == "not_due"
        assert len(rig.sender.calls) == 1
        main = "\n".join(rig.sender.calls[0])
        assert "acme/mcp-github" in main and "zed/mcp-sdk-py" in main
        assert "gateway" in main  # facet tag of repo 11 (p 0.9 >= facet_min_p)
        assert "tags = decider answers" in main and "🆕 new repo" in main  # legend
        assert "🚀" not in main and "v1.0.0" not in main  # releases belong to their own message
        assert "troll/mcp-blog" not in main
        lines12 = main.splitlines()
        start = next(i for i, ln in enumerate(lines12) if "zed/mcp-sdk-py" in ln)  # repo 12: no facet answers
        block12 = []
        for ln in lines12[start:]:
            if block12 and (ln.startswith("🆕") or ln.startswith("🔎") or not ln.strip()):
                break
            block12.append(ln)
        assert len(block12) == 3 and "gateway" not in "\n".join(block12)
        assert block12[-1].startswith("<code>") and block12[-1].endswith("</code>")  # kind / lvl only, no facet tag

        # 09:15: only the releases slot is due
        second = rig.run(local(7, 9, 15))
        assert second.status == "ok", second.errors
        assert second.digest.status == "not_due"
        assert second.releases.status == "sent" and second.releases.item_count == 1
        assert len(rig.sender.calls) == 2
        releases = "\n".join(rig.sender.calls[1])
        assert "🚀" in releases and "acme/mcp-github" in releases and "v1.0.0" in releases
        assert "zed/mcp-sdk-py" not in releases and "gateway" not in releases
        assert "tags = decider answers" not in releases  # the legend belongs to the main digest only

        rows = conn.execute("SELECT slot, kind, status FROM digests WHERE status = 'sent' ORDER BY id").fetchall()
        assert [(r["slot"], r["kind"]) for r in rows] == [("08:30", "main"), ("releases:09:00", "releases")]
        assert conn.execute("SELECT count(*) FROM events WHERE digest_id IS NULL").fetchone()[0] == 0

        # a later pass the same day sends nothing
        third = rig.run(local(7, 9, 40))
        assert third.status == "ok" and third.digest.status == "not_due" and third.releases.status == "not_due"
        assert len(rig.sender.calls) == 2
    finally:
        conn.close()
