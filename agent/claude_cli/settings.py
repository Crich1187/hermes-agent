"""Hermetic ``--settings`` JSON file generator for the claude_cli adapter.

PR 3 of the Hermes Claude Code CLI adapter. Produces a restrictive JSON
settings document plus a writer that drops it onto disk with ``0600`` mode
inside a ``0700`` per-session temp directory. The PR 4 adapter calls
``make_session_settings_dir()`` once per session, then
``write_settings_file(generate_settings(...), parent_dir=...)`` per turn
(or once and reused, at the adapter's discretion).

No subprocess, mcp, or session-id logic lives here.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Optional


def generate_settings(
    *,
    workspace_dir: str,
    allowed_tools: Optional[list[str]] = None,
    disallowed_tools: Optional[list[str]] = None,
    trusted_folders: Optional[list[str]] = None,
) -> dict:
    """Build the hermetic ``--settings`` JSON document.

    Args:
        workspace_dir: Absolute path to the Hermes-session workspace; included
            as the first entry of ``trustedFolders``. Required, non-empty.
        allowed_tools: Tool allowlist passed to ``permissions.tools.allowed``.
            Defaults to ``[]`` (no tools enabled). The default-deny ``["*"]``
            in ``permissions.tools.denied`` always remains unless
            ``disallowed_tools`` is explicitly overridden.
        disallowed_tools: Tool denylist passed to ``permissions.tools.denied``.
            Defaults to ``["*"]`` (deny-all). Set to a concrete list to use
            an explicit denylist instead of the catch-all.
        trusted_folders: Additional trusted folders appended after
            ``workspace_dir``. Defaults to no additions.

    Returns:
        A fresh dict per call (no shared state). The shape matches the spec
        at design.md lines 221-244.

    Raises:
        ValueError: if ``workspace_dir`` is empty.
    """
    if not workspace_dir:
        raise ValueError("workspace_dir must be a non-empty absolute path")
    folders = [workspace_dir]
    if trusted_folders:
        folders.extend(trusted_folders)
    return {
        "trustedFolders": folders,
        "permissions": {
            "tools": {
                "allowed": list(allowed_tools) if allowed_tools else [],
                "denied": list(disallowed_tools) if disallowed_tools else ["*"],
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


def make_session_settings_dir(prefix: str = "hermes-claude-cli-") -> Path:
    """Create a per-session tempdir with mode ``0700``.

    Args:
        prefix: Directory-name prefix passed to ``tempfile.mkdtemp``.

    Returns:
        Absolute ``Path`` to the new directory. Caller owns cleanup; the
        adapter typically removes it in ``ClaudeCliAdapter.close()``.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    os.chmod(path, 0o700)
    return path


def write_settings_file(settings_dict: dict, *, parent_dir: Path) -> Path:
    """Write ``settings_dict`` to ``parent_dir/settings-<uuid>.json``.

    The file is created with mode ``0600``. ``parent_dir`` must already be
    mode ``0700`` (typically produced by ``make_session_settings_dir``).

    Args:
        settings_dict: The dict to serialise as JSON.
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
    file_path = parent_dir / f"settings-{uuid.uuid4().hex}.json"
    # O_CREAT|O_WRONLY|O_EXCL ensures we never overwrite an existing file,
    # and the 0o600 mode argument lands before any data is written.
    fd = os.open(
        file_path,
        os.O_CREAT | os.O_WRONLY | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings_dict, f, ensure_ascii=False, indent=2)
    except BaseException:
        # Best-effort cleanup on write failure.
        try:
            file_path.unlink()
        except FileNotFoundError:
            pass
        raise
    # Defensive: enforce 0600 in case umask interfered before fdopen wrote.
    os.chmod(file_path, 0o600)
    return file_path
