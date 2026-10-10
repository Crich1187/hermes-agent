"""Only validated profile metadata and projects stores opt into sharing."""
import os
from pathlib import Path
import stat

import pytest

from gateway import status
from hermes_cli.profiles import read_profile_meta, write_profile_meta
from hermes_cli.sqlite_util import open_db
from hermes_constants import shared_profile_file_mode


@pytest.fixture
def shared_home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "profile"
    home.mkdir()
    os.chown(home, -1, os.getegid())
    os.chmod(home, 0o2770)
    monkeypatch.setenv("HERMES_SKIP_CHMOD", "1")
    monkeypatch.setenv("HERMES_HOME_MODE", "2770")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path.resolve() / "dashboard"))
    return home


def test_explicit_home_validation_does_not_change_runtime_scope(shared_home):
    from hermes_constants import get_hermes_home
    before = get_hermes_home()
    target = shared_home / "state.db"
    assert shared_profile_file_mode(target) is None
    assert shared_profile_file_mode(target, home=shared_home) == 0o660
    assert get_hermes_home() == before


def test_foreign_profile_meta_replacements_remain_shared(shared_home):
    path = shared_home / "profile.yaml"
    for name in ("first", "second"):
        write_profile_meta(shared_home, display_name=name)
        assert read_profile_meta(shared_home)["display_name"] == name
        assert stat.S_IMODE(path.stat().st_mode) == 0o660
        assert path.stat().st_gid == shared_home.stat().st_gid


def test_foreign_runtime_status_replacements_remain_shared(shared_home):
    path = shared_home / "gateway_state.json"
    for name in ("starting", "running"):
        status._write_json_file(path, {"gateway_state": name})
        assert status.read_runtime_status(path)["gateway_state"] == name
        assert stat.S_IMODE(path.stat().st_mode) == 0o660
        assert path.stat().st_gid == shared_home.stat().st_gid


def test_projects_db_creates_shared_main_and_sidecars(shared_home):
    path = shared_home / "projects.db"
    old_umask = os.umask(0o077)
    try:
        conn = open_db(path, db_label="projects.db",
                       initialize=lambda conn: conn.execute("CREATE TABLE projects (name TEXT)"))
    finally:
        os.umask(old_umask)
    try:
        conn.execute("INSERT INTO projects VALUES ('gateway')")
        conn.commit()
        other = open_db(path, db_label="projects.db")
        try:
            assert other.execute("SELECT name FROM projects").fetchone()[0] == "gateway"
        finally:
            other.close()
        for suffix in ("", "-wal", "-shm"):
            file = path.with_name(path.name + suffix)
            if file.exists():
                assert stat.S_IMODE(file.stat().st_mode) == 0o660
                assert file.stat().st_gid == shared_home.stat().st_gid
    finally:
        conn.close()


def test_unsafe_metadata_and_project_paths_fail_closed(shared_home):
    os.chmod(shared_home, 0o2777)
    for call in (lambda: write_profile_meta(shared_home, display_name="unsafe"),
                 lambda: status._write_json_file(shared_home / "gateway_state.json", {}),
                 lambda: open_db(shared_home / "projects.db", db_label="projects.db")):
        with pytest.raises(PermissionError):
            call()
    assert list(shared_home.iterdir()) == []


def test_shared_gateway_lock_creation_and_probe_are_observational(shared_home, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(shared_home))
    monkeypatch.setattr(status, "_gateway_lock_handle", None)
    old_umask = os.umask(0o077)
    try:
        assert status.acquire_gateway_runtime_lock()
    finally:
        os.umask(old_umask)
    path = shared_home / "gateway.lock"
    try:
        before = path.read_bytes()
        assert stat.S_IMODE(path.stat().st_mode) == 0o660
        held = status._gateway_lock_handle
        monkeypatch.setattr(status, "_gateway_lock_handle", None)
        assert status.is_gateway_runtime_lock_active(path)
        assert path.read_bytes() == before
        monkeypatch.setattr(status, "_gateway_lock_handle", held)
    finally:
        status.release_gateway_runtime_lock()


def test_gateway_probe_permission_denial_never_unlinks(shared_home, monkeypatch):
    path = shared_home / "gateway.lock"
    path.write_text("live")
    os.chmod(path, 0o660)
    original = open

    def denied(candidate, *args, **kwargs):
        if Path(candidate) == path:
            raise PermissionError("synthetic denial")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    with pytest.raises(RuntimeError, match="not inspectable"):
        status.is_gateway_runtime_lock_active(path)
    assert path.exists()


def test_private_metadata_keeps_existing_0600(shared_home, monkeypatch):
    monkeypatch.delenv("HERMES_SKIP_CHMOD")
    for name in ("profile.yaml", "gateway_state.json"):
        path = shared_home / name
        path.write_text("{}")
        os.chmod(path, 0o600)
    write_profile_meta(shared_home, display_name="private")
    status._write_json_file(shared_home / "gateway_state.json", {})
    for name in ("profile.yaml", "gateway_state.json"):
        assert stat.S_IMODE((shared_home / name).stat().st_mode) == 0o600
