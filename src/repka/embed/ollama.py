"""Ollama embedder: `POST {url}/api/embed` with a list of inputs, returns one vector per input."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import httpx


class EmbedderUnavailable(RuntimeError):
    """The embedding service cannot be reached or failed at runtime. Exit code 1, not a config error."""


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(
        self,
        url: str,
        model: str,
        timeout_s: float = 60,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self._client = httpx.Client(
            base_url=self.url, timeout=timeout_s, transport=transport
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = self._client.post(
                "/api/embed", json={"model": self.model, "input": list(texts)}
            )
        except httpx.ConnectError:
            raise EmbedderUnavailable(
                f"Ollama is not running at {self.url}: start it with `ollama serve`"
            ) from None
        except httpx.TimeoutException:
            raise EmbedderUnavailable(
                f"Ollama at {self.url} did not answer in time"
            ) from None
        except httpx.HTTPError as exc:
            raise EmbedderUnavailable(f"Ollama request failed: {exc}") from None
        if resp.status_code == 404:
            raise EmbedderUnavailable(  # expected and fixable: a one-line stage error, not a traceback each run
                f"embedding model {self.model} missing: run `ollama pull {self.model}`"
            )
        if resp.status_code != 200:
            raise EmbedderUnavailable(
                f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            vectors: Sequence[Sequence[float]] = resp.json()["embeddings"]
        except (ValueError, KeyError, TypeError):
            raise EmbedderUnavailable(
                "Ollama response has no `embeddings` list"
            ) from None
        if len(vectors) != len(texts):
            raise EmbedderUnavailable(
                f"Ollama returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return [[float(x) for x in vec] for vec in vectors]

    def close(self) -> None:
        self._client.close()
