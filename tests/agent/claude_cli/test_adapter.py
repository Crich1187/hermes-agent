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


class TestProviderConfigDefaults:
    def test_defaults_match_spec(self) -> None:
        from agent.claude_cli.adapter import ProviderConfig

        config = ProviderConfig()

        assert config.binary == "claude"
        assert config.min_version == (2, 1, 143)
        assert config.primary_concurrency == 8
        assert config.aux_concurrency == 4
        assert config.turn_idle_timeout_seconds == 120.0
        assert config.aux_call_timeout_seconds == 60.0
        assert config.cancel_grace_seconds == 5.0
        assert config.shutdown_grace_seconds == 30.0
        assert config.session_busy_policy == "queue"
        assert config.mcp_servers_allowlist == []
        assert config.allowed_tools == []
        assert config.disallowed_tools == [
            "Bash", "Write", "Edit", "WebFetch", "WebSearch",
        ]
        assert config.strip_env == ["ANTHROPIC_API_KEY"]
        assert config.cross_provider_fallback is False
        assert config.probe_cache_seconds == 86400.0
        assert config.workspace_dir == ""
        assert config.session_ttl_seconds == 86400.0

    def test_default_lists_are_independent_per_instance(self) -> None:
        # Regression guard against mutable default sharing.
        from agent.claude_cli.adapter import ProviderConfig

        a = ProviderConfig()
        b = ProviderConfig()
        a.allowed_tools.append("Read")
        a.disallowed_tools.append("Sentinel")
        a.mcp_servers_allowlist.append({"name": "x"})
        a.strip_env.append("FOO")

        assert b.allowed_tools == []
        assert "Sentinel" not in b.disallowed_tools
        assert b.mcp_servers_allowlist == []
        assert "FOO" not in b.strip_env

    def test_overrides_take_effect(self) -> None:
        from agent.claude_cli.adapter import ProviderConfig

        config = ProviderConfig(
            primary_concurrency=2,
            aux_concurrency=1,
            session_busy_policy="reject",
            workspace_dir="/tmp/hermes-workspace",
        )

        assert config.primary_concurrency == 2
        assert config.aux_concurrency == 1
        assert config.session_busy_policy == "reject"
        assert config.workspace_dir == "/tmp/hermes-workspace"


class TestMessageTypedDict:
    def test_message_accepts_role_and_content(self) -> None:
        from agent.claude_cli.adapter import Message

        msg: Message = {"role": "user", "content": "hello"}
        assert msg["role"] == "user"
        assert msg["content"] == "hello"
