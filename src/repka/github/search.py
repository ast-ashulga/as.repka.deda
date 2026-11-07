"""Repository search: pagination, date bisection, resume.

GitHub search returns at most 1,000 results per query, so a window is split by date until every slice
reports `total_count <= 1000` (a 1-day slice that is still bigger is kept and flagged `capped`, RF2).
Each finished slice is recorded in `search_cursor` and every page is committed, so a run stopped by
`RateLimited` keeps its data and the next run skips what is done (RF1).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import ceil
from typing import Protocol

from repka.domain.matrix import Query, Window, build_queries
from repka.domain.schema import Domain
from repka.github.client import SearchPage
from repka.models import Event, RepoHit
from repka.store import repo

log = logging.getLogger(__name__)

SEARCH_RESULT_CAP = 1000  # GitHub never returns more than this many results for one query
RETRY_PER_PAGE = 50  # page size used to re-read a page flagged `incomplete_results`


class SearchClient(Protocol):
    def search_repos(self, q: str, page: int = 1, per_page: int | None = None) -> SearchPage: ...


@dataclass(frozen=True)
class Slice:
    field: str  # "pushed" or "created"
    start: date
    end: date

    def qualifier(self) -> str:
        return f"{self.field}:{self.start.isoformat()}..{self.end.isoformat()}"

    def key(self) -> str:
        return self.qualifier()

    def days(self) -> int:
        return (self.end - self.start).days + 1

    def halves(self) -> tuple[Slice, Slice]:
        mid = self.start + (self.end - self.start) // 2
        return Slice(self.field, self.start, mid), Slice(self.field, mid + timedelta(days=1), self.end)


@dataclass
class QueryStats:
    slices: int = 0  # leaf slices read to the end
    pages: int = 0  # search requests made (probes of bisected slices and retries included)
    hits: int = 0  # items stored
    new: int = 0  # repos not seen in the store before
    capped: int = 0
    incomplete: int = 0

    def add(self, other: QueryStats) -> None:
        for name in ("slices", "pages", "hits", "new", "capped", "incomplete"):
            setattr(self, name, getattr(self, name) + getattr(other, name))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def hit_from_item(item: dict) -> RepoHit:
    lic = item.get("license") or {}
    spdx = lic.get("spdx_id")
    return RepoHit(
        repo_id=int(item["id"]),
        node_id=item["node_id"],
        full_name=item["full_name"],
        html_url=item["html_url"],
        description=item.get("description"),
        language=item.get("language"),
        topics=tuple(item.get("topics") or ()),
        stars=int(item.get("stargazers_count", 0)),
        forks=int(item.get("forks_count", 0)),
        is_fork=bool(item.get("fork", False)),
        created_at=_dt(item["created_at"]),
        pushed_at=_dt(item["pushed_at"]),
        archived=bool(item.get("archived", False)),
        license=(lic.get("name") if spdx == "NOASSERTION" else spdx) or None,
    )


class _Collector:
    def __init__(
        self,
        client: SearchClient,
        conn: sqlite3.Connection,
        domain_id: str,
        query: Query,
        now: str,
        budget: int,
        emit_new: bool,
        per_page: int,
    ) -> None:
        self.client = client
        self.conn = conn
        self.domain_id = domain_id
        self.query = query
        self.now = now
        self.budget = budget
        self.emit_new = emit_new
        self.per_page = per_page
        self.max_pages = ceil(SEARCH_RESULT_CAP / per_page)
        self.stats = QueryStats()
        self.seen: set[int] = set()

    def _search(self, q: str, page: int, per_page: int) -> SearchPage:
        self.stats.pages += 1
        return self.client.search_repos(q, page=page, per_page=per_page)

    def _page(self, q: str, page: int, first: SearchPage | None = None) -> list[dict]:
        """Items of one logical page. A page flagged `incomplete_results` is re-read once at per_page=50."""
        result = first or self._search(q, page, self.per_page)
        if not result.incomplete:
            return result.items
        self.stats.incomplete += 1
        retry_pp = RETRY_PER_PAGE if self.per_page % RETRY_PER_PAGE == 0 else self.per_page
        factor = self.per_page // retry_pp
        log.warning("incomplete results for %r page %d; retrying with per_page=%d", q, page, retry_pp)
        items: list[dict] = []
        for sub in range(factor * (page - 1) + 1, factor * page + 1):
            chunk = self._search(q, sub, retry_pp).items
            items.extend(chunk)
            if len(chunk) < retry_pp:
                break
        return items

    def _store(self, items: list[dict]) -> None:
        for item in items:
            hit = hit_from_item(item)
            self.stats.hits += 1
            self.stats.new += repo.upsert_repo(self.conn, hit, self.now)
            first_in_domain = not repo.domain_has_repo(self.conn, self.domain_id, hit.repo_id)
            repo.link_domain_repo(self.conn, self.domain_id, hit.repo_id, self.query.key, self.now)
            if first_in_domain and self.emit_new:
                repo.add_event(
                    self.conn,
                    Event(
                        repo_id=hit.repo_id,
                        domain_id=self.domain_id,
                        kind="new_repo",
                        payload={"created_at": hit.created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
                        detected_at=_dt(self.now),
                    ),
                )
            self.seen.add(hit.repo_id)
        self.conn.commit()

    def slice(self, sl: Slice) -> bool:
        """Collect one slice (bisecting if needed). True when it is fully done, False if the budget ran out."""
        key = sl.key()
        if repo.cursor_done(self.conn, self.domain_id, self.query.key, key):
            return True
        if self.budget - self.stats.hits <= 0:
            return False
        q = f"{self.query.q} {sl.qualifier()}"
        first = self._search(q, 1, self.per_page)
        total = first.total_count
        if total > SEARCH_RESULT_CAP and sl.days() > 1:
            for half in sl.halves():  # the probe page is discarded
                if not self.slice(half):
                    return False
            repo.mark_cursor(self.conn, self.domain_id, self.query.key, key, total, False, self.now)
            self.conn.commit()
            return True
        capped = total > SEARCH_RESULT_CAP
        if capped:
            self.stats.capped += 1
            log.warning("slice capped: %s %s has %d results, keeping the first %d", self.query.q, key, total, SEARCH_RESULT_CAP)
        page = 1
        while True:
            items = self._page(q, page, first if page == 1 else None)
            room = self.budget - self.stats.hits
            self._store(items[:room])
            last = len(items) < self.per_page or page >= self.max_pages
            if len(items) > room or (len(items) == room and not last):
                return False  # the budget ended inside this slice
            if last:
                break
            page += 1
        self.stats.slices += 1
        repo.mark_cursor(self.conn, self.domain_id, self.query.key, key, total, capped, self.now)
        self.conn.commit()
        return True


def collect_query(
    client: SearchClient,
    conn: sqlite3.Connection,
    domain_id: str,
    query: Query,
    window: Window,
    now: str,
    budget: int,
    emit_new: bool,
    *,
    per_page: int = 100,
) -> tuple[QueryStats, set[int]]:
    """Collect one query over the window. Returns the stats and the repo ids seen.

    `budget` is the number of hits still allowed for the domain scan. `emit_new=False` is the silent
    baseline of a domain's first scan (RF8). `RateLimited` propagates; finished pages are committed.
    """
    collector = _Collector(client, conn, domain_id, query, now, budget, emit_new, per_page)
    collector.slice(Slice(window.field, window.start, window.end))
    return collector.stats, collector.seen


def collect_search(
    client: SearchClient,
    conn: sqlite3.Connection,
    domain: Domain,
    window: Window,
    now: str,
    max_repos: int,
    emit_new: bool,
    *,
    per_page: int = 100,
    progress: Callable[[int, int, Query, QueryStats], None] | None = None,
) -> tuple[QueryStats, set[int]]:
    """Collect every query of the domain's matrix, summed, within a total budget of `max_repos` hits.

    `progress(index, count, query, stats)` is called after each finished query (1-based index).
    """
    total = QueryStats()
    seen: set[int] = set()
    queries = build_queries(domain)
    for index, query in enumerate(queries, 1):
        left = max_repos - total.hits
        if left <= 0:
            break
        stats, ids = collect_query(client, conn, domain.id, query, window, now, left, emit_new, per_page=per_page)
        total.add(stats)
        seen |= ids
        if progress is not None:
            progress(index, len(queries), query, stats)
    return total, seen
