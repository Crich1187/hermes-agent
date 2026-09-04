"""Regression tests: pending-drain + finally-cleanup races must not spawn
duplicate agents OR silently drop messages that arrived during cleanup.

Two related races in gateway/platforms/base.py:_process_message_background:

1. Pending-drain path (previous line 1931):
   ``del self._active_sessions[session_key]`` opened a window where a
   concurrent inbound message could pass the Level-1 guard, spawn its
   own _process_message_background, and run simultaneously with the
   recursive drain.  Two agents on one session_key = duplicate responses.

2. Finally-cleanup path (previous line 1990-1991):
   Between the awaits in finally (typing_task, stop_typing) and the
   ``del self._active_sessions[session_key]``, a new message could
   land in _pending_messages.  The del ran anyway, and the message was
   silently dropped — user never got a reply.

Fix: keep the _active_sessions entry live across the turn chain and
clear the Event instead of deleting; in finally, drain any
late-arrival pending message by spawning a task instead of
dropping it.

root-nypt.3: the handoff probe must not re-enter a hanging
``wait_for(asyncio.shield(typing_task))`` path after the sync point —
that turned PASS into TimeoutError under load without an arbitrary sleep
waiver. Production joins typing unshielded and sets ``stop_event``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


@pytest.fixture(autouse=True)
def _isolate_delivery_ledger(monkeypatch, tmp_path):
    """Race probes must not share the process-wide delivery ledger SQLite file.

    Under load, ``asyncio.to_thread(record_obligation)`` contended on the
    shared HERMES_HOME state.db and timed out the deterministic wait_for
    barriers (root-nypt.3 flake). Disable the ledger for this module.
    """
    monkeypatch.setattr(
        "gateway.delivery_ledger.ledger_enabled",
        lambda config=None: False,
    )


class _StubAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False):
        pass

    async def disconnect(self):
        pass

    async def send(self, chat_id, text, **kwargs):
        return SendResult(success=True, message_id="m1")

    async def get_chat_info(self, chat_id):
        return {}


def _make_adapter():
    # typing_indicator off: these tests probe session-guard / pending-drain
    # races, not the typing refresh loop. Leaving typing on re-introduced
    # flake via shielded joins under load (root-nypt.3).
    adapter = _StubAdapter(
        PlatformConfig(enabled=True, token="", typing_indicator=False),
        Platform.TELEGRAM,
    )
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="m1")
    )
    return adapter


def _make_event(text="hi", chat_id="42"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"
        ),
        message_id=f"id-{text}",
    )


def _sk(chat_id="42"):
    return build_session_key(
        SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm")
    )


@pytest.mark.asyncio
async def test_pending_drain_keeps_active_session_guard_live():
    """Fix for R5: during pending-drain cleanup, _active_sessions must stay
    populated so concurrent inbound messages can't spawn a duplicate
    _process_message_background.  We only CLEAR the Event, never delete."""
    adapter = _make_adapter()
    sk = _sk()

    # Register a slow handler so the agent is "mid-processing" when the
    # pending message arrives.
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_processed = asyncio.Event()
    handoff_entered = asyncio.Event()
    release_handoff = asyncio.Event()

    async def handler(event):
        first_started.set()
        await release_first.wait()
        if event.text == "M2":
            second_processed.set()
        return "done"

    adapter._message_handler = handler

    async def stop_typing_during_handoff(*args, **kwargs):
        # Sync point only — do not call the real stop path here. Re-entering
        # a shielded typing join after the probe resumed was the flake that
        # timed out second_processed under load (root-nypt.3).
        handoff_entered.set()
        await release_handoff.wait()
        return None

    adapter._stop_typing_refresh = stop_typing_during_handoff

    # Spawn M1 through handle_message.
    await adapter.handle_message(_make_event(text="M1"))

    # Wait until M1 is actively running inside the handler.
    await asyncio.wait_for(first_started.wait(), timeout=5.0)

    # Assert: session is active.
    assert sk in adapter._active_sessions
    active_event = adapter._active_sessions[sk]

    # Simulate pending message (M2) queued while M1 runs.
    adapter._pending_messages[sk] = _make_event(text="M2")

    # Release M1 — pending-drain block now runs.  During its cleanup
    # awaits, _active_sessions[sk] must remain populated (same object
    # reference) so any M3 arriving in that window hits the busy-handler.
    release_first.set()

    try:
        # Pause inside the handoff's typing cleanup. Production has already
        # cleared the interrupt Event and has not yet transferred task ownership.
        await asyncio.wait_for(handoff_entered.wait(), timeout=5.0)

        # Across the drain transition, the Event object must be the SAME
        # reference (not replaced, not deleted).
        assert sk in adapter._active_sessions, (
            "_active_sessions[session_key] was deleted during pending-drain — "
            "opens a window for duplicate-agent spawn"
        )
        assert adapter._active_sessions[sk] is active_event, (
            "_active_sessions[session_key] was replaced during pending-drain — "
            "the old Event may have waiters that now won't be signaled"
        )

        # Finish drain without relying on scheduler speed.
        release_handoff.set()
        await asyncio.wait_for(second_processed.wait(), timeout=5.0)
    finally:
        release_handoff.set()
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_finally_cleanup_drains_late_arrival_pending():
    """Fix for R6: if a message lands in _pending_messages during the
    finally-block cleanup awaits, the finally must spawn a drain task
    instead of deleting _active_sessions and dropping the message."""
    adapter = _make_adapter()
    sk = _sk()

    processed = []
    late_processed = asyncio.Event()

    async def handler(event):
        processed.append(event.text)
        if event.text == "LATE":
            late_processed.set()
        return "ok"

    adapter._message_handler = handler

    # Inject at the start of typing stop (the finally await window) without
    # depending on sleep(0) scheduling or a shielded typing join completing.
    original_stop_refresh = adapter._stop_typing_refresh
    injected = {"done": False}

    async def stop_refresh_injects_pending(chat_id, typing_task=None, **kwargs):
        if not injected["done"]:
            adapter._pending_messages[sk] = _make_event(text="LATE")
            injected["done"] = True
        # Avoid joining a live typing task in the probe — pass None so the
        # production stop path only clears platform typing state.
        return await original_stop_refresh(chat_id, None, **kwargs)

    adapter._stop_typing_refresh = stop_refresh_injects_pending

    # Send M1.
    await adapter.handle_message(_make_event(text="M1"))

    # Drain: wait for the late-drain task itself to process LATE.
    await asyncio.wait_for(late_processed.wait(), timeout=5.0)

    await adapter.cancel_background_tasks()

    assert "M1" in processed, "M1 was not processed"
    assert "LATE" in processed, (
        "Late-arrival pending message was silently dropped — finally "
        "cleanup should have spawned a drain task"
    )


@pytest.mark.asyncio
async def test_no_pending_cleans_up_normally():
    """Regression guard: when no pending message exists, the finally
    block must still delete _active_sessions as before (no leak)."""
    adapter = _make_adapter()
    sk = _sk()

    async def handler(event):
        return "ok"

    adapter._message_handler = handler

    await adapter.handle_message(_make_event(text="solo"))

    # Await the task that owns this session rather than sampling cleanup after
    # an arbitrary wall-clock delay. Do not shield — a shielded wait can time
    # out while leaving the owner task running (root-nypt.9.1 class of hang).
    owner_task = adapter._session_tasks[sk]
    await asyncio.wait_for(owner_task, timeout=5.0)

    assert sk not in adapter._active_sessions, (
        "_active_sessions was not cleaned up after a normal turn with no pending"
    )
    assert sk not in adapter._pending_messages

    await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_pending_drain_does_not_drop_guard_under_contended_stop():
    """Negative control: even when stop_typing_refresh is slow (but finite),
    the active-session guard object identity stays stable across handoff."""
    adapter = _make_adapter()
    sk = _sk()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_processed = asyncio.Event()
    saw_guard = []

    async def handler(event):
        first_started.set()
        await release_first.wait()
        if event.text == "M2":
            second_processed.set()
        return "done"

    adapter._message_handler = handler
    original = adapter._stop_typing_refresh

    async def slow_but_finite_stop(chat_id, typing_task=None, **kwargs):
        # Finite delay (not an arbitrary flake sleep in the test harness
        # waiting for a race window) — proves production awaits completion
        # without deleting the guard mid-stop.
        if sk in adapter._active_sessions:
            saw_guard.append(adapter._active_sessions[sk])
        await asyncio.sleep(0)
        return await original(chat_id, None, **kwargs)

    adapter._stop_typing_refresh = slow_but_finite_stop

    await adapter.handle_message(_make_event(text="M1"))
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    active_event = adapter._active_sessions[sk]
    adapter._pending_messages[sk] = _make_event(text="M2")
    release_first.set()
    await asyncio.wait_for(second_processed.wait(), timeout=5.0)
    assert saw_guard, "stop path never observed the active guard"
    assert all(g is active_event for g in saw_guard)
    await adapter.cancel_background_tasks()
