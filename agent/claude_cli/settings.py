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
