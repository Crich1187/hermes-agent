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
        self._aexit_done = False

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

    async def __aenter__(self) -> "ClaudeProcess":
        self._start_tasks()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Idempotency: if we've already cleaned up, return immediately.
        if self._aexit_done:
            return
        try:
            # If the body raised or the caller cancelled, ensure the child
            # process is terminated. Otherwise we still drain to completion.
            if self._proc.returncode is None:
                self._cancel_token.cancel()
            # Wait for reader tasks and the process to finish.
            tasks: list[asyncio.Task[Any]] = []
            for t in (self._stdout_task, self._stderr_task, self._wait_task):
                if t is not None:
                    tasks.append(t)
            if tasks:
                # gather with return_exceptions so a parser failure in stdout
                # task does not block stderr/wait cleanup.
                await asyncio.gather(*tasks, return_exceptions=True)
            # Cancel the cancel_watcher if it's still waiting (process exited
            # normally and the token was never cancelled).
            if self._cancel_watcher is not None and not self._cancel_watcher.done():
                self._cancel_token.cancel()
                try:
                    await asyncio.wait_for(self._cancel_watcher, timeout=1.0)
                except asyncio.TimeoutError:
                    self._cancel_watcher.cancel()
            # Close stdin if it's still open.
            if self._proc.stdin is not None and not self._proc.stdin.is_closing():
                try:
                    self._proc.stdin.close()
                except Exception:  # noqa: BLE001 — best-effort close
                    pass
        finally:
            self._aexit_done = True

    def _start_tasks(self) -> None:
        """Idempotently start stdout/stderr reader + wait tasks."""
        if self._tasks_started:
            return
        self._tasks_started = True
        self._stdout_task = asyncio.create_task(
            self._drain_stdout(), name="claude-proc-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="claude-proc-stderr"
        )
        self._wait_task = asyncio.create_task(
            self._proc.wait(), name="claude-proc-wait"
        )
        self._cancel_watcher = asyncio.create_task(
            self._watch_cancel(), name="claude-proc-cancel-watcher"
        )

    async def _drain_stdout(self) -> None:
        assert self._proc.stdout is not None
        try:
            while True:
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                for event in self._parser.feed(chunk):
                    await self._event_queue.put(event)
            for event in self._parser.close():
                await self._event_queue.put(event)
        except errors.ProtocolError as exc:
            logger.warning("stream-json protocol error: %s", exc)
            # Signal end-of-stream; consumer's events() exits, __aexit__
            # observes a non-zero exit_code or surfaces the parser failure.
        finally:
            await self._event_queue.put(None)  # sentinel

    async def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        while True:
            chunk = await self._proc.stderr.read(65536)
            if not chunk:
                break
            self._stderr_bytes_seen += len(chunk)
            self._stderr_buf.append(chunk)
            # Bound memory: keep only enough trailing bytes to render the digest.
            while (
                sum(len(b) for b in self._stderr_buf)
                > self._stderr_digest_bytes * 2
            ):
                self._stderr_buf.popleft()

    async def _watch_cancel(self) -> None:
        """Wait for cancel; on signal, kill the process group."""
        await self._cancel_token.wait()
        await self._kill_process_group()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed stream-json events until the subprocess closes stdout."""
        self._start_tasks()
        while True:
            event = await self._event_queue.get()
            if event is None:
                return
            yield event

    @property
    def stderr_digest(self) -> str:
        """Redacted, length-capped stderr capture for logging.

        Returns the trailing ``stderr_digest_bytes`` of stderr (UTF-8 with
        replacement) prefixed with a ``[truncated N bytes]`` marker when the
        full stderr exceeded the cap.
        """
        joined = b"".join(self._stderr_buf)
        if not joined:
            return ""
        if self._stderr_bytes_seen <= self._stderr_digest_bytes:
            return joined.decode("utf-8", errors="replace")
        tail = joined[-self._stderr_digest_bytes :]
        prefix = f"[truncated {self._stderr_bytes_seen - self._stderr_digest_bytes} bytes]\n"
        return prefix + tail.decode("utf-8", errors="replace")

    async def _kill_process_group(self) -> None:
        """Stub — filled in by Task 5 (cancellation)."""
        return


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
