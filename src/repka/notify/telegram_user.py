"""Send HTML message parts from the user's own Telegram account via fast-mcp-telegram.

The destination (chat, topic) comes from `notify:` in `.config.yaml`; whether to send at all is the
caller's decision (`notify.enabled`). Nothing here raises: failures become `SendResult(ok=False)`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from repka.notify import mcp_client
from repka.progress.active import stderr_line


@dataclass
class SendResult:
    ok: bool
    sent: int = 0
    message_ids: list[int] = field(default_factory=list)
    error: str | None = None
    delivery_unknown: bool = False  # the call timed out: the message may or may not have gone out


class Sender(Protocol):
    def send(self, parts: list[str]) -> SendResult:
        """Synchronous: callers (digest run) are sync code."""
        ...


def extract_payload(result: Any) -> dict[str, Any]:
    """Pull the JSON object out of an MCP CallToolResult (structured or text)."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        # FastMCP wraps non-object returns as {"result": ...}
        inner = structured.get("result")
        return inner if set(structured) == {"result"} and isinstance(inner, dict) else structured
    text = "".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": text.strip() or "empty response"}
    return data if isinstance(data, dict) else {"ok": False, "error": "unexpected response shape"}


def _send_error(result: Any, payload: dict[str, Any]) -> str | None:
    if getattr(result, "isError", False) or payload.get("ok") is False or "message_id" not in payload:
        return str(payload.get("error") or payload.get("message") or "unknown error")
    return None


async def deliver_parts(session: Any, chat_id: str, parts: list[str], topic_id: int | None) -> SendResult:
    """One `send_message` per part (HTML), in order. Stops at the first failed part. Never raises."""
    ids: list[int] = []
    for part in parts:
        args: dict[str, Any] = {"chat_id": chat_id, "message": part, "parse_mode": "html"}
        if topic_id is not None:
            args["reply_to_id"] = topic_id
        unknown = False
        try:
            res = await session.call_tool("send_message", args)
            payload = extract_payload(res)
            error = _send_error(res, payload)
            if error is None:
                ids.append(int(payload["message_id"]))
        except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
            error = f"{type(exc).__name__}: {exc}"
            unknown = isinstance(exc, TimeoutError)  # no answer is not "not delivered"
        if error is not None:
            return SendResult(ok=False, sent=len(ids), message_ids=ids, error=error, delivery_unknown=unknown)
    return SendResult(ok=True, sent=len(ids), message_ids=ids)


def _default_session() -> AbstractAsyncContextManager[Any]:
    return mcp_client.open_session(allow=mcp_client.SEND_TOOLS)


class TelegramUserSender:
    """Delivers all parts of one message through ONE MCP session (one server process)."""

    def __init__(
        self,
        chat_id: str,
        topic_id: int | None = None,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._topic_id = topic_id
        self._factory = session_factory or _default_session

    async def _send(self, parts: list[str]) -> SendResult:
        outcome: list[SendResult] = []
        try:
            async with self._factory() as session:
                outcome.append(await deliver_parts(session, self._chat_id, parts, self._topic_id))
        except Exception as exc:  # noqa: BLE001 - incl. the ExceptionGroup anyio raises on close
            if outcome:  # delivery finished; only closing the session failed. Do not report sent parts as lost.
                stderr_line(f"warning: closing the Telegram session failed: {type(exc).__name__}", "WARNING")
                return outcome[0]
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return outcome[0]

    def send(self, parts: list[str]) -> SendResult:
        try:
            return asyncio.run(self._send(parts))
        except Exception as exc:  # noqa: BLE001 - never raises
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")
