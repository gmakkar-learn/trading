"""TechnicalStrategy: SMA crossover + RSI + MACD composite scorer.

Subscribes to OHLCV_CANDLES. Receives a CandleEvent with full OHLCV history,
computes three indicators via pandas-ta, and produces a weighted composite score.

Disabled by default (enabled: false in active.yaml). Enable in Phase 4.
"""
from __future__ import annotations

import logging

import pandas as pd
import pandas_ta as ta

from infrastructure.event_bus.events import CandleEvent, DataEvent, TradingSignal
from agents.strategy_engine.base_strategy import BaseStrategy
from agents.strategy_engine.data_feeds.feed_types import DataFeedType
from agents.strategy_engine.strategy_context import StrategyContext

logger = logging.getLogger(__name__)


class TechnicalStrategy(BaseStrategy):
    strategy_id = "technical_v1"
    subscriptions = [DataFeedType.OHLCV_CANDLES]

    async def evaluate(self, event: DataEvent, context: StrategyContext) -> TradingSignal | None:
        if not isinstance(event, CandleEvent):
            return None
        if len(event.candles) < 210:
            logger.debug("%s: only %d candles, need 210 for SMA200", event.ticker, len(event.candles))
            return None

        df = _to_dataframe(event.candles)
        cfg = self.config.get("indicators", {})

        sma_score, sma_detail = _score_sma(df, cfg.get("sma_crossover", {}))
        rsi_score, rsi_detail = _score_rsi(df, cfg.get("rsi", {}))
        macd_score, macd_detail = _score_macd(df, cfg.get("macd", {}))

        weights = cfg.get("weights", {"sma": 0.40, "rsi": 0.30, "macd": 0.30})
        composite = (
            sma_score  * weights.get("sma",  0.40) +
            rsi_score  * weights.get("rsi",  0.30) +
            macd_score * weights.get("macd", 0.30)
        )

        thresholds = self.config.get("thresholds", {})
        buy_threshold  = thresholds.get("buy",  65.0)
        sell_threshold = thresholds.get("sell", 35.0)

        if composite >= buy_threshold:
            action = "BUY"
        elif composite <= sell_threshold:
            action = "SELL"
        else:
            return None  # HOLD — no signal worth routing

        if composite >= 80 or composite <= 20:
            confidence = "high"
        elif composite >= 70 or composite <= 30:
            confidence = "medium"
        else:
            confidence = "low"

        rationale = (
            f"SMA50/200={sma_detail} (score={sma_score:.0f}) | "
            f"RSI={rsi_detail} (score={rsi_score:.0f}) | "
            f"MACD={macd_detail} (score={macd_score:.0f}) | "
            f"composite={composite:.1f}"
        )

        logger.info(
            "TechnicalStrategy signal: %s %s score=%.1f — %s",
            event.ticker, action, composite, rationale,
        )

        return TradingSignal(
            ticker=event.ticker,
            market_id=event.market_id,
            strategy_type="technical",
            strategy_id=self.strategy_id,
            composite_score=round(composite, 1),
            recommended_action=action,
            confidence=confidence,
            rationale=rationale,
            context={
                "sma_score": sma_score,
                "rsi_score": rsi_score,
                "macd_score": macd_score,
                "sma_detail": sma_detail,
                "rsi_detail": rsi_detail,
                "macd_detail": macd_detail,
                "close": event.close,
                "candle_count": len(event.candles),
            },
        )


# ── Indicator scorers ─────────────────────────────────────────────────────────

def _to_dataframe(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["close"] = df["close"].astype(float)
    df["open"]  = df["open"].astype(float)
    df["high"]  = df["high"].astype(float)
    df["low"]   = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df


def _score_sma(df: pd.DataFrame, cfg: dict) -> tuple[float, str]:
    fast = cfg.get("fast", 50)
    slow = cfg.get("slow", 200)
    close = df["close"]

    sma_fast = ta.sma(close, length=fast)
    sma_slow = ta.sma(close, length=slow)
    if sma_fast is None or sma_slow is None:
        return 50.0, "N/A"

    f_now  = float(sma_fast.iloc[-1])
    s_now  = float(sma_slow.iloc[-1])
    f_prev = float(sma_fast.iloc[-4])  # 3 candles back to detect recent cross
    s_prev = float(sma_slow.iloc[-4])

    detail = f"SMA{fast}={f_now:.2f}/SMA{slow}={s_now:.2f}"
    pct_gap = (f_now - s_now) / s_now * 100

    # Golden cross (recent)
    if f_prev <= s_prev and f_now > s_now:
        return 100.0, f"golden_cross {detail}"
    # Death cross (recent)
    if f_prev >= s_prev and f_now < s_now:
        return 0.0, f"death_cross {detail}"
    # Trending above
    if f_now > s_now:
        return 70.0 if pct_gap > 2 else 58.0, f"above {detail}"
    # Trending below
    return 30.0 if pct_gap < -2 else 42.0, f"below {detail}"


def _score_rsi(df: pd.DataFrame, cfg: dict) -> tuple[float, str]:
    period    = cfg.get("period", 14)
    oversold  = cfg.get("oversold",  30)
    overbought = cfg.get("overbought", 70)

    rsi = ta.rsi(df["close"], length=period)
    if rsi is None or rsi.dropna().empty:
        return 50.0, "N/A"

    val = float(rsi.dropna().iloc[-1])
    detail = f"{val:.1f}"

    if val < 25:
        return 90.0, detail
    if val < oversold:
        return 75.0, detail
    if val < 45:
        return 62.0, detail
    if val < 55:
        return 50.0, detail
    if val < overbought:
        return 38.0, detail
    if val < 80:
        return 20.0, detail
    return 10.0, detail


def _score_macd(df: pd.DataFrame, cfg: dict) -> tuple[float, str]:
    fast   = cfg.get("fast",   12)
    slow   = cfg.get("slow",   26)
    signal = cfg.get("signal",  9)

    macd_df = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
    if macd_df is None or macd_df.empty:
        return 50.0, "N/A"

    macd_col   = f"MACD_{fast}_{slow}_{signal}"
    signal_col = f"MACDs_{fast}_{slow}_{signal}"

    if macd_col not in macd_df.columns or signal_col not in macd_df.columns:
        return 50.0, "N/A"

    macd_line   = macd_df[macd_col].dropna()
    signal_line = macd_df[signal_col].dropna()
    if len(macd_line) < 3:
        return 50.0, "N/A"

    m_now  = float(macd_line.iloc[-1])
    s_now  = float(signal_line.iloc[-1])
    m_prev = float(macd_line.iloc[-3])
    s_prev = float(signal_line.iloc[-3])

    detail = f"MACD={m_now:.3f}/sig={s_now:.3f}"

    # Bullish crossover (recent)
    if m_prev <= s_prev and m_now > s_now:
        return 90.0, f"bullish_cross {detail}"
    # Bearish crossover (recent)
    if m_prev >= s_prev and m_now < s_now:
        return 10.0, f"bearish_cross {detail}"
    # MACD above signal
    if m_now > s_now:
        return 65.0, f"above {detail}"
    return 35.0, f"below {detail}"
