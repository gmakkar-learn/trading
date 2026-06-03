import { Fragment, useEffect, useState } from "react";
import { api } from "./api";
import type { Signal, Position, Holding, BrokerOrder, Health, ServiceHealth, BacktestResult, BacktestSignalRow } from "./api";
import { TvChart } from "./components/TvChart";
import "./App.css";

type Tab = "signals" | "positions" | "orders" | "watchlist" | "chart" | "backtest";

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

function fmt(n: number | null | undefined, suffix = "") {
  if (n == null) return "—";
  return n.toLocaleString() + suffix;
}
function fmtPct(n: number | null | undefined) {
  if (n == null) return "—";
  return (n > 0 ? "+" : "") + n + "%";
}
function fmtM(n: number | null | undefined) {
  if (n == null) return "—";
  return "$" + n.toLocaleString() + "M";
}

function RationaleView({ rationale }: { rationale: string }) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let parsed: any = null;
  let preamble = "";
  try {
    const obj = JSON.parse(rationale);
    if (obj && typeof obj === "object" && ("revenue" in obj || "earnings" in obj)) parsed = obj;
  } catch {}

  if (!parsed) {
    // Try to extract an embedded JSON object from within the text
    const jsonStart = rationale.indexOf('{"');
    if (jsonStart > 0) {
      try {
        const obj = JSON.parse(rationale.slice(jsonStart));
        if (obj && typeof obj === "object" && ("revenue" in obj || "earnings" in obj)) {
          parsed = obj;
          preamble = rationale.slice(0, jsonStart).trim();
        }
      } catch {}
    }
  }

  if (!parsed) {
    return <span style={{ color: "#d1d5db", fontSize: 12, lineHeight: 1.6 }}>{rationale}</span>;
  }

  const pctColor = (v: number | null | undefined): string =>
    v == null ? "#9ca3af" : v > 0 ? "#4ade80" : v < 0 ? "#f87171" : "#9ca3af";

  const cell = (label: string, value: string, color?: string) => (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 110 }}>
      <span style={{ fontSize: 10, color: "#6b7280", textTransform: "uppercase" as const, letterSpacing: "0.05em" }}>{label}</span>
      <span style={{ fontSize: 13, color: color ?? "#f9fafb", fontWeight: 600 }}>{value}</span>
    </div>
  );

  const rev = parsed.revenue;
  const earn = parsed.earnings;
  const mar = parsed.margins;
  const guid = parsed.guidance;
  const div = parsed.dividend;
  const exc = parsed.exceptional_items;
  const notes: string | undefined = parsed.notes;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {preamble && (
        <div style={{ fontSize: 11, color: "#9ca3af", lineHeight: 1.6, borderLeft: "2px solid #374151", paddingLeft: 8, whiteSpace: "pre-wrap" }}>
          {preamble}
        </div>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
        {rev?.actual != null     && cell("Revenue",    fmtM(rev.actual))}
        {rev?.yoy_growth_pct != null && cell("Rev YoY", fmtPct(rev.yoy_growth_pct), pctColor(rev.yoy_growth_pct))}
        {earn?.eps_actual != null    && cell("EPS",      "$" + earn.eps_actual)}
        {earn?.eps_yoy_growth_pct != null && cell("EPS YoY", fmtPct(earn.eps_yoy_growth_pct), pctColor(earn.eps_yoy_growth_pct))}
        {earn?.net_income_actual != null  && cell("Net Income", fmtM(earn.net_income_actual))}
        {earn?.net_income_yoy_growth_pct != null && cell("NI YoY", fmtPct(earn.net_income_yoy_growth_pct), pctColor(earn.net_income_yoy_growth_pct))}
      </div>
      {mar && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          {mar.gross_margin_pct != null   && cell("Gross Margin", fmt(mar.gross_margin_pct, "%"))}
          {mar.operating_margin_pct != null && cell("Op. Margin",  fmt(mar.operating_margin_pct, "%"))}
          {mar.operating_margin_direction  && cell("Margin Trend", String(mar.operating_margin_direction))}
        </div>
      )}
      {(guid || div) && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          {guid?.provided && cell("Guidance",
            guid.direction ? String(guid.direction)
              : guid.revenue_next_quarter ? "Next Q Rev: " + fmtM(guid.revenue_next_quarter)
              : "provided")}
          {div?.declared && cell("Dividend",
            div.change
              ? String(div.change) + (div.amount ? " → $" + div.amount : "")
              : "declared")}
        </div>
      )}
      {exc?.present && (
        <div style={{ background: "#451a03", border: "1px solid #92400e", borderRadius: 6, padding: "6px 10px", fontSize: 12, color: "#fcd34d" }}>
          ⚠ Exceptional items ({fmtM(exc.impact_millions)}): {String(exc.description ?? "")}
        </div>
      )}
      {notes && (
        <div style={{ fontSize: 12, color: "#9ca3af", lineHeight: 1.6, borderTop: "1px solid #1f2937", paddingTop: 8 }}>
          {notes}
        </div>
      )}
    </div>
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
            <tr style={{ color: "#9ca3af", borderBottom: "1px solid #374151" }}>
              {["Time", "Ticker", "Strategy", "Action", "Score", "Conf", "Disposition", "Details"].map(h => (
                <th key={h} style={{ padding: "6px 8px", fontWeight: 500, textAlign: "left" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody style={{ textAlign: "left" }}>
            {signals.map(s => {
              const ts = s.received_at || s.created_at;
              const isOpen = expanded === s.signal_id;
              return (
                <Fragment key={s.signal_id}>
                  <tr
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
                    <tr style={{ background: "#0f172a" }}>
                      <td colSpan={8} style={{ padding: "12px 16px" }}>
                        {s.rationale
                          ? <RationaleView rationale={s.rationale} />
                          : <em style={{ color: "#6b7280", fontSize: 12 }}>No rationale</em>}
                      </td>
                    </tr>
                  )}
                </Fragment>
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
  const [newTicker, setNewTicker] = useState("");
  const [newMarket, setNewMarket] = useState("us");
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => api.watchlist().then(d => setData(d.watchlist));
  useEffect(() => { load(); }, []);

  const handleAdd = async () => {
    const t = newTicker.trim().toUpperCase();
    if (!t) return;
    setAdding(true);
    setError(null);
    try {
      const res = await api.addTicker(newMarket, t);
      setData(prev => ({ ...prev, [newMarket]: res.watchlist }));
      setNewTicker("");
    } catch (e: unknown) {
      setError((e as Error).message);
    } finally {
      setAdding(false);
    }
  };

  const handleRemove = async (market: string, ticker: string) => {
    setRemoving(`${market}:${ticker}`);
    setError(null);
    try {
      const res = await api.removeTicker(market, ticker);
      setData(prev => ({ ...prev, [market]: res.watchlist }));
    } catch (e: unknown) {
      setError((e as Error).message);
    } finally {
      setRemoving(null);
    }
  };

  return (
    <div>
      {/* Add ticker form */}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 24, flexWrap: "wrap" }}>
        <select
          value={newMarket}
          onChange={e => setNewMarket(e.target.value)}
          style={{ padding: "6px 10px", background: "#1f2937", border: "1px solid #374151", borderRadius: 6, color: "#f9fafb" }}
        >
          <option value="us">US</option>
          <option value="india">India</option>
        </select>
        <input
          value={newTicker}
          onChange={e => setNewTicker(e.target.value.toUpperCase())}
          onKeyDown={e => e.key === "Enter" && handleAdd()}
          placeholder="Ticker symbol (e.g. AAPL)"
          style={{ padding: "6px 10px", background: "#1f2937", border: "1px solid #374151", borderRadius: 6, color: "#f9fafb", width: 200 }}
        />
        <button
          onClick={handleAdd}
          disabled={adding || !newTicker.trim()}
          style={{ padding: "6px 16px", background: "#2563eb", border: "none", borderRadius: 6, color: "#fff", cursor: "pointer", fontWeight: 600 }}
        >
          {adding ? "Adding…" : "+ Add"}
        </button>
        {error && <span style={{ color: "#f87171", fontSize: 13 }}>{error}</span>}
      </div>

      {/* Per-market ticker lists */}
      {Object.entries(data).map(([market, tickers]) => (
        <div key={market} style={{ marginBottom: 28 }}>
          <h3 style={{ color: "#9ca3af", marginBottom: 10, textTransform: "uppercase" as const, fontSize: 13, letterSpacing: 1, margin: "0 0 10px" }}>
            {market} <span style={{ color: "#4b5563", fontWeight: 400 }}>({tickers.length})</span>
          </h3>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {tickers.map(t => {
              const key = `${market}:${t}`;
              const isRemoving = removing === key;
              return (
                <div key={t} style={{
                  display: "flex", alignItems: "center", gap: 4,
                  background: "#1f2937", border: "1px solid #374151",
                  borderRadius: 6, padding: "4px 6px 4px 12px", fontSize: 14,
                  opacity: isRemoving ? 0.5 : 1,
                }}>
                  <span>{t}</span>
                  <button
                    onClick={() => handleRemove(market, t)}
                    disabled={isRemoving}
                    title={`Remove ${t}`}
                    style={{
                      background: "none", border: "none", color: "#6b7280",
                      cursor: "pointer", fontSize: 16, lineHeight: 1,
                      padding: "0 2px", borderRadius: 3,
                    }}
                    onMouseEnter={e => (e.currentTarget.style.color = "#ef4444")}
                    onMouseLeave={e => (e.currentTarget.style.color = "#6b7280")}
                  >
                    ×
                  </button>
                </div>
              );
            })}
            {tickers.length === 0 && (
              <span style={{ color: "#4b5563", fontSize: 13 }}>No tickers — add one above</span>
            )}
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

function pctColor(v: number | null | undefined): string {
  if (v == null) return "#9ca3af";
  return v > 0 ? "#4ade80" : v < 0 ? "#f87171" : "#9ca3af";
}

function fmtPctBt(v: number | null | undefined): string {
  if (v == null) return "—";
  return (v >= 0 ? "+" : "") + v.toFixed(1) + "%";
}

function verdictBadge(v: "PASS" | "FAIL" | "inconclusive") {
  const style: Record<string, { bg: string; label: string }> = {
    PASS:          { bg: "#16a34a", label: "PASS ✓" },
    FAIL:          { bg: "#dc2626", label: "FAIL ✗" },
    inconclusive:  { bg: "#6b7280", label: "n < 10" },
  };
  const s = style[v] ?? style.inconclusive;
  return (
    <span style={{ background: s.bg, color: "#fff", padding: "2px 8px", borderRadius: 4, fontSize: 12, fontWeight: 600 }}>
      {s.label}
    </span>
  );
}

function BacktestTab() {
  const [data, setData] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.backtestIndia());
    } catch (e: unknown) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  if (loading) return <p style={{ color: "#9ca3af" }}>Loading backtest data…</p>;
  if (error) return (
    <div>
      <p style={{ color: "#f87171" }}>{error}</p>
      <p style={{ color: "#6b7280", fontSize: 13 }}>
        Run: <code style={{ background: "#1e293b", padding: "2px 6px", borderRadius: 4 }}>
          uv run python scripts/backtest_india.py --months 36 --min-score 70 --export data/backtest_india.json
        </code>
      </p>
    </div>
  );
  if (!data) return null;

  const { config, pipeline, summary, tiers, gate_comparison, signals } = data;
  const th = (label: string, right = false) => (
    <th style={{ padding: "6px 8px", color: "#6b7280", fontSize: 12, textAlign: right ? "right" : "left", fontWeight: 400, borderBottom: "1px solid #374151" }}>{label}</th>
  );
  const td = (content: React.ReactNode, right = false) => (
    <td style={{ padding: "7px 8px", textAlign: right ? "right" : "left", borderBottom: "1px solid #1f2937", fontSize: 13 }}>{content}</td>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 28 }}>

      {/* Verdict banner */}
      <div style={{
        padding: "16px 20px",
        borderRadius: 8,
        border: `2px solid ${summary.gate_pass ? "#16a34a" : "#dc2626"}`,
        background: summary.gate_pass ? "#052e16" : "#450a0a",
      }}>
        <div style={{ fontSize: 18, fontWeight: 700, color: summary.gate_pass ? "#4ade80" : "#f87171", marginBottom: 8 }}>
          Phase 3 Pre-Live Gate: {summary.gate_pass ? "PASS ✓" : "FAIL ✗"}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 24, fontSize: 13, color: "#d1d5db" }}>
          <span>+10d win rate: <strong style={{ color: summary.gate_pass ? "#4ade80" : "#f87171" }}>{(summary.win_rate_10d * 100).toFixed(1)}%</strong></span>
          <span>90% CI: [{(summary.ci_lo * 100).toFixed(1)}%, {(summary.ci_hi * 100).toFixed(1)}%]</span>
          <span>n = {summary.n_buy} signals</span>
          <span>p-value: {summary.p_value.toFixed(3)}</span>
          <span>EV: <span style={{ color: pctColor(summary.expected_value) }}>{fmtPctBt(summary.expected_value)}</span> per signal</span>
          <span style={{ color: "#6b7280", fontSize: 12 }}>
            {config.months}m · score≥{config.min_score} · {config.universe.length} tickers · {config.benchmark}
          </span>
        </div>
      </div>

      {/* Signal pipeline + summary side by side */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 24 }}>
        {/* Pipeline funnel */}
        <div style={{ flex: "1 1 260px" }}>
          <h3 style={{ color: "#9ca3af", fontSize: 13, marginBottom: 10, textTransform: "uppercase", letterSpacing: 1 }}>Signal Pipeline</h3>
          <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
            <tbody>
              {[
                ["Total filings",          pipeline.total_filings,   ""],
                ["Too recent (<35d)",      pipeline.too_recent,      "#6b7280"],
                ["XBRL / scoring error",   pipeline.no_xbrl,         "#6b7280"],
                [`Below score ${config.min_score}`, pipeline.below_score, "#6b7280"],
                ["BUY pre-regime",         pipeline.buy_pre_regime,  "#d97706"],
                ["Regime filtered",        pipeline.regime_filtered, "#dc2626"],
                ["BUY signals (final)",    pipeline.passed,          "#4ade80"],
              ].map(([label, val, color]) => (
                <tr key={label as string}>
                  <td style={{ padding: "4px 8px", color: "#9ca3af" }}>{label}</td>
                  <td style={{ padding: "4px 8px", textAlign: "right", fontWeight: 600, color: (color as string) || "#f9fafb" }}>{val}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Return profile */}
        <div style={{ flex: "1 1 200px" }}>
          <h3 style={{ color: "#9ca3af", fontSize: 13, marginBottom: 10, textTransform: "uppercase", letterSpacing: 1 }}>Return Profile (+10d)</h3>
          <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
            <tbody>
              {[
                ["Avg win",         fmtPctBt(summary.avg_win),   "#4ade80"],
                ["Avg loss",        fmtPctBt(summary.avg_loss),  "#f87171"],
                ["Mean alpha",      fmtPctBt(summary.mean_alpha_10d), pctColor(summary.mean_alpha_10d)],
                ["z-score",         data.summary.z_score.toFixed(2), "#d1d5db"],
                ["Significance",    summary.p_value <= 0.10 ? "p≤0.10 ✓" : `p=${summary.p_value.toFixed(3)}`, summary.p_value <= 0.10 ? "#4ade80" : "#d97706"],
              ].map(([label, val, color]) => (
                <tr key={label as string}>
                  <td style={{ padding: "4px 8px", color: "#9ca3af" }}>{label}</td>
                  <td style={{ padding: "4px 8px", textAlign: "right", fontWeight: 600, color: color as string }}>{val}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Gate comparison */}
      <div>
        <h3 style={{ color: "#9ca3af", fontSize: 13, marginBottom: 10, textTransform: "uppercase", letterSpacing: 1 }}>Gate Comparison</h3>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr>{th("Gate")}{th("n", true)}{th("+10d Win Rate", true)}{th("Avg Alpha", true)}{th("Verdict")}</tr></thead>
          <tbody>
            {gate_comparison.map(g => (
              <tr key={g.gate}>
                {td(g.gate)}
                {td(g.n, true)}
                {td(<span style={{ color: g.win_rate >= 0.55 ? "#4ade80" : "#f87171", fontWeight: 600 }}>{(g.win_rate * 100).toFixed(1)}%</span>, true)}
                {td(<span style={{ color: pctColor(g.avg_alpha) }}>{fmtPctBt(g.avg_alpha)}</span>, true)}
                {td(verdictBadge(g.verdict))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Score tier breakdown */}
      {tiers.length > 0 && (
        <div>
          <h3 style={{ color: "#9ca3af", fontSize: 13, marginBottom: 10, textTransform: "uppercase", letterSpacing: 1 }}>Score Tier Breakdown (+10d)</h3>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>{th("Tier")}{th("n", true)}{th("Win Rate", true)}{th("90% CI")}{th("Avg Return", true)}{th("Avg Alpha", true)}</tr></thead>
            <tbody>
              {tiers.map(t => (
                <tr key={t.tier}>
                  {td(<strong>{t.tier}</strong>)}
                  {td(t.n, true)}
                  {td(<span style={{ color: t.win_rate >= 0.55 ? "#4ade80" : "#f87171", fontWeight: 600 }}>{(t.win_rate * 100).toFixed(1)}%</span>, true)}
                  {td(<span style={{ color: "#6b7280", fontSize: 12 }}>[{(t.ci_lo * 100).toFixed(1)}%, {(t.ci_hi * 100).toFixed(1)}%]</span>)}
                  {td(<span style={{ color: pctColor(t.avg_return) }}>{fmtPctBt(t.avg_return)}</span>, true)}
                  {td(<span style={{ color: pctColor(t.avg_alpha) }}>{fmtPctBt(t.avg_alpha)}</span>, true)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Signal table */}
      <div>
        <h3 style={{ color: "#9ca3af", fontSize: 13, marginBottom: 10, textTransform: "uppercase", letterSpacing: 1 }}>
          BUY Signals ({signals.length})
        </h3>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr>
              {th("Ticker")}{th("Quarter")}{th("Filed")}{th("Score", true)}
              {th("Tech Gate", true)}{th("Conf")}
              {th("+5d", true)}{th("+10d", true)}{th("+30d", true)}
            </tr>
          </thead>
          <tbody>
            {(signals as BacktestSignalRow[]).map((s, i) => {
              const alpha10 = s.ret_10d != null && s.nifty_10d != null ? s.ret_10d - s.nifty_10d : null;
              return (
                <tr key={i}>
                  {td(<strong>{s.ticker}</strong>)}
                  {td(<span style={{ color: "#9ca3af" }}>{s.quarter}</span>)}
                  {td(<span style={{ color: "#6b7280" }}>{s.filing_date}</span>)}
                  {td(<span style={{ color: s.score >= 80 ? "#4ade80" : "#d1d5db" }}>{s.score.toFixed(1)}</span>, true)}
                  {td(
                    s.tech_score == null
                      ? <span style={{ color: "#6b7280" }}>—</span>
                      : <span style={{ color: s.hybrid_pass ? "#4ade80" : "#f59e0b" }}>
                          {s.hybrid_pass ? "✓" : "✗"} {s.tech_score.toFixed(0)}
                        </span>,
                    true
                  )}
                  {td(<span style={{ fontSize: 11, color: "#9ca3af" }}>{s.confidence}</span>)}
                  {td(<span style={{ color: pctColor(s.ret_5d) }}>{fmtPctBt(s.ret_5d)}</span>, true)}
                  {td(
                    <span style={{ color: pctColor(s.ret_10d) }}>
                      {fmtPctBt(s.ret_10d)}
                      {alpha10 != null && <span style={{ color: pctColor(alpha10), fontSize: 11, marginLeft: 4 }}>({alpha10 >= 0 ? "+" : ""}{alpha10.toFixed(1)})</span>}
                    </span>,
                    true
                  )}
                  {td(<span style={{ color: pctColor(s.ret_30d) }}>{fmtPctBt(s.ret_30d)}</span>, true)}
                </tr>
              );
            })}
          </tbody>
        </table>
        <p style={{ color: "#6b7280", fontSize: 11, marginTop: 8 }}>
          +10d alpha vs Nifty Midcap 100 shown in brackets. Generated {new Date(data.generated_at).toLocaleString()}.
        </p>
      </div>
    </div>
  );
}

const SERVICE_LABELS: Record<string, string> = {
  database:     "DB",
  telegram:     "Telegram",
  broker_us:    "Alpaca",
  broker_india: "Upstox",
};

function HealthWidget({ health }: { health: Health | null }) {
  const dotColor = (s: ServiceHealth | undefined): string => {
    if (!s) return "#6b7280";
    return s.status === "ok" ? "#22c55e" : s.status === "degraded" ? "#f59e0b" : "#ef4444";
  };

  if (!health) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#6b7280" }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: "#6b7280", display: "inline-block" }} />
        connecting…
      </div>
    );
  }

  const services = health.services ?? {};
  const entries = Object.keys(SERVICE_LABELS).map(key => ({
    key,
    label: SERVICE_LABELS[key],
    svc: services[key] as ServiceHealth | undefined,
  }));

  const allOk = entries.every(e => e.svc?.status === "ok");

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
      {/* Overall indicator */}
      <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12, color: allOk ? "#22c55e" : "#f59e0b" }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: allOk ? "#22c55e" : "#f59e0b", display: "inline-block" }} />
        {allOk ? "healthy" : "degraded"}
      </div>
      {/* Per-service dots */}
      <div style={{ display: "flex", gap: 6 }}>
        {entries.map(({ key, label, svc }) => {
          const color = dotColor(svc);
          const status = svc?.status ?? "unknown";
          const detail = svc?.detail ? ` — ${svc.detail}` : "";
          const tooltip = `${label}: ${status}${detail}`;
          return (
            <div key={key} title={tooltip} style={{ display: "flex", alignItems: "center", gap: 4, cursor: "default" }}>
              <span style={{
                width: 10, height: 10, borderRadius: "50%", background: color,
                display: "inline-block",
                boxShadow: svc?.status !== "ok" ? `0 0 6px ${color}` : "none",
              }} />
              <span style={{ fontSize: 11, color: "#9ca3af" }}>{label}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("signals");
  const [chartTicker, setChartTicker] = useState("");
  const [chartMarket, setChartMarket] = useState<"us" | "india">("us");
  const [health, setHealth] = useState<Health | null>(null);

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
    { id: "signals",  label: "Signals" },
    { id: "positions",label: "Positions" },
    { id: "orders",   label: "Orders" },
    { id: "watchlist",label: "Watchlist" },
    { id: "chart",    label: "Chart" },
    { id: "backtest", label: "Backtest" },
  ];

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", background: "#0f172a", color: "#f1f5f9", minHeight: "100vh", padding: 24 }}>
      <div style={{ maxWidth: 1000, margin: "0 auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
          <div>
            <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>Auto Trader 01</h1>
            <div style={{ fontSize: 12, color: "#6b7280", marginTop: 2 }}>Algorithmic trading dashboard</div>
          </div>
          <HealthWidget health={health} />
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
        {tab === "backtest" && <BacktestTab />}
      </div>
    </div>
  );
}
