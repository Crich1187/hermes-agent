# `agent/claude_cli/` — Claude Code CLI subprocess adapter

This package implements the subprocess transport for routing Hermes' Anthropic-
bound calls through the official `claude` CLI binary, billing against the
user's Claude Max plan instead of the third-party "extra usage" credit bucket
that direct-HTTPS callers hit.

## Status

**PR 1 of 6 — landed.** Probe + protocol parser + CLI contract documentation.
The adapter itself is not yet wired into Hermes' provider runtime; see
`docs/superpowers/specs/2026-05-16-hermes-claude-code-cli-adapter-design.md`
for the full plan and v1 scope.

Subsequent PRs (not yet landed):

- PR 2: `process.py` — subprocess spawn / drain / kill primitives.
- PR 3: `settings.py`, `mcp_config.py`, `session_store.py`.
- PR 4: `adapter.py` — the provider adapter, registered as `claude_code_cli`.
- PR 5: end-to-end wiring; `model.provider: claude-code-subprocess` becomes selectable.
- PR 6 (optional): cross-provider fallback behavior.

## What ships in PR 1

| Module | Purpose |
|---|---|
| `errors.py` | Exception hierarchy: `ClaudeCliError` base + 7 subclasses. |
| `protocol.py` | `StreamJsonParser` — pure NDJSON parser for `claude --print --output-format stream-json` output. No I/O. |
| `probe.py` | Compatibility probe: binary discovery, version check, env hygiene, cache, `_run_basic_invocation_assertion`, `extract_session_id`, `run_probe`, CLI entry point. |

## Running the probe

The probe runs at adapter init in production (results cached for 24h) and as
a CLI entry point for operators:

```bash
cd /root/.hermes/hermes-agent-claude-cli-pr1
./venv/bin/python -m agent.claude_cli.probe [--no-cache] [--binary-path /path/to/claude]
```

Output is a JSON `ProbeResult`. Exit code 0 if `ok=true`, 1 otherwise.

## Running the integration tests

The integration tests against the real `claude` binary are gated by the
`integration` pytest marker. They consume real Anthropic plan tokens
(short prompts, total spend is trivial).

```bash
cd /root/.hermes/hermes-agent-claude-cli-pr1
set -a && source /run/infisical/hermes.env && set +a
./venv/bin/python -m pytest tests/e2e/test_claude_cli_probe.py -v -m integration
```

By default, integration tests skip if `CLAUDE_CODE_OAUTH_TOKEN` is not set.

## CLI contract

See Appendix A of the design spec at
`/root/docs/superpowers/specs/2026-05-16-hermes-claude-code-cli-adapter-design.md`
for the empirically-verified CLI contract (prompt transport, session_id schema,
flag behavior, model alias mapping, hermetic-config posture).

## Test coverage

- 38 unit tests in `tests/agent/claude_cli/` — package skeleton, errors, parser
  happy path + chunk boundaries + failure modes, probe binary discovery +
  version parsing + env hygiene + cache + runner orchestration.
- 11 e2e integration tests in `tests/e2e/test_claude_cli_probe.py` — real-
  binary probe: stream-json invocation, `--resume`, permissioning canaries,
  hermetic settings, process group cleanup, `--no-session-persistence`, model
  alias acceptance, egress methodology.
