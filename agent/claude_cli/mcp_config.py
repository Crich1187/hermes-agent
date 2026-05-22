"""``--mcp-config`` JSON file generator for the claude_cli adapter.

PR 3 of the Hermes Claude Code CLI adapter. v1 default is an empty MCP
allowlist (no servers exposed to the ``claude`` subprocess). Deployments
opt in per-server via ``providers.<name>.mcp_servers_allowlist`` in the
Hermes provider config; that list is passed to ``generate_mcp_config()``
verbatim and rendered as ``{"mcpServers": {<name>: <server-config>}}``.

No subprocess, settings, or session-id logic lives here.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Optional


def generate_mcp_config(allowlist: Optional[list[dict]]) -> dict:
    """Build the ``--mcp-config`` JSON document.

    Args:
        allowlist: List of MCP server entries from
            ``providers.<name>.mcp_servers_allowlist``. Each entry must have
            a ``name`` key; remaining keys form the server config that
            ``claude`` consumes. ``None`` or an empty list yields an empty
            ``mcpServers`` map (no MCP servers exposed to the subprocess) —
            the v1 default per the design spec lines 246-248.

    Returns:
        A fresh dict per call of shape ``{"mcpServers": {<name>: <server>}}``.

    Raises:
        ValueError: if an entry lacks a ``name`` key or if duplicate names
            appear in the list.
    """
    servers: dict[str, dict] = {}
    if not allowlist:
        return {"mcpServers": servers}
    for entry in allowlist:
        if "name" not in entry:
            raise ValueError(
                f"mcp allowlist entry missing required 'name' key: {entry!r}"
            )
        name = entry["name"]
        if name in servers:
            raise ValueError(
                f"duplicate mcp allowlist entry name: {name!r}"
            )
        servers[name] = {k: v for k, v in entry.items() if k != "name"}
    return {"mcpServers": servers}


def write_mcp_config_file(config_dict: dict, *, parent_dir: Path) -> Path:
    """Write ``config_dict`` to ``parent_dir/mcp-config-<uuid>.json``.

    Same filesystem semantics as ``settings.write_settings_file``:
    ``parent_dir`` must be mode ``0700``, file is created with mode
    ``0600``.

    Args:
        config_dict: The dict to serialise as JSON.
        parent_dir: An existing directory with mode ``0700``.

    Returns:
        Absolute ``Path`` to the new file.

    Raises:
        PermissionError: if ``parent_dir`` is not exactly mode ``0700``.
        FileNotFoundError: if ``parent_dir`` does not exist.
    """
    parent_dir = Path(parent_dir)
    if not parent_dir.exists():
        raise FileNotFoundError(f"parent_dir does not exist: {parent_dir}")
    parent_mode = stat.S_IMODE(parent_dir.stat().st_mode)
    if parent_mode != 0o700:
        raise PermissionError(
            f"parent_dir must be mode 0700, got {oct(parent_mode)}: {parent_dir}"
        )
    file_path = parent_dir / f"mcp-config-{uuid.uuid4().hex}.json"
    fd = os.open(
        file_path,
        os.O_CREAT | os.O_WRONLY | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=2)
    except BaseException:
        try:
            file_path.unlink()
        except FileNotFoundError:
            pass
        raise
    os.chmod(file_path, 0o600)
    return file_path
