"""HybridStrategy: BUY only when fundamental AND technical confirm.

Subscribes to OHLCV_CANDLES. On each candle event:
  1. Checks signal_cache for a recent fundamental BUY for this ticker.
  2. If found with score >= fundamental_min_score, evaluates SMA/RSI/MACD.
  3. If technical composite >= technical_min_score, emits a hybrid TradingSignal
     with a weighted combination of both scores.

The fundamental signal is cached by the engine when FundamentalStrategy fires.
There is no extra infrastructure — HybridStrategy is purely a reader/combiner.
"""
from __future__ import annotations

import logging
from datetime import datetime

from agents.strategy_engine.base_strategy import BaseStrategy
from agents.strategy_engine.data_feeds.feed_types import DataFeedType
from agents.strategy_engine.strategy_context import StrategyContext
from agents.strategy_engine.strategies.technical.strategy import (
    _score_macd, _score_rsi, _score_sma, _to_dataframe,
)
from infrastructure.event_bus.events import CandleEvent, DataEvent, TradingSignal

logger = logging.getLogger(__name__)


class HybridStrategy(BaseStrategy):
    strategy_id = "hybrid_v1"
    subscriptions = [DataFeedType.OHLCV_CANDLES]

    async def evaluate(self, event: DataEvent, context: StrategyContext) -> TradingSignal | None:
        if not isinstance(event, CandleEvent):
            return None
        if context.signal_cache is None:
            return None

        # Market-specific overrides (e.g. india disables the technical gate)
        market_cfg        = self.config.get("market_overrides", {}).get(event.market_id, {})
        tech_gate_enabled = bool(market_cfg.get("technical_gate_enabled", True))

        # ── 1. Fundamental gate ───────────────────────────────────────────────
        fund_id    = self.config.get("fundamental_strategy_id", "fundamental_v1")
        fund_signal = context.signal_cache.get(event.ticker, fund_id)
        if fund_signal is None or fund_signal.recommended_action != "BUY":
            return None

        ttl_hours = float(self.config.get("fundamental_ttl_hours", 168.0))
        age_secs  = (datetime.utcnow() - fund_signal.created_at).total_seconds()
        if age_secs > ttl_hours * 3600:
            logger.debug(
                "Hybrid %s: fundamental signal age %.0fh > %.0fh TTL — skip",
                event.ticker, age_secs / 3600, ttl_hours,
            )
            return None

        thresholds = self.config.get("thresholds", {})
        fund_min   = float(
            market_cfg.get("fundamental_min_score",
                           thresholds.get("fundamental_min_score", 70.0))
        )
        if fund_signal.composite_score < fund_min:
            logger.debug(
                "Hybrid %s: fundamental %.1f < min %.1f — skip",
                event.ticker, fund_signal.composite_score, fund_min,
            )
            return None

        # ── 2. Technical gate (skipped when market_overrides disables it) ─────
        if tech_gate_enabled:
            if len(event.candles) < 210:
                logger.debug("Hybrid %s: only %d candles (need 210) — skip", event.ticker, len(event.candles))
                return None

            df      = _to_dataframe(event.candles)
            ind_cfg = self.config.get("indicators", {})
            w       = ind_cfg.get("weights", {"sma": 0.40, "rsi": 0.30, "macd": 0.30})

            sma_score,  sma_detail  = _score_sma (df, ind_cfg.get("sma_crossover", {}))
            rsi_score,  rsi_detail  = _score_rsi (df, ind_cfg.get("rsi",           {}))
            macd_score, macd_detail = _score_macd(df, ind_cfg.get("macd",          {}))

            tech_score = (
                sma_score  * w.get("sma",  0.40) +
                rsi_score  * w.get("rsi",  0.30) +
                macd_score * w.get("macd", 0.30)
            )

            tech_min = float(thresholds.get("technical_min_score", 60.0))
            if tech_score < tech_min:
                logger.debug(
                    "Hybrid %s: technical %.1f < min %.1f (SMA=%s RSI=%s MACD=%s) — skip",
                    event.ticker, tech_score, tech_min, sma_detail, rsi_detail, macd_detail,
                )
                return None

            # ── 3. Combined score ─────────────────────────────────────────────
            wts          = self.config.get("weights", {})
            fw           = float(wts.get("fundamental", 0.60))
            tw           = float(wts.get("technical",   0.40))
            combined     = fund_signal.composite_score * fw + tech_score * tw
            combined_min = float(thresholds.get("combined_buy", 65.0))

            if combined < combined_min:
                logger.debug("Hybrid %s: combined %.1f < min %.1f — skip", event.ticker, combined, combined_min)
                return None

            signal_context = {
                "fundamental_score":     fund_signal.composite_score,
                "fundamental_signal_id": fund_signal.signal_id,
                "technical_score":       round(tech_score, 1),
                "sma_score":             sma_score,
                "rsi_score":             rsi_score,
                "macd_score":            macd_score,
                "last_price":            event.close,
                "technical_gate":        "enabled",
            }
            rationale = (
                f"fundamental={fund_signal.composite_score:.0f} ({fund_signal.confidence}) | "
                f"technical: SMA={sma_detail}, RSI={rsi_detail}, MACD={macd_detail}, "
                f"score={tech_score:.0f} | combined={combined:.1f}"
            )
            logger.info(
                "HybridStrategy BUY: %s  combined=%.1f  fundamental=%.1f  technical=%.1f",
                event.ticker, combined, fund_signal.composite_score, tech_score,
            )

        else:
            # Technical gate disabled for this market — fundamental score is the combined score.
            combined     = fund_signal.composite_score
            combined_min = float(market_cfg.get("combined_buy", thresholds.get("combined_buy", 65.0)))

            if combined < combined_min:
                logger.debug("Hybrid %s: fundamental %.1f < combined_buy %.1f — skip", event.ticker, combined, combined_min)
                return None

            signal_context = {
                "fundamental_score":     fund_signal.composite_score,
                "fundamental_signal_id": fund_signal.signal_id,
                "last_price":            event.close,
                "technical_gate":        "disabled",
            }
            rationale = (
                f"fundamental={fund_signal.composite_score:.0f} ({fund_signal.confidence}) | "
                f"technical gate disabled for market={event.market_id} | score={combined:.1f}"
            )
            logger.info(
                "HybridStrategy BUY (tech gate off): %s  fundamental=%.1f  market=%s",
                event.ticker, fund_signal.composite_score, event.market_id,
            )

        confidence = "high" if combined >= 80 else "medium" if combined >= 70 else "low"

        return TradingSignal(
            ticker=event.ticker,
            market_id=event.market_id,
            strategy_type="hybrid",
            strategy_id=self.strategy_id,
            composite_score=round(combined, 1),
            recommended_action="BUY",
            confidence=confidence,
            rationale=rationale,
            context=signal_context,
        )
