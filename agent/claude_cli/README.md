# `agent/claude_cli/` — Claude Code CLI subprocess adapter

This package implements the subprocess transport for routing Hermes' Anthropic-
bound calls through the official `claude` CLI binary, billing against the
user's Claude Max plan instead of the third-party "extra usage" credit bucket
that direct-HTTPS callers hit.

## Status

**PR 1 of 6 — landed. PR 2 of 6 — landed (process layer). PR 3 of 6 — landed (config + session).** Probe + protocol parser + CLI contract documentation + subprocess lifecycle + hermetic settings, mcp_config, session store.
The adapter itself is not yet wired into Hermes' provider runtime; see
`docs/superpowers/specs/2026-05-16-hermes-claude-code-cli-adapter-design.md`
for the full plan and v1 scope.

> **Scope caveat for `run_probe()`:** in PR 1, `run_probe()` runs only the
> basic-invocation assertion (binary spawns, stream-json parses, stdin
> prompt transport works). The remaining contract assertions documented in
> Appendix A — `--resume`, hermetic settings precedence, `--allowedTools`
> denial, `--strict-mcp-config`, process group cleanup,
> `--no-session-persistence`, model alias acceptance — are covered only by
> the e2e integration tests in `tests/e2e/test_claude_cli_probe.py`. PR 4
> (adapter wiring) will fold these into `run_probe()` so adapter init can
> fail closed on any of them.

Subsequent PRs (not yet landed):

- PR 2: `process.py` — subprocess spawn / drain / kill primitives. **Landed.**
- PR 3: `settings.py`, `mcp_config.py`, `session_store.py`. **Landed.**
- PR 4: `adapter.py` — the provider adapter, registered as `claude_code_cli`.
- PR 5: end-to-end wiring; `model.provider: claude-code-subprocess` becomes selectable.
- PR 6 (optional): cross-provider fallback behavior.

## What ships in PR 1 + PR 2 + PR 3

| Module | Purpose |
|---|---|
| `errors.py` | Exception hierarchy: `ClaudeCliError` base + 7 subclasses. |
| `protocol.py` | `StreamJsonParser` — pure NDJSON parser for `claude --print --output-format stream-json` output. No I/O. |
| `probe.py` | Compatibility probe: binary discovery, version check, env hygiene, cache, `_run_basic_invocation_assertion`, `extract_session_id`, `run_probe`, CLI entry point. |
| `process.py` | `ClaudeProcess` + `CancelToken` + `spawn()`: subprocess lifecycle with concurrent stdout/stderr drain, `start_new_session=True` pgroup isolation, SIGTERM→grace→SIGKILL cancellation, and context-managed cleanup. |
| `settings.py` | `generate_settings(...)` + `write_settings_file(...)` + `make_session_settings_dir(...)`: hermetic ``--settings`` JSON file generator. Restrictive default-deny tool permissions; 0600 file inside 0700 per-session tempdir. |
| `mcp_config.py` | `generate_mcp_config(...)` + `write_mcp_config_file(...)`: ``--mcp-config`` JSON file generator. Empty-by-default allowlist; same 0600/0700 filesystem semantics. |
| `session_store.py` | `SessionStore` class: in-memory ``hermes_session_id -> claude_session_id`` map with TTL eviction (`time.monotonic()` clock, injectable `now`). v1 in-memory only; persistence is a follow-up. |

## Running the probe

The probe runs at adapter init in production (results cached for 24h) and as
a CLI entry point for operators:

```bash
cd /root/.hermes/hermes-agent
./venv/bin/python -m agent.claude_cli.probe [--no-cache] [--binary-path /path/to/claude]
```

Output is a JSON `ProbeResult`. Exit code 0 if `ok=true`, 1 otherwise.

## Running the integration tests

The integration tests against the real `claude` binary are gated by the
`integration` pytest marker. They consume real Anthropic plan tokens
(short prompts, total spend is trivial).

```bash
cd /root/.hermes/hermes-agent
set -a && source /run/infisical/hermes.env && set +a
./venv/bin/python -m pytest tests/e2e/test_claude_cli_probe.py -v -m integration
```

By default, integration tests skip if `CLAUDE_CODE_OAUTH_TOKEN` is not set.

## CLI contract

See Appendix A of the design spec at
`/root/docs/superpowers/specs/2026-05-16-hermes-claude-code-cli-adapter-design.md`
for the empirically-verified CLI contract (prompt transport, session_id schema,
flag behavior, model alias mapping, hermetic-config posture).

## PR 2 usage example

```python
from agent.claude_cli import spawn, CancelToken

token = CancelToken()
proc = await spawn(
    argv=["claude", "-p", "--output-format", "stream-json", "--verbose"],
    env=sanitized_env,
    cancel_token=token,
    cancel_grace_seconds=5.0,
)
async with proc:
    proc._proc.stdin.write(b"hello\n")
    proc._proc.stdin.close()
    async for event in proc.events():
        handle(event)
    await proc.wait_until_exit()

# Cleanup is automatic on __aexit__: stdin closed, reader tasks awaited,
# process group reaped. Calling token.cancel() from another task triggers
# SIGTERM → 5s grace → SIGKILL.
```

## PR 3 usage example

```python
from agent.claude_cli import (
    SessionStore,
    generate_mcp_config,
    generate_settings,
    make_session_settings_dir,
    write_mcp_config_file,
    write_settings_file,
)

# Per Hermes session, build a tempdir and drop the two config files.
session_dir = make_session_settings_dir()
try:
    settings_path = write_settings_file(
        generate_settings(
            workspace_dir="/srv/hermes/sessions/abc",
            allowed_tools=[],  # default-deny everything
        ),
        parent_dir=session_dir,
    )
    mcp_path = write_mcp_config_file(
        generate_mcp_config(allowlist=None),  # v1 default: no MCP servers
        parent_dir=session_dir,
    )
    # argv for spawn():
    argv = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--settings", str(settings_path),
        "--mcp-config", str(mcp_path),
        "--strict-mcp-config",
    ]
    # Pass argv to agent.claude_cli.spawn(...) from PR 2.
finally:
    # Adapter is responsible for cleanup at session close.
    pass

# Session id mapping (in-memory, TTL = 24h by default):
store = SessionStore()
claude_id, is_new = store.get_or_create("hermes-session-xyz")
if is_new:
    # No prior mapping — adapter starts a fresh `claude` invocation, then
    # captures the claude_session_id from the first stream event and stores it:
    store.set("hermes-session-xyz", "claude-session-real-id-from-stream")
else:
    # Resume the existing claude session:
    argv.extend(["--resume", claude_id])

# House-keeping (call periodically, e.g. from a Hermes maintenance loop):
store.evict_expired()
```

## Test coverage

- 59 unit tests in `tests/agent/claude_cli/` — package skeleton, errors, parser
  happy path + chunk boundaries + failure modes, probe binary discovery +
  version parsing + env hygiene + cache + runner orchestration, plus PR 3 generators and session store.
- PR 3 unit tests in `tests/agent/claude_cli/test_settings.py`,
  `test_mcp_config.py`, `test_session_store.py` — generator output shape
  per spec, filesystem mode enforcement (0600 file, 0700 parent),
  session-id mapping core operations, TTL eviction with injectable clock.
- 11 e2e integration tests in `tests/e2e/test_claude_cli_probe.py` — real-
  binary probe: stream-json invocation, `--resume`, permissioning canaries,
  hermetic settings, process group cleanup, `--no-session-persistence`, model
  alias acceptance, egress methodology.
