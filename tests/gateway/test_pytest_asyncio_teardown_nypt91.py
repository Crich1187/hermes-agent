"""root-nypt.9.1 — pytest-asyncio teardown must exit cleanly after gateway tests.

Historical failure: selected gateway tests printed PASSED, then hung inside
pytest-asyncio ``_scoped_runner`` → ``asyncio.Runner.close`` because a typing
refresh task remained non-joinable (``wait_for(asyncio.shield(typing_task))``
timed out while the task was still running). Assertion-only green is not
success — the per-file subprocess must reach a terminal exit.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token=""), Platform.DISCORD)
        self.sent: list[str] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


async def _hold_typing_stop_event(_chat_id, interval=2.0, metadata=None, stop_event=None):
    try:
        if stop_event is not None:
            await stop_event.wait()
        else:
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise


async def _hold_typing_wait_for_antipattern(
    _chat_id, interval=2.0, metadata=None, stop_event=None
):
    """Pre-fix anti-pattern cited in base.py (wait_for owns Event.wait)."""
    if stop_event is None:
        stop_event = asyncio.Event()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


@pytest.mark.asyncio
async def test_process_message_leaves_no_typing_task():
    adapter = _Adapter()
    adapter._keep_typing = _hold_typing_stop_event
    created: list[asyncio.Task] = []
    orig = asyncio.create_task

    def _track(coro, *args, **kwargs):
        task = orig(coro, *args, **kwargs)
        created.append(task)
        return task

    async def handler(_e):
        return "ok"

    adapter.set_message_handler(handler)
    ev = MessageEvent(
        text="x",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="1", chat_type="dm"),
        message_id="m",
    )
    asyncio.create_task = _track  # type: ignore[assignment]
    try:
        await adapter._process_message_background(ev, build_session_key(ev.source))
    finally:
        asyncio.create_task = orig  # type: ignore[assignment]

    assert adapter.sent == ["ok"]
    pending = [t for t in created if not t.done()]
    assert pending == [], f"leaked tasks after process_message: {pending}"


@pytest.mark.asyncio
async def test_stop_typing_refresh_sets_stop_event_and_joins():
    adapter = _Adapter()
    stop = asyncio.Event()
    task = asyncio.create_task(_hold_typing_wait_for_antipattern("1", stop_event=stop))
    await asyncio.sleep(0)
    t0 = time.monotonic()
    await adapter._stop_typing_refresh("1", task, timeout=0.5, stop_event=stop)
    assert time.monotonic() - t0 < 2.0
    assert stop.is_set()
    assert task.done()


async def _cancellation_resistant_child(stop_event: asyncio.Event) -> None:
    """Ignores CancelledError until stop_event; then delays so outer can cancel mid-join."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            continue
    # Cooperative stop acknowledged — brief tail so wait_for is still in flight
    # when the outer cleanup task is cancelled (Gate3 sabotage timing).
    try:
        await asyncio.sleep(0.35)
    except asyncio.CancelledError:
        return


async def _resist_until_stop(stop_event: asyncio.Event) -> None:
    """Ignores CancelledError until stop_event (no post-stop delay)."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            continue


async def _legacy_shielded_stop(
    typing_task: asyncio.Task,
    *,
    timeout: float = 0.2,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Pre-fix / anti-pattern: wait_for(shield(...)) leaves child pending."""
    if stop_event is not None and not stop_event.is_set():
        stop_event.set()
    if typing_task is not None and not typing_task.done():
        typing_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(typing_task), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


@pytest.mark.asyncio
async def test_stop_typing_refresh_propagates_outer_cancel_after_joining_child():
    """Gate3 Major: outer CancelledError must propagate; child must still join."""
    adapter = _Adapter()
    stop = asyncio.Event()
    child = asyncio.create_task(_cancellation_resistant_child(stop))
    await asyncio.sleep(0)

    outer = asyncio.create_task(
        adapter._stop_typing_refresh("1", child, timeout=1.0, stop_event=stop)
    )
    # Let helper set stop_event, cancel child, and enter wait_for during the
    # child's post-stop delay window.
    for _ in range(50):
        if stop.is_set():
            break
        await asyncio.sleep(0.01)
    assert stop.is_set()
    await asyncio.sleep(0.05)
    assert not outer.done(), "outer must still be joining when we cancel it"
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer

    assert child.done(), "child must be joined even when outer cleanup is cancelled"


@pytest.mark.asyncio
async def test_legacy_shield_leaves_child_pending_new_path_joins():
    """Right-reason sabotage: shielded join pends; fixed helper joins."""
    stop_old = asyncio.Event()
    child_old = asyncio.create_task(_resist_until_stop(stop_old))
    await asyncio.sleep(0)
    # Intentionally omit stop_event so shield+timeout cannot cooperatively end child.
    await _legacy_shielded_stop(child_old, timeout=0.05, stop_event=None)
    assert not child_old.done(), "legacy shield path must leave resistant child pending"
    stop_old.set()
    child_old.cancel()
    await asyncio.wait_for(child_old, timeout=1.0)
    assert child_old.done()

    adapter = _Adapter()
    stop_new = asyncio.Event()
    child_new = asyncio.create_task(_cancellation_resistant_child(stop_new))
    await asyncio.sleep(0)
    await adapter._stop_typing_refresh(
        "1", child_new, timeout=1.0, stop_event=stop_new
    )
    assert stop_new.is_set()
    assert child_new.done(), "fixed path must join the resistant child"


@pytest.mark.parametrize(
    "nodeid",
    [
        "tests/gateway/test_73771_media_resend_dedup.py::"
        "test_first_delivery_not_poisoned_by_current_turn_tool_output",
        "tests/gateway/test_delivery_ledger_producer.py::"
        "TestProducerHook::test_send_failure_leaves_failed_row",
    ],
)
def test_affected_gateway_node_exits_cleanly_under_faulthandler(nodeid: str):
    """Subprocess proof: PASSED alone is insufficient — process must exit 0."""
    env = os.environ.copy()
    env.setdefault("TZ", "UTC")
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("PYTHONHASHSEED", "0")
    for key in list(env):
        if key.endswith(
            (
                "_API_KEY",
                "_TOKEN",
                "_SECRET",
                "_PASSWORD",
                "_CREDENTIALS",
                "_ACCESS_KEY",
                "_PRIVATE_KEY",
                "_OAUTH_TOKEN",
                "_WEBHOOK_SECRET",
            )
        ):
            env.pop(key, None)

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-o",
        "addopts=",
        "-o",
        "faulthandler_timeout=20",
        "-o",
        "faulthandler_exit_on_timeout=true",
        "-q",
        "--tb=line",
        nodeid,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, (
        f"node {nodeid} did not exit cleanly rc={proc.returncode}\n"
        f"stdout_tail={proc.stdout[-1500:]}\nstderr_tail={proc.stderr[-1500:]}"
    )
    combined = proc.stdout + proc.stderr
    assert "passed" in combined
