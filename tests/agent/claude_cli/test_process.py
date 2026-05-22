"""Unit + integration tests for agent.claude_cli.process."""

from __future__ import annotations

import asyncio
import os
import sys

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
    @pytest.mark.xfail(reason="kill_process_group not yet implemented (Task 5)")
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
