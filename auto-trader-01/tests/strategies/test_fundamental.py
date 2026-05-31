"""Tests for FundamentalStrategy scoring and signal generation."""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, patch

from agents.strategy_engine.strategies.fundamental.composite_scorer import score, ScoredResult
from agents.strategy_engine.strategies.fundamental.result_document import (
    ResultDocument, RevenueData, EarningsData, MarginData,
    GuidanceData, DividendData, ExceptionalItems,
)
from agents.strategy_engine.data_feeds.feed_types import DataFeedType
from agents.strategy_engine.strategies.fundamental.strategy import FundamentalStrategy


# ---------- helpers ----------

def _make_result(
    revenue_yoy: float | None = None,
    eps_yoy: float | None = None,
    margin_dir: str | None = None,
    guidance_provided: bool = False,
    guidance_dir: str | None = None,
    dividend_declared: bool = False,
    dividend_change: str | None = None,
    exceptional: bool = False,
    confidence: str = "medium",
) -> ResultDocument:
    return ResultDocument(
        ticker="AAPL",
        quarter="Q1 FY2024",
        revenue=RevenueData(yoy_growth_pct=revenue_yoy),
        earnings=EarningsData(eps_yoy_growth_pct=eps_yoy),
        margins=MarginData(operating_margin_direction=margin_dir),
        guidance=GuidanceData(provided=guidance_provided, direction=guidance_dir),
        dividend=DividendData(declared=dividend_declared, change=dividend_change),
        exceptional_items=ExceptionalItems(present=exceptional),
        confidence=confidence,
    )


def _config() -> dict:
    return {
        "scoring": {
            "weights": {
                "pat_beat": 0.30,
                "revenue_beat": 0.20,
                "margin_direction": 0.20,
                "guidance_change": 0.15,
                "dividend_signal": 0.10,
                "exceptional_penalty": -0.15,
            },
            "thresholds": {
                "strong_buy": 75,
                "moderate_buy": 60,
                "neutral_low": 40,
            },
        }
    }


def _strategy_config() -> dict:
    cfg = _config()
    cfg["scoring"]["thresholds"]["neutral_low"] = 40
    return cfg


# ---------- composite_scorer tests ----------

def test_score_strong_buy():
    result = _make_result(
        revenue_yoy=12.0,
        eps_yoy=22.0,
        margin_dir="expanding",
        guidance_provided=True,
        guidance_dir="raised",
        dividend_declared=True,
        dividend_change="increased",
        exceptional=False,
        confidence="high",
    )
    scored = score(result, _config())
    assert scored.composite_score >= 75
    assert scored.action == "BUY"


def test_score_below_threshold_gives_sell():
    result = _make_result(
        revenue_yoy=-8.0,
        eps_yoy=-15.0,
        margin_dir="contracting",
        guidance_provided=True,
        guidance_dir="cut",
        confidence="medium",
    )
    scored = score(result, _config())
    assert scored.composite_score < 40
    assert scored.action == "SELL"


def test_score_exceptional_items_reduces_score():
    clean = _make_result(revenue_yoy=10.0, eps_yoy=12.0, margin_dir="expanding")
    exceptional = _make_result(revenue_yoy=10.0, eps_yoy=12.0, margin_dir="expanding", exceptional=True)
    config = _config()
    assert score(exceptional, config).composite_score < score(clean, config).composite_score


def test_score_null_fields_handled():
    result = _make_result()  # all None
    scored = score(result, _config())
    assert 0.0 <= scored.composite_score <= 100.0
    assert scored.action in ("BUY", "SELL", "HOLD")


def test_score_guidance_not_included_when_not_provided():
    without = _make_result(revenue_yoy=5.0, eps_yoy=5.0, guidance_provided=False)
    with_good = _make_result(revenue_yoy=5.0, eps_yoy=5.0, guidance_provided=True, guidance_dir="raised")
    config = _config()
    # Good guidance should push score higher than no guidance
    assert score(with_good, config).composite_score > score(without, config).composite_score


# ---------- strategy subscription / id tests ----------

def test_strategy_subscribes_to_announcements():
    strategy = FundamentalStrategy(_strategy_config())
    assert DataFeedType.ANNOUNCEMENTS in strategy.subscriptions


def test_strategy_id():
    strategy = FundamentalStrategy(_strategy_config())
    assert strategy.strategy_id == "fundamental_v1"


# ---------- integration: evaluate with mocked Claude ----------

def _strong_result_doc(ticker: str = "AAPL") -> ResultDocument:
    return ResultDocument(
        ticker=ticker,
        quarter="Q1 FY2024",
        revenue=RevenueData(actual_millions=119_600.0, yoy_growth_pct=12.0),
        earnings=EarningsData(eps_actual=2.18, eps_yoy_growth_pct=16.0),
        margins=MarginData(operating_margin_direction="expanding"),
        guidance=GuidanceData(provided=True, direction="raised"),
        dividend=DividendData(declared=False),
        exceptional_items=ExceptionalItems(present=False),
        confidence="high",
        notes="Strong Q1 beat",
        raw_claude_response='{"ticker": "AAPL"}',
    )


def _weak_result_doc(ticker: str = "AAPL") -> ResultDocument:
    return ResultDocument(
        ticker=ticker,
        quarter="Q1 FY2024",
        revenue=RevenueData(yoy_growth_pct=-5.0),
        earnings=EarningsData(eps_yoy_growth_pct=-12.0),
        margins=MarginData(operating_margin_direction="contracting"),
        guidance=GuidanceData(provided=True, direction="cut"),
        dividend=DividendData(declared=False),
        exceptional_items=ExceptionalItems(present=False),
        confidence="medium",
        notes="Missed all metrics",
        raw_claude_response='{"ticker": "AAPL"}',
    )


@pytest.mark.asyncio
async def test_evaluate_returns_buy_signal_on_strong_result(
    sample_announcement_event, strategy_context
):
    strategy = FundamentalStrategy(_strategy_config())

    with patch.object(strategy._extractor, "extract", new_callable=AsyncMock, return_value="Earnings press release text. " * 10), \
         patch.object(strategy._claude, "analyse", new_callable=AsyncMock, return_value=_strong_result_doc()), \
         patch(
             "agents.strategy_engine.strategies.fundamental.strategy.enrich",
             new_callable=AsyncMock,
             return_value={"market_cap": 3_000_000_000_000},
         ):
        signal = await strategy.evaluate(sample_announcement_event, strategy_context)

    assert signal is not None
    assert signal.ticker == "AAPL"
    assert signal.recommended_action == "BUY"
    assert signal.composite_score >= 60


@pytest.mark.asyncio
async def test_evaluate_returns_none_below_neutral(
    sample_announcement_event, strategy_context
):
    strategy = FundamentalStrategy(_strategy_config())

    with patch.object(strategy._extractor, "extract", new_callable=AsyncMock, return_value="Weak earnings text"), \
         patch.object(strategy._claude, "analyse", new_callable=AsyncMock, return_value=_weak_result_doc()), \
         patch(
             "agents.strategy_engine.strategies.fundamental.strategy.enrich",
             new_callable=AsyncMock,
             return_value={},
         ):
        signal = await strategy.evaluate(sample_announcement_event, strategy_context)

    # Score will be below neutral_low=40 → strategy returns None
    assert signal is None


@pytest.mark.asyncio
async def test_evaluate_skips_non_202_filings(sample_announcement_event, strategy_context):
    strategy = FundamentalStrategy(_strategy_config())
    sample_announcement_event.items = ["8.01"]  # no 2.02
    signal = await strategy.evaluate(sample_announcement_event, strategy_context)
    assert signal is None


@pytest.mark.asyncio
async def test_evaluate_returns_none_on_extraction_failure(
    sample_announcement_event, strategy_context
):
    strategy = FundamentalStrategy(_strategy_config())

    with patch.object(strategy._extractor, "extract", new_callable=AsyncMock, side_effect=Exception("network error")):
        signal = await strategy.evaluate(sample_announcement_event, strategy_context)

    assert signal is None
