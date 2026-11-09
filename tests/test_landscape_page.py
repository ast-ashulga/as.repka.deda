"""The Landscape page under Streamlit's AppTest, on a scratch REPKA_HOME with synthetic vectors."""

from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("umap")
apptest = pytest.importorskip("streamlit.testing.v1")

import numpy as np  # noqa: E402
from helpers_digest import add_repo, add_verdict, make_db  # noqa: E402

import repka  # noqa: E402
from repka.config import db_path  # noqa: E402
from repka.domain.schema import Domain, SearchSpec, save_domain  # noqa: E402
from repka.store.vectors import upsert_embedding  # noqa: E402

PAGE = Path(repka.__file__).resolve().parents[2] / "dashboard" / "pages" / "2_Landscape.py"


def _seed(fork_policy, with_vectors):
    search = SearchSpec(keywords=(("mcp",),), topics=(), languages=(), exclude=(), fork_policy=fork_policy)
    save_domain(Domain(id="d1", name="D1", enabled=True, scan_every=timedelta(hours=12), profile="p", search=search))
    conn = make_db(db_path())
    rng = np.random.default_rng(0)
    for i in range(1, 21):
        add_repo(conn, i, stars=10 * i)
        add_verdict(conn, i, accepted=True)
        if with_vectors:
            centre = np.eye(8)[i % 2] * 5
            upsert_embedding(conn, i, "nomic-embed-text", centre + rng.normal(0, 0.3, 8), f"h{i}", "2026-11-07T08:00:00Z")
    conn.commit()
    conn.close()


def _run():
    at = apptest.AppTest.from_file(str(PAGE), default_timeout=120)
    at.run()
    assert not at.exception
    return at


def test_empty_state_without_embeddings_and_fork_hint(home):
    _seed("exclude", with_vectors=False)
    at = _run()
    assert any("repka embed" in i.value for i in at.info)
    assert any("fork policy" in i.value for i in at.info)


def test_recompute_then_page_renders_the_map(home):
    _seed("include", with_vectors=True)
    at = _run()
    assert any("Recompute" in i.value for i in at.info)  # nothing computed yet: no automatic compute
    at.button[0].click().run()
    assert not at.exception
    assert not any("not been computed" in i.value for i in at.info)
    assert [s.value for s in at.subheader] == ["Clusters", "Repositories"]
