import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from repka import config
from repka.cli import main
from repka.notify import cli as notify_cli
from repka.notify.mcp_client import GuardedSession, ToolNotAllowed
from repka.notify.telegram_user import SendResult, TelegramUserSender, deliver_parts


class FakeSession:
    """Async call_tool that records calls; `fail_at` is the 1-based call number that returns an error."""

    def __init__(self, fail_at=None, raises=None):
        self.calls = []
        self.fail_at = fail_at
        self.raises = raises

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self.raises:
            raise self.raises
        n = len(self.calls)
        if n == self.fail_at:
            return SimpleNamespace(structuredContent={"ok": False, "error": "flood wait"}, isError=False, content=[])
        return SimpleNamespace(structuredContent={"message_id": 100 + n}, isError=False, content=[])


def run(session, parts, chat="me", topic=None):
    return asyncio.run(deliver_parts(session, chat, parts, topic))


def test_send_posts_html_parts_in_order():
    s = FakeSession()
    result = run(s, ["a", "b", "c"])
    assert result.ok and result.sent == 3 and result.message_ids == [101, 102, 103]
    assert [c[1]["message"] for c in s.calls] == ["a", "b", "c"]
    assert all(c[0] == "send_message" and c[1]["parse_mode"] == "html" for c in s.calls)


def test_topic_id_sent_as_reply_to_id():
    s = FakeSession()
    run(s, ["a"], topic=7)
    assert s.calls[0][1]["reply_to_id"] == 7
    s = FakeSession()
    run(s, ["a"])
    assert "reply_to_id" not in s.calls[0][1]


def test_chat_id_me_passed_through():
    s = FakeSession()
    run(s, ["a"], chat="me")
    assert s.calls[0][1]["chat_id"] == "me"


def test_first_failed_part_stops_delivery():
    s = FakeSession(fail_at=2)
    result = run(s, ["a", "b", "c"])
    assert not result.ok and result.sent == 1 and result.error == "flood wait"
    assert len(s.calls) == 2


def test_exception_in_session_becomes_send_result():
    result = run(FakeSession(raises=RuntimeError("boom")), ["a"])
    assert not result.ok and "RuntimeError: boom" in result.error

    @asynccontextmanager
    async def broken_factory():
        raise OSError("server did not start")
        yield

    result = TelegramUserSender("me", session_factory=broken_factory).send(["a"])
    assert not result.ok and "server did not start" in result.error

    @asynccontextmanager
    async def close_fails():
        yield FakeSession()
        raise RuntimeError("close failed")

    result = TelegramUserSender("me", session_factory=close_fails).send(["a"])
    assert result.ok and result.sent == 1  # delivered; a failing close must not report the parts as lost


def test_guarded_session_rejects_other_tools():
    inner = FakeSession()
    guarded = GuardedSession(inner, {"send_message"})
    with pytest.raises(ToolNotAllowed):
        asyncio.run(guarded.call_tool("get_messages", {"chat_id": "me"}))
    assert inner.calls == []


def test_server_env_has_no_github_token(home, monkeypatch):
    (home / ".env").write_text("REPKA_GITHUB_TOKEN=ghp_secret\nREPKA_TG_API_ID=123\nREPKA_TG_API_HASH=abc\n")
    for key in ("REPKA_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.setenv(key, "ghp_secret")
    env = config.telegram_server_env()
    assert "ghp_secret" not in env.values()
    assert env["API_ID"] == "123" and env["API_HASH"] == "abc" and env["SESSION_NAME"] == "telegram"
    assert env["SESSION_DIR"] == str(home / ".sessions") and env["SERVER_MODE"] == "stdio"
    assert oct((home / ".sessions").stat().st_mode & 0o777) == "0o700"


def test_credentials_repr_hides_values(home):
    (home / ".env").write_text("REPKA_TG_API_ID=123456\nREPKA_TG_API_HASH=hashvalue\n")
    creds = config.telegram_credentials()
    assert "123456" not in repr(creds) and "hashvalue" not in repr(creds)
    (home / ".env").write_text("REPKA_TG_API_ID=123456\n")
    with pytest.raises(config.ConfigError, match="REPKA_TG_API_HASH") as exc:
        config.telegram_credentials()
    assert "123456" not in str(exc.value)


def test_missing_session_hint_mentions_notify_login(home):
    with pytest.raises(config.ConfigError, match="uv run repka notify login"):
        config.require_session()


@pytest.fixture
def no_sender(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a Telegram session must not be opened")

    monkeypatch.setattr(notify_cli, "TelegramUserSender", boom)


def test_notify_test_refused_when_disabled(home, capsys, no_sender):
    assert main(["notify", "test", "hello"]) == 2
    assert "notify.enabled" in capsys.readouterr().err


def test_notify_test_dry_run_prints_without_session(home, capsys, no_sender):
    assert main(["notify", "test", "a <b> & c", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "a &lt;b&gt; &amp; c" in out and "me" in out


def test_notify_test_text_over_the_telegram_limit_is_split(home, capsys, no_sender):
    text = "\n".join(["x" * 3000, "y" * 3000])
    assert main(["notify", "test", text, "--dry-run"]) == 0
    assert "--- part 2/2 ---" in capsys.readouterr().out


def test_notify_test_failure_text_is_printable(home, capsys, monkeypatch):
    (home / ".config.yaml").write_text("notify:\n  enabled: true\n")
    monkeypatch.setattr(config, "telegram_credentials", lambda: None)
    monkeypatch.setattr(config, "require_session", lambda: None)
    failing = SendResult(ok=False, sent=0, error="boom \x1b]0;pwned\x07\x1b[31m")
    monkeypatch.setattr(notify_cli, "TelegramUserSender", lambda *_a: SimpleNamespace(send=lambda _parts: failing))
    assert main(["notify", "test", "hi"]) == 1
    err = capsys.readouterr().err
    assert "boom" in err and "\x1b" not in err and "\x07" not in err


def test_timeout_marks_delivery_unknown_but_other_errors_do_not():
    timed_out = run(FakeSession(raises=TimeoutError("MCP tool 'send_message' did not answer")), ["a"])
    assert not timed_out.ok and timed_out.sent == 0 and timed_out.delivery_unknown
    refused = run(FakeSession(fail_at=1), ["a"])
    assert not refused.ok and not refused.delivery_unknown
    assert not run(FakeSession(raises=ValueError("boom")), ["a"]).delivery_unknown
