"""``--mcp-config`` JSON file generator for the claude_cli adapter.

PR 3 of the Hermes Claude Code CLI adapter. v1 default is an empty MCP
allowlist (no servers exposed to the ``claude`` subprocess). Deployments
opt in per-server via ``providers.<name>.mcp_servers_allowlist`` in the
Hermes provider config; that list is passed to ``generate_mcp_config()``
verbatim and rendered as ``{"mcpServers": {<name>: <server-config>}}``.

No subprocess, settings, or session-id logic lives here.
"""

from __future__ import annotations
