"""Explicit profile sharing preserves DB access without dropping SQLite locks."""
import grp
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB, _secure_state_db_files


@pytest.fixture
def shared_home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "profile"
    home.mkdir()
    os.chown(home, -1, os.getegid())
    os.chmod(home, 0o2770)
    monkeypatch.setenv("HERMES_SKIP_CHMOD", "1")
    monkeypatch.setenv("HERMES_HOME_MODE", "2770")
    # A dashboard's launch home differs from the explicit target profile.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path.resolve() / "dashboard"))
    return home


def _assert_modes(path, mode):
    for suffix in ("", "-wal", "-shm"):
        item = path.with_name(path.name + suffix)
        if item.exists():
            assert stat.S_IMODE(item.stat().st_mode) == mode
            assert item.stat().st_gid == path.parent.stat().st_gid


def test_shared_state_real_open_under_restrictive_umask(shared_home):
    path = shared_home / "state.db"
    old_umask = os.umask(0o077)
    try:
        db = SessionDB(db_path=path)
    finally:
        os.umask(old_umask)
    try:
        db.create_session("gateway", "cli")
        _assert_modes(path, 0o660)
        reader = SessionDB(db_path=path, read_only=True)
        try:
            assert reader.get_session("gateway")["id"] == "gateway"
        finally:
            reader.close()
    finally:
        db.close()


def test_shared_state_a_b_a_isolation(shared_home, monkeypatch):
    other = shared_home.with_name("other")
    other.mkdir()
    os.chown(other, -1, os.getegid())
    os.chmod(other, 0o2770)
    for home, name in ((shared_home, "a"), (other, "b"), (shared_home, "a")):
        db = SessionDB(db_path=home / "state.db")
        try:
            db.create_session(name, "cli")
            assert db.get_session("b" if name == "a" else "a") is None
            _assert_modes(home / "state.db", 0o660)
        finally:
            db.close()


def test_correct_shared_modes_never_chmod_foreign_owner(shared_home, monkeypatch):
    path = shared_home / "state.db"
    for suffix in ("", "-wal", "-shm"):
        target = path.with_name(path.name + suffix)
        target.touch()
        os.chmod(target, 0o660)
    original_uid = os.geteuid()
    gid = shared_home.stat().st_gid
    monkeypatch.setattr(os, "geteuid", lambda: original_uid + 12345)
    monkeypatch.setattr(os, "getegid", lambda: gid)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_gid=gid, pw_name="gateway"))

    def forbidden_chmod(*args, **kwargs):
        pytest.fail("group user attempted chmod on an already protected database")

    monkeypatch.setattr(os, "chmod", forbidden_chmod)
    _secure_state_db_files(path, create_main=True)


@pytest.mark.parametrize("read_only", [True, False])
@pytest.mark.parametrize("unsafe", ["parent", "symlink-parent", "symlink-db", "hardlink", "mode", "sidecar"])
def test_shared_state_denies_unsafe_paths_before_connect(shared_home, monkeypatch, read_only, unsafe):
    path = shared_home / "state.db"
    path.touch()
    os.chmod(path, 0o660)
    if unsafe == "parent":
        os.chmod(shared_home, 0o770)
    elif unsafe == "symlink-parent":
        link = shared_home.with_name("linked")
        link.symlink_to(shared_home, target_is_directory=True)
        path = link / "state.db"
    elif unsafe == "symlink-db":
        path.unlink()
        target = shared_home / "other"
        target.touch()
        os.chmod(target, 0o660)
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(path, shared_home / "duplicate")
    elif unsafe == "mode":
        os.chmod(path, 0o666)
    else:
        sidecar = shared_home / "state.db-wal"
        sidecar.symlink_to(path)
    with pytest.raises(PermissionError):
        SessionDB(db_path=path, read_only=read_only)


def test_shared_state_denies_untrusted_owner(shared_home, monkeypatch):
    path = shared_home / "state.db"
    path.touch()
    os.chmod(path, 0o660)
    gid = shared_home.stat().st_gid
    original = Path.lstat

    def foreign_owner(candidate):
        info = original(candidate)
        if candidate == path:
            values = list(info)
            values[4] = os.geteuid() + 12345
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_owner)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_gid=gid + 1, pw_name="outsider"))
    monkeypatch.setattr(grp, "getgrgid", lambda group: SimpleNamespace(gr_mem=[]))
    with pytest.raises(PermissionError):
        SessionDB(db_path=path)


def test_private_state_defaults_remain_0600(shared_home, monkeypatch):
    monkeypatch.delenv("HERMES_SKIP_CHMOD")
    monkeypatch.delenv("HERMES_HOME_MODE")
    path = shared_home / "state.db"
    db = SessionDB(db_path=path)
    try:
        db.create_session("private", "cli")
        _assert_modes(path, 0o600)
    finally:
        db.close()


@pytest.mark.linux_only
def test_shared_state_two_uid_gateway_dashboard_interoperability(monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("synthetic two-UID test requires root to drop child credentials")
    account = pwd.getpwnam("daemon")
    with tempfile.TemporaryDirectory(prefix="hermes-shared-db-") as directory:
        os.chmod(directory, 0o755)
        home = Path(directory) / "profile"
        home.mkdir()
        os.chown(home, 0, account.pw_gid)
        os.chmod(home, 0o2770)
        monkeypatch.setenv("HERMES_SKIP_CHMOD", "1")
        monkeypatch.setenv("HERMES_HOME_MODE", "2770")
        path = home / "state.db"
        db = SessionDB(db_path=path)
        try:
            db.create_session("gateway", "cli")
            _assert_modes(path, 0o660)
            code = f'''
from pathlib import Path
from hermes_state import SessionDB
path = Path({str(path)!r})
reader = SessionDB(db_path=path, read_only=True)
assert reader.get_session("gateway")["id"] == "gateway"
reader.close()
db = SessionDB(db_path=path)
db.create_session("dashboard", "cli")
assert db.get_session("gateway")["id"] == "gateway"
db.close()
print("PASS two-UID shared state")
'''
            result = subprocess.run([sys.executable, "-c", code],
                                    user=account.pw_uid, group=account.pw_gid, extra_groups=[account.pw_gid],
                                    capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr
            assert "PASS two-UID shared state" in result.stdout
            assert db.get_session("dashboard")["id"] == "dashboard"
            _assert_modes(path, 0o660)
        finally:
            db.close()
