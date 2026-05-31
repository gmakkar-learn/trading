# Auto Trader 01 — Optimization Reference

This document describes all performance, cost, and latency optimizations in the system,
both those already implemented and those identified for future implementation.

---

## Table of Contents

1. [Context: Where Time and Money Go](#1-context-where-time-and-money-go)
2. [Implemented Optimizations](#2-implemented-optimizations)
   - [2.1 Document Fetch Cache](#21-document-fetch-cache)
   - [2.2 Claude Analysis Cache](#22-claude-analysis-cache)
   - [2.3 Anthropic Server-Side Prompt Caching](#23-anthropic-server-side-prompt-caching)
   - [2.4 Concurrent Filing Evaluation](#24-concurrent-filing-evaluation)
   - [2.5 Deterministic Extraction (temperature=0)](#25-deterministic-extraction-temperature0)
3. [Future Optimizations](#3-future-optimizations)
   - [3.1 Cache SEC EDGAR Filing Discovery](#31-cache-sec-edgar-filing-discovery)
   - [3.2 Cache yfinance Context Enrichment](#32-cache-yfinance-context-enrichment)
   - [3.3 Smarter Document Truncation](#33-smarter-document-truncation)
   - [3.4 Use Haiku for Extraction, Sonnet for Verification](#34-use-haiku-for-extraction-sonnet-for-verification)
   - [3.5 Anthropic Batch API for Backtesting](#35-anthropic-batch-api-for-backtesting)
   - [3.6 Parallel Ticker Fetching in AnnouncementFeed](#36-parallel-ticker-fetching-in-announcementfeed)
   - [3.7 Incremental Backtesting](#37-incremental-backtesting)
   - [3.8 Database-Backed Cache](#38-database-backed-cache)
   - [3.9 Streaming Claude Responses](#39-streaming-claude-responses)
   - [3.10 Token Budget via Extended Thinking](#310-token-budget-via-extended-thinking)
4. [Observed Timings](#4-observed-timings)

---

## 1. Context: Where Time and Money Go

A single backtester run over 3 quarters (44 filings, 10 tickers) involves:

| Step | Cost driver | Latency driver |
|---|---|---|
| SEC EDGAR CIK lookup | None (free) | 1 HTTP call × ~200ms |
| SEC submissions API | None (free) | 10 calls × ~500ms, rate-limited |
| Filing index HTML (EX-99.1 discovery) | None (free) | 44 calls × ~500ms, rate-limited |
| Document fetch (press release HTML/PDF) | None (free) | 44 calls × ~1–3s |
| Claude extraction | ~$0.003–0.006 per call (Sonnet 4.6) | 44 calls × ~3–8s per call |
| yfinance context enrichment | None (free) | ~30 calls × ~1s |
| yfinance price fetch (with `--no-prices` off) | None (free) | ~30 calls × ~2s |

On a cold run (no cache), the bottleneck is Claude: 44 calls × ~5s average = ~3–4 minutes
of wall time, plus ~$0.15–0.25 in API costs.

On a warm run (full cache), Claude is skipped entirely. The bottleneck shifts to SEC EDGAR
API discovery, which is rate-limited to 5 concurrent requests and includes a 0.5s
inter-ticker pause.

---

## 2. Implemented Optimizations

### 2.1 Document Fetch Cache

**File:** `infrastructure/cache/document_cache.py`
**Integrated in:** `agents/strategy_engine/strategies/fundamental/pdf_extractor.py`

SEC EDGAR press release documents (HTML and PDF) are cached to disk after the first fetch.
The cache key is a SHA-256 hash of the document URL. Because SEC EDGAR documents are
permanent once filed, no TTL is needed — the content never changes.

```
.cache/docs/{sha256(url)}.txt
```

**Effect:** Eliminates all HTTP calls to SEC EDGAR for document content on repeat runs.
A 44-filing run fetches ~44 documents; after the first run, all 44 are cache hits.

**Invalidation:** Manual — delete `.cache/docs/` to force re-fetch. This is only
necessary if you suspect a document was updated (extremely rare for SEC filings).

---

### 2.2 Claude Analysis Cache

**File:** `infrastructure/cache/analysis_cache.py`
**Integrated in:** `agents/strategy_engine/strategies/fundamental/claude_client.py`

Claude's raw text response for each filing is cached to disk after the first API call.
The cache key is `{filing_id}_{prompt_version_hash[:8]}`.

```
.cache/analyses/{filing_id}_{prompt_hash}.txt
```

The prompt version hash is computed from the full text of `result_extraction.txt` at
startup. When the extraction prompt is edited (e.g. to fix exceptional_items logic),
the hash changes and all cached analyses are automatically invalidated — the next run
fetches fresh results from Claude for every filing.

**Effect:** Zero Claude API calls on repeat backtester runs for previously seen filings.
Cost drops from ~$0.15–0.25 per run to near-zero on warm runs.

**Invalidation:** Automatic when `result_extraction.txt` is modified. Manual invalidation:
delete `.cache/analyses/` or change the prompt.

---

### 2.3 Anthropic Server-Side Prompt Caching

**File:** `agents/strategy_engine/strategies/fundamental/claude_client.py`
**Prompt file:** `agents/strategy_engine/strategies/fundamental/prompts/result_extraction.txt`

The extraction system prompt is static across all calls (ticker and quarter were moved
from the system prompt into the user message). This allows Anthropic's API to cache the
system prompt server-side using the `cache_control: {"type": "ephemeral"}` flag.

```python
system=[{
    "type": "text",
    "text": self._system_prompt,
    "cache_control": {"type": "ephemeral"},
}]
```

The user message now carries the per-call context:
```
Company: {ticker}
Reported period: {quarter}

{press_release_text}
```

**Effect:** On the first call in a session, Anthropic caches the system prompt tokens.
Subsequent calls within the cache window (5 minutes) pay ~10% of the input token cost
for the system prompt. At ~400 system prompt tokens per call, this saves roughly
$0.00036 per call — small individually but meaningful at scale or with larger prompts.

**Limitation:** The current system prompt is ~400 tokens. Anthropic's minimum cacheable
size is 1,024 tokens for Sonnet models. The cache_control flag is set correctly but the
prompt is too short to trigger caching today. The code is future-ready: as the prompt
grows (e.g. adding few-shot examples), caching will activate automatically.

---

### 2.4 Concurrent Filing Evaluation

**File:** `backtester/simulation_runner.py`

Filings are now evaluated concurrently using `asyncio.gather` with a semaphore limiting
simultaneous Claude API calls to 4:

```python
semaphore = asyncio.Semaphore(4)

async def _process(event):
    async with semaphore:
        ...

results = list(await asyncio.gather(*[_process(e) for e in events]))
```

`asyncio.gather` preserves order, so results remain chronologically sorted.

The semaphore of 4 is conservative — it avoids saturating the Claude API rate limit
while still providing 4× throughput on cold runs. On warm runs (cache hits), the
semaphore is not the bottleneck and can be raised without risk.

**Effect:** On a cold 44-filing run, wall time drops roughly proportionally to
concurrency (capped by rate limits). On warm runs, the gain is smaller since cache
hits are near-instant.

---

### 2.5 Deterministic Extraction (temperature=0)

**File:** `agents/strategy_engine/strategies/fundamental/claude_client.py`

Claude's `temperature` is set to `0` for all extraction calls:

```python
message = await self._client.messages.create(
    model=_MODEL,
    max_tokens=_MAX_TOKENS,
    temperature=0,
    ...
)
```

**Effect:** Identical documents produce identical extraction results across runs.
Before this fix, the same press release could yield a BUY signal in one run and a
HOLD in another, making backtester results unreliable for weight tuning. With
`temperature=0`, results are fully reproducible — a prerequisite for meaningful
backtesting.

---

## 3. Future Optimizations

### 3.1 Cache SEC EDGAR Filing Discovery

**Files to modify:** `agents/strategy_engine/data_feeds/announcement_feed.py`

Currently uncached on every run:
- `https://www.sec.gov/files/company_tickers.json` — CIK map (~218KB, changes rarely)
- `https://data.sec.gov/submissions/CIK{cik}.json` — filing list per ticker (10 calls)
- `https://www.sec.gov/Archives/.../index.htm` — EX-99.1 discovery per filing (44 calls)

**Proposed approach:** Extend `DocumentCache` (or create `ApiResponseCache`) to cache
these JSON/HTML responses with a TTL (e.g. 24 hours for submissions, 30 days for index
pages which are immutable once filed).

```python
# submissions: TTL 24h (new filings arrive daily)
# filing index HTML: no TTL (immutable once filed)
cache_key = f"submissions_{cik}"
```

**Expected gain:** Reduces second-run wall time from ~170s to ~30–40s. The SEC EDGAR
submissions fetch is currently the dominant remaining bottleneck on warm runs.

---

### 3.2 Cache yfinance Context Enrichment

**File:** `agents/strategy_engine/strategies/fundamental/context_enricher.py`

`enrich(ticker)` calls `yfinance` for `fast_info` (market cap, P/E ratio) on every
signal. This is ~1s per call and happens for every filing that produces a signal
(~30–40 calls per backtester run).

**Proposed approach:** Cache enrichment results by `(ticker, date)` with a 24-hour TTL.
For backtesting, the enrichment date should be the filing date, not today.

**Expected gain:** Saves ~30–40s per warm run.

---

### 3.3 Smarter Document Truncation

**File:** `agents/strategy_engine/strategies/fundamental/pdf_extractor.py`

Currently the extractor takes the first 60,000 characters of the document. For long
press releases, the most important content (revenue tables, EPS, guidance) typically
appears in the first 10–15KB. The remaining 45KB is often legal boilerplate, footnotes,
and safe harbour statements that consume tokens without adding signal.

**Proposed approach:** Extract text in two passes:
1. Take the first 8,000 characters (opening summary + headline numbers)
2. Scan for section headers matching patterns like "Revenue", "Results of Operations",
   "Earnings Per Share", "Outlook", "Guidance" and extract up to 500 characters around
   each match
3. Concatenate and cap at 20,000 characters

**Expected gain:** Reduces input tokens per call by ~50–70%, cutting Claude API costs
by a similar proportion. Also reduces extraction latency since smaller prompts process
faster.

---

### 3.4 Use Haiku for Extraction, Sonnet for Verification

**File:** `agents/strategy_engine/strategies/fundamental/claude_client.py`

Claude Haiku 4.5 costs roughly 20× less than Sonnet 4.6 and is significantly faster.
For structured data extraction from well-formatted press releases, Haiku performs
comparably to Sonnet.

**Proposed approach:** Two-stage pipeline:
1. **Haiku** extracts the raw JSON (cheap, fast)
2. **Sonnet** is called only if Haiku's `confidence` field is `"low"` or if the
   extracted JSON fails schema validation

**Expected gain:** ~70–80% reduction in Claude API costs for clean documents (most
filings). Haiku latency is also ~3× faster, improving first-run wall time.

**Trade-off:** Small risk of lower extraction quality on unusual document formats.
A/B test on the existing 44-filing benchmark before enabling in production.

---

### 3.5 Anthropic Batch API for Backtesting

**File:** `backtester/simulation_runner.py`

Anthropic's [Message Batches API](https://docs.anthropic.com/en/docs/build-with-claude/batch-processing)
allows sending up to 10,000 requests in a single batch, processed asynchronously within
24 hours, at 50% of standard API pricing.

**Proposed approach:** Add a `--batch` flag to the backtester CLI. When set:
1. Fetch all documents (using document cache)
2. Collect all cache-missing analyses into a batch
3. Submit batch to Anthropic API
4. Poll for completion (or use webhook)
5. Write results to analysis cache
6. Proceed with scoring

**Expected gain:** 50% cost reduction on cold runs. No latency benefit (batch takes up
to 24h), so only suitable for the backtester, not live trading.

---

### 3.6 Parallel Ticker Fetching in AnnouncementFeed

**File:** `agents/strategy_engine/data_feeds/announcement_feed.py`

Currently, `stream_events()` fetches filings for tickers sequentially with a 0.5s
inter-ticker pause. With 10 tickers, this adds 4.5s of deliberate sleep plus sequential
HTTP latency.

**Proposed approach:** Fetch submissions for all tickers concurrently using
`asyncio.gather`, keeping the existing `Semaphore(5)` for per-request rate limiting:

```python
tasks = [self.fetch_8k_filings(ticker, client, since=since) for ticker in tickers]
all_filings = await asyncio.gather(*tasks)
```

Remove the `asyncio.sleep(0.5)` inter-ticker pause — the semaphore already controls
concurrency and SEC's rate limit applies per-request, not per-ticker.

**Expected gain:** Reduces filing discovery phase from ~30s to ~8–10s for 10 tickers.

---

### 3.7 Incremental Backtesting

**Files:** `backtester/historical_loader.py`, `backtester/simulation_runner.py`

Currently every backtester run re-evaluates all filings in the date range, even those
already processed in a previous run. The analysis cache avoids the Claude API cost but
still incurs the filing discovery and scoring overhead.

**Proposed approach:** Store backtest results in the PostgreSQL database (the `signals`
table already exists). At the start of a run, query which `filing_id`s already have a
stored signal. Skip those events in the simulation runner, loading stored results
directly from the DB instead.

```python
already_done = await db.get_signal_filing_ids(strategy_id, market_id)
new_events = [e for e in events if e.filing_id not in already_done]
```

**Expected gain:** Near-instant repeat runs for unchanged date ranges. Only new filings
(e.g. from the most recent quarter) require evaluation.

---

### 3.8 Database-Backed Cache

**Files:** `infrastructure/cache/`

The current file-based cache works well for single-machine local development but has
limitations for production:
- Not shared across multiple processes or machines
- No TTL enforcement without a cleanup job
- No cache hit/miss metrics

**Proposed approach:** Add a `DbCache` implementation backed by a new `cache` PostgreSQL
table with columns `(key, value, created_at, expires_at)`. Wire `DocumentCache` and
`AnalysisCache` to use `DbCache` in production (controlled by `config/system/default.yaml`).

The file-based cache remains the default for local development (no DB required for tests).

---

### 3.9 Streaming Claude Responses

**File:** `agents/strategy_engine/strategies/fundamental/claude_client.py`

The current implementation waits for the full Claude response before parsing. With
streaming, the JSON response begins arriving immediately and can be parsed incrementally,
reducing perceived latency in the live trading path.

**Proposed approach:** Use `client.messages.stream()` and accumulate the response.
For the backtester this provides no benefit (results are batched). For live trading
(Phase 2+), streaming reduces the delay between filing detection and signal generation.

**Expected gain:** Reduce live-path latency by ~1–3s depending on response length.
No cost impact.

---

### 3.10 Token Budget via Extended Thinking

Not currently applicable. Extended thinking increases tokens and cost. Noted here as
a non-optimization: do not enable extended thinking for structured extraction tasks
where `temperature=0` and a well-defined schema already produce reliable output.

---

## 4. Observed Timings

All measurements on a local Mac with Docker-hosted PostgreSQL, 10 tickers, 3 quarters
(44 filings), `--no-prices` flag set.

| Run | Cache state | Wall time | Claude API calls | Notes |
|---|---|---|---|---|
| Before any caching | Cold | ~6 min | 44 | Sequential, default temperature |
| After caching + concurrency | Cold | ~6 min | 44 | Cache populated, results stored |
| After caching + concurrency | Warm | ~170s | 0 | All docs + analyses cached |
| Projected after §3.1 + §3.2 | Warm | ~30–40s | 0 | SEC + yfinance also cached |
| Projected with §3.4 (Haiku) | Cold | ~2 min | 44 (Haiku) | ~70% cost reduction |
| Projected with §3.5 (Batch) | Cold (async) | up to 24h | 44 | 50% cost reduction |

---

*Last updated: May 2026 — Phase 1 implementation complete.*
