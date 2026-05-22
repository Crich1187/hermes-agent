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
