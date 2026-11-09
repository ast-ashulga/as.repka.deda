"""Landscape map maths (P2-T03): 2-D projection, clustering and cluster labels. Pure: numpy in, numpy out.

No streamlit and no SQLite here; `dashboard_data` loads vectors and caches the result. Needs the
`dashboard` dependency group (umap-learn, scikit-learn, pandas).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

import numpy as np

NOISE = -1
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in into is it its of on or that the this to use used "
    "using with your you can based build built tool tools simple".split()
)


def project(matrix: np.ndarray, method: str = "umap", seed: int = 42) -> np.ndarray:
    """(n, dim) vectors -> (n, 2) float64 points. Deterministic for a given seed."""
    n = len(matrix)
    if n < 3:  # too few points for a neighbour graph: lay them out on a line
        return np.column_stack([np.arange(n, dtype=float), np.zeros(n)])
    if method == "umap":
        import umap

        reducer = umap.UMAP(
            n_components=2, n_neighbors=min(15, n - 1), metric="cosine", random_state=seed, init="random"
        )
        return np.asarray(reducer.fit_transform(matrix), dtype=float)
    if method == "tsne":
        from sklearn.manifold import TSNE

        tsne = TSNE(n_components=2, perplexity=min(30.0, (n - 1) / 3), random_state=seed, init="random")
        return np.asarray(tsne.fit_transform(matrix), dtype=float)
    raise ValueError(f"unknown projection method {method!r} (umap or tsne)")


def cluster(points: np.ndarray, min_cluster_size: int = 5) -> np.ndarray:
    """HDBSCAN labels for (n, d) points; -1 is noise. HDBSCAN itself is deterministic."""
    n = len(points)
    if n < max(min_cluster_size, 2):
        return np.full(n, NOISE, dtype=int)
    from sklearn.cluster import HDBSCAN

    return HDBSCAN(min_cluster_size=min_cluster_size, copy=True).fit_predict(points).astype(int)


def _terms(topics: Sequence[str] | None, description: str | None) -> set[str]:
    text = description if isinstance(description, str) else ""  # a NULL description reaches here as NaN
    words = {w for w in re.findall(r"[a-z][a-z0-9+#.-]{2,}", text.lower()) if w not in _STOPWORDS}
    return words | {t.lower() for t in (topics or ())}


def label_clusters(frame: Any, top: int = 3) -> dict[int, list[str]]:
    """Top terms per cluster (noise excluded) from `topics` (list) and `description` columns.

    A term scores by how many of the cluster's repos carry it, discounted by how common it is overall.
    """
    docs = [_terms(t, d) for t, d in zip(frame["topics"], frame["description"], strict=True)]
    labels = list(frame["cluster"])
    overall = Counter(term for doc in docs for term in doc)
    out: dict[int, list[str]] = {}
    for label in sorted(set(labels) - {NOISE}):
        counts = Counter(term for doc, lab in zip(docs, labels, strict=True) if lab == label for term in doc)
        scored = sorted(
            counts, key=lambda t: (-counts[t] * np.log(1 + len(docs) / overall[t]), t)
        )
        out[int(label)] = scored[:top]
    return out


def params_hash(ids: Sequence[int], matrix: np.ndarray, method: str, seed: int, min_cluster_size: int) -> str:
    """Cache key: vector content (ids + bytes) and parameters. Any changed vector changes it."""
    h = hashlib.sha256()
    h.update(json.dumps([method, seed, min_cluster_size, list(ids), list(matrix.shape)]).encode())
    h.update(np.ascontiguousarray(matrix, dtype=np.float32).tobytes())
    return h.hexdigest()
