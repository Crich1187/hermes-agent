"""Tests for the top-level probe runner orchestration."""

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from agent.claude_cli import probe, errors


@pytest.mark.asyncio
async def test_run_probe_returns_ok_when_all_assertions_pass(tmp_path, monkeypatch):
    """run_probe stitches discover_binary, version check, env hygiene,
    and assertion runs into a ProbeResult with ok=True when everything passes.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    fake_binary = tmp_path / "claude"
    fake_binary.write_bytes(b"fake binary content")
    fake_binary.chmod(0o755)

    with patch.object(probe, "discover_binary", return_value=str(fake_binary)), \
         patch.object(probe, "_get_version_from_binary", new_callable=AsyncMock) as mock_v, \
         patch.object(probe, "_run_basic_invocation_assertion", new_callable=AsyncMock) as mock_b:
        mock_v.return_value = (2, 1, 143)
        mock_b.return_value = {"basic_invocation": "ok", "stdin_prompt_transport": "ok"}
        config = probe.ProbeConfig(
            min_version=(2, 1, 143),
            cache_path=tmp_path / "cache.json",
            adapter_code_version="0.1.0",
        )
        result = await probe.run_probe(config)
    assert result.ok is True
    assert result.binary_path == str(fake_binary)
    assert result.version == (2, 1, 143)
    assert "basic_invocation" in result.assertions


@pytest.mark.asyncio
async def test_run_probe_returns_error_when_binary_missing(tmp_path, monkeypatch):
    """run_probe returns ok=False with the error message when claude is missing."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    with patch.object(
        probe, "discover_binary",
        side_effect=errors.ClaudeCliUnavailable("not found"),
    ):
        config = probe.ProbeConfig(
            min_version=(2, 1, 143),
            cache_path=tmp_path / "cache.json",
            adapter_code_version="0.1.0",
        )
        result = await probe.run_probe(config)
    assert result.ok is False
    assert "not found" in (result.error or "")


@pytest.mark.asyncio
async def test_run_probe_uses_cache_when_inputs_unchanged(tmp_path, monkeypatch):
    """A second run_probe call with identical inputs reuses the cached result."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    fake_binary = tmp_path / "claude"
    fake_binary.write_bytes(b"fake")
    fake_binary.chmod(0o755)

    call_count = {"value": 0}

    async def fake_basic(*a, **kw):
        call_count["value"] += 1
        return {"basic_invocation": "ok"}

    with patch.object(probe, "discover_binary", return_value=str(fake_binary)), \
         patch.object(probe, "_get_version_from_binary", new_callable=AsyncMock) as mock_v, \
         patch.object(probe, "_run_basic_invocation_assertion", side_effect=fake_basic):
        mock_v.return_value = (2, 1, 143)
        config = probe.ProbeConfig(
            min_version=(2, 1, 143),
            cache_path=tmp_path / "cache.json",
            adapter_code_version="0.1.0",
        )
        await probe.run_probe(config)
        await probe.run_probe(config)
    assert call_count["value"] == 1, (
        "second run_probe call should reuse cache, not re-run assertions"
    )
