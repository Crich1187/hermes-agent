"""Subprocess lifecycle wrapper for one `claude` CLI invocation.

PR 2 of the Hermes Claude Code CLI adapter. Wraps a single
`asyncio.create_subprocess_exec` call:

  * spawns with `start_new_session=True` so the child gets its own
    process group (pgid == child pid),
  * drains stdout into the PR 1 ``StreamJsonParser`` and stderr into
    a bounded buffer concurrently with ``proc.wait()``,
  * exposes parsed events via ``events()``,
  * exposes a redacted, truncated stderr digest for logging,
  * cancels via ``os.killpg(pgid, SIGTERM)`` with a configurable grace
    period before escalating to ``SIGKILL``,
  * guarantees full cleanup (drain + reap) via ``async with``.

No parser, settings, mcp_config, session_store, or adapter logic lives
here — those are PR 3+.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import deque
from typing import Any, AsyncIterator, Optional

from agent.claude_cli import errors
from agent.claude_cli.protocol import StreamJsonParser

logger = logging.getLogger(__name__)

DEFAULT_CANCEL_GRACE_SECONDS = 5.0
DEFAULT_STDERR_DIGEST_BYTES = 4096


class CancelToken:
    """Cooperative cancellation flag for one ``ClaudeProcess``.

    Set once by the caller (or by ``ClaudeProcess`` itself on internal
    failure). Idempotent: repeated ``cancel()`` calls are no-ops.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._event.set()

    async def wait(self) -> None:
        """Block until ``cancel()`` is called."""
        await self._event.wait()


class ClaudeProcess:
    """One ``claude`` subprocess invocation. Construct via ``spawn()``."""

    # Placeholders filled in by later tasks.
    def __init__(self) -> None:
        raise NotImplementedError("ClaudeProcess is constructed via spawn()")


async def spawn(
    argv: list[str],
    env: dict[str, str],
    cwd: Optional[str] = None,
    *,
    cancel_token: Optional[CancelToken] = None,
    cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    stderr_digest_bytes: int = DEFAULT_STDERR_DIGEST_BYTES,
) -> ClaudeProcess:
    """Spawn a ``claude`` subprocess and return a started ``ClaudeProcess``.

    Filled in by Task 2.
    """
    raise NotImplementedError
