"""Tests for the gateway /queue command handler (root-eh4).

/queue schedules the NEXT user turn without touching the current one. It is
the turn-boundary sibling of /steer:

  * /steer  → lands INSIDE the active run, after the next tool call.
  * /queue  → lands as the next user turn, after the active run finishes.

Both surfaces already existed; what was missing was coverage of the two
together, and one real defect it exposed: the /steer fallback paths assigned
``adapter._pending_messages[key]`` directly instead of joining the /queue
FIFO, so "/queue A" followed by "/steer B" while the agent was still starting
silently discarded A. Those fallbacks now use ``_enqueue_fifo``.

These tests drive the real ``GatewayRunner._handle_message`` with isolated
per-test session state (no shared fixtures, no live agent, no network).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, message_id: str = "m1") -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id=message_id)


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_runner(session_entry: SessionEntry):
    """Fresh runner + adapter per test — no state shared between tests."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_a, **_k: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *a, **k: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner, adapter


def _running(runner, sk):
    agent = MagicMock()
    agent.steer.return_value = True
    runner._running_agents[sk] = agent
    return agent


def _slot(adapter, sk):
    ev = adapter._pending_messages.get(sk)
    return getattr(ev, "text", None)


def _overflow(runner, sk):
    return [e.text for e in getattr(runner, "_queued_events", {}).get(sk, [])]


@pytest.mark.asyncio
async def test_queue_stores_next_turn_without_interrupting():
    """The core AC: /queue pre-loads a follow-up and never interrupts."""
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    agent = _running(runner, sk)

    result = await runner._handle_message(_make_event("/queue then summarise findings"))

    assert result is not None
    assert "queued" in result.lower()
    assert "can't run mid-turn" not in result.lower()
    assert _slot(adapter, sk) == "then summarise findings"
    agent.interrupt.assert_not_called()
    agent.steer.assert_not_called()


@pytest.mark.asyncio
async def test_queue_without_payload_returns_usage_and_queues_nothing():
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    agent = _running(runner, sk)

    result = await runner._handle_message(_make_event("/queue"))

    assert result is not None and "usage" in result.lower()
    assert adapter._pending_messages == {}
    assert _overflow(runner, sk) == []
    agent.interrupt.assert_not_called()
    agent.steer.assert_not_called()


@pytest.mark.asyncio
async def test_multiple_queues_stack_fifo_through_the_real_handler():
    """Ordering is deterministic FIFO: slot first, then overflow in order."""
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    _running(runner, sk)

    for i, text in enumerate(("first task", "second task", "third task")):
        await runner._handle_message(_make_event(f"/queue {text}", message_id=f"m{i}"))

    assert _slot(adapter, sk) == "first task"
    assert _overflow(runner, sk) == ["second task", "third task"]
    assert runner._queue_depth(sk, adapter=adapter) == 3
    # Each item is its own turn — never merged into a neighbour.
    assert "second" not in (_slot(adapter, sk) or "")


@pytest.mark.asyncio
async def test_steer_and_queue_interleave_without_crossover_or_duplication():
    """/steer lands in-run; /queue lands at the turn boundary. No crossover."""
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    agent = _running(runner, sk)

    await runner._handle_message(_make_event("/queue A", message_id="m1"))
    await runner._handle_message(_make_event("/steer B", message_id="m2"))
    await runner._handle_message(_make_event("/queue C", message_id="m3"))

    # The steer reached the agent exactly once...
    agent.steer.assert_called_once_with("B")
    # ...and was NOT also replayed as a next-turn message.
    assert _slot(adapter, sk) == "A"
    assert _overflow(runner, sk) == ["C"]
    assert "B" not in [_slot(adapter, sk), *_overflow(runner, sk)]
    assert runner._queue_depth(sk, adapter=adapter) == 2
    agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_steer_fallback_while_starting_does_not_discard_a_queued_turn():
    """Regression (root-eh4): the fallback assigned the slot directly, so a
    prior /queue was silently lost. It must join the FIFO instead."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = _AGENT_PENDING_SENTINEL

    await runner._handle_message(_make_event("/queue FIRST", message_id="m1"))
    await runner._handle_message(_make_event("/steer SECOND", message_id="m2"))

    assert _slot(adapter, sk) == "FIRST", "the queued turn was discarded"
    assert _overflow(runner, sk) == ["SECOND"]
    assert runner._queue_depth(sk, adapter=adapter) == 2


@pytest.mark.asyncio
async def test_steer_fallback_without_steer_method_also_preserves_order():
    """Same guarantee on the 'agent lacks steer()' fallback path."""
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    legacy = SimpleNamespace(interrupt=lambda *_a, **_k: None)  # no .steer
    runner._running_agents[sk] = legacy

    await runner._handle_message(_make_event("/queue FIRST", message_id="m1"))
    await runner._handle_message(_make_event("/steer SECOND", message_id="m2"))

    assert _slot(adapter, sk) == "FIRST"
    assert _overflow(runner, sk) == ["SECOND"]


@pytest.mark.asyncio
async def test_queue_state_is_isolated_between_sessions():
    """Isolation: one runner's queue never leaks into another."""
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    _running(runner, sk)

    await runner._handle_message(_make_event("/queue only mine"))

    assert list(adapter._pending_messages) == [sk]
    runner2, adapter2 = _make_runner(_session_entry())
    assert adapter2._pending_messages == {}
    assert getattr(runner2, "_queued_events", {}) == {}


def test_queue_is_registered_and_bypasses_the_active_session_guard():
    """/queue must resolve as a command and be allowed to run mid-turn."""
    from hermes_cli.commands import (
        ACTIVE_SESSION_BYPASS_COMMANDS,
        resolve_command,
        should_bypass_active_session,
    )

    cmd = resolve_command("queue")
    assert cmd is not None and cmd.name == "queue"
    assert "queue" in ACTIVE_SESSION_BYPASS_COMMANDS
    assert should_bypass_active_session("queue") is True
