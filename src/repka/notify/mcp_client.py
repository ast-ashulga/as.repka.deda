"""Stdio connection to the fast-mcp-telegram server (ported from the reference project).

The server exposes far more than repka needs (raw MTProto, edit, read, send-to-phone, ...) and the
session is a full user account. So every call goes through `GuardedSession`, which lets only the
allowlisted tools through and gives each call a deadline. repka never reads chats: the only
allowlist is `SEND_TOOLS`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from repka import config

SEND_TOOLS = frozenset({"send_message"})
INIT_TIMEOUT = 60.0  # seconds to start the server and finish the MCP handshake
CALL_TIMEOUT = 120.0  # seconds for one tool call (a Telegram flood-wait must not hang a run forever)


class ToolNotAllowed(Exception):
    """Raised before anything is sent to the server: this tool is outside the caller's allowlist."""


class GuardedSession:
    """Wraps an MCP ClientSession: allowlisted tool names only, and a timeout on every call."""

    def __init__(self, session: Any, allow: Collection[str], timeout: float = CALL_TIMEOUT) -> None:
        self._session = session
        self._allow = frozenset(allow)
        self._timeout = timeout

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in self._allow:
            raise ToolNotAllowed(f"MCP tool {name!r} is not allowed here (allowed: {', '.join(sorted(self._allow))})")
        try:
            return await asyncio.wait_for(self._session.call_tool(name, arguments), self._timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"MCP tool {name!r} did not answer within {self._timeout:.0f}s") from None


def _open_log() -> Any:
    """Append-only server log, readable by this user only (it can contain message text)."""
    directory = config.logs_dir()
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory / "server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "a")


@asynccontextmanager
async def open_session(*, allow: Collection[str]) -> AsyncIterator[GuardedSession]:
    """Start the fast-mcp-telegram subprocess with repka's Telegram session; yield a guarded client.

    `allow` names the tools the caller may use (least privilege). Raises ConfigError when the
    credentials or the session file are missing.
    """
    env = config.telegram_server_env()  # validates the credentials; only the subprocess receives them
    config.require_session()
    params = StdioServerParameters(
        command=config.venv_script("fast-mcp-telegram"),
        args=[],
        env=env,
        # cwd away from the project root so the server never auto-loads repka's .env
        cwd=str(config.session_dir()),
    )
    log = _open_log()
    try:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), INIT_TIMEOUT)
                yield GuardedSession(session, allow)
    finally:
        log.close()
