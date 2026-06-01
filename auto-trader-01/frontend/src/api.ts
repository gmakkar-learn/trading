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

export interface Health {
  status: string;
  timestamp: string;
  brokers: Record<string, string>;
  active_markets: string[];
}

export const api = {
  health: () => get<Health>("/health"),
  signals: (market?: string) =>
    get<{ signals: Signal[] }>(`/api/signals${market ? `?market=${market}` : ""}`),
  positions: () => get<{ positions: Position[]; holdings: Holding[] }>("/api/positions"),
  watchlist: () => get<{ watchlist: Record<string, string[]> }>("/api/watchlist"),
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
};
