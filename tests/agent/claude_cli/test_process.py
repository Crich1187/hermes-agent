"""Unit + integration tests for agent.claude_cli.process."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

import pytest

from agent.claude_cli import errors
from agent.claude_cli.process import CancelToken, ClaudeProcess, spawn


class TestCancelToken:
    def test_starts_uncancelled(self):
        token = CancelToken()
        assert token.cancelled is False

    def test_cancel_sets_flag(self):
        token = CancelToken()
        token.cancel()
        assert token.cancelled is True

    def test_cancel_is_idempotent(self):
        token = CancelToken()
        token.cancel()
        token.cancel()
        token.cancel()
        assert token.cancelled is True

    @pytest.mark.asyncio
    async def test_wait_resolves_when_cancelled(self):
        token = CancelToken()
        waiter = asyncio.create_task(token.wait())
        await asyncio.sleep(0)  # let waiter park
        assert not waiter.done()
        token.cancel()
        await asyncio.wait_for(waiter, timeout=1.0)
        assert waiter.done()


class TestSpawnLifecycle:
    @pytest.mark.asyncio
    async def test_spawn_returns_claude_process(self):
        proc = await spawn(
            argv=["/bin/sh", "-c", "exit 0"],
            env={"PATH": "/usr/bin:/bin"},
        )
        assert isinstance(proc, ClaudeProcess)
        await proc.wait_until_exit()
        assert proc.exit_code == 0

    @pytest.mark.asyncio
    async def test_spawn_starts_new_process_group(self):
        # The child must be a session leader (own pgid == own pid).
        proc = await spawn(
            argv=["/bin/sh", "-c", "echo $$; sleep 0.1; exit 0"],
            env={"PATH": "/usr/bin:/bin"},
        )
        assert proc.pid > 0
        assert proc.pgid == proc.pid
        await proc.wait_until_exit()
        assert proc.exit_code == 0

    @pytest.mark.asyncio
    async def test_spawn_propagates_oserror_as_spawn_failed(self):
        with pytest.raises(errors.SubprocessSpawnFailed):
            await spawn(
                argv=["/nonexistent/binary/path/xyz"],
                env={"PATH": "/usr/bin:/bin"},
            )

    @pytest.mark.asyncio
    async def test_exit_code_is_none_while_running(self):
        proc = await spawn(
            argv=["/bin/sh", "-c", "sleep 0.5; exit 7"],
            env={"PATH": "/usr/bin:/bin"},
        )
        assert proc.exit_code is None
        await proc.wait_until_exit()
        assert proc.exit_code == 7


class TestEventsAndStderr:
    @pytest.mark.asyncio
    async def test_events_yields_parsed_stdout_lines(self):
        # Emit two NDJSON events on stdout.
        script = (
            'printf \'{"type":"assistant","data":"hi"}\\n\'; '
            'printf \'{"type":"result","status":"ok"}\\n\''
        )
        proc = await spawn(
            argv=["/bin/sh", "-c", script],
            env={"PATH": "/usr/bin:/bin"},
        )
        events = []
        async for ev in proc.events():
            events.append(ev)
        await proc.wait_until_exit()
        assert [e["type"] for e in events] == ["assistant", "result"]
        assert proc.exit_code == 0

    @pytest.mark.asyncio
    async def test_stderr_digest_captures_stderr(self):
        script = "printf 'something went wrong\\n' >&2"
        proc = await spawn(
            argv=["/bin/sh", "-c", script],
            env={"PATH": "/usr/bin:/bin"},
        )
        # Drain events to allow reader tasks to complete.
        async for _ in proc.events():
            pass
        await proc.wait_until_exit()
        assert "something went wrong" in proc.stderr_digest

    @pytest.mark.asyncio
    async def test_stderr_digest_is_bounded(self):
        # Emit ~50KB on stderr; digest should cap at default 4096 bytes.
        script = "python3 -c \"import sys; sys.stderr.write('x'*50000); sys.stderr.flush()\""
        proc = await spawn(
            argv=["/bin/sh", "-c", script],
            env={"PATH": "/usr/bin:/bin"},
        )
        async for _ in proc.events():
            pass
        await proc.wait_until_exit()
        # Digest must fit in stderr_digest_bytes (default 4096) plus a small
        # truncation marker.
        assert len(proc.stderr_digest) <= 4096 + 64

    @pytest.mark.asyncio
    async def test_events_tolerates_concurrent_stdout_and_stderr(self):
        # Reader tasks must drain BOTH streams; if stderr blocks the OS pipe,
        # stdout would never drain. Emit lots of stderr alongside two events.
        script = (
            'python3 -c "import sys; sys.stderr.write(\\"e\\"*200000); sys.stderr.flush()"; '
            'printf \'{"type":"assistant","data":"hi"}\\n\'; '
            'printf \'{"type":"result"}\\n\''
        )
        proc = await spawn(
            argv=["/bin/sh", "-c", script],
            env={"PATH": "/usr/bin:/bin"},
        )
        events = []
        async for ev in proc.events():
            events.append(ev)
        await proc.wait_until_exit()
        assert any(e["type"] == "result" for e in events)


class TestAsyncContextManager:
    @pytest.mark.asyncio
    async def test_aenter_returns_self(self):
        proc = await spawn(
            argv=["/bin/sh", "-c", "exit 0"],
            env={"PATH": "/usr/bin:/bin"},
        )
        async with proc as p:
            assert p is proc
        assert proc.exit_code == 0

    @pytest.mark.asyncio
    async def test_aexit_drains_and_reaps_on_normal_completion(self):
        script = (
            'printf \'{"type":"assistant","data":"hi"}\\n\'; '
            'printf \'{"type":"result"}\\n\''
        )
        proc = await spawn(
            argv=["/bin/sh", "-c", script],
            env={"PATH": "/usr/bin:/bin"},
        )
        events = []
        async with proc as p:
            async for ev in p.events():
                events.append(ev)
        # After __aexit__, the process MUST be reaped.
        assert proc.exit_code == 0
        assert any(e["type"] == "result" for e in events)

    @pytest.mark.asyncio
    async def test_aexit_cleans_up_on_exception_in_body(self):
        # User raises inside the context — ClaudeProcess still cleans up.
        proc = await spawn(
            argv=["/bin/sh", "-c", "sleep 5; exit 0"],
            env={"PATH": "/usr/bin:/bin"},
        )
        with pytest.raises(ValueError, match="caller bug"):
            async with proc:
                raise ValueError("caller bug")
        # Cleanup must have terminated the child.
        assert proc.exit_code is not None
        # Reader tasks must have ended (no leaked tasks holding pipes open).
        assert proc._stdout_task is None or proc._stdout_task.done()
        assert proc._stderr_task is None or proc._stderr_task.done()

    @pytest.mark.asyncio
    async def test_aexit_is_idempotent(self):
        proc = await spawn(
            argv=["/bin/sh", "-c", "exit 0"],
            env={"PATH": "/usr/bin:/bin"},
        )
        async with proc:
            pass
        # Calling __aexit__ again must not raise.
        await proc.__aexit__(None, None, None)
        assert proc.exit_code == 0


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_sigterm_kills_cooperative_child(self):
        # Default sh handles SIGTERM by exiting; cancel ends it fast.
        token = CancelToken()
        proc = await spawn(
            argv=["/bin/sh", "-c", "sleep 30"],
            env={"PATH": "/usr/bin:/bin"},
            cancel_token=token,
            cancel_grace_seconds=2.0,
        )
        async with proc:
            await asyncio.sleep(0.05)
            token.cancel()
            await proc.wait_until_exit()
        # SIGTERM exit: returncode is negative SIGTERM (-15) on POSIX.
        assert proc.exit_code is not None
        assert proc.exit_code != 0

    @pytest.mark.asyncio
    async def test_cancel_sigkill_escalation_for_stubborn_child(self):
        # Bash that traps SIGTERM and refuses to die.
        # The grace is set very short so the test runs in < 2s.
        token = CancelToken()
        script = (
            "trap '' TERM; "  # ignore SIGTERM
            "while :; do sleep 0.1; done"
        )
        proc = await spawn(
            argv=["/bin/bash", "-c", script],
            env={"PATH": "/usr/bin:/bin"},
            cancel_token=token,
            cancel_grace_seconds=0.5,
        )
        start = asyncio.get_event_loop().time()
        async with proc:
            await asyncio.sleep(0.1)
            token.cancel()
            await proc.wait_until_exit()
        elapsed = asyncio.get_event_loop().time() - start
        # Must have escalated to SIGKILL within (grace + a margin).
        assert elapsed < 3.0
        # SIGKILL: returncode == -9 on POSIX.
        assert proc.exit_code == -signal.SIGKILL

    @pytest.mark.asyncio
    async def test_multiple_cancels_are_idempotent(self):
        token = CancelToken()
        proc = await spawn(
            argv=["/bin/sh", "-c", "sleep 30"],
            env={"PATH": "/usr/bin:/bin"},
            cancel_token=token,
            cancel_grace_seconds=1.0,
        )
        async with proc:
            token.cancel()
            token.cancel()
            token.cancel()
            await proc.wait_until_exit()
        # Process exited, no exceptions raised by repeated cancels.
        assert proc.exit_code is not None

    @pytest.mark.asyncio
    async def test_kill_process_group_reaps_grandchildren(self):
        # Spawn a bash that spawns a sub-bash holding a pipe. killpg must
        # reap both. We assert by PID-search after the parent exits.
        token = CancelToken()
        script = "(sleep 30) & echo $! ; wait"
        proc = await spawn(
            argv=["/bin/bash", "-c", script],
            env={"PATH": "/usr/bin:/bin"},
            cancel_token=token,
            cancel_grace_seconds=0.5,
        )
        async with proc:
            async for _ in proc.events():
                # First (non-JSON) stdout line is the grandchild pid. Parser
                # tolerates a few non-JSON lines. We break early.
                break
            await asyncio.sleep(0.1)
            token.cancel()
            await proc.wait_until_exit()
        # The grandchild is in the same pgid; killpg(pgid, SIGKILL) reaped it.
        try:
            os.killpg(proc.pgid, 0)
            still_alive = True
        except (ProcessLookupError, PermissionError):
            still_alive = False
        assert not still_alive


class TestRealClaudeIntegration:
    """End-to-end against the real claude binary.

    Gated by ``HERMES_CLAUDE_CLI_INTEGRATION=1`` env var. Consumes Claude Max
    plan tokens (one short prompt; pennies of equivalent API value).
    """

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        os.environ.get("HERMES_CLAUDE_CLI_INTEGRATION") != "1",
        reason="set HERMES_CLAUDE_CLI_INTEGRATION=1 to run",
    )
    async def test_real_claude_basic_stream_invocation(self):
        from agent.claude_cli.probe import (
            check_env_hygiene,
            discover_binary,
        )

        # The conftest hermetic_environment fixture removes CLAUDE_CODE_OAUTH_TOKEN
        # for all tests (it's a credential). For this integration test only,
        # restore it from the backup captured before hermetic filtering started.
        # This mimics the real production scenario where the token is available.
        original_token = getattr(os, "HERMES_CLAUDE_TOKEN_BACKUP", None)
        if original_token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = original_token

        binary = discover_binary()
        env = check_env_hygiene(dict(os.environ), require_token=True)

        proc = await spawn(
            argv=[
                binary,
                "-p",
                "Reply with exactly one word: pong",
                "--output-format", "stream-json",
                "--verbose",
                "--no-session-persistence",
                "--allowedTools", "",
            ],
            env=env,
        )
        events: list[dict[str, Any]] = []
        async with proc:
            # Close stdin since we passed the prompt via argv (matches probe
            # pattern for one-shot turns where no stdin is used).
            if proc._proc.stdin is not None:
                proc._proc.stdin.close()
            async for ev in proc.events():
                events.append(ev)
            await proc.wait_until_exit()

        assert proc.exit_code == 0, (
            f"claude exited {proc.exit_code}; stderr digest: {proc.stderr_digest[:500]}"
        )
        assert any(e.get("type") == "assistant" for e in events), (
            f"no assistant event; got types={[e.get('type') for e in events]}"
        )
        assert any(e.get("type") == "result" for e in events), (
            f"no result event; got types={[e.get('type') for e in events]}"
        )
