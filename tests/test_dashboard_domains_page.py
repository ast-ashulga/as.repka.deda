"""The Domains page under Streamlit's AppTest, on a scratch REPKA_HOME. Needs the dashboard group."""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("streamlit")

from helpers_digest import make_db  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from repka.config import db_path  # noqa: E402
from repka.domain.schema import load_domain  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "1_Domains.py"
EXAMPLE = REPO / "domains" / "example-mcp.yaml"


def _page(home) -> AppTest:
    at = AppTest.from_file(str(PAGE), default_timeout=30)
    at.run()
    return at


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _with_example(home) -> Path:
    domains = home / "domains"
    domains.mkdir()
    shutil.copy(EXAMPLE, domains / "example-mcp.yaml")
    return domains


def test_page_renders_with_domains_and_status(home):
    _with_example(home)
    make_db(db_path()).close()
    at = _page(home)
    assert not at.exception
    assert not at.error


def test_invalid_form_writes_nothing(home):
    domains = home / "domains"
    at = _page(home)
    at.text_input(key="(new domain):name").input("Demo")
    at.text_area(key="(new domain):profile").input("A demo domain.")
    # no keyword groups and no topics: the form is invalid
    _button(at, "Save domain").click().run()
    assert not at.exception
    assert any("keyword groups" in e.value for e in at.error)
    assert not domains.exists() or not any(domains.glob("*.yaml"))


def test_new_domain_is_saved_disabled_by_default(home):
    at = _page(home)
    at.text_input(key="(new domain):name").input("Demo domain")
    at.text_input(key="(new domain):id").input("demo-domain")
    at.text_area(key="(new domain):profile").input("Demo domain.")
    at.text_area(key="(new domain):groups").input("agent harness")
    _button(at, "Save domain").click().run()
    assert not at.exception
    saved = load_domain(home / "domains" / "demo-domain.yaml")
    assert saved.enabled is False
    assert saved.search.keywords == (("agent harness",),)


def test_editing_changes_scan_every_and_keeps_unshown_fields(home):
    domains = _with_example(home)
    before = load_domain(domains / "example-mcp.yaml")
    at = _page(home)
    at.selectbox(key="target").select("example-mcp").run()
    at.text_input(key="example-mcp:scan_every").input("7d")
    _button(at, "Save domain").click().run()
    assert not at.exception
    after = load_domain(domains / "example-mcp.yaml")
    assert after.scan_every.days == 7
    assert after.search == before.search
    assert after.key_files == before.key_files
    assert after.triage_questions == before.triage_questions


def test_delete_needs_confirmation(home):
    domains = _with_example(home)
    at = _page(home)
    at.selectbox(key="target").select("example-mcp").run()
    at.button(key="example-mcp:delete").click().run()
    assert (domains / "example-mcp.yaml").exists()
    assert any("Tick the confirmation" in s.value for s in at.error)

    at.checkbox(key="example-mcp:confirm_delete").check().run()
    at.button(key="example-mcp:delete").click().run()
    assert not (domains / "example-mcp.yaml").exists()
