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
