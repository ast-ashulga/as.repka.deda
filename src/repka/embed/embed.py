"""Embed the accepted repos of a domain: text building, change detection and batched, committed writes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from repka.embed.ollama import Embedder
from repka.progress import notify
from repka.store.repo import repos_for_domain
from repka.store.vectors import embedding_hash, upsert_embedding
from repka.textutil import printable, truncate

# nomic-embed-text works without task prefixes (wiki/spikes/T04-ollama.md): both stay empty.
# A change here re-embeds everything, because the hash covers the prefixed text.
DOC_PREFIX = ""
QUERY_PREFIX = ""
README_CHARS = 2000


def embedding_text(row: sqlite3.Row) -> str:
    """Name, description, topics and README excerpt of a repo. Repo text is untrusted: printable() first."""
    topics = ", ".join(json.loads(row["topics_json"] or "[]"))
    parts = [
        row["full_name"],
        row["description"] or "",
        f"Topics: {topics}" if topics else "",
    ]
    excerpt = row["readme_excerpt"]
    if excerpt:
        parts.append(truncate(excerpt, README_CHARS))
    return printable("\n".join(p for p in parts if p))


def input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class EmbedStats:
    pending: int = 0
    embedded: int = 0


def embed_domain(
    conn: sqlite3.Connection,
    domain_id: str,
    embedder: Embedder,
    batch: int,
    now: str,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> EmbedStats:
    """Embed accepted, active repos whose stored hash differs from their current text.

    Stores under `embedder.model`. Commits after every batch, so an interrupted run keeps finished batches.
    """
    stats = EmbedStats()
    todo: list[tuple[int, str, str]] = []
    for row in repos_for_domain(conn, domain_id, accepted_only=True):
        if row["status"] != "active":
            continue
        text = DOC_PREFIX + embedding_text(row)
        digest = input_hash(text)
        if embedding_hash(conn, row["repo_id"], embedder.model) != digest:
            todo.append((row["repo_id"], text, digest))
    stats.pending = len(todo)
    notify(on_progress, 0, stats.pending, "")
    for start in range(0, len(todo), batch):
        chunk = todo[start : start + batch]
        vectors = embedder.embed([text for _, text, _ in chunk])
        for (repo_id, _, digest), vector in zip(chunk, vectors, strict=True):
            upsert_embedding(conn, repo_id, embedder.model, vector, digest, now)
        conn.commit()
        stats.embedded += len(chunk)
        notify(on_progress, stats.embedded, stats.pending, "")
    return stats
