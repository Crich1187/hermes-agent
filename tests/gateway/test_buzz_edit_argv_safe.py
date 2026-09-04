"""root-12e9 Gate3 remediation: private edit content must not appear on argv.

Uses a real buzz-cli that supports ``messages edit --content -`` (household
fork SHA ≥ 023860d0 / root-67m5i). The binary is selected via
``HERMES_TEST_BUZZ_CLI`` or a known worktree build path — never mutates the
live Hermes deploy checkout or ``/root/buzz`` production binary.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from pathlib import Path

import pytest

MARKER = "ROOT12E9_PRIV_ARGV_ABSENT_MARKER"
EVENT = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

_CANDIDATE_CLIS = (
    os.environ.get("HERMES_TEST_BUZZ_CLI", ""),
    "/root/.worktrees/buzz-root-67m5i-wBp1S/target/debug/buzz",
)


def _safe_edit_buzz_cli() -> Path | None:
    for raw in _CANDIDATE_CLIS:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        help_out = subprocess.run(
            [str(path), "messages", "edit", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        text = (help_out.stdout or "") + (help_out.stderr or "")
        if "Use '-' to read from stdin" in text or "--content-file" in text:
            return path
    return None


def _cmdline_contains(raw: bytes, needle: bytes) -> bool:
    return any(
        raw[i : i + len(needle)] == needle
        for i in range(max(0, len(raw) - len(needle) + 1))
    )


@pytest.mark.skipif(
    _safe_edit_buzz_cli() is None,
    reason="argv-safe buzz-cli (≥023860d0) not available for /proc proof",
)
@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc").is_dir(),
    reason="Linux /proc required",
)
def test_real_buzz_edit_stdin_omits_marker_from_proc_cmdline():
    """Spawn real ``buzz messages edit --content -`` with stdin held open.

    While the child blocks on stdin, ``/proc/<pid>/cmdline`` must not contain
    the private marker (value-safe: boolean only — never print marker/cmdline).
    """
    buzz = str(_safe_edit_buzz_cli())
    # Throwaway hex key material for env only — never logged or asserted as text.
    throwaway_key = secrets.token_hex(32)

    child = subprocess.Popen(
        [buzz, "messages", "edit", "--event", EVENT, "--content", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "BUZZ_RELAY_URL": "http://127.0.0.1:1",
            "BUZZ_PRIVATE_KEY": throwaway_key,
        },
    )
    pid = child.pid
    deadline = time.time() + 8.0
    raw_seen: bytes | None = None
    marker_in_argv = True
    has_content_flag = False
    try:
        while time.time() < deadline:
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            except FileNotFoundError:
                break
            if raw:
                raw_seen = raw
                marker_in_argv = _cmdline_contains(raw, MARKER.encode())
                has_content_flag = _cmdline_contains(raw, b"--content")
                if has_content_flag:
                    break
            if child.poll() is not None:
                break
            time.sleep(0.01)
        assert raw_seen is not None, "never observed /proc/<pid>/cmdline"
        assert has_content_flag, "expected --content in argv"
        assert not marker_in_argv, "private marker present in argv on stdin-safe path"
    finally:
        if child.stdin is not None:
            try:
                child.stdin.write(MARKER.encode())
                child.stdin.close()
            except BrokenPipeError:
                pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
