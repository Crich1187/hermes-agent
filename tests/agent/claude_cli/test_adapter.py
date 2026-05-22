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


import asyncio
from pathlib import Path

# We'll monkeypatch agent.claude_cli.adapter.run_probe; create a tiny stand-in.
from agent.claude_cli.probe import ProbeResult


def _ok_probe_result(binary_path: str = "/usr/local/bin/claude") -> ProbeResult:
    # Matches ProbeResult's real fields from PR 1.
    return ProbeResult(
        cache_key="fake-cache-key",
        binary_path=binary_path,
        version=(2, 1, 143),
        timestamp=0.0,
        ok=True,
        assertions={"basic_invocation": "ok"},
        error=None,
    )


def _failed_probe_result(error: str) -> ProbeResult:
    return ProbeResult(
        cache_key="",
        binary_path="",
        version=(0, 0, 0),
        timestamp=0.0,
        ok=False,
        assertions={},
        error=error,
    )


class TestAdapterConstruction:
    def test_construct_stores_config_and_env(self) -> None:
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig

        config = ProviderConfig(primary_concurrency=2, aux_concurrency=1)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "PATH": "/usr/bin"}
        adapter = ClaudeCliAdapter(config, env)

        # Spot-check that config and env round-trip.
        assert adapter.config is config
        assert adapter.config.primary_concurrency == 2
        # We don't assert env identity (the adapter may copy).
        assert "CLAUDE_CODE_OAUTH_TOKEN" in adapter._env

    def test_construct_does_not_perform_io(self, tmp_path: Path) -> None:
        # No subprocess, no file creation, no probe call at __init__.
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig

        config = ProviderConfig(
            probe_cache_path=tmp_path / "probe.json",
            workspace_dir=str(tmp_path),
        )
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        adapter = ClaudeCliAdapter(config, env)

        # Probe cache file must NOT exist yet (only init() can create it).
        assert not (tmp_path / "probe.json").exists()
        # _session_settings_dir is lazy.
        assert adapter._session_settings_dir is None
        assert adapter._closed is False


class TestAdapterInit:
    @pytest.mark.asyncio
    async def test_init_ok_strips_anthropic_api_key_and_stores_spawn_env(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig

        # Stub run_probe.
        async def fake_run_probe(_config):
            return _ok_probe_result()

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)

        env = {
            "CLAUDE_CODE_OAUTH_TOKEN": "real-token",
            "ANTHROPIC_API_KEY": "sk-must-be-scrubbed",
            "PATH": "/usr/bin",
        }
        caplog.set_level("WARNING")
        adapter = ClaudeCliAdapter(ProviderConfig(), env)
        await adapter.init()

        assert "ANTHROPIC_API_KEY" not in adapter._spawn_env
        assert adapter._spawn_env["CLAUDE_CODE_OAUTH_TOKEN"] == "real-token"
        # A warning is logged because ANTHROPIC_API_KEY was present.
        assert any(
            "ANTHROPIC_API_KEY" in record.getMessage() for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_init_missing_oauth_token_raises_auth_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig
        from agent.claude_cli.errors import ClaudeCliAuthMissing

        async def fake_run_probe(_config):
            return _ok_probe_result()

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)

        adapter = ClaudeCliAdapter(ProviderConfig(), env={})
        with pytest.raises(ClaudeCliAuthMissing):
            await adapter.init()

    @pytest.mark.asyncio
    async def test_init_probe_unavailable_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig
        from agent.claude_cli.errors import ClaudeCliUnavailable

        async def fake_run_probe(_config):
            return _failed_probe_result("ClaudeCliUnavailable: not on PATH")

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)

        adapter = ClaudeCliAdapter(
            ProviderConfig(), env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        )
        with pytest.raises(ClaudeCliUnavailable):
            await adapter.init()

    @pytest.mark.asyncio
    async def test_init_probe_version_too_old_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig
        from agent.claude_cli.errors import ClaudeCliVersionTooOld

        async def fake_run_probe(_config):
            return _failed_probe_result(
                "ClaudeCliVersionTooOld: installed claude is 2.0.0"
            )

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)

        adapter = ClaudeCliAdapter(
            ProviderConfig(), env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        )
        with pytest.raises(ClaudeCliVersionTooOld):
            await adapter.init()

    @pytest.mark.asyncio
    async def test_init_probe_egress_detected_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig
        from agent.claude_cli.errors import HermesDirectAnthropicEgressDetected

        async def fake_run_probe(_config):
            return _failed_probe_result(
                "HermesDirectAnthropicEgressDetected: leaked HTTPS"
            )

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)

        adapter = ClaudeCliAdapter(
            ProviderConfig(), env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        )
        with pytest.raises(HermesDirectAnthropicEgressDetected):
            await adapter.init()

    @pytest.mark.asyncio
    async def test_init_probe_incompatible_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig
        from agent.claude_cli.errors import ClaudeCliIncompatible

        async def fake_run_probe(_config):
            return _failed_probe_result("ClaudeCliIncompatible: no result event")

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)

        adapter = ClaudeCliAdapter(
            ProviderConfig(), env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        )
        with pytest.raises(ClaudeCliIncompatible):
            await adapter.init()

    @pytest.mark.asyncio
    async def test_init_unknown_probe_error_raises_claude_cli_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig
        from agent.claude_cli.errors import ClaudeCliError

        async def fake_run_probe(_config):
            return _failed_probe_result("UnknownErrorClass: weird")

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)

        adapter = ClaudeCliAdapter(
            ProviderConfig(), env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        )
        with pytest.raises(ClaudeCliError):
            await adapter.init()


def _shell_quote(s: str) -> str:
    """Single-quote a string safely for /bin/sh -c."""
    return "'" + s.replace("'", "'\\''") + "'"


def _fake_claude_argv_one_event(session_id: str, content: str = "hello") -> list[str]:
    """Build a /bin/sh argv that emits one canned stream-json conversation."""
    payload = (
        f'{{"type":"system","subtype":"init","session_id":"{session_id}"}}'
        f"\n"
        f'{{"type":"assistant","message":{{"role":"assistant",'
        f'"content":[{{"type":"text","text":"{content}"}}]}},'
        f'"session_id":"{session_id}"}}'
        f"\n"
        f'{{"type":"result","subtype":"success","session_id":"{session_id}"}}'
        f"\n"
    )
    # Drain stdin (so adapter's writer succeeds) then emit JSON, then exit.
    script = f"cat >/dev/null; printf '%s' {_shell_quote(payload)}"
    return ["/bin/sh", "-c", script]


class TestPrimaryTurnHappyPath:
    @pytest.mark.asyncio
    async def test_first_turn_streams_events_and_sets_session_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig
        from agent.claude_cli.process import CancelToken

        async def fake_run_probe(_config):
            return _ok_probe_result()

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)

        # Override the binary at argv-build time.
        captured_argv: list[list[str]] = []
        real_spawn = adapter_mod.spawn

        async def intercepting_spawn(argv, env, cwd=None, **kw):
            captured_argv.append(list(argv))
            new_argv = _fake_claude_argv_one_event(
                session_id="claude-session-abc"
            )
            return await real_spawn(new_argv, env=env, cwd=cwd, **kw)

        monkeypatch.setattr(adapter_mod, "spawn", intercepting_spawn)

        adapter = ClaudeCliAdapter(
            ProviderConfig(),
            env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"},
        )
        await adapter.init()

        events: list[dict] = []
        token = CancelToken()
        async for event in adapter.primary_turn(
            hermes_session_id="hermes-1",
            messages=[{"role": "user", "content": "hi"}],
            model="claude-opus-4-6",
            cancel_token=token,
        ):
            events.append(event)

        # Stream included system + assistant + result.
        event_types = [e.get("type") for e in events]
        assert "system" in event_types
        assert "assistant" in event_types
        assert "result" in event_types

        # Session was learned and stored.
        stored, is_new = adapter._sessions.get_or_create("hermes-1")
        assert stored == "claude-session-abc"
        assert is_new is False

        # First-turn argv had --settings, --mcp-config, --strict-mcp-config,
        # --output-format stream-json, --verbose, --model — but NOT --resume.
        assert len(captured_argv) == 1
        argv = captured_argv[0]
        assert "--output-format" in argv and "stream-json" in argv
        assert "--verbose" in argv
        assert "--settings" in argv
        assert "--mcp-config" in argv
        assert "--strict-mcp-config" in argv
        assert "--model" in argv
        assert "claude-opus-4-6" in argv
        assert "--resume" not in argv

    @pytest.mark.asyncio
    async def test_first_turn_emits_telemetry(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from agent.claude_cli import adapter as adapter_mod
        from agent.claude_cli.adapter import ClaudeCliAdapter, ProviderConfig
        from agent.claude_cli.process import CancelToken

        async def fake_run_probe(_config):
            return _ok_probe_result()

        real_spawn = adapter_mod.spawn

        async def fake_spawn(argv, env, cwd=None, **kw):
            return await real_spawn(
                _fake_claude_argv_one_event(session_id="claude-xyz"),
                env=env,
                cwd=cwd,
                **kw,
            )

        monkeypatch.setattr(adapter_mod, "run_probe", fake_run_probe)
        monkeypatch.setattr(adapter_mod, "spawn", fake_spawn)

        adapter = ClaudeCliAdapter(
            ProviderConfig(), env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        )
        await adapter.init()

        caplog.set_level("INFO")
        async for _ in adapter.primary_turn(
            hermes_session_id="hermes-tele",
            messages=[{"role": "user", "content": "hi"}],
            model="claude-opus-4-6",
            cancel_token=CancelToken(),
        ):
            pass

        messages = [r.getMessage() for r in caplog.records]
        assert any("claude_cli.turn.start" in m for m in messages)
        assert any("claude_cli.turn.end" in m for m in messages)
