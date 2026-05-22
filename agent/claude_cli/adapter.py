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
