"""Explicit shared-profile permissions survive auth and dashboard file writes."""

import os
import secrets
import stat

import pytest

import hermes_cli.auth as auth
import hermes_constants
from hermes_cli.config import load_env, save_config, save_env_value


@pytest.mark.linux_only
def test_shared_profile_auth_config_and_env_alternate_without_permission_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SKIP_CHMOD", "1")
    monkeypatch.setenv("HERMES_HOME_MODE", "2770")
    homes = [tmp_path / name for name in ("alpha", "beta")]
    values = {home: secrets.token_hex(16) for home in homes}
    for home in homes:
        home.mkdir(mode=0o2770)
        home.chmod(0o2770)
        (home / ".env").touch(mode=0o660)
        (home / ".env").chmod(0o660)
        (home / "config.yaml").write_text("{}\n")
        (home / "config.yaml").chmod(0o660)
    for home in (homes[0], homes[1], homes[0]):
        token = hermes_constants.set_hermes_home_override(home)
        try:
            with auth._auth_store_lock():
                auth._save_auth_store({"version": 1, "providers": {home.name: {"api_key": values[home]}}})
                assert auth._load_auth_store()["providers"][home.name]["api_key"] == values[home]
            save_config({"display": {"language": "en"}})
            save_env_value("PROFILE_TEST_CREDENTIAL", values[home])
            assert load_env()["PROFILE_TEST_CREDENTIAL"] == values[home]
            for name in ("auth.json", "auth.lock", "config.yaml", ".env"):
                info = (home / name).stat()
                assert stat.S_IMODE(info.st_mode) == 0o660
                assert info.st_gid == home.stat().st_gid
            assert stat.S_IMODE(home.stat().st_mode) == 0o2770
        finally:
            hermes_constants.reset_hermes_home_override(token)
    assert values[homes[1]] not in (homes[0] / "auth.json").read_text()
    assert values[homes[0]] not in (homes[1] / "auth.json").read_text()


@pytest.mark.linux_only
@pytest.mark.parametrize("shared, parent_mode, target_link", [(False, 0o2770, False), (True, 0o2777, False), (True, 0o2770, True)])
def test_default_stays_private_and_explicit_insecure_sharing_is_rejected(tmp_path, monkeypatch, shared, parent_mode, target_link):
    home = tmp_path / "profile"
    home.mkdir()
    home.chmod(parent_mode)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SKIP_CHMOD", raising=False)
    monkeypatch.delenv("HERMES_HOME_MODE", raising=False)
    if shared:
        monkeypatch.setenv("HERMES_SKIP_CHMOD", "1")
        monkeypatch.setenv("HERMES_HOME_MODE", "2770")
    target = home / "auth.json"
    if target_link:
        outside = tmp_path / "outside.json"
        outside.write_text("{}\n")
        target.symlink_to(outside)
    if shared:
        with pytest.raises(PermissionError):
            auth._save_auth_store({"version": 1, "providers": {}})
        assert stat.S_IMODE(home.stat().st_mode) == parent_mode
        if target_link:
            assert outside.read_text() == "{}\n"
    else:
        with auth._auth_store_lock():
            auth._save_auth_store({"version": 1, "providers": {}})
        assert stat.S_IMODE(home.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
