"""ClaudeCliAdapter — glues PRs 1–3 into one provider unit.

PR 4 of the Hermes Claude Code CLI adapter. The adapter is a thin
orchestration layer:

  - `__init__(config, env)` stores config + env, constructs the
    SessionStore, the two asyncio.Semaphore concurrency gates, and the
    per-session lock dict.
  - `init()` runs the PR 1 probe, scrubs ANTHROPIC_API_KEY from the spawn
    env (with a warning), validates CLAUDE_CODE_OAUTH_TOKEN presence,
    and refuses to start on any probe failure.
  - `primary_turn()` acquires the per-Hermes-session lock + the primary
    semaphore, writes the PR 3 settings + mcp-config files into a
    per-session tempdir, spawns the subprocess via PR 2, streams events,
    captures the claude_session_id on the first event, drives a
    per-turn idle watchdog, and yields events to the caller.
  - `aux_call()` acquires only the aux semaphore (no session resumption,
    no per-session lock), spawns a single-shot subprocess with
    `--no-session-persistence`, concatenates assistant text, and honors
    the deadline.
  - `close()` flips the closed flag, drains in-flight turns up to
    `shutdown_grace_seconds`, escalates to cancel + SIGKILL, and removes
    the per-session settings tempdir.

PR 5 wires this adapter into Hermes' provider runtime as
`claude_code_cli`; until then the adapter is constructable and unit-
testable but not reachable via `model.provider`.
"""

from __future__ import annotations


from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict


@dataclass
class ProviderConfig:
    """Runtime config for ClaudeCliAdapter (mirrors the YAML shape).

    Owned locally by the claude_cli package in PR 4. PR 5 wires Hermes'
    YAML reader to instantiate this; the provider registry receives the
    populated dataclass and constructs the adapter.
    """

    binary: str = "claude"
    min_version: tuple[int, int, int] = (2, 1, 143)
    primary_concurrency: int = 8
    aux_concurrency: int = 4
    turn_idle_timeout_seconds: float = 120.0
    aux_call_timeout_seconds: float = 60.0
    cancel_grace_seconds: float = 5.0
    shutdown_grace_seconds: float = 30.0
    session_busy_policy: str = "queue"  # "queue" | "reject"
    mcp_servers_allowlist: list[dict] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(
        default_factory=lambda: ["Bash", "Write", "Edit", "WebFetch", "WebSearch"]
    )
    strip_env: list[str] = field(default_factory=lambda: ["ANTHROPIC_API_KEY"])
    cross_provider_fallback: bool = False
    probe_cache_seconds: float = 86400.0
    probe_cache_path: Path = field(
        default_factory=lambda: Path.home() / ".hermes/cache/claude_cli_probe.json"
    )
    workspace_dir: str = ""
    session_ttl_seconds: float = 86400.0
    adapter_code_version: str = "0.4.0"


class Message(TypedDict):
    role: Literal["user", "assistant", "system"]
    content: str
