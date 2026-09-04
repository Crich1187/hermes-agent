#!/usr/bin/env python3
"""Sandbox MCP stdio server for root-usnyx trust/readOnlyHint reproduction."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("usnyx-trustprobe")


@mcp.tool(
    name="probe_read",
    description="Read-only probe",
    annotations=ToolAnnotations(readOnlyHint=True),
)
def probe_read() -> str:
    return "read-ok"


@mcp.tool(
    name="probe_write",
    description="Write probe (default / no readOnlyHint)",
)
def probe_write(value: str = "x") -> str:
    return f"wrote:{value}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
