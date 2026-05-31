"""Extracts plain text from SEC EDGAR press releases (HTML or PDF)."""
from __future__ import annotations
import logging
import os
from io import BytesIO

import httpx

from infrastructure.cache.document_cache import DocumentCache

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 60_000  # guard against very long documents


class DocumentExtractor:
    def __init__(self, user_agent: str | None = None, cache_dir: str = ".cache/docs") -> None:
        ua = user_agent or os.environ.get("SEC_EDGAR_USER_AGENT", "auto-trader-01 contact@example.com")
        self._headers = {"User-Agent": ua, "Host": "www.sec.gov"}
        self._cache = DocumentCache(cache_dir)

    async def extract(self, url: str) -> str:
        """Fetch and return plain text from the document URL (cached)."""
        cached = self._cache.get(url)
        if cached is not None:
            return cached

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url, headers=self._headers)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            content = resp.content
            text_content = resp.text

        if "pdf" in content_type or url.lower().endswith(".pdf"):
            result = self._extract_pdf(content)
        else:
            result = self._extract_html(text_content)

        self._cache.put(url, result)
        return result

    def _extract_html(self, html: str) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "meta", "link"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)[:MAX_TEXT_CHARS]

    def _extract_pdf(self, content: bytes) -> str:
        import pdfplumber
        pages: list[str] = []
        with pdfplumber.open(BytesIO(content)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
        return "\n".join(pages)[:MAX_TEXT_CHARS]
