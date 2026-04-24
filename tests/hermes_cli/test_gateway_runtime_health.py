from hermes_cli.gateway import _runtime_health_lines


def test_runtime_health_lines_include_fatal_platform_and_startup_reason(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "startup_failed",
            "exit_reason": "telegram conflict",
            "platforms": {
                "telegram": {
                    "state": "fatal",
                    "error_message": "another poller is active",
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ telegram: another poller is active" in lines
    assert "⚠ Last startup issue: telegram conflict" in lines


def test_runtime_health_lines_include_connected_target_platforms(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "telegram": {"state": "connected"},
                "whatsapp": {"state": "connected"},
                "slack": {"state": "connected"},
            },
        },
    )

    lines = _runtime_health_lines()

    assert "✓ telegram: runtime connected" in lines
    assert "✓ whatsapp: runtime connected" in lines
    assert "✓ slack: runtime connected" in lines


def test_runtime_health_lines_include_retrying_target_platforms(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "telegram": {
                    "state": "retrying",
                    "error_message": "failed to connect",
                },
                "whatsapp": {
                    "state": "disconnected",
                    "error_message": None,
                },
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ telegram: runtime retrying — failed to connect" in lines
    assert "⚠ whatsapp: runtime disconnected" in lines


def test_runtime_health_lines_skip_connected_platforms_when_gateway_stopped(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "stopped",
            "exit_reason": "clean shutdown",
            "platforms": {
                "telegram": {"state": "connected"},
                "whatsapp": {"state": "connected"},
            },
        },
    )

    lines = _runtime_health_lines()

    assert "✓ telegram: runtime connected" not in lines
    assert "✓ whatsapp: runtime connected" not in lines
    assert "⚠ Last shutdown reason: clean shutdown" in lines
