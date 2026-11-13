"""The home page under Streamlit's AppTest: digest kinds, the waiting queue and recent releases."""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("streamlit")

from helpers_digest import make_db  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from repka.config import db_path  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "dashboard" / "app.py"


def _page(home, config: str | None = None) -> AppTest:
    domains = home / "domains"
    domains.mkdir()
    shutil.copy(REPO / "domains" / "example-mcp.yaml", domains / "example-mcp.yaml")
    example = domains / "example-mcp.yaml"
    example.write_text(example.read_text().replace("enabled: false", "enabled: true"))  # the example ships disabled
    if config:
        (home / ".config.yaml").write_text(config)
    conn = make_db(db_path())
    conn.execute(
        "INSERT INTO digests (slot, local_date, status, item_count, kind) VALUES ('releases:09:00', '2026-11-07', 'sent', 2, 'releases')"
    )
    conn.commit()
    conn.close()
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.run()
    return at


def test_home_shows_kinds_and_says_waiting_is_off_by_default(home):
    at = _page(home)
    assert not at.exception and not at.error
    headers = [h.value for h in at.subheader]
    assert {"Last digests", "Waiting for the next digest", "Recent releases"} <= set(headers)
    digests = at.dataframe[2].value  # domains, runs, digests
    assert "kind" in digests.columns and digests.iloc[0]["slot"] == "09:00"
    assert any("Off:" in i.value for i in at.info)  # both flags false: nothing is held back


def test_home_lists_the_waiting_queue_when_a_flag_is_on(home):
    at = _page(home, "digest:\n  insights: true\n")
    assert not at.exception and not at.error
    assert not any("Off:" in i.value for i in at.info)
    assert any(i.value == "Nothing is waiting." for i in at.info)
