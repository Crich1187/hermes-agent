# Hermes `/snapshot` rollback safety

**Bead:** `root-8ei`  
**Surfaces:** `/snapshot` (alias `/snap`) → `cli._handle_snapshot_command` → `hermes_cli.backup.create_quick_snapshot` / `restore_quick_snapshot`

## What `/snapshot` is

A **HERMES_HOME state snapshot**: copies critical config/state files under
`{HERMES_HOME}/state-snapshots/<id>/` with a `manifest.json`. Create → modify →
restore returns those files to the prior bytes.

Default captured set includes `config.yaml`, `.env`, `auth.json`, `state.db`,
`SOUL.md`, `AGENTS.md`, cron jobs, gateway/pairing state. Skills trees and the
agent repo are **not** in the quick set (use `hermes backup` for a full zip).

## Versus `/rollback` and git

| Mechanism | Scope | Purpose |
|---|---|---|
| **`/snapshot` / `/snap`** | Files under **HERMES_HOME** (config/state) | Roll back Hermes identity/config/state before risky edits |
| **`/rollback`** | **Project working directory** filesystem checkpoints (shadow git via `tools.checkpoint_manager`) | Undo agent file edits in the workspace / chat-linked checkpoints |
| **git** | Version-controlled source trees | Source history; not a substitute for HERMES_HOME secrets/state |

Do not use `/rollback` expecting `config.yaml` / `SOUL.md` under `~/.hermes` to
revert. Do not use `/snapshot` expecting a project cwd file edit to revert.

## Safety (fail-closed)

- Snapshot **ids** and **labels** must be single path components (no `/`, `\`, `..`).
- Manifest file entries with traversal are refused; restore returns `False`.
- Symlinks whose resolved target escapes HERMES_HOME (or the snapshot dir) are skipped/refused.
- Missing snapshot directory, missing `manifest.json`, or corrupt JSON → restore `False`.
- Operations accept an explicit `hermes_home=` for tests; production uses `get_hermes_home()` / `HERMES_HOME`.

## Commands

```text
/snapshot                  # list
/snapshot create [label]   # create
/snapshot restore <id>     # restore (or 1-based index from list)
/snapshot prune [N]        # keep N newest (default 20)
/snap …                    # alias
```

After restoring `state.db`, restart Hermes so open connections pick up the file.

## Privacy

Snapshots may contain `.env` / `auth.json`. Treat `state-snapshots/` like secrets;
do not paste manifests or file contents into tickets.
