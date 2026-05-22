"""Unit tests for agent.claude_cli.mcp_config."""

from __future__ import annotations


def test_module_is_importable():
    from agent.claude_cli import mcp_config  # noqa: F401


import json
import os
import stat

import pytest


class TestGenerateMcpConfig:
    def test_none_allowlist_yields_empty_mcpservers(self):
        from agent.claude_cli.mcp_config import generate_mcp_config

        assert generate_mcp_config(None) == {"mcpServers": {}}

    def test_empty_list_yields_empty_mcpservers(self):
        from agent.claude_cli.mcp_config import generate_mcp_config

        assert generate_mcp_config([]) == {"mcpServers": {}}

    def test_allowlist_entries_keyed_by_name(self):
        from agent.claude_cli.mcp_config import generate_mcp_config

        allowlist = [
            {
                "name": "filesystem",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            },
            {
                "name": "github",
                "command": "docker",
                "args": ["run", "-i", "ghcr.io/example/mcp-github"],
                "env": {"GITHUB_TOKEN": "redacted"},
            },
        ]
        result = generate_mcp_config(allowlist)
        assert set(result["mcpServers"].keys()) == {"filesystem", "github"}
        assert result["mcpServers"]["filesystem"] == {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        }
        assert result["mcpServers"]["github"] == {
            "command": "docker",
            "args": ["run", "-i", "ghcr.io/example/mcp-github"],
            "env": {"GITHUB_TOKEN": "redacted"},
        }

    def test_missing_name_raises(self):
        from agent.claude_cli.mcp_config import generate_mcp_config

        with pytest.raises(ValueError, match="name"):
            generate_mcp_config([{"command": "npx", "args": []}])

    def test_duplicate_names_raise(self):
        from agent.claude_cli.mcp_config import generate_mcp_config

        with pytest.raises(ValueError, match="duplicate"):
            generate_mcp_config([
                {"name": "x", "command": "a"},
                {"name": "x", "command": "b"},
            ])

    def test_result_is_a_fresh_dict_per_call(self):
        from agent.claude_cli.mcp_config import generate_mcp_config

        a = generate_mcp_config(None)
        b = generate_mcp_config(None)
        a["mcpServers"]["sentinel"] = {"command": "x"}
        assert b["mcpServers"] == {}


class TestWriteMcpConfigFile:
    def test_writes_json_with_0600_mode(self, tmp_path):
        from agent.claude_cli.mcp_config import (
            generate_mcp_config,
            write_mcp_config_file,
        )

        os.chmod(tmp_path, 0o700)
        config = generate_mcp_config(None)
        path = write_mcp_config_file(config, parent_dir=tmp_path)
        assert path.exists()
        assert path.parent == tmp_path
        assert path.name.startswith("mcp-config-") and path.name.endswith(".json")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
        with open(path) as f:
            assert json.load(f) == {"mcpServers": {}}

    def test_rejects_parent_dir_with_loose_mode(self, tmp_path):
        from agent.claude_cli.mcp_config import (
            generate_mcp_config,
            write_mcp_config_file,
        )

        os.chmod(tmp_path, 0o755)
        config = generate_mcp_config(None)
        with pytest.raises(PermissionError, match="0700"):
            write_mcp_config_file(config, parent_dir=tmp_path)

    def test_unique_filename_per_call(self, tmp_path):
        from agent.claude_cli.mcp_config import (
            generate_mcp_config,
            write_mcp_config_file,
        )

        os.chmod(tmp_path, 0o700)
        config = generate_mcp_config(None)
        a = write_mcp_config_file(config, parent_dir=tmp_path)
        b = write_mcp_config_file(config, parent_dir=tmp_path)
        assert a != b
        assert a.exists() and b.exists()
