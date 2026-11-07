"""`repka embed [--domain ID] [--json]` and `repka similar <owner/name> [-k N] [--domain ID] [--json]`."""

from __future__ import annotations

import argparse
import json
import sys

from repka import config
from repka.config import AppConfig, ConfigError, load_config
from repka.domain.schema import Domain, get_domain, load_domains
from repka.embed.embed import embed_domain
from repka.embed.ollama import EmbedderUnavailable, OllamaEmbedder
from repka.store.db import connect, utcnow
from repka.store.repo import repos_for_domain
from repka.store.vectors import get_index, load_matrix
from repka.textutil import printable, truncate

EMBED_EPILOG = """\
Computes Ollama embeddings for the accepted repos of a domain (all enabled domains by default) whose text
changed since their last embedding. Runs even when `embed.enabled` is false. Requires a running Ollama with
the `embed.model` pulled; a repo whose text did not change is never sent again.
"""

SIMILAR_EPILOG = """\
Lists the repos whose stored embedding is closest to the given repo (cosine similarity, most similar first).
Uses only the database: Ollama does not have to be running. Run `repka embed` first.
"""


def _positive(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return n


def _select_domains(domain_id: str | None) -> list[Domain]:
    if domain_id:
        return [
            get_domain(domain_id)
        ]  # explicit: runs even if the domain is not enabled
    return [d for d in load_domains() if d.enabled]


def make_embedder(cfg: AppConfig) -> OllamaEmbedder:
    if cfg.embed.backend != "ollama":
        raise ConfigError(f"embed.backend must be ollama (got {cfg.embed.backend!r})")
    return OllamaEmbedder(cfg.embed.url, cfg.embed.model)


def _require_db() -> None:
    if not config.db_path().exists():
        raise ConfigError(
            f"no database at {config.db_path()}; run `repka db migrate` first"
        )


def _cmd_embed(args: argparse.Namespace) -> int:
    cfg = load_config()
    domains = _select_domains(args.domain)
    if not domains:
        print(
            "no enabled domains: nothing to embed (see: repka domain list)",
            file=sys.stderr,
        )
        return 0
    if not cfg.embed.enabled:
        print(
            "note: embed.enabled is false in .config.yaml; that flag only governs `repka run`. "
            "Embedding is running because `repka embed` was invoked explicitly.",
            file=sys.stderr,
        )
    _require_db()
    embedder = make_embedder(cfg)
    conn = connect()
    results: list[tuple[str, int, int]] = []
    try:
        for d in domains:
            stats = embed_domain(conn, d.id, embedder, cfg.embed.batch, utcnow())
            results.append((d.id, stats.pending, stats.embedded))
    except EmbedderUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        embedder.close()
        conn.close()
    if args.json:
        out = {
            "model": cfg.embed.model,
            "domains": [
                {"domain": d, "pending": p, "embedded": e} for d, p, e in results
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for domain_id, pending, embedded in results:
        print(
            f"{domain_id}: pending {pending}, embedded {embedded} with {cfg.embed.model}"
        )
    return 0


def _cmd_similar(args: argparse.Namespace) -> int:
    cfg = load_config()
    _require_db()
    model = cfg.embed.model
    conn = connect(readonly=True)
    try:
        row = conn.execute(
            "SELECT repo_id, full_name FROM repos WHERE full_name = ? COLLATE NOCASE",
            (args.repo,),
        ).fetchone()
        if row is None:
            raise ConfigError(
                f"repo {args.repo} is not in the database: collect it first (repka collect)"
            )
        repo_id, full_name = row["repo_id"], row["full_name"]
        ids, matrix = load_matrix(conn, model, [repo_id])
        if not ids:
            raise ConfigError(
                f"no embedding for {full_name} with model {model}: run `repka embed` first"
            )
        candidates = None
        if args.domain:
            candidates = {
                r["repo_id"]
                for r in repos_for_domain(conn, args.domain, accepted_only=True)
            }
        hits = get_index(conn, model).knn(
            matrix[0], args.k + 1, candidates
        )  # +1: the repo itself comes back
        hits = [(rid, sim) for rid, sim in hits if rid != repo_id][: args.k]
        rows = []
        for rid, sim in hits:
            info = conn.execute(
                "SELECT full_name, description, html_url FROM repos WHERE repo_id = ?",
                (rid,),
            ).fetchone()
            rows.append((rid, info, sim))
    finally:
        conn.close()
    if args.json:
        out = {
            "query": full_name,
            "model": model,
            "results": [
                {
                    "repo_id": rid,
                    "full_name": info["full_name"],
                    "description": info["description"],
                    "html_url": info["html_url"],
                    "similarity": round(sim, 6),
                }
                for rid, info, sim in rows
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("note: no other embedded repos to compare with", file=sys.stderr)
    print(f"{'similarity':>10}  full_name  description")
    for _, info, sim in rows:
        name = printable(info["full_name"])
        desc = truncate(printable(info["description"] or ""), 80)
        print(f"{sim:>10.3f}  {name}  {desc}".rstrip())
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    embed = sub.add_parser(
        "embed",
        help="compute embeddings for accepted repos (Ollama)",
        description="Embed the accepted repos of a domain whose text changed since the last run.",
        epilog=EMBED_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    embed.add_argument(
        "--domain",
        metavar="ID",
        help="only this domain (default: every enabled domain)",
    )
    embed.add_argument(
        "--json", action="store_true", help="print JSON instead of a summary"
    )
    embed.set_defaults(func=_cmd_embed)

    similar = sub.add_parser(
        "similar",
        help="list the repos closest to one repo by embedding",
        description="Nearest neighbours of a repo by cosine similarity of its stored embedding.",
        epilog=SIMILAR_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    similar.add_argument(
        "repo", metavar="owner/name", help="a repo already in the database"
    )
    similar.add_argument(
        "-k",
        type=_positive,
        default=10,
        metavar="N",
        help="how many neighbours (default 10)",
    )
    similar.add_argument(
        "--domain",
        metavar="ID",
        help="only accepted repos of this domain are candidates",
    )
    similar.add_argument(
        "--json", action="store_true", help="print JSON instead of a table"
    )
    similar.set_defaults(func=_cmd_similar)
