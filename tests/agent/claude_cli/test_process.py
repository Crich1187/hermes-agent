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
