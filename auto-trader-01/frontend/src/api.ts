const BASE = import.meta.env.VITE_API_BASE ?? "";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export interface Signal {
  signal_id: string;
  ticker: string;
  market_id: string;
  strategy_type: string;
  strategy_id: string;
  composite_score: number;
  recommended_action: string;
  confidence: string;
  rationale: string;
  created_at: string;
  received_at: string;
  // disposition fields
  disposition: "received" | "approved" | "rejected" | "order_placed";
  rejection_reason: string | null;
  order_id: string | null;
}

export interface Position {
  ticker: string;
  market_id: string;
  quantity: number;
  average_price: number;
  current_price: number;
  unrealised_pnl: number;
  currency: string;
}

export interface Holding {
  ticker: string;
  market_id: string;
  quantity: number;
  average_price: number;
  current_price: number;
  currency: string;
}

export interface BrokerOrder {
  broker_order_id: string;
  ticker: string;
  side: string;
  quantity: number;
  order_type: string;
  status: string;
  limit_price: number;
  fill_price: number;
  filled_qty: number;
  created_at: string | null;
}

export interface ServiceHealth {
  status: "ok" | "degraded" | "error";
  detail: string;
}

export interface Health {
  status: string;
  timestamp: string;
  services: Record<string, ServiceHealth>;
  brokers: Record<string, string>;
  active_markets: string[];
}

export interface BacktestSignalRow {
  ticker: string;
  quarter: string;
  filing_date: string;
  score: number;
  action: string;
  confidence: string;
  ret_5d: number | null;
  ret_10d: number | null;
  ret_30d: number | null;
  nifty_5d: number | null;
  nifty_10d: number | null;
  nifty_30d: number | null;
  tech_score: number | null;
  hybrid_pass: boolean;
}

export interface BacktestTier {
  tier: string;
  n: number;
  wins: number;
  win_rate: number;
  ci_lo: number;
  ci_hi: number;
  avg_return: number;
  avg_alpha: number;
}

export interface BacktestGateRow {
  gate: string;
  n: number;
  wins: number;
  win_rate: number;
  avg_alpha: number;
  verdict: "PASS" | "FAIL" | "inconclusive";
}

export interface BacktestResult {
  generated_at: string;
  config: {
    months: number;
    min_score: number;
    universe: string[];
    benchmark: string;
  };
  pipeline: {
    total_filings: number;
    too_recent: number;
    no_xbrl: number;
    below_score: number;
    buy_pre_regime: number;
    regime_filtered: number;
    passed: number;
  };
  summary: {
    n_buy: number;
    n_hybrid: number;
    win_rate_10d: number;
    ci_lo: number;
    ci_hi: number;
    p_value: number;
    z_score: number;
    gate_pass: boolean;
    mean_alpha_10d: number;
    expected_value: number;
    avg_win: number;
    avg_loss: number;
  };
  tiers: BacktestTier[];
  gate_comparison: BacktestGateRow[];
  signals: BacktestSignalRow[];
}

export const api = {
  health: () => get<Health>("/health"),
  signals: (market?: string) =>
    get<{ signals: Signal[] }>(`/api/signals${market ? `?market=${market}` : ""}`),
  positions: () => get<{ positions: Position[]; holdings: Holding[] }>("/api/positions"),
  watchlist: () => get<{ watchlist: Record<string, string[]> }>("/api/watchlist"),
  addTicker: (market_id: string, ticker: string) =>
    post<{ ok: boolean; watchlist: string[] }>(`/api/watchlist/${market_id}`, { ticker }),
  removeTicker: (market_id: string, ticker: string) =>
    fetch(`${BASE}/api/watchlist/${market_id}/${encodeURIComponent(ticker)}`, { method: "DELETE" })
      .then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.json() as Promise<{ ok: boolean; watchlist: string[] }>; }),
  orders: (market?: string, status?: string) =>
    get<{ orders: BrokerOrder[] }>(`/api/orders?market_id=${market ?? "us"}&status=${status ?? "all"}`),
  placeOrder: (body: {
    ticker: string;
    market_id: string;
    side: string;
    quantity: number;
    order_type: string;
    limit_price: number;
  }) => post("/api/orders", body),
  backtestIndia: () => get<BacktestResult>("/api/backtest/india"),
};
