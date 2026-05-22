"""Unit tests for agent.claude_cli.settings."""

from __future__ import annotations


def test_module_is_importable():
    from agent.claude_cli import settings  # noqa: F401


class TestGenerateSettings:
    def test_defaults_match_spec_shape(self):
        from agent.claude_cli.settings import generate_settings

        result = generate_settings(workspace_dir="/tmp/hermes-session-abc")
        assert result == {
            "trustedFolders": ["/tmp/hermes-session-abc"],
            "permissions": {
                "tools": {
                    "allowed": [],
                    "denied": ["*"],
                },
                "mcp": {
                    "trustAllServers": False,
                },
            },
            "hooks": {},
            "skills": {"external_dirs": []},
            "autoUpdater": {"enabled": False},
            "telemetry": {"enabled": False},
        }

    def test_allowed_tools_passthrough(self):
        from agent.claude_cli.settings import generate_settings

        result = generate_settings(
            workspace_dir="/tmp/ws",
            allowed_tools=["Read", "Grep"],
        )
        assert result["permissions"]["tools"]["allowed"] == ["Read", "Grep"]
        # Default-deny remains in place even when allow list is non-empty.
        assert result["permissions"]["tools"]["denied"] == ["*"]

    def test_disallowed_tools_replaces_default_denied(self):
        from agent.claude_cli.settings import generate_settings

        result = generate_settings(
            workspace_dir="/tmp/ws",
            disallowed_tools=["Bash", "Write", "Edit"],
        )
        assert result["permissions"]["tools"]["denied"] == ["Bash", "Write", "Edit"]

    def test_extra_trusted_folders_extend_workspace(self):
        from agent.claude_cli.settings import generate_settings

        result = generate_settings(
            workspace_dir="/tmp/ws",
            trusted_folders=["/srv/shared-readonly"],
        )
        assert result["trustedFolders"] == ["/tmp/ws", "/srv/shared-readonly"]

    def test_workspace_dir_required_non_empty(self):
        from agent.claude_cli.settings import generate_settings
        import pytest

        with pytest.raises(ValueError, match="workspace_dir"):
            generate_settings(workspace_dir="")

    def test_result_is_a_fresh_dict_per_call(self):
        # Callers must be able to mutate without poisoning a cached default.
        from agent.claude_cli.settings import generate_settings

        a = generate_settings(workspace_dir="/tmp/a")
        b = generate_settings(workspace_dir="/tmp/b")
        a["permissions"]["tools"]["allowed"].append("Read")
        assert b["permissions"]["tools"]["allowed"] == []


import json
import os
import stat
from pathlib import Path

import pytest


class TestMakeSessionSettingsDir:
    def test_returns_existing_path_with_0700_mode(self, tmp_path, monkeypatch):
        # Confine TMPDIR so we don't leave stray dirs in /tmp.
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        from agent.claude_cli.settings import make_session_settings_dir

        path = make_session_settings_dir()
        try:
            assert path.exists()
            assert path.is_dir()
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o700, f"expected 0700, got {oct(mode)}"
        finally:
            # Caller owns cleanup per the docstring; do it here for tidy tests.
            for child in path.iterdir():
                child.unlink()
            path.rmdir()

    def test_prefix_is_applied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        from agent.claude_cli.settings import make_session_settings_dir

        path = make_session_settings_dir(prefix="custom-prefix-")
        try:
            assert path.name.startswith("custom-prefix-")
        finally:
            path.rmdir()

    def test_default_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        from agent.claude_cli.settings import make_session_settings_dir

        path = make_session_settings_dir()
        try:
            assert path.name.startswith("hermes-claude-cli-")
        finally:
            path.rmdir()


class TestWriteSettingsFile:
    def test_writes_json_with_0600_mode(self, tmp_path):
        from agent.claude_cli.settings import (
            generate_settings,
            write_settings_file,
        )

        # Caller is responsible for the 0700 parent; emulate that here.
        os.chmod(tmp_path, 0o700)
        settings = generate_settings(workspace_dir="/tmp/ws")
        path = write_settings_file(settings, parent_dir=tmp_path)

        assert path.exists()
        assert path.parent == tmp_path
        assert path.name.startswith("settings-") and path.name.endswith(".json")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
        with open(path) as f:
            loaded = json.load(f)
        assert loaded == settings

    def test_rejects_parent_dir_with_loose_mode(self, tmp_path):
        from agent.claude_cli.settings import (
            generate_settings,
            write_settings_file,
        )

        os.chmod(tmp_path, 0o755)  # group/other-readable
        settings = generate_settings(workspace_dir="/tmp/ws")
        with pytest.raises(PermissionError, match="0700"):
            write_settings_file(settings, parent_dir=tmp_path)

    def test_unique_filename_per_call(self, tmp_path):
        from agent.claude_cli.settings import (
            generate_settings,
            write_settings_file,
        )

        os.chmod(tmp_path, 0o700)
        settings = generate_settings(workspace_dir="/tmp/ws")
        a = write_settings_file(settings, parent_dir=tmp_path)
        b = write_settings_file(settings, parent_dir=tmp_path)
        assert a != b
        assert a.exists() and b.exists()

    def test_json_round_trips_with_unicode(self, tmp_path):
        from agent.claude_cli.settings import (
            generate_settings,
            write_settings_file,
        )

        os.chmod(tmp_path, 0o700)
        settings = generate_settings(
            workspace_dir="/tmp/ws-éñ",
            allowed_tools=["Read"],
        )
        path = write_settings_file(settings, parent_dir=tmp_path)
        with open(path, encoding="utf-8") as f:
            assert json.load(f) == settings
