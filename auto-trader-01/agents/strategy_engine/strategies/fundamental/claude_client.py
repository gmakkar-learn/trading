"""Claude Sonnet client for earnings press release extraction."""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path

import anthropic

from .result_document import ResultDocument
from infrastructure.cache.analysis_cache import AnalysisCache

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "result_extraction.txt"
_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048


class ClaudeClient:
    def __init__(self, api_key: str | None = None, cache_dir: str = ".cache/analyses") -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._system_prompt = _PROMPT_PATH.read_text()
        # Analysis cache — keyed by filing_id, auto-invalidates when prompt changes
        self._cache = AnalysisCache(cache_dir=cache_dir, prompt_text=self._system_prompt)

    async def analyse(
        self, text: str, ticker: str, quarter: str, filing_id: str | None = None
    ) -> ResultDocument:
        """Send press release text to Claude and return structured ResultDocument.

        Results are cached by filing_id so repeat backtester runs skip the API call.
        """
        if filing_id:
            cached_raw = self._cache.get(filing_id)
            if cached_raw is not None:
                data = self._parse_json(cached_raw, ticker, quarter)
                result = ResultDocument.from_claude_response(data, ticker, quarter)
                result.raw_claude_response = cached_raw
                return result

        # Ticker and quarter go in the user message so the system prompt stays
        # identical across calls, enabling Anthropic server-side prompt caching.
        user_content = f"Company: {ticker}\nReported period: {quarter}\n\n{text}"

        message = await self._client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            system=[{
                "type": "text",
                "text": self._system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text

        if filing_id:
            self._cache.put(filing_id, raw)

        data = self._parse_json(raw, ticker, quarter)
        result = ResultDocument.from_claude_response(data, ticker, quarter)
        result.raw_claude_response = raw
        return result

    def _parse_json(self, raw: str, ticker: str, quarter: str) -> dict:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.error("Claude returned unparseable JSON for %s %s:\n%s", ticker, quarter, raw[:300])
        return {}
