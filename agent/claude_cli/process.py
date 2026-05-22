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

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        cancel_token: CancelToken,
        cancel_grace_seconds: float,
        stderr_digest_bytes: int,
    ) -> None:
        self._proc = proc
        self._cancel_token = cancel_token
        self._cancel_grace_seconds = cancel_grace_seconds
        self._stderr_digest_bytes = stderr_digest_bytes
        self._pgid = proc.pid  # start_new_session=True → pgid == pid
        self._cancelled_kill_done = False
        self._stderr_buf: deque[bytes] = deque()
        self._stderr_bytes_seen = 0
        self._stdout_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._wait_task: Optional[asyncio.Task[int]] = None
        self._cancel_watcher: Optional[asyncio.Task[None]] = None
        self._parser = StreamJsonParser()
        self._event_queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()
        self._tasks_started = False

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def pgid(self) -> int:
        return self._pgid

    @property
    def exit_code(self) -> Optional[int]:
        return self._proc.returncode

    async def wait_until_exit(self) -> int:
        """Block until the child exits. Returns the exit code."""
        return await self._proc.wait()


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
    if cancel_token is None:
        cancel_token = CancelToken()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as exc:
        raise errors.SubprocessSpawnFailed(
            f"failed to spawn {argv[0]!r}: {exc}"
        ) from exc
    return ClaudeProcess(
        proc,
        cancel_token=cancel_token,
        cancel_grace_seconds=cancel_grace_seconds,
        stderr_digest_bytes=stderr_digest_bytes,
    )
