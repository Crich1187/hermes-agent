"""Hermes Claude Code CLI subprocess adapter.

This package implements the subprocess transport for routing Anthropic-bound
calls through the official ``claude`` CLI binary instead of direct HTTPS to
``api.anthropic.com``. See ``docs/superpowers/specs/2026-05-16-hermes-claude-
code-cli-adapter-design.md`` for the design and rationale.

PR 1 shipped the exception hierarchy, the stream-json parser, and the
compatibility probe. PR 2 shipped the subprocess primitive (``ClaudeProcess``,
``CancelToken``, ``spawn``). PR 3 (this commit) ships three small generator
modules: ``settings`` (``--settings`` JSON), ``mcp_config`` (``--mcp-config``
JSON), and ``session_store`` (in-memory hermes->claude session id map with
TTL). PR 4 will glue them together into ``ClaudeCliAdapter``.
"""

from agent.claude_cli.errors import (
    ClaudeCliAuthMissing,
    ClaudeCliError,
    ClaudeCliIncompatible,
    ClaudeCliUnavailable,
    ClaudeCliVersionTooOld,
    HermesDirectAnthropicEgressDetected,
    ProtocolError,
    PromptTooLarge,
    SubprocessSpawnFailed,
)
from agent.claude_cli.mcp_config import (
    generate_mcp_config,
    write_mcp_config_file,
)
from agent.claude_cli.process import CancelToken, ClaudeProcess, spawn
from agent.claude_cli.session_store import SessionStore
from agent.claude_cli.settings import (
    generate_settings,
    make_session_settings_dir,
    write_settings_file,
)

__all__ = [
    "CancelToken",
    "ClaudeCliAuthMissing",
    "ClaudeCliError",
    "ClaudeCliIncompatible",
    "ClaudeCliUnavailable",
    "ClaudeCliVersionTooOld",
    "ClaudeProcess",
    "HermesDirectAnthropicEgressDetected",
    "ProtocolError",
    "PromptTooLarge",
    "SessionStore",
    "SubprocessSpawnFailed",
    "generate_mcp_config",
    "generate_settings",
    "make_session_settings_dir",
    "spawn",
    "write_mcp_config_file",
    "write_settings_file",
]
