"""Unit tests for probe helpers — no real subprocess calls."""

import dataclasses
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.claude_cli import errors, probe


def test_discover_binary_returns_path_when_present():
    """When `claude` is on PATH, discover_binary returns the resolved path."""
    with patch("agent.claude_cli.probe.shutil.which", return_value="/usr/local/bin/claude"):
        assert probe.discover_binary() == "/usr/local/bin/claude"


def test_discover_binary_raises_when_missing():
    """When `claude` is not on PATH, discover_binary raises ClaudeCliUnavailable."""
    with patch("agent.claude_cli.probe.shutil.which", return_value=None):
        with pytest.raises(errors.ClaudeCliUnavailable, match="not found"):
            probe.discover_binary()


def test_discover_binary_honors_explicit_path():
    """When an explicit path is given and exists, no PATH lookup is performed."""
    with patch("agent.claude_cli.probe.os.path.isfile", return_value=True), patch(
        "agent.claude_cli.probe.os.access", return_value=True
    ):
        assert probe.discover_binary("/opt/custom/claude") == "/opt/custom/claude"


def test_discover_binary_explicit_path_missing_raises():
    """Explicit path that doesn't exist raises ClaudeCliUnavailable."""
    with patch("agent.claude_cli.probe.os.path.isfile", return_value=False):
        with pytest.raises(errors.ClaudeCliUnavailable, match="/opt/custom/claude"):
            probe.discover_binary("/opt/custom/claude")


def test_parse_version_string_extracts_semver():
    """parse_version_string extracts the numeric version from `claude --version` output."""
    assert probe.parse_version_string("2.1.143 (Claude Code)\n") == (2, 1, 143)
    assert probe.parse_version_string("2.1.143") == (2, 1, 143)
    assert probe.parse_version_string("v2.1.143 (Claude Code)") == (2, 1, 143)


def test_parse_version_string_unparseable_raises():
    """parse_version_string raises on output it can't parse."""
    with pytest.raises(errors.ClaudeCliIncompatible, match="unexpected"):
        probe.parse_version_string("hello world\n")


def test_check_version_passes_when_at_or_above_floor():
    """check_version returns the parsed version tuple when at or above the floor."""
    assert probe.check_version((2, 1, 143), min_version=(2, 1, 143)) == (2, 1, 143)
    assert probe.check_version((2, 2, 0), min_version=(2, 1, 143)) == (2, 2, 0)


def test_check_version_raises_when_below_floor():
    """check_version raises ClaudeCliVersionTooOld when below the floor."""
    with pytest.raises(errors.ClaudeCliVersionTooOld, match="2.1.142.+2.1.143"):
        probe.check_version((2, 1, 142), min_version=(2, 1, 143))


def test_check_env_hygiene_strips_anthropic_api_key():
    """ANTHROPIC_API_KEY is removed from the returned env to prevent billing-path leak."""
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-xxx",
        "ANTHROPIC_API_KEY": "sk-ant-api03-yyy",
        "HOME": "/root",
    }
    sanitized = probe.check_env_hygiene(env)
    assert "ANTHROPIC_API_KEY" not in sanitized
    assert sanitized["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-xxx"
    assert sanitized["HOME"] == "/root"


def test_check_env_hygiene_requires_oauth_token_by_default():
    """Missing CLAUDE_CODE_OAUTH_TOKEN raises ClaudeCliAuthMissing."""
    with pytest.raises(errors.ClaudeCliAuthMissing, match="CLAUDE_CODE_OAUTH_TOKEN"):
        probe.check_env_hygiene({"HOME": "/root"})


def test_check_env_hygiene_token_required_false_skips_token_check():
    """When require_token=False, missing token is tolerated (for partial probes)."""
    sanitized = probe.check_env_hygiene({"HOME": "/root"}, require_token=False)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in sanitized


def test_check_env_hygiene_empty_token_raises():
    """A present-but-empty CLAUDE_CODE_OAUTH_TOKEN is treated as missing."""
    with pytest.raises(errors.ClaudeCliAuthMissing):
        probe.check_env_hygiene({"CLAUDE_CODE_OAUTH_TOKEN": ""})


def test_check_env_hygiene_strips_additional_vars_from_config():
    """Configured strip_env names are removed from the returned env."""
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-xxx",
        "FOO": "bar",
        "BAZ": "qux",
    }
    sanitized = probe.check_env_hygiene(env, strip_env=["FOO", "BAZ"])
    assert "FOO" not in sanitized
    assert "BAZ" not in sanitized


def test_cache_key_is_stable_for_same_inputs():
    """Same inputs always produce the same cache key."""
    inputs = probe.CacheKeyInputs(
        binary_hash="abc123",
        version=(2, 1, 143),
        provider_config_hash="def456",
        settings_schema_version="1.0",
        mcp_config_hash="ghi789",
        env_hygiene_state="ANTHROPIC_API_KEY=stripped,CLAUDE_CODE_OAUTH_TOKEN=set",
        adapter_code_version="0.1.0",
    )
    assert probe.cache_key(inputs) == probe.cache_key(inputs)


def test_cache_key_changes_when_binary_changes():
    """A different binary hash produces a different cache key."""
    inputs_a = probe.CacheKeyInputs(
        binary_hash="abc123",
        version=(2, 1, 143),
        provider_config_hash="x",
        settings_schema_version="1.0",
        mcp_config_hash="y",
        env_hygiene_state="z",
        adapter_code_version="0.1.0",
    )
    inputs_b = dataclasses.replace(inputs_a, binary_hash="different")
    assert probe.cache_key(inputs_a) != probe.cache_key(inputs_b)


def test_cache_key_changes_when_adapter_code_version_changes():
    """A different adapter code version produces a different cache key.

    Ensures that probe results from a previous adapter version don't satisfy
    the current adapter, which may rely on a different set of assertions.
    """
    inputs_a = probe.CacheKeyInputs(
        binary_hash="abc123",
        version=(2, 1, 143),
        provider_config_hash="x",
        settings_schema_version="1.0",
        mcp_config_hash="y",
        env_hygiene_state="z",
        adapter_code_version="0.1.0",
    )
    inputs_b = dataclasses.replace(inputs_a, adapter_code_version="0.2.0")
    assert probe.cache_key(inputs_a) != probe.cache_key(inputs_b)


def test_cache_save_and_load_roundtrip(tmp_path):
    """A saved probe result reloads from disk with identical fields."""
    cache_path = tmp_path / "probe.json"
    result = probe.ProbeResult(
        cache_key="testkey",
        binary_path="/usr/local/bin/claude",
        version=(2, 1, 143),
        timestamp=1234567890.0,
        ok=True,
        assertions={"prompt_via_stdin": "ok", "resume_continuity": "ok"},
        error=None,
    )
    probe.save_cache(result, cache_path)
    loaded = probe.load_cache(cache_path)
    assert loaded == result


def test_cache_load_returns_none_when_missing(tmp_path):
    """load_cache returns None when the cache file does not exist."""
    assert probe.load_cache(tmp_path / "nonexistent.json") is None


def test_cache_load_returns_none_when_malformed(tmp_path):
    """load_cache returns None (not raises) when the cache file is corrupt."""
    cache_path = tmp_path / "probe.json"
    cache_path.write_text("not json at all")
    assert probe.load_cache(cache_path) is None


def test_compute_binary_hash_matches_sha256(tmp_path):
    """compute_binary_hash returns the SHA-256 of the file bytes."""
    binary = tmp_path / "fake-claude"
    binary.write_bytes(b"fake content")
    expected = hashlib.sha256(b"fake content").hexdigest()
    assert probe.compute_binary_hash(str(binary)) == expected
