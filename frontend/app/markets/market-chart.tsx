"use client";

export type MarketChartProps = {
  candles: Array<{ open_time: string; open: string; high: string; low: string; close: string }>;
  quoteTelemetry?: { bid: string; ask: string; observed_at: string } | null;
};

/** Application adapter: the page never depends on a chart vendor. */
export function MarketChart({ candles, quoteTelemetry }: MarketChartProps) {
  return (
    <section aria-label="MarketChart">
      <p>{candles.length} candle canonical</p>
      {quoteTelemetry && <p>Quote telemetry (non-canonical): {quoteTelemetry.bid} / {quoteTelemetry.ask}</p>}
    </section>
  );
}
