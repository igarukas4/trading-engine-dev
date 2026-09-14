"use client";

export type MarketChartProps = { candles: Array<{ open_time: string; open: string; high: string; low: string; close: string }>; quoteTelemetry?: { bid: string; ask: string; observed_at: string } | null };

/** Application adapter: page modules never depend on a chart vendor. */
export function MarketChart({ candles, quoteTelemetry }: MarketChartProps) {
  const values = candles.length ? candles.slice(-24) : [];
  const max = values.length ? Math.max(...values.map(c => Number(c.high))) : 1; const min = values.length ? Math.min(...values.map(c => Number(c.low))) : 0; const range = max - min || 1;
  return <section className="market-chart" aria-label="MarketChart"><div className="chart-gridlines"><span>1.0950</span><span>1.0900</span><span>1.0850</span><span>1.0800</span></div>{values.length ? <svg viewBox="0 0 720 280" preserveAspectRatio="none" role="img" aria-label={`${values.length} candle canonical`}>{values.map((candle, index) => { const x = 14 + index * (690 / Math.max(values.length, 1)); const y = (value: number) => 266 - ((value - min) / range) * 235; const up = Number(candle.close) >= Number(candle.open); return <g key={`${candle.open_time}-${index}`} className={up ? "candle-up" : "candle-down"}><line x1={x} x2={x} y1={y(Number(candle.high))} y2={y(Number(candle.low))}/><rect x={x - 5} y={Math.min(y(Number(candle.open)), y(Number(candle.close)))} width="10" height={Math.max(2, Math.abs(y(Number(candle.close)) - y(Number(candle.open))))}/></g>; })}</svg> : <div className="chart-placeholder"><span>Belum ada candle canonical</span><small>Pilih pair yang tersedia untuk memuat MarketState</small></div>}<div className="chart-legend"><span><i className="green-dot"/> Canonical candles</span>{quoteTelemetry && <span>Bid / Ask {quoteTelemetry.bid} / {quoteTelemetry.ask}</span>}<span>Read-only · No order entry</span></div></section>;
}
