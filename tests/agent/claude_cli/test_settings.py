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
