import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated repka home: REPKA_HOME points at a temp dir and no host token leaks in."""
    monkeypatch.setenv("REPKA_HOME", str(tmp_path))
    monkeypatch.delenv("REPKA_GITHUB_TOKEN", raising=False)
    return tmp_path
