"""Smoke test for the claude_cli package skeleton and error hierarchy."""

import pytest

from agent.claude_cli import errors


def test_error_hierarchy():
    """All claude_cli exceptions inherit from ClaudeCliError."""
    assert issubclass(errors.ClaudeCliUnavailable, errors.ClaudeCliError)
    assert issubclass(errors.ClaudeCliVersionTooOld, errors.ClaudeCliError)
    assert issubclass(errors.ClaudeCliAuthMissing, errors.ClaudeCliError)
    assert issubclass(errors.ClaudeCliIncompatible, errors.ClaudeCliError)
    assert issubclass(errors.HermesDirectAnthropicEgressDetected, errors.ClaudeCliError)
    assert issubclass(errors.ProtocolError, errors.ClaudeCliError)
    assert issubclass(errors.PromptTooLarge, errors.ClaudeCliError)


def test_error_message_attribute():
    """All errors accept a str message and preserve it."""
    e = errors.ClaudeCliVersionTooOld("found 2.0.0, need 2.1.143")
    assert str(e) == "found 2.0.0, need 2.1.143"


class TestPr4ErrorClasses:
    def test_claude_cli_exited_carries_exit_code_and_stderr_digest(self) -> None:
        from agent.claude_cli.errors import ClaudeCliError, ClaudeCliExited

        exc = ClaudeCliExited(exit_code=137, stderr_digest="bad things\n")
        assert isinstance(exc, ClaudeCliError)
        assert exc.exit_code == 137
        assert exc.stderr_digest == "bad things\n"
        assert "137" in str(exc)

    def test_claude_cli_hung_is_claude_cli_error(self) -> None:
        from agent.claude_cli.errors import ClaudeCliError, ClaudeCliHung

        exc = ClaudeCliHung("no event for 120.0s")
        assert isinstance(exc, ClaudeCliError)
        assert "120" in str(exc)

    def test_claude_cli_aux_timeout_is_claude_cli_error(self) -> None:
        from agent.claude_cli.errors import ClaudeCliAuxTimeout, ClaudeCliError

        exc = ClaudeCliAuxTimeout("aux call exceeded 60.0s")
        assert isinstance(exc, ClaudeCliError)

    def test_session_busy_error_is_claude_cli_error(self) -> None:
        from agent.claude_cli.errors import ClaudeCliError, SessionBusyError

        exc = SessionBusyError("session abc is busy")
        assert isinstance(exc, ClaudeCliError)

    def test_pr4_error_classes_reexported_from_package(self) -> None:
        from agent.claude_cli import (
            ClaudeCliAuxTimeout,
            ClaudeCliExited,
            ClaudeCliHung,
            SessionBusyError,
        )

        # Construction smoke test.
        _ = ClaudeCliExited(exit_code=1, stderr_digest="")
        _ = ClaudeCliHung("hung")
        _ = ClaudeCliAuxTimeout("timed out")
        _ = SessionBusyError("busy")
