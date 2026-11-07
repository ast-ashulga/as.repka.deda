"""Spike (P1-T04): confirm the Ollama embedding endpoint, dimension, JSON shape and speed.

Run from the repo root:  uv run python scripts/spike_ollama.py [--url URL] [--model NAME]

Not part of the package; not run by the test suite (tests never touch Ollama).
"""

from __future__ import annotations

import argparse
import math
import platform
import statistics
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
RELATED = [
    "A Python client library for the Model Context Protocol with tools and resources for MCP servers.",
    "Build Model Context Protocol servers and clients in Python: tools, prompts and resource handlers.",
]
UNRELATED = [
    "A recipe collection for sourdough bread: hydration ratios, starter maintenance and proofing times.",
    "Chocolate chip cookies with brown butter and flaky sea salt, baked until golden at the edges.",
]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb)


def embed(client: httpx.Client, url: str, model: str, texts: list[str]) -> dict:
    resp = client.post(f"{url}/api/embed", json={"model": model, "input": texts})
    resp.raise_for_status()
    return resp.json()


def readme_like_texts(count: int, size: int = 1500) -> list[str]:
    """Offline README-sized texts: chunks of the wiki's own Markdown (no network)."""
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in sorted((REPO_ROOT / "wiki").rglob("*.md")))
    chunks = [corpus[i : i + size] for i in range(0, len(corpus) - size, size)]
    if len(chunks) < count:
        raise SystemExit(f"error: wiki corpus yields only {len(chunks)} chunks, need {count}")
    return chunks[:count]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--model", default="nomic-embed-text")
    args = parser.parse_args(argv)

    print(f"python: {sys.version.split()[0]} ({platform.machine()})")
    print(f"endpoint: {args.url}/api/embed  model: {args.model}")

    with httpx.Client(timeout=120) as client:
        version = client.get(f"{args.url}/api/version").json()
        print(f"ollama version: {version.get('version')}")

        # 1. Shape and dimension of a 3-text request.
        sample = embed(client, args.url, args.model, RELATED[:1] + RELATED[1:] + UNRELATED[:1])
        print(f"response keys: {sorted(sample)}")
        vectors = sample["embeddings"]
        print(f"batch size returned: {len(vectors)}  dim: {len(vectors[0])}")
        print(f"all dims equal: {len({len(v) for v in vectors}) == 1}")
        print(f"float values sample: {vectors[0][:3]}")

        # 2. Sanity: related pair vs unrelated pairs, with and without task prefixes.
        for label, prefix_a, prefix_b in [
            ("no prefix", "", ""),
            ("search_document/search_query", "search_query: ", "search_document: "),
        ]:
            texts = [prefix_a + RELATED[0], prefix_b + RELATED[1]] + [prefix_b + t for t in UNRELATED]
            v = embed(client, args.url, args.model, texts)["embeddings"]
            related = cosine(v[0], v[1])
            unrelated = max(cosine(v[0], v[2]), cosine(v[0], v[3]))
            print(
                f"sanity [{label}]: related={related:.4f} unrelated_max={unrelated:.4f} "
                f"passes={related > unrelated}"
            )

        # 3. Timing: 32 README-sized texts, several runs.
        texts = readme_like_texts(32)
        print(f"timing texts: 32 x ~{len(texts[0])} chars")
        timings = []
        for _ in range(3):
            start = time.perf_counter()
            out = embed(client, args.url, args.model, texts)
            timings.append(time.perf_counter() - start)
        assert len(out["embeddings"]) == 32
        print(f"batch of 32 wall time (s): {[round(t, 3) for t in timings]}")
        print(f"median per batch: {statistics.median(timings):.3f} s  per text: {statistics.median(timings) / 32 * 1000:.1f} ms")
        print(f"ollama load/eval info: total_duration={out.get('total_duration')} load_duration={out.get('load_duration')} prompt_eval_count={out.get('prompt_eval_count')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
