import { useEffect, useState } from "react";
import { api } from "./api";
import type { Signal, Position, Holding, BrokerOrder } from "./api";
import { TvChart } from "./components/TvChart";
import "./App.css";

type Tab = "signals" | "positions" | "orders" | "watchlist" | "chart";

function badge(action: string) {
  const colors: Record<string, string> = { BUY: "#16a34a", SELL: "#dc2626", HOLD: "#d97706" };
  return (
    <span style={{ background: colors[action] ?? "#6b7280", color: "#fff", padding: "2px 8px", borderRadius: 4, fontSize: 12 }}>
      {action}
    </span>
  );
}

function confBadge(c: string) {
  const colors: Record<string, string> = { high: "#16a34a", medium: "#d97706", low: "#dc2626" };
  return (
    <span style={{ background: colors[c] ?? "#6b7280", color: "#fff", padding: "2px 6px", borderRadius: 4, fontSize: 11 }}>
      {c}
    </span>
  );
}

const DISPOSITION_STYLE: Record<string, { color: string; label: string }> = {
  order_placed: { color: "#16a34a", label: "ORDER PLACED" },
  approved:     { color: "#d97706", label: "PENDING ORDER" },
  rejected:     { color: "#dc2626", label: "REJECTED" },
  received:     { color: "#6b7280", label: "PROCESSING" },
};

function dispositionBadge(d: string) {
  const s = DISPOSITION_STYLE[d] ?? { color: "#6b7280", label: d.toUpperCase() };
  return (
    <span style={{ background: s.color, color: "#fff", padding: "2px 7px", borderRadius: 4, fontSize: 11, fontWeight: 600 }}>
      {s.label}
    </span>
  );
}

function SignalsTab({ onChart }: { onChart: (ticker: string, market: "us" | "india") => void }) {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [market, setMarket] = useState("");
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.signals(market || undefined);
      setSignals(data.signals);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [market]);

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", gap: 8, alignItems: "center" }}>
        <select value={market} onChange={e => setMarket(e.target.value)} style={{ padding: "4px 8px" }}>
          <option value="">All markets</option>
          <option value="us">US</option>
          <option value="india">India</option>
        </select>
        <button onClick={load} disabled={loading}>{loading ? "Loading…" : "Refresh"}</button>
      </div>
      {signals.length === 0 && !loading && <p style={{ color: "#9ca3af" }}>No signals yet.</p>}
      {signals.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ color: "#9ca3af", borderBottom: "1px solid #374151", textAlign: "left" }}>
              {["Time", "Ticker", "Strategy", "Action", "Score", "Conf", "Disposition", "Details"].map(h => (
                <th key={h} style={{ padding: "6px 8px", fontWeight: 500 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {signals.map(s => {
              const ts = s.received_at || s.created_at;
              const isOpen = expanded === s.signal_id;
              return (
                <>
                  <tr
                    key={s.signal_id}
                    style={{ borderBottom: "1px solid #1f2937", cursor: "pointer" }}
                    onClick={() => setExpanded(isOpen ? null : s.signal_id)}
                    title="Click to expand rationale"
                  >
                    <td style={{ padding: "8px", color: "#6b7280", fontSize: 11, whiteSpace: "nowrap" }}>
                      {ts ? new Date(ts).toLocaleString() : "—"}
                    </td>
                    <td style={{ padding: "8px", fontWeight: 600 }}>
                      <span
                        style={{ cursor: "pointer", textDecoration: "underline dotted" }}
                        onClick={e => { e.stopPropagation(); onChart(s.ticker, s.market_id as "us" | "india"); }}
                        title="View chart"
                      >
                        {s.ticker}
                      </span>
                      <span style={{ fontSize: 11, color: "#9ca3af", marginLeft: 4 }}>{s.market_id.toUpperCase()}</span>
                    </td>
                    <td style={{ padding: "8px", color: "#9ca3af" }}>{s.strategy_id || s.strategy_type}</td>
                    <td style={{ padding: "8px" }}>{badge(s.recommended_action)}</td>
                    <td style={{ padding: "8px" }}>{s.composite_score?.toFixed(1) ?? "—"}</td>
                    <td style={{ padding: "8px" }}>{confBadge(s.confidence)}</td>
                    <td style={{ padding: "8px" }}>{dispositionBadge(s.disposition ?? "received")}</td>
                    <td style={{ padding: "8px", color: "#9ca3af", fontSize: 12, maxWidth: 220 }}>
                      {s.disposition === "rejected" && s.rejection_reason
                        ? <span style={{ color: "#f87171" }}>{s.rejection_reason}</span>
                        : s.order_id
                        ? <span style={{ color: "#6ee7b7", fontFamily: "monospace" }}>{s.order_id}</span>
                        : "—"}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr key={`${s.signal_id}-rationale`} style={{ background: "#0f172a" }}>
                      <td colSpan={8} style={{ padding: "10px 16px", color: "#d1d5db", fontSize: 12, lineHeight: 1.6 }}>
                        {s.rationale || <em style={{ color: "#6b7280" }}>No rationale</em>}
                      </td>
                    </tr>
                  )}
                </>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function PositionsTab({ onChart }: { onChart: (ticker: string, market: "us" | "india") => void }) {
  const [positions, setPositions] = useState<Position[]>([]);
  const [holdings, setHoldings] = useState<Holding[]>([]);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.positions();
      setPositions(data.positions);
      setHoldings(data.holdings);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const pnlColor = (v: number) => v >= 0 ? "#16a34a" : "#dc2626";

  const th = (label: string) => (
    <th style={{ padding: "6px 8px", color: "#6b7280", fontSize: 12, textAlign: "left", fontWeight: 400 }}>{label}</th>
  );

  return (
    <div>
      <button onClick={load} disabled={loading} style={{ marginBottom: 12 }}>{loading ? "Loading…" : "Refresh"}</button>
      <h3 style={{ color: "#9ca3af", marginBottom: 8 }}>Open Positions</h3>
      {positions.length === 0 && <p style={{ color: "#9ca3af" }}>No open positions.</p>}
      {positions.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse", marginBottom: 24 }}>
          <thead><tr>{th("Ticker")}{th("Market")}{th("Qty")}{th("Avg Price")}{th("Current")}{th("P&L")}</tr></thead>
          <tbody>
            {positions.map(p => (
              <tr key={`${p.market_id}-${p.ticker}`} style={{ borderTop: "1px solid #1f2937" }}>
                <td style={{ padding: "8px" }}>
                  <strong style={{ cursor: "pointer" }} onClick={() => onChart(p.ticker, p.market_id as "us" | "india")} title="View chart">
                    {p.ticker}
                  </strong>
                </td>
                <td style={{ color: "#9ca3af", padding: "8px" }}>{p.market_id}</td>
                <td style={{ padding: "8px" }}>{p.quantity}</td>
                <td style={{ padding: "8px" }}>{p.average_price.toFixed(2)}</td>
                <td style={{ padding: "8px" }}>{p.current_price.toFixed(2)}</td>
                <td style={{ padding: "8px", color: pnlColor(p.unrealised_pnl) }}>
                  {p.unrealised_pnl >= 0 ? "+" : ""}{p.unrealised_pnl.toFixed(2)} {p.currency}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3 style={{ color: "#9ca3af", marginBottom: 8 }}>Holdings</h3>
      {holdings.length === 0 && <p style={{ color: "#9ca3af" }}>No holdings.</p>}
      {holdings.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr>{th("Ticker")}{th("Market")}{th("Qty")}{th("Avg Price")}{th("Current")}</tr></thead>
          <tbody>
            {holdings.map(h => (
              <tr key={`${h.market_id}-${h.ticker}`} style={{ borderTop: "1px solid #1f2937" }}>
                <td style={{ padding: "8px" }}><strong>{h.ticker}</strong></td>
                <td style={{ color: "#9ca3af", padding: "8px" }}>{h.market_id}</td>
                <td style={{ padding: "8px" }}>{h.quantity}</td>
                <td style={{ padding: "8px" }}>{h.average_price.toFixed(2)}</td>
                <td style={{ padding: "8px" }}>{h.current_price.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

interface OrderForm {
  ticker: string;
  market_id: string;
  side: string;
  quantity: number;
  order_type: string;
  limit_price: number;
}

const STATUS_COLOR: Record<string, string> = {
  OPEN: "#d97706", FILLED: "#16a34a", CANCELLED: "#6b7280", REJECTED: "#dc2626",
};

function OrdersTab() {
  const [orders, setOrders] = useState<BrokerOrder[]>([]);
  const [market, setMarket] = useState("us");
  const [statusFilter, setStatusFilter] = useState("all");
  const [listLoading, setListLoading] = useState(false);

  const [form, setForm] = useState<OrderForm>({
    ticker: "", market_id: "us", side: "BUY", quantity: 1, order_type: "LIMIT", limit_price: 0,
  });
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const loadOrders = async () => {
    setListLoading(true);
    try {
      const data = await api.orders(market, statusFilter);
      setOrders(data.orders);
    } finally {
      setListLoading(false);
    }
  };

  useEffect(() => { loadOrders(); }, [market, statusFilter]);

  const submit = async () => {
    setResult(null); setError(null); setSubmitting(true);
    try {
      const r = await api.placeOrder(form) as { status: string; broker_order_id: string; message?: string };
      setResult(`${r.status} — broker ref: ${r.broker_order_id || "—"}`);
      loadOrders();
    } catch (e: unknown) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const inp = (key: keyof OrderForm, type = "text") => (
    <input
      type={type}
      value={String(form[key])}
      onChange={e => setForm(f => ({ ...f, [key]: type === "number" ? Number(e.target.value) : e.target.value }))}
      style={{ padding: "6px 10px", background: "#1f2937", border: "1px solid #374151", borderRadius: 6, color: "#f9fafb", width: "100%" }}
    />
  );

  const sel = (key: keyof OrderForm, opts: string[]) => (
    <select
      value={String(form[key])}
      onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
      style={{ padding: "6px 10px", background: "#1f2937", border: "1px solid #374151", borderRadius: 6, color: "#f9fafb", width: "100%" }}
    >
      {opts.map(o => <option key={o}>{o}</option>)}
    </select>
  );

  const row = (label: string, node: React.ReactNode) => (
    <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13 }}>
      <span style={{ color: "#9ca3af" }}>{label}</span>
      {node}
    </label>
  );

  return (
    <div>
      {/* Order list */}
      <div style={{ marginBottom: 24 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
          <select value={market} onChange={e => setMarket(e.target.value)} style={{ padding: "4px 8px" }}>
            <option value="us">US</option>
            <option value="india">India</option>
          </select>
          <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} style={{ padding: "4px 8px" }}>
            <option value="all">All</option>
            <option value="open">Open</option>
            <option value="closed">Closed</option>
          </select>
          <button onClick={loadOrders} disabled={listLoading}>{listLoading ? "Loading…" : "Refresh"}</button>
        </div>
        {orders.length === 0 && !listLoading && <p style={{ color: "#9ca3af", fontSize: 13 }}>No orders found.</p>}
        {orders.length > 0 && (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ color: "#9ca3af", borderBottom: "1px solid #374151", textAlign: "left" }}>
                {["Ticker", "Side", "Type", "Qty", "Filled", "Limit $", "Fill $", "Status", "Created"].map(h => (
                  <th key={h} style={{ padding: "6px 8px", fontWeight: 500 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {orders.map(o => (
                <tr key={o.broker_order_id} style={{ borderBottom: "1px solid #1f2937" }}>
                  <td style={{ padding: "8px", fontWeight: 600 }}>{o.ticker}</td>
                  <td style={{ padding: "8px", color: o.side === "BUY" ? "#16a34a" : "#dc2626" }}>{o.side}</td>
                  <td style={{ padding: "8px", color: "#9ca3af" }}>{o.order_type}</td>
                  <td style={{ padding: "8px" }}>{o.quantity}</td>
                  <td style={{ padding: "8px" }}>{o.filled_qty}</td>
                  <td style={{ padding: "8px" }}>{o.limit_price > 0 ? o.limit_price.toFixed(2) : "—"}</td>
                  <td style={{ padding: "8px" }}>{o.fill_price > 0 ? o.fill_price.toFixed(2) : "—"}</td>
                  <td style={{ padding: "8px" }}>
                    <span style={{ color: STATUS_COLOR[o.status] ?? "#9ca3af", fontWeight: 600 }}>{o.status}</span>
                  </td>
                  <td style={{ padding: "8px", color: "#6b7280", fontSize: 11 }}>
                    {o.created_at ? new Date(o.created_at).toLocaleString() : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Manual order entry */}
      <div style={{ borderTop: "1px solid #374151", paddingTop: 20, maxWidth: 480 }}>
        <h3 style={{ color: "#9ca3af", marginBottom: 16, fontSize: 14 }}>Manual Order Entry</h3>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {row("Ticker", inp("ticker"))}
          {row("Market", sel("market_id", ["us", "india"]))}
          {row("Side", sel("side", ["BUY", "SELL"]))}
          {row("Quantity", inp("quantity", "number"))}
          {row("Order Type", sel("order_type", ["LIMIT", "MARKET"]))}
          {form.order_type === "LIMIT" && row("Limit Price", inp("limit_price", "number"))}
          <button
            onClick={submit}
            disabled={submitting || !form.ticker}
            style={{ padding: "8px 20px", background: "#2563eb", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer" }}
          >
            {submitting ? "Placing…" : "Place Order"}
          </button>
          {result && <div style={{ color: "#16a34a", fontSize: 13 }}>✓ {result}</div>}
          {error && <div style={{ color: "#dc2626", fontSize: 13 }}>✗ {error}</div>}
        </div>
      </div>
    </div>
  );
}

function WatchlistTab() {
  const [data, setData] = useState<Record<string, string[]>>({});

  useEffect(() => {
    api.watchlist().then(d => setData(d.watchlist));
  }, []);

  return (
    <div>
      {Object.entries(data).map(([market, tickers]) => (
        <div key={market} style={{ marginBottom: 24 }}>
          <h3 style={{ color: "#9ca3af", marginBottom: 8, textTransform: "uppercase", fontSize: 13, letterSpacing: 1 }}>{market}</h3>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {tickers.map(t => (
              <span key={t} style={{ background: "#1f2937", border: "1px solid #374151", borderRadius: 6, padding: "4px 12px", fontSize: 14 }}>
                {t}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ChartTab({ ticker, market, onTickerChange, onMarketChange }: {
  ticker: string;
  market: "us" | "india";
  onTickerChange: (t: string) => void;
  onMarketChange: (m: "us" | "india") => void;
}) {
  const [watchlist, setWatchlist] = useState<Record<string, string[]>>({});

  useEffect(() => {
    api.watchlist().then(d => setWatchlist(d.watchlist));
  }, []);

  const tickers = watchlist[market] ?? [];

  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 16 }}>
        <select
          value={market}
          onChange={e => {
            onMarketChange(e.target.value as "us" | "india");
            onTickerChange("");
          }}
          style={{ padding: "4px 8px" }}
        >
          <option value="us">US</option>
          <option value="india">India</option>
        </select>
        <select
          value={ticker}
          onChange={e => onTickerChange(e.target.value)}
          style={{ padding: "4px 8px", minWidth: 120 }}
        >
          <option value="">— select ticker —</option>
          {tickers.map(t => <option key={t}>{t}</option>)}
        </select>
        <input
          type="text"
          placeholder="or type symbol…"
          value={ticker}
          onChange={e => onTickerChange(e.target.value.toUpperCase())}
          style={{ padding: "4px 8px", background: "#1f2937", border: "1px solid #374151", borderRadius: 4, color: "#f9fafb", width: 120 }}
        />
      </div>
      {ticker
        ? <TvChart ticker={ticker} market={market} height={560} />
        : <p style={{ color: "#9ca3af" }}>Select a ticker to view the chart.</p>
      }
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("signals");
  const [chartTicker, setChartTicker] = useState("");
  const [chartMarket, setChartMarket] = useState<"us" | "india">("us");
  const [health, setHealth] = useState<{ status: string; brokers: Record<string, string> } | null>(null);

  const goToChart = (ticker: string, market: "us" | "india") => {
    setChartTicker(ticker);
    setChartMarket(market);
    setTab("chart");
  };

  useEffect(() => {
    api.health().then(setHealth).catch(() => null);
    const t = setInterval(() => api.health().then(setHealth).catch(() => null), 30000);
    return () => clearInterval(t);
  }, []);

  const tabs: { id: Tab; label: string }[] = [
    { id: "signals", label: "Signals" },
    { id: "positions", label: "Positions" },
    { id: "orders", label: "Orders" },
    { id: "watchlist", label: "Watchlist" },
    { id: "chart", label: "Chart" },
  ];

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", background: "#0f172a", color: "#f1f5f9", minHeight: "100vh", padding: 24 }}>
      <div style={{ maxWidth: 1000, margin: "0 auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
          <div>
            <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>Auto Trader 01</h1>
            <div style={{ fontSize: 12, color: "#6b7280", marginTop: 2 }}>Algorithmic trading dashboard</div>
          </div>
          {health && (
            <div style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12 }}>
              {Object.entries(health.brokers).map(([m, s]) => (
                <span key={m} style={{
                  background: s === "ok" ? "#14532d" : "#7f1d1d",
                  color: "#fff", padding: "3px 8px", borderRadius: 4,
                }}>
                  {m}: {s}
                </span>
              ))}
            </div>
          )}
        </div>

        <div style={{ display: "flex", gap: 4, borderBottom: "1px solid #1e293b", marginBottom: 24 }}>
          {tabs.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                padding: "10px 20px", background: "none", border: "none",
                borderBottom: tab === t.id ? "2px solid #3b82f6" : "2px solid transparent",
                color: tab === t.id ? "#3b82f6" : "#6b7280",
                cursor: "pointer", fontSize: 14, fontWeight: tab === t.id ? 600 : 400,
              }}
            >
              {t.label}
            </button>
          ))}
        </div>

        {tab === "signals" && <SignalsTab onChart={goToChart} />}
        {tab === "positions" && <PositionsTab onChart={goToChart} />}
        {tab === "orders" && <OrdersTab />}
        {tab === "watchlist" && <WatchlistTab />}
        {/* Keep ChartTab mounted at all times so the TV widget and drawing tools survive tab switches */}
        <div style={{ display: tab === "chart" ? "block" : "none" }}>
          <ChartTab ticker={chartTicker} market={chartMarket} onTickerChange={setChartTicker} onMarketChange={setChartMarket} />
        </div>
      </div>
    </div>
  );
}
