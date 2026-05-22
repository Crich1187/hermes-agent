"""Conftest for agent.claude_cli tests.

Captures CLAUDE_CODE_OAUTH_TOKEN before the main conftest's hermetic
environment fixture strips it, so integration tests can restore it.
"""

import os
import pytest


# Module-level backup set by pytest_configure hook
_ORIGINAL_CLAUDE_TOKEN = None


def pytest_configure(config):
    """Capture the original CLAUDE_CODE_OAUTH_TOKEN at pytest startup.

    This runs before any fixtures, including the hermetic_environment fixture
    that deletes it. We store it so integration tests can restore it.
    """
    global _ORIGINAL_CLAUDE_TOKEN
    _ORIGINAL_CLAUDE_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    # Also store in os module namespace for test access
    os.HERMES_CLAUDE_TOKEN_BACKUP = _ORIGINAL_CLAUDE_TOKEN
