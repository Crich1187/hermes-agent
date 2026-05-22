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
