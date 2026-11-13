import io
import sys

from repka.cli import main
from repka.config import domains_dir
from repka.domain.matrix import build_queries
from repka.domain.schema import load_domain, save_domain
from repka.domain.wizard import run_wizard

DESCRIPTION = "MCP servers, clients and SDKs."


class ScriptedIO:
    """Answers come from a list in the order the wizard asks. Running out of answers fails the test."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.messages: list[str] = []

    def _next(self, prompt: str) -> str:
        if not self.answers:
            raise AssertionError(f"no scripted answer for {prompt!r}")
        return self.answers.pop(0)

    def ask(self, prompt: str, default: str = "") -> str:
        return self._next(prompt) or default

    def ask_multiline(self, prompt: str) -> str:
        return self._next(prompt)

    def confirm(self, prompt: str, default: bool = False) -> bool:
        value = self._next(prompt).strip().lower()
        return default if not value else value in ("y", "yes")

    def echo(self, text: str) -> None:
        self.messages.append(text)


def _answers(*, enable: str = "", probe: str = "", save: str = "y") -> list[str]:
    """Every answer in the order run_wizard asks them. `probe` is asked only when a probe is wired (pass "y"/"n")."""
    answers = [
        "MCP tools",  # name -> id suggested as mcp-tools
        "",  # id (Enter takes the suggestion)
        DESCRIPTION,  # description (multi-line prompt)
        "model context protocol, mcp server; mcp client",  # keyword groups
        "mcp",  # topics
        "Python, TypeScript",  # languages
        "minecraft",  # excludes
        "",  # min stars (default 5)
        "",  # fork policy (default exclude)
        "",  # scan every (default 12h)
        enable,  # enable now (default no)
    ]
    if probe:
        answers.append(probe)
    answers.append(save)
    return answers


def test_wizard_scripted_creates_valid_domain(home):
    domain = run_wizard(ScriptedIO(_answers()))
    assert domain is not None
    path = save_domain(domain)
    assert load_domain(path) == domain
    assert domain.search.keywords == (("model context protocol", "mcp server"), ("mcp client",))
    assert domain.search.languages == ("Python", "TypeScript")


def test_wizard_default_enabled_false(home):
    domain = run_wizard(ScriptedIO(_answers()))
    assert domain is not None and domain.enabled is False


def test_wizard_declined_save_returns_none(home):
    assert run_wizard(ScriptedIO(_answers(save="n"))) is None


def test_wizard_probe_called_per_query_only_when_confirmed(home):
    calls: list[str] = []

    def probe(q: str) -> int:
        calls.append(q)
        return 7

    assert run_wizard(ScriptedIO(_answers(probe="n")), probe=probe) is not None
    assert calls == []

    domain = run_wizard(ScriptedIO(_answers(probe="y")), probe=probe)
    assert domain is not None
    assert calls == [q.q for q in build_queries(domain)]


def test_wizard_rejects_existing_id_unless_confirmed_overwrite(home):
    domains_dir().mkdir(parents=True)
    (domains_dir() / "mcp-tools.yaml").write_text("placeholder", encoding="utf-8")

    # Answers: name, id (default mcp-tools exists), then either overwrite? or a new id.
    declined = ScriptedIO(_answers())
    declined.answers[2:2] = ["n", "mcp-tools-2"]
    domain = run_wizard(declined)
    assert domain is not None and domain.id == "mcp-tools-2"
    assert any("already exists" in message for message in declined.messages)

    overwrite = ScriptedIO(_answers())
    overwrite.answers.insert(2, "y")
    domain = run_wizard(overwrite)
    assert domain is not None and domain.id == "mcp-tools"


def test_domain_new_cli_reads_piped_answers(home, tmp_path, monkeypatch, capsys):
    description = tmp_path / "desc.md"
    description.write_text(DESCRIPTION + "\n", encoding="utf-8")
    piped = "\n".join(
        [
            "MCP tools",  # name
            "",  # id (suggested)
            "model context protocol, mcp server",  # keyword groups
            "mcp",  # topics
            "",  # languages
            "",  # excludes
            "",  # min stars
            "",  # fork policy
            "",  # scan every
            "",  # enable now -> no
            "y",  # save
        ]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(piped + "\n"))

    assert main(["domain", "new", "--from-file", str(description)]) == 0

    written = domains_dir() / "mcp-tools.yaml"
    assert written.is_file()
    assert capsys.readouterr().out.strip() == str(written)
    assert load_domain(written).enabled is False
    assert main(["domain", "plan", "mcp-tools", "--json"]) == 0


def test_domain_new_cli_eof_saves_nothing(home, tmp_path, monkeypatch, capsys):
    description = tmp_path / "desc.md"
    description.write_text(DESCRIPTION + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO("MCP tools\n"))  # answers end early

    assert main(["domain", "new", "--from-file", str(description)]) == 2
    assert "nothing was saved" in capsys.readouterr().err
    assert not (domains_dir() / "mcp-tools.yaml").exists()
