import hashlib
import json

import httpx
import pytest

from helpers_digest import add_repo, add_verdict, make_db
from repka import config
from repka.cli import main
from repka.config import ConfigError
from repka.domain.schema import Domain, SearchSpec
from repka.embed import cli as embed_cli
from repka.embed import embed as embed_mod
from repka.embed.embed import EmbedStats, embed_domain, embedding_text, input_hash
from repka.embed.ollama import EmbedderUnavailable, OllamaEmbedder
from repka.store import vectors

NOW = "2026-11-07T09:00:00Z"
DIM = 8


def fake_vector(text: str) -> list[float]:
    raw = hashlib.sha256(text.encode("utf-8")).digest()[:DIM]
    return [b / 255.0 + 0.01 for b in raw]


class FakeEmbedder:
    def __init__(self, model="fake-embed"):
        self.model = model
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [fake_vector(t) for t in texts]


@pytest.fixture
def conn(tmp_path):
    c = make_db(tmp_path / "e.db")
    yield c
    c.close()


def ollama_with(handler):
    return OllamaEmbedder(
        "http://ollama.test", "nomic-embed-text", transport=httpx.MockTransport(handler)
    )


def test_ollama_embed_batches_and_parses():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": "nomic-embed-text", "embeddings": [[1.0, 0.0], [0.0, 1.0]]},
        )

    vectors_out = ollama_with(handler).embed(["alpha", "beta"])
    assert seen["path"] == "/api/embed"
    assert seen["body"] == {"model": "nomic-embed-text", "input": ["alpha", "beta"]}
    assert vectors_out == [[1.0, 0.0], [0.0, 1.0]]


def test_connection_refused_gives_hint():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EmbedderUnavailable, match="ollama serve"):
        ollama_with(handler).embed(["x"])


def test_model_missing_is_config_error():
    def handler(request):
        return httpx.Response(
            404,
            json={"error": 'model "nomic-embed-text" not found, try pulling it first'},
        )

    with pytest.raises(ConfigError, match="ollama pull nomic-embed-text"):
        ollama_with(handler).embed(["x"])


def test_embed_domain_only_accepted_and_changed(conn):
    add_repo(conn, 1, description="accepted one")
    add_repo(conn, 2, description="rejected one")
    add_repo(conn, 3, description="accepted two")
    add_verdict(conn, 1, accepted=True)
    add_verdict(conn, 2, accepted=False)
    add_verdict(conn, 3, accepted=True)
    conn.execute("UPDATE repos SET readme_excerpt = 'Readme body' WHERE repo_id = 1")
    embedder = FakeEmbedder()

    stats = embed_domain(conn, "d1", embedder, batch=32, now=NOW)

    assert stats == EmbedStats(pending=2, embedded=2)
    sent = embedder.calls[0]
    assert len(sent) == 2 and not any("rejected one" in t for t in sent)
    assert vectors.embedding_hash(conn, 2, "fake-embed") is None

    conn.execute("UPDATE repos SET description = 'new text' WHERE repo_id = 3")
    embedder2 = FakeEmbedder()
    stats = embed_domain(conn, "d1", embedder2, batch=32, now=NOW)
    assert stats == EmbedStats(pending=1, embedded=1)
    assert "new text" in embedder2.calls[0][0]


def test_second_run_embeds_nothing(conn):
    add_repo(conn, 1)
    add_verdict(conn, 1, accepted=True)
    embed_domain(conn, "d1", FakeEmbedder(), batch=32, now=NOW)

    embedder = FakeEmbedder()
    assert embed_domain(conn, "d1", embedder, batch=32, now=NOW) == EmbedStats(
        pending=0, embedded=0
    )
    assert embedder.calls == []


def test_model_change_reembeds_and_keeps_old_rows(conn):
    add_repo(conn, 1)
    add_verdict(conn, 1, accepted=True)
    embed_domain(conn, "d1", FakeEmbedder("model-a"), batch=32, now=NOW)

    stats = embed_domain(conn, "d1", FakeEmbedder("model-b"), batch=32, now=NOW)

    assert stats.embedded == 1
    models = sorted(
        r["model"]
        for r in conn.execute("SELECT model FROM embeddings WHERE repo_id = 1")
    )
    assert models == ["model-a", "model-b"]


def test_doc_prefix_applied(conn, monkeypatch):
    monkeypatch.setattr(embed_mod, "DOC_PREFIX", "search_document: ")
    add_repo(conn, 1, description="prefixed")
    add_verdict(conn, 1, accepted=True)
    embedder = FakeEmbedder()

    embed_domain(conn, "d1", embedder, batch=32, now=NOW)

    sent = embedder.calls[0][0]
    assert sent.startswith("search_document: ")
    assert sent == "search_document: " + embedding_text(
        conn.execute("SELECT * FROM repos WHERE repo_id = 1").fetchone()
    )
    stored = vectors.embedding_hash(conn, 1, "fake-embed")
    assert stored == input_hash(sent)


def test_embedding_text_is_printable_and_bounded(conn):
    add_repo(conn, 1, description="bad\x1b[31m text")
    conn.execute("UPDATE repos SET readme_excerpt = ? WHERE repo_id = 1", ("x" * 5000,))
    text = embedding_text(
        conn.execute("SELECT * FROM repos WHERE repo_id = 1").fetchone()
    )
    assert "\x1b" not in text
    assert len(text) < 2200


def test_similar_cli_lists_neighbours(home, capsys):
    conn = make_db(config.db_path())
    add_repo(conn, 1, full_name="o/base", description="base")
    add_repo(conn, 2, full_name="o/near", description="near one")
    add_repo(conn, 3, full_name="o/far", description="far one")
    vectors.upsert_embedding(conn, 1, "nomic-embed-text", [1.0, 0.0], "h", NOW)
    vectors.upsert_embedding(conn, 2, "nomic-embed-text", [0.9, 0.1], "h", NOW)
    vectors.upsert_embedding(conn, 3, "nomic-embed-text", [0.0, 1.0], "h", NOW)
    conn.commit()
    conn.close()

    assert main(["similar", "o/base", "-k", "5"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "o/base" not in "\n".join(lines[1:])
    assert lines[1].split()[1] == "o/near"
    assert lines[2].split()[1] == "o/far"

    assert main(["similar", "o/base", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["full_name"] for r in payload["results"]] == ["o/near", "o/far"]


def test_similar_unknown_repo_is_usage_error(home, capsys):
    make_db(config.db_path()).close()
    assert main(["similar", "o/nope"]) == 2
    assert "error:" in capsys.readouterr().err


def test_embed_cli_reports_ollama_down(home, capsys, monkeypatch):
    conn = make_db(config.db_path())
    add_repo(conn, 1)
    add_verdict(conn, 1, accepted=True)
    conn.commit()
    conn.close()
    domain = Domain(id="d1", name="D", profile="p", search=SearchSpec((), (), (), ()))
    monkeypatch.setattr(embed_cli, "_select_domains", lambda _id: [domain])

    def down(_request):
        raise httpx.ConnectError("refused", request=_request)

    monkeypatch.setattr(
        embed_cli,
        "make_embedder",
        lambda cfg: OllamaEmbedder(
            cfg.embed.url, cfg.embed.model, transport=httpx.MockTransport(down)
        ),
    )

    assert main(["embed"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err and "ollama serve" in err
