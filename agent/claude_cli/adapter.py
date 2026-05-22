"""ClaudeCliAdapter — glues PRs 1–3 into one provider unit.

PR 4 of the Hermes Claude Code CLI adapter. The adapter is a thin
orchestration layer:

  - `__init__(config, env)` stores config + env, constructs the
    SessionStore, the two asyncio.Semaphore concurrency gates, and the
    per-session lock dict.
  - `init()` runs the PR 1 probe, scrubs ANTHROPIC_API_KEY from the spawn
    env (with a warning), validates CLAUDE_CODE_OAUTH_TOKEN presence,
    and refuses to start on any probe failure.
  - `primary_turn()` acquires the per-Hermes-session lock + the primary
    semaphore, writes the PR 3 settings + mcp-config files into a
    per-session tempdir, spawns the subprocess via PR 2, streams events,
    captures the claude_session_id on the first event, drives a
    per-turn idle watchdog, and yields events to the caller.
  - `aux_call()` acquires only the aux semaphore (no session resumption,
    no per-session lock), spawns a single-shot subprocess with
    `--no-session-persistence`, concatenates assistant text, and honors
    the deadline.
  - `close()` flips the closed flag, drains in-flight turns up to
    `shutdown_grace_seconds`, escalates to cancel + SIGKILL, and removes
    the per-session settings tempdir.

PR 5 wires this adapter into Hermes' provider runtime as
`claude_code_cli`; until then the adapter is constructable and unit-
testable but not reachable via `model.provider`.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional, TypedDict

from agent.claude_cli import errors
from agent.claude_cli.mcp_config import generate_mcp_config, write_mcp_config_file
from agent.claude_cli.probe import ProbeConfig, ProbeResult, run_probe
from agent.claude_cli.process import CancelToken, spawn
from agent.claude_cli.session_store import SessionStore
from agent.claude_cli.settings import (
    generate_settings,
    make_session_settings_dir,
    write_settings_file,
)

logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    """Runtime config for ClaudeCliAdapter (mirrors the YAML shape).

    Owned locally by the claude_cli package in PR 4. PR 5 wires Hermes'
    YAML reader to instantiate this; the provider registry receives the
    populated dataclass and constructs the adapter.
    """

    binary: str = "claude"
    min_version: tuple[int, int, int] = (2, 1, 143)
    primary_concurrency: int = 8
    aux_concurrency: int = 4
    turn_idle_timeout_seconds: float = 120.0
    aux_call_timeout_seconds: float = 60.0
    cancel_grace_seconds: float = 5.0
    shutdown_grace_seconds: float = 30.0
    session_busy_policy: str = "queue"  # "queue" | "reject"
    mcp_servers_allowlist: list[dict] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(
        default_factory=lambda: ["Bash", "Write", "Edit", "WebFetch", "WebSearch"]
    )
    strip_env: list[str] = field(default_factory=lambda: ["ANTHROPIC_API_KEY"])
    cross_provider_fallback: bool = False
    probe_cache_seconds: float = 86400.0
    probe_cache_path: Path = field(
        default_factory=lambda: Path.home() / ".hermes/cache/claude_cli_probe.json"
    )
    workspace_dir: str = ""
    session_ttl_seconds: float = 86400.0
    adapter_code_version: str = "0.4.0"


class Message(TypedDict):
    role: Literal["user", "assistant", "system"]
    content: str


_PROBE_ERROR_CLASS_MAP: dict[str, type[errors.ClaudeCliError]] = {
    "ClaudeCliUnavailable": errors.ClaudeCliUnavailable,
    "ClaudeCliVersionTooOld": errors.ClaudeCliVersionTooOld,
    "ClaudeCliAuthMissing": errors.ClaudeCliAuthMissing,
    "ClaudeCliIncompatible": errors.ClaudeCliIncompatible,
    "HermesDirectAnthropicEgressDetected": errors.HermesDirectAnthropicEgressDetected,
    "ProtocolError": errors.ProtocolError,
}


def _raise_from_probe_failure(result: ProbeResult) -> None:
    """Re-materialise a typed exception from a failed ProbeResult."""
    err = result.error or ""
    class_name, _, message = err.partition(": ")
    cls = _PROBE_ERROR_CLASS_MAP.get(class_name, errors.ClaudeCliError)
    raise cls(message or err or "probe failed without a message")


class ClaudeCliAdapter:
    """Adapter that routes Hermes calls through the ``claude`` CLI subprocess."""

    def __init__(self, config: ProviderConfig, env: dict[str, str]) -> None:
        self.config = config
        self._env: dict[str, str] = dict(env)
        self._spawn_env: dict[str, str] = {}  # populated by init()
        self._sessions = SessionStore(ttl_seconds=config.session_ttl_seconds)
        self._primary_sem = asyncio.Semaphore(config.primary_concurrency)
        self._aux_sem = asyncio.Semaphore(config.aux_concurrency)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_guard = asyncio.Lock()
        self._session_settings_dir: Optional[Path] = None
        self._inflight: set[asyncio.Task[Any]] = set()
        self._closed: bool = False
        self._inited: bool = False

    async def init(self) -> None:
        """Run the probe, scrub the env, populate ``_spawn_env``."""
        if self._inited:
            return

        from agent.claude_cli.probe import check_env_hygiene

        if "ANTHROPIC_API_KEY" in self._env:
            logger.warning(
                "ANTHROPIC_API_KEY present in adapter env; stripping from "
                "subprocess env per spec lifecycle table",
                extra={"strip_env": self.config.strip_env},
            )

        try:
            sanitized = check_env_hygiene(
                self._env,
                require_token=True,
                strip_env=list(self.config.strip_env),
            )
        except errors.ClaudeCliAuthMissing:
            raise

        probe_config = ProbeConfig(
            min_version=self.config.min_version,
            cache_path=self.config.probe_cache_path,
            adapter_code_version=self.config.adapter_code_version,
            binary_path=self.config.binary if self.config.binary != "claude" else None,
            require_token=True,
            strip_env=tuple(self.config.strip_env),
            cache_ttl_seconds=self.config.probe_cache_seconds,
        )
        result = await run_probe(probe_config)
        if not result.ok:
            _raise_from_probe_failure(result)

        self._spawn_env = sanitized
        self._inited = True

    # ---- helpers ----

    async def _get_session_lock(self, hermes_session_id: str) -> asyncio.Lock:
        async with self._session_locks_guard:
            lock = self._session_locks.get(hermes_session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[hermes_session_id] = lock
            return lock

    def _ensure_settings_dir(self) -> Path:
        if self._session_settings_dir is None:
            self._session_settings_dir = make_session_settings_dir()
        return self._session_settings_dir

    def _resolved_workspace_dir(self) -> str:
        return self.config.workspace_dir or str(self._ensure_settings_dir())

    def _build_primary_argv(
        self,
        *,
        settings_path: Path,
        mcp_path: Path,
        model: str,
        claude_session_id: Optional[str],
    ) -> list[str]:
        argv = [
            self.config.binary,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--settings", str(settings_path),
            "--mcp-config", str(mcp_path),
            "--strict-mcp-config",
            "--allowedTools", ",".join(self.config.allowed_tools),
            "--model", model,
        ]
        if claude_session_id is not None:
            argv.extend(["--resume", claude_session_id])
        return argv

    @staticmethod
    def _format_prompt(messages: list[Message], is_new: bool) -> str:
        """Serialise messages to the prompt fed via stdin."""
        if not messages:
            return ""
        if not is_new:
            return messages[-1]["content"]
        return "\n\n".join(f"{m['role']}: {m['content']}" for m in messages)

    # ---- primary_turn ----

    async def primary_turn(
        self,
        hermes_session_id: str,
        messages: list[Message],
        *,
        model: str,
        cancel_token: CancelToken,
        deadline: Optional[float] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        if self._closed:
            raise errors.ClaudeCliError("adapter is closed")
        if not self._inited:
            raise errors.ClaudeCliError("adapter.init() has not been called")

        lock = await self._get_session_lock(hermes_session_id)
        if self.config.session_busy_policy == "reject" and lock.locked():
            raise errors.SessionBusyError(
                f"session {hermes_session_id!r} is busy and policy=reject"
            )

        async with lock:
            async with self._primary_sem:
                async for event in self._run_primary_turn(
                    hermes_session_id=hermes_session_id,
                    messages=messages,
                    model=model,
                    cancel_token=cancel_token,
                    deadline=deadline,
                ):
                    yield event

    async def _run_primary_turn(
        self,
        *,
        hermes_session_id: str,
        messages: list[Message],
        model: str,
        cancel_token: CancelToken,
        deadline: Optional[float],
    ) -> AsyncIterator[dict[str, Any]]:
        claude_id, is_new = self._sessions.get_or_create(hermes_session_id)
        settings_dir = self._ensure_settings_dir()
        settings_path = write_settings_file(
            generate_settings(
                workspace_dir=self._resolved_workspace_dir(),
                allowed_tools=self.config.allowed_tools,
                disallowed_tools=self.config.disallowed_tools,
            ),
            parent_dir=settings_dir,
        )
        mcp_path = write_mcp_config_file(
            generate_mcp_config(self.config.mcp_servers_allowlist),
            parent_dir=settings_dir,
        )
        argv = self._build_primary_argv(
            settings_path=settings_path,
            mcp_path=mcp_path,
            model=model,
            claude_session_id=claude_id if not is_new else None,
        )
        prompt = self._format_prompt(messages, is_new=is_new)

        proc = await spawn(
            argv=argv,
            env=self._spawn_env,
            cwd=self.config.workspace_dir or None,
            cancel_token=cancel_token,
            cancel_grace_seconds=self.config.cancel_grace_seconds,
        )

        started_at = time.monotonic()
        event_count = 0
        logger.info(
            "claude_cli.turn.start",
            extra={
                "hermes_session_id": hermes_session_id,
                "claude_session_id": claude_id,
                "model": model,
                "pid": proc.pid,
                "started_at": started_at,
            },
        )

        async with proc:
            # Feed stdin then close so claude knows the prompt is complete.
            assert proc._proc.stdin is not None
            try:
                proc._proc.stdin.write(prompt.encode("utf-8"))
                await proc._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # Child closed stdin early — surfaces as exit-with-error below.
                pass
            finally:
                if not proc._proc.stdin.is_closing():
                    proc._proc.stdin.close()

            try:
                async for event in self._stream_with_watchdog(
                    proc, hermes_session_id, claude_id
                ):
                    event_count += 1
                    yield event
            except BaseException:
                cancel_token.cancel()
                raise

        exit_code = proc.exit_code if proc.exit_code is not None else -1
        duration_ms = int((time.monotonic() - started_at) * 1000)
        if exit_code != 0:
            stderr_digest = proc.stderr_digest
            logger.info(
                "claude_cli.turn.error",
                extra={
                    "hermes_session_id": hermes_session_id,
                    "claude_session_id": self._sessions.get_or_create(
                        hermes_session_id
                    )[0],
                    "exit_code": exit_code,
                    "duration_ms": duration_ms,
                    "event_count": event_count,
                    "stderr_size": len(stderr_digest),
                    "error_class": "ClaudeCliExited",
                    "error_message_redacted": "subprocess non-zero exit",
                },
            )
            raise errors.ClaudeCliExited(
                exit_code=exit_code, stderr_digest=stderr_digest
            )

        logger.info(
            "claude_cli.turn.end",
            extra={
                "hermes_session_id": hermes_session_id,
                "claude_session_id": self._sessions.get_or_create(
                    hermes_session_id
                )[0],
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "event_count": event_count,
                "stderr_size": len(proc.stderr_digest),
            },
        )

    async def _stream_with_watchdog(
        self,
        proc,
        hermes_session_id: str,
        starting_claude_id: Optional[str],
    ) -> AsyncIterator[dict[str, Any]]:
        idle_timeout = self.config.turn_idle_timeout_seconds
        event_iter = proc.events().__aiter__()
        captured_id = starting_claude_id
        while True:
            try:
                event = await asyncio.wait_for(
                    event_iter.__anext__(), timeout=idle_timeout
                )
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as exc:
                raise errors.ClaudeCliHung(
                    f"no stream-json event for {idle_timeout:.1f}s on session "
                    f"{hermes_session_id!r}"
                ) from exc

            if captured_id is None:
                sid = event.get("session_id")
                if isinstance(sid, str) and sid:
                    self._sessions.set(hermes_session_id, sid)
                    captured_id = sid

            yield event
