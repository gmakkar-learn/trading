"""Daily OHLCV candle feed for US (yfinance) and India (yfinance .NS suffix).

Fetches `lookback_days` of daily candles per ticker and emits one CandleEvent per ticker.
The CandleEvent carries the full history so TechnicalStrategy can compute indicators
without making its own network calls.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from infrastructure.event_bus.events import CandleEvent

logger = logging.getLogger(__name__)

# India NSE tickers need a .NS suffix for yfinance
_INDIA_SUFFIX = ".NS"


class CandleFeed:
    """Fetches daily OHLCV history for a list of tickers via yfinance.

    US tickers: plain symbol (AAPL, MSFT).
    India tickers: symbol + .NS (RELIANCE.NS, TCS.NS).
    """

    def __init__(self, market_id: str, tickers: list[str], lookback_days: int = 210) -> None:
        self._market_id = market_id
        self._tickers = tickers
        self._lookback = lookback_days

    async def stream_events(
        self, tickers: list[str] | None = None
    ) -> AsyncIterator[CandleEvent]:
        tickers = tickers or self._tickers
        loop = asyncio.get_event_loop()
        for ticker in tickers:
            try:
                yf_symbol = f"{ticker}{_INDIA_SUFFIX}" if self._market_id == "india" else ticker
                df = await loop.run_in_executor(None, self._fetch, yf_symbol, self._lookback)
                if df is None or df.empty or len(df) < 10:
                    logger.warning("Insufficient candle data for %s (%s)", ticker, yf_symbol)
                    continue

                latest = df.iloc[-1]
                candles = [
                    {
                        "date": str(idx.date()),
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    }
                    for idx, row in df.iterrows()
                ]
                logger.info("CandleFeed: %s — %d candles, close=%.2f", ticker, len(candles), float(latest["Close"]))
                yield CandleEvent(
                    ticker=ticker,
                    market_id=self._market_id,
                    timeframe="1d",
                    open=float(latest["Open"]),
                    high=float(latest["High"]),
                    low=float(latest["Low"]),
                    close=float(latest["Close"]),
                    volume=float(latest["Volume"]),
                    candles=candles,
                )
            except Exception as exc:
                logger.error("CandleFeed error for %s: %s", ticker, exc)
            await asyncio.sleep(0.1)

    @staticmethod
    def _fetch(symbol: str, lookback_days: int):
        import yfinance as yf
        period = f"{lookback_days}d"
        df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
        return df
