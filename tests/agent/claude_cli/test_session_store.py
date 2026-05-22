"""Unit tests for agent.claude_cli.session_store."""

from __future__ import annotations


def test_module_is_importable():
    from agent.claude_cli import session_store  # noqa: F401


class TestSessionStoreCore:
    def test_get_or_create_unknown_returns_placeholder(self):
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore()
        claude_id, is_new = store.get_or_create("hermes-1")
        assert claude_id is None
        assert is_new is True

    def test_set_then_get_returns_existing(self):
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore()
        # First call registers the placeholder.
        store.get_or_create("hermes-1")
        # Adapter fills in the real claude session id once the stream yields it.
        store.set("hermes-1", "claude-session-abc")
        claude_id, is_new = store.get_or_create("hermes-1")
        assert claude_id == "claude-session-abc"
        assert is_new is False

    def test_set_without_prior_get_is_allowed(self):
        # The adapter may seed a mapping directly when resuming from a
        # persisted Hermes session.
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore()
        store.set("hermes-1", "claude-session-abc")
        claude_id, is_new = store.get_or_create("hermes-1")
        assert claude_id == "claude-session-abc"
        assert is_new is False

    def test_forget_drops_mapping(self):
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore()
        store.set("hermes-1", "claude-session-abc")
        store.forget("hermes-1")
        claude_id, is_new = store.get_or_create("hermes-1")
        assert claude_id is None
        assert is_new is True

    def test_forget_unknown_is_noop(self):
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore()
        # Must not raise.
        store.forget("never-seen")

    def test_independent_keys_do_not_collide(self):
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore()
        store.set("hermes-1", "claude-A")
        store.set("hermes-2", "claude-B")
        a_id, _ = store.get_or_create("hermes-1")
        b_id, _ = store.get_or_create("hermes-2")
        assert a_id == "claude-A"
        assert b_id == "claude-B"

    def test_set_overwrites_existing(self):
        # If a Hermes session is re-bound to a fresh claude session id,
        # later set() wins.
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore()
        store.set("hermes-1", "claude-A")
        store.set("hermes-1", "claude-B")
        claude_id, is_new = store.get_or_create("hermes-1")
        assert claude_id == "claude-B"
        assert is_new is False


class TestSessionStoreTtl:
    def test_evict_expired_removes_past_ttl_entries(self):
        from agent.claude_cli.session_store import SessionStore
        import time

        store = SessionStore(ttl_seconds=60.0)
        # Seed two entries at known monotonic instants by snapshotting now.
        t0 = time.monotonic()
        store.set("old", "claude-old")
        store.set("fresh", "claude-fresh")
        # Pretend t0+120s has arrived; old TTL=60s should be evicted.
        evicted = store.evict_expired(now=t0 + 120.0)
        # Both entries were inserted at ~t0; both are >60s old at t0+120.
        assert evicted == 2

    def test_evict_expired_keeps_current_entries(self):
        from agent.claude_cli.session_store import SessionStore
        import time

        store = SessionStore(ttl_seconds=60.0)
        t0 = time.monotonic()
        store.set("a", "claude-a")
        store.set("b", "claude-b")
        # t0+30s: nothing past TTL.
        evicted = store.evict_expired(now=t0 + 30.0)
        assert evicted == 0
        claude_a, is_new_a = store.get_or_create("a")
        assert claude_a == "claude-a"
        assert is_new_a is False

    def test_evict_expired_partial(self):
        # Two entries with different inserted_at; only the older expires.
        # Use internal _entries access to construct without time.sleep.
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore(ttl_seconds=60.0)
        store.set("old", "claude-old")
        t_old = store._entries["old"].inserted_at
        store._entries["fresh"] = type(store._entries["old"])(
            claude_session_id="claude-fresh",
            inserted_at=t_old + 40.0,
        )
        # At t_old + 90: "old" is 90s old (evict), "fresh" is 50s old (keep).
        evicted = store.evict_expired(now=t_old + 90.0)
        assert evicted == 1
        # "fresh" survives (assert this BEFORE touching "old").
        claude_fresh, is_new_fresh = store.get_or_create("fresh")
        assert claude_fresh == "claude-fresh"
        assert is_new_fresh is False
        # "old" is gone — get_or_create returns a fresh placeholder.
        claude_old, is_new_old = store.get_or_create("old")
        assert claude_old is None
        assert is_new_old is True

    def test_evict_expired_returns_zero_on_empty_store(self):
        from agent.claude_cli.session_store import SessionStore

        store = SessionStore(ttl_seconds=60.0)
        assert store.evict_expired() == 0

    def test_evict_expired_uses_time_monotonic_by_default(self):
        from agent.claude_cli.session_store import SessionStore

        # ttl_seconds=0.01 → after a tiny real sleep, the entry should be
        # evictable without passing ``now``.
        import time as _time

        store = SessionStore(ttl_seconds=0.01)
        store.set("x", "claude-x")
        _time.sleep(0.05)
        assert store.evict_expired() == 1

    def test_get_or_create_re_registers_after_expiry(self):
        from agent.claude_cli.session_store import SessionStore
        import time

        store = SessionStore(ttl_seconds=60.0)
        t0 = time.monotonic()
        store.set("x", "claude-x")
        # Without calling evict_expired, ``get_or_create`` past TTL must still
        # behave as if there were no mapping (returns placeholder).
        # We exercise this by re-binding inserted_at directly:
        store._entries["x"].inserted_at = t0 - 3600.0
        claude_id, is_new = store.get_or_create("x")
        assert claude_id is None
        assert is_new is True

    def test_ttl_seconds_must_be_positive(self):
        from agent.claude_cli.session_store import SessionStore
        import pytest

        with pytest.raises(ValueError, match="positive"):
            SessionStore(ttl_seconds=0.0)
        with pytest.raises(ValueError, match="positive"):
            SessionStore(ttl_seconds=-1.0)
