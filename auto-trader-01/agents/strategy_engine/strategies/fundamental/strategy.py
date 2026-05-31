"""FundamentalStrategy: extracts earnings data from SEC 8-K press releases via Claude Sonnet."""
from __future__ import annotations
import logging
from datetime import datetime

from infrastructure.event_bus.events import AnnouncementEvent, DataEvent, TradingSignal
from agents.strategy_engine.base_strategy import BaseStrategy
from agents.strategy_engine.data_feeds.feed_types import DataFeedType
from agents.strategy_engine.strategy_context import StrategyContext
from .pdf_extractor import DocumentExtractor
from .claude_client import ClaudeClient
from . import composite_scorer
from .context_enricher import enrich

logger = logging.getLogger(__name__)


class FundamentalStrategy(BaseStrategy):
    strategy_id = "fundamental_v1"
    subscriptions = [DataFeedType.ANNOUNCEMENTS]

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._extractor = DocumentExtractor()
        self._claude = ClaudeClient()

    async def evaluate(self, event: DataEvent, context: StrategyContext) -> TradingSignal | None:
        if not isinstance(event, AnnouncementEvent):
            return None
        if event.filing_type not in ("8-K", "FinancialResults", "BoardMeeting"):
            return None
        # For 8-K, require item 2.02 (Results of Operations)
        if event.filing_type == "8-K" and "2.02" not in event.items:
            return None

        ticker = event.ticker
        quarter = _quarter_label(event.published_at)
        logger.info("Evaluating %s %s — %s", ticker, quarter, event.document_url)

        # 1. Extract text from press release
        try:
            text = await self._extractor.extract(event.document_url)
        except Exception as exc:
            logger.error("Extraction failed for %s: %s", ticker, exc)
            return None

        if not text or len(text) < 100:
            logger.warning("Insufficient text for %s (%d chars)", ticker, len(text) if text else 0)
            return None

        # 2. Claude extraction (filing_id enables analysis cache)
        try:
            result_doc = await self._claude.analyse(text, ticker, quarter, filing_id=event.filing_id)
        except Exception as exc:
            logger.error("Claude analysis failed for %s: %s", ticker, exc)
            return None

        # 3. Composite score
        scored = composite_scorer.score(result_doc, self.config)

        neutral_low = self.config.get("scoring", {}).get("thresholds", {}).get("neutral_low", 40)
        if scored.composite_score < neutral_low and scored.action != "SELL":
            logger.debug("%s score %.1f below neutral — no signal", ticker, scored.composite_score)
            return None

        # 4. Price context enrichment
        price_ctx = await enrich(ticker)

        return TradingSignal(
            ticker=ticker,
            market_id=event.market_id,
            strategy_type="fundamental",
            strategy_id=self.strategy_id,
            composite_score=scored.composite_score,
            recommended_action=scored.action,
            confidence=scored.confidence,
            rationale=result_doc.raw_claude_response[:2000],
            context={**scored.context, **price_ctx},
        )


def _quarter_label(dt: datetime) -> str:
    q = (dt.month - 1) // 3 + 1
    return f"Q{q} FY{dt.year}"
