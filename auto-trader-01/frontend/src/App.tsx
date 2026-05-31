import { useEffect, useState } from "react";
import { api } from "./api";
import type { Signal, Position, Holding } from "./api";
import "./App.css";

type Tab = "signals" | "positions" | "orders" | "watchlist";

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

function SignalsTab() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [market, setMarket] = useState("");
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.signals(market || undefined);
      setSignals(data.signals.slice().reverse());
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
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {signals.map(s => (
          <div key={s.signal_id} style={{ border: "1px solid #374151", borderRadius: 8, padding: 16, background: "#111827" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <span style={{ fontWeight: 700, fontSize: 18 }}>
                {s.ticker} <span style={{ fontSize: 12, color: "#9ca3af" }}>{s.market_id.toUpperCase()}</span>
              </span>
              <div style={{ display: "flex", gap: 6 }}>
                {badge(s.recommended_action)}
                {confBadge(s.confidence)}
              </div>
            </div>
            <div style={{ color: "#9ca3af", fontSize: 13, marginBottom: 6 }}>
              Score: <strong style={{ color: "#f9fafb" }}>{s.composite_score.toFixed(1)}</strong>
              {" · "}{s.strategy_type}{" · "}{new Date(s.created_at).toLocaleString()}
            </div>
            <div style={{ fontSize: 13, color: "#d1d5db", lineHeight: 1.5 }}>{s.rationale}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function PositionsTab() {
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
                <td style={{ padding: "8px" }}><strong>{p.ticker}</strong></td>
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

function OrdersTab() {
  const [form, setForm] = useState<OrderForm>({
    ticker: "", market_id: "us", side: "BUY", quantity: 1, order_type: "LIMIT", limit_price: 0,
  });
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    setResult(null); setError(null); setLoading(true);
    try {
      const r = await api.placeOrder(form) as { status: string; broker_order_id: string; message?: string };
      setResult(`${r.status} — broker ref: ${r.broker_order_id || "—"}`);
    } catch (e: unknown) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
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
    <div style={{ maxWidth: 480 }}>
      <h3 style={{ color: "#9ca3af", marginBottom: 16 }}>Manual Order Entry</h3>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {row("Ticker", inp("ticker"))}
        {row("Market", sel("market_id", ["us", "india"]))}
        {row("Side", sel("side", ["BUY", "SELL"]))}
        {row("Quantity", inp("quantity", "number"))}
        {row("Order Type", sel("order_type", ["LIMIT", "MARKET"]))}
        {form.order_type === "LIMIT" && row("Limit Price", inp("limit_price", "number"))}
        <button
          onClick={submit}
          disabled={loading || !form.ticker}
          style={{ padding: "8px 20px", background: "#2563eb", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer" }}
        >
          {loading ? "Placing…" : "Place Order"}
        </button>
        {result && <div style={{ color: "#16a34a", fontSize: 13 }}>✓ {result}</div>}
        {error && <div style={{ color: "#dc2626", fontSize: 13 }}>✗ {error}</div>}
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

export default function App() {
  const [tab, setTab] = useState<Tab>("signals");
  const [health, setHealth] = useState<{ status: string; brokers: Record<string, string> } | null>(null);

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

        {tab === "signals" && <SignalsTab />}
        {tab === "positions" && <PositionsTab />}
        {tab === "orders" && <OrdersTab />}
        {tab === "watchlist" && <WatchlistTab />}
      </div>
    </div>
  );
}
