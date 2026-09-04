"""Rollback-safety tests for Hermes /snapshot (root-8ei).

Exercises create → modify → restore exact-byte round-trips under an isolated
temporary HERMES_HOME, plus traversal/symlink and missing/corrupt fail-closed
behavior. Does not touch live ~/.hermes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli.backup import (
    create_quick_snapshot,
    restore_quick_snapshot,
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME; also pin env so nothing touches the live home."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    (home / "config.yaml").write_bytes(b"model:\n  provider: openrouter\n  name: test\n")
    (home / ".env").write_bytes(b"OPENROUTER_API_KEY=test-key-exact-bytes\n")
    (home / "auth.json").write_bytes(b'{"providers":{"openrouter":{"ok":true}}}\n')
    (home / "SOUL.md").write_bytes(b"# Soul\nidentity-fixture-v1\n")
    (home / "AGENTS.md").write_bytes(b"# Agents\nrouter notes\n")
    (home / "cron").mkdir()
    (home / "cron" / "jobs.json").write_bytes(b'{"jobs":[{"id":"j1"}]}\n')

    db_path = home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'hello world')")
    conn.commit()
    conn.close()
    return home


class TestSnapshotExactByteRoundTrip:
    def test_create_list_restore_exact_bytes_after_config_mutation(self, hermes_home):
        before = {
            rel: _sha256(hermes_home / rel)
            for rel in (
                "config.yaml",
                ".env",
                "auth.json",
                "SOUL.md",
                "AGENTS.md",
                "cron/jobs.json",
            )
        }
        # state.db is copied via sqlite3.backup(); assert logical contents, not
        # raw page bytes (sidecar rewrite can change the file digest).
        conn = sqlite3.connect(str(hermes_home / "state.db"))
        before_rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        conn.close()

        snap_id = create_quick_snapshot(label="pre-risk", hermes_home=hermes_home)
        assert snap_id is not None
        assert "pre-risk" in snap_id

        # Mutate config + soul as the risky edit under test.
        (hermes_home / "config.yaml").write_bytes(b"model:\n  provider: anthropic\n")
        (hermes_home / "SOUL.md").write_bytes(b"# Soul\nCORRUPTED\n")
        conn = sqlite3.connect(str(hermes_home / "state.db"))
        conn.execute("INSERT INTO sessions VALUES ('s2', 'mutated')")
        conn.commit()
        conn.close()
        assert _sha256(hermes_home / "config.yaml") != before["config.yaml"]
        assert _sha256(hermes_home / "SOUL.md") != before["SOUL.md"]

        assert restore_quick_snapshot(snap_id, hermes_home=hermes_home) is True

        for rel, digest in before.items():
            assert _sha256(hermes_home / rel) == digest, f"{rel} bytes mismatch after restore"

        conn = sqlite3.connect(str(hermes_home / "state.db"))
        after_rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        conn.close()
        assert after_rows == before_rows


class TestSnapshotFailClosed:
    def test_missing_snapshot_returns_false(self, hermes_home):
        assert restore_quick_snapshot("no-such-snapshot", hermes_home=hermes_home) is False

    def test_corrupt_manifest_returns_false(self, hermes_home):
        snap_id = create_quick_snapshot(label="ok", hermes_home=hermes_home)
        manifest = hermes_home / "state-snapshots" / snap_id / "manifest.json"
        manifest.write_text("{not-json", encoding="utf-8")
        assert restore_quick_snapshot(snap_id, hermes_home=hermes_home) is False
        # Live config untouched by failed restore
        assert b"openrouter" in (hermes_home / "config.yaml").read_bytes()

    def test_missing_manifest_returns_false(self, hermes_home):
        snap_id = create_quick_snapshot(label="ok", hermes_home=hermes_home)
        (hermes_home / "state-snapshots" / snap_id / "manifest.json").unlink()
        assert restore_quick_snapshot(snap_id, hermes_home=hermes_home) is False

    def test_traversal_snapshot_id_refused(self, hermes_home):
        assert restore_quick_snapshot("../config.yaml", hermes_home=hermes_home) is False
        assert restore_quick_snapshot("foo/../../etc", hermes_home=hermes_home) is False
        assert restore_quick_snapshot("/tmp/evil", hermes_home=hermes_home) is False

    def test_label_with_path_separator_rejected(self, hermes_home):
        with pytest.raises(ValueError):
            create_quick_snapshot(label="evil/../label", hermes_home=hermes_home)

    def test_manifest_path_traversal_entry_refused(self, hermes_home, tmp_path):
        snap_id = create_quick_snapshot(label="base", hermes_home=hermes_home)
        snap_dir = hermes_home / "state-snapshots" / snap_id
        # Plant a payload that would escape if restore joined naively.
        escape_payload = snap_dir / "evil.txt"
        escape_payload.write_text("pwn", encoding="utf-8")
        meta = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
        meta["files"]["../escaped.txt"] = 3
        (snap_dir / "manifest.json").write_text(json.dumps(meta), encoding="utf-8")

        outside = hermes_home.parent / "escaped.txt"
        assert restore_quick_snapshot(snap_id, hermes_home=hermes_home) is False
        assert not outside.exists()

    def test_symlink_escape_on_create_is_skipped(self, hermes_home, tmp_path):
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("TOPSECRET", encoding="utf-8")
        link = hermes_home / "config.yaml"
        link.unlink()
        link.symlink_to(secret)

        snap_id = create_quick_snapshot(label="symlink", hermes_home=hermes_home)
        # Other files still snapshot; escaped symlink must not be copied as config.yaml
        # if resolve escapes — config.yaml symlink resolves outside home → skipped.
        if snap_id is None:
            # Acceptable fail-closed if only remaining files were also skipped.
            return
        snap_cfg = hermes_home / "state-snapshots" / snap_id / "config.yaml"
        assert not snap_cfg.exists() or snap_cfg.resolve() != secret.resolve()
        if snap_cfg.exists():
            # If copied via follow, content would be secret — refuse that outcome.
            assert snap_cfg.read_text(encoding="utf-8") != "TOPSECRET"
