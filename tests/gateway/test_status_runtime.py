from pathlib import Path

from gateway import status


def test_write_runtime_status_reset_platforms_clears_stale_entries(monkeypatch, tmp_path):
    runtime_path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: runtime_path)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
    monkeypatch.setattr(status.os, "getpid", lambda: 999)
    monkeypatch.setattr(status.sys, "argv", ["hermes", "gateway", "run"])

    runtime_path.write_text(
        '{"platforms": {"telegram": {"state": "connected"}}, "gateway_state": "stopped"}'
    )

    status.write_runtime_status(gateway_state="starting", reset_platforms=True)

    payload = status.read_runtime_status()
    assert payload["platforms"] == {}
    assert payload["gateway_state"] == "starting"


def test_write_runtime_status_preserves_platforms_without_reset(monkeypatch, tmp_path):
    runtime_path = tmp_path / "gateway_state.json"
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: runtime_path)
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
    monkeypatch.setattr(status.os, "getpid", lambda: 999)
    monkeypatch.setattr(status.sys, "argv", ["hermes", "gateway", "run"])

    runtime_path.write_text(
        '{"platforms": {"telegram": {"state": "connected"}}, "gateway_state": "stopped"}'
    )

    status.write_runtime_status(gateway_state="starting")

    payload = status.read_runtime_status()
    assert payload["platforms"]["telegram"]["state"] == "connected"
    assert payload["gateway_state"] == "starting"
