import { useEffect, useRef } from "react";

declare global {
  interface Window {
    TradingView: {
      widget: new (config: Record<string, unknown>) => void;
    };
  }
}

interface TvChartProps {
  ticker: string;
  market: "us" | "india";
  height?: number;
}

export function TvChart({ ticker, market, height = 500 }: TvChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const widgetRef    = useRef<HTMLScriptElement | null>(null);
  const containerId  = `tv-chart-${ticker}-${market}`;

  const tvSymbol = market === "india" ? `NSE:${ticker}` : `NASDAQ:${ticker}`;
  const tvUrl    = `https://www.tradingview.com/chart/?symbol=${tvSymbol}`;

  useEffect(() => {
    if (!containerRef.current) return;

    // Remove any previously injected widget script
    if (widgetRef.current) {
      widgetRef.current.remove();
    }

    const script = document.createElement("script");
    script.src   = "https://s3.tradingview.com/tv.js";
    script.async = true;
    script.onload = () => {
      if (!window.TradingView) return;
      new window.TradingView.widget({
        container_id:      containerId,
        symbol:            tvSymbol,
        interval:          "D",
        theme:             "dark",
        style:             "1",       // candlesticks
        locale:            "en",
        toolbar_bg:        "#1a1a2e",
        enable_publishing: false,
        hide_side_toolbar: false,
        autosize:          true,
      });
    };
    document.head.appendChild(script);
    widgetRef.current = script;

    return () => {
      if (widgetRef.current) {
        widgetRef.current.remove();
        widgetRef.current = null;
      }
    };
  }, [ticker, market, tvSymbol, containerId]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <a
          href={tvUrl}
          target="_blank"
          rel="noopener noreferrer"
          style={{ fontSize: 12, color: "#888", textDecoration: "none" }}
        >
          Open in TradingView ↗
        </a>
      </div>
      <div
        id={containerId}
        ref={containerRef}
        style={{ height, width: "100%" }}
      />
    </div>
  );
}
