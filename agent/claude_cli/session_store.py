"""In-memory ``hermes_session_id -> claude_session_id`` map with TTL eviction.

PR 3 of the Hermes Claude Code CLI adapter. Provides ``SessionStore``,
used by the PR 4 adapter to remember which ``claude --resume <id>`` to
pass for a given Hermes session. v1 is **in-memory only**; persistence to
Hermes' session storage is a follow-up (PR 4 or later) per the design
spec lines 250-257.

The TTL clock uses ``time.monotonic()``; ``evict_expired()`` accepts an
injectable ``now`` so tests do not need to mock the clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class _Entry:
    """Internal record for a single hermes->claude session binding.

    ``claude_session_id`` is ``None`` while a placeholder is held between
    ``get_or_create()`` and the adapter's first ``set()`` (the moment the
    first stream-json event with the real claude session id arrives).
    ``inserted_at`` is a monotonic timestamp used by TTL eviction.
    """

    claude_session_id: Optional[str]
    inserted_at: float


class SessionStore:
    """In-memory ``hermes_session_id -> claude_session_id`` map with TTL.

    v1 is in-memory only; persistence to Hermes' session storage is a
    follow-up (PR 4 or later) per design spec lines 250-257.

    Thread/coroutine safety: the operations on a single store are not
    re-entrant; the PR 4 adapter calls them from a per-hermes-session
    mutex, so no internal locking is required in v1.
    """

    def __init__(self, *, ttl_seconds: float = 24 * 3600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, _Entry] = {}

    def get_or_create(self, hermes_session_id: str) -> tuple[Optional[str], bool]:
        """Return ``(claude_session_id, is_new)`` for a Hermes session.

        - Existing non-expired mapping → ``(claude_id, False)``.
        - No mapping (or expired): register a placeholder entry and return
          ``(None, True)``. The caller (adapter) must call ``set()`` after
          the first claude stream event arrives with the real id.
        """
        now = time.monotonic()
        entry = self._entries.get(hermes_session_id)
        if entry is not None and (now - entry.inserted_at) < self._ttl_seconds:
            return entry.claude_session_id, False
        # No entry, or entry past TTL: register a fresh placeholder.
        self._entries[hermes_session_id] = _Entry(
            claude_session_id=None,
            inserted_at=now,
        )
        return None, True

    def set(self, hermes_session_id: str, claude_session_id: str) -> None:
        """Bind ``hermes_session_id`` to ``claude_session_id`` (overwrites)."""
        self._entries[hermes_session_id] = _Entry(
            claude_session_id=claude_session_id,
            inserted_at=time.monotonic(),
        )

    def forget(self, hermes_session_id: str) -> None:
        """Drop the mapping for ``hermes_session_id`` if present (no-op otherwise)."""
        self._entries.pop(hermes_session_id, None)

    def evict_expired(self, *, now: Optional[float] = None) -> int:
        """Remove entries whose age exceeds ``ttl_seconds``.

        Args:
            now: Monotonic-clock instant in seconds. Defaults to
                ``time.monotonic()``; tests may pass an explicit value to
                avoid sleeping for the TTL duration.

        Returns:
            The number of entries removed.
        """
        if now is None:
            now = time.monotonic()
        ttl = self._ttl_seconds
        expired_keys = [
            key
            for key, entry in self._entries.items()
            if (now - entry.inserted_at) >= ttl
        ]
        for key in expired_keys:
            del self._entries[key]
        return len(expired_keys)
