"""Unit + gated integration tests for ClaudeCliAdapter (PR 4)."""

from __future__ import annotations

import pytest


class TestImports:
    def test_adapter_module_importable(self) -> None:
        from agent.claude_cli import adapter  # noqa: F401

    def test_provider_config_importable(self) -> None:
        from agent.claude_cli.adapter import ProviderConfig  # noqa: F401

    def test_message_importable(self) -> None:
        from agent.claude_cli.adapter import Message  # noqa: F401

    def test_claude_cli_adapter_importable(self) -> None:
        from agent.claude_cli.adapter import ClaudeCliAdapter  # noqa: F401
