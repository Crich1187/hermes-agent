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
