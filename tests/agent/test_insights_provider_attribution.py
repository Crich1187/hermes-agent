"""Deterministic tests for /insights provider attribution (root-e4k).

Covers:
  - alias normalization (alibaba-coding-plan → alibaba, dashscope host → alibaba)
  - required providers always present
  - missing data vs zero spend status
  - time-window cutoff (sessions outside --days excluded)
  - format_gateway (AC slash-command surface) emits required Providers rows
"""

from __future__ import annotations

import time

import pytest

from agent.insights import (
    REQUIRED_INSIGHT_PROVIDERS,
    InsightsEngine,
    canonicalize_billing_provider,
    _provider_spend_status,
)
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "insights_providers.db")
    yield session_db
    session_db.close()


def test_canonicalize_billing_provider_aliases():
    assert canonicalize_billing_provider("zai", None) == "zai"
    assert canonicalize_billing_provider("ZAI", "https://api.z.ai/v1") == "zai"
    assert canonicalize_billing_provider(None, "https://api.z.ai/api") == "zai"

    assert canonicalize_billing_provider("alibaba-coding-plan", None) == "alibaba"
    assert (
        canonicalize_billing_provider(
            None, "https://coding-intl.dashscope.aliyuncs.com/v1"
        )
        == "alibaba"
    )
    assert canonicalize_billing_provider("alibaba", None) == "alibaba"

    assert canonicalize_billing_provider("anthropic", None) == "anthropic"
    assert (
        canonicalize_billing_provider(None, "https://api.anthropic.com")
        == "anthropic"
    )

    assert canonicalize_billing_provider("nous", None) == "nous"
    assert (
        canonicalize_billing_provider(
            None, "https://inference-api.nousresearch.com/v1"
        )
        == "nous"
    )

    assert canonicalize_billing_provider("", None) == "unknown"
    assert canonicalize_billing_provider(None, None) == "unknown"
    assert canonicalize_billing_provider("custom", "http://100.83.229.25:4000/v1") == "custom"


def test_provider_spend_status_missing_vs_zero():
    assert (
        _provider_spend_status(
            sessions=0, estimated_cost=0.0, included_sessions=0, unknown_sessions=0
        )
        == "missing"
    )
    assert (
        _provider_spend_status(
            sessions=3, estimated_cost=0.0, included_sessions=3, unknown_sessions=0
        )
        == "zero_spend"
    )
    assert (
        _provider_spend_status(
            sessions=2, estimated_cost=0.0, included_sessions=0, unknown_sessions=2
        )
        == "unknown_pricing"
    )
    assert (
        _provider_spend_status(
            sessions=1, estimated_cost=1.25, included_sessions=0, unknown_sessions=0
        )
        == "spend"
    )


def _seed_session(
    db: SessionDB,
    *,
    session_id: str,
    started_at: float,
    model: str,
    billing_provider: str | None,
    billing_base_url: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> None:
    db.create_session(session_id=session_id, source="cli", model=model, user_id="u1")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, billing_provider = ?, billing_base_url = ? WHERE id = ?",
        (started_at, billing_provider, billing_base_url, session_id),
    )
    db.update_token_counts(session_id, input_tokens=input_tokens, output_tokens=output_tokens)
    db.end_session(session_id, end_reason="user_exit")
    db._conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE id = ?",
        (started_at + 60, session_id),
    )
    db._conn.commit()


def test_required_providers_always_present_and_nous_missing(db):
    now = time.time()
    # zai + alibaba aliases + anthropic in window; nous absent
    _seed_session(
        db,
        session_id="z1",
        started_at=now - 86400,
        model="glm-5.1",
        billing_provider="zai",
        input_tokens=1000,
        output_tokens=100,
    )
    _seed_session(
        db,
        session_id="a1",
        started_at=now - 2 * 86400,
        model="qwen-plus",
        billing_provider="alibaba-coding-plan",
        input_tokens=2000,
        output_tokens=200,
    )
    _seed_session(
        db,
        session_id="c1",
        started_at=now - 3 * 86400,
        model="claude-sonnet-4",
        billing_provider="anthropic",
        input_tokens=500,
        output_tokens=50,
    )

    engine = InsightsEngine(db)
    report = engine.generate(days=30)
    by_name = {p["provider"]: p for p in report["providers"]}

    for name in REQUIRED_INSIGHT_PROVIDERS:
        assert name in by_name
        assert by_name[name]["required"] is True

    assert by_name["nous"]["status"] == "missing"
    assert by_name["nous"]["sessions"] == 0
    assert by_name["zai"]["sessions"] == 1
    assert by_name["alibaba"]["sessions"] == 1  # alias folded
    assert by_name["anthropic"]["sessions"] == 1

    text = engine.format_terminal(report)
    assert "Providers" in text or "🏷️" in text
    assert "nous" in text and "no data" in text
    assert "zai" in text
    assert "alibaba" in text
    assert "anthropic" in text


def test_format_gateway_emits_required_providers(db):
    """Gate3 Minor 1 — /insights uses format_gateway; must be load-bearing.

    Sabotage on the prior candidate proved gutting format_gateway's Providers
    block still left the suite green. This test exercises the AC surface.
    """
    now = time.time()
    _seed_session(
        db,
        session_id="gw-z",
        started_at=now - 86400,
        model="glm-5.1",
        billing_provider="zai",
        input_tokens=1000,
        output_tokens=100,
    )
    _seed_session(
        db,
        session_id="gw-a",
        started_at=now - 2 * 86400,
        model="qwen-plus",
        billing_provider="alibaba-coding-plan",
        input_tokens=2000,
        output_tokens=200,
    )
    _seed_session(
        db,
        session_id="gw-c",
        started_at=now - 3 * 86400,
        model="claude-sonnet-4",
        billing_provider="anthropic",
        input_tokens=500,
        output_tokens=50,
    )
    # Non-required custom with sessions should appear; zero-session noise must not
    # displace required rows.
    _seed_session(
        db,
        session_id="gw-custom",
        started_at=now - 4 * 86400,
        model="local-model",
        billing_provider="custom",
        input_tokens=10,
        output_tokens=5,
    )

    engine = InsightsEngine(db)
    report = engine.generate(days=30)
    text = engine.format_gateway(report)

    assert "Providers" in text or "🏷️" in text
    for name in REQUIRED_INSIGHT_PROVIDERS:
        assert name in text, f"required provider {name!r} missing from format_gateway"
    assert "no data" in text  # nous missing in this fixture
    assert "zai" in text
    assert "alibaba" in text
    assert "anthropic" in text
    assert "custom" in text


def test_time_window_excludes_old_sessions(db):
    now = time.time()
    # Inside 7-day window
    _seed_session(
        db,
        session_id="in",
        started_at=now - 2 * 86400,
        model="glm-5",
        billing_provider="zai",
    )
    # Outside 7-day window
    _seed_session(
        db,
        session_id="out",
        started_at=now - 20 * 86400,
        model="glm-5",
        billing_provider="zai",
        input_tokens=99999,
        output_tokens=99999,
    )

    engine = InsightsEngine(db)
    report = engine.generate(days=7)
    zai = next(p for p in report["providers"] if p["provider"] == "zai")
    assert zai["sessions"] == 1
    assert zai["total_tokens"] == 150  # 100+50 from fixture defaults


def test_nous_inferred_from_base_url(db):
    now = time.time()
    _seed_session(
        db,
        session_id="n1",
        started_at=now - 86400,
        model="hermes-3",
        billing_provider=None,
        billing_base_url="https://inference-api.nousresearch.com/v1",
        input_tokens=10,
        output_tokens=5,
    )
    engine = InsightsEngine(db)
    report = engine.generate(days=30)
    nous = next(p for p in report["providers"] if p["provider"] == "nous")
    assert nous["sessions"] == 1
    assert nous["status"] != "missing"
