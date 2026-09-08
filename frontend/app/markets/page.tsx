"use client";

import { useEffect, useState } from "react";
import { MarketChart } from "./market-chart";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
type Candle = { open_time: string; open: string; high: string; low: string; close: string };

export default function MarketsPage() {
  const [selectedAccount, setSelectedAccount] = useState("");
  const [pair, setPair] = useState("");
  const [timeframe, setTimeframe] = useState("M15");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [resyncing, setResyncing] = useState(false);
  const [quoteTelemetry, setQuoteTelemetry] = useState<{ bid: string; ask: string; observed_at: string } | null>(null);

  useEffect(() => {
    const account = new URLSearchParams(window.location.search).get("account") ?? "";
    setSelectedAccount(account);
  }, []);

  useEffect(() => {
    if (!selectedAccount || !pair) return;
    fetch(`${apiBase}/api/v1/broker-accounts/${selectedAccount}/candles?pair=${encodeURIComponent(pair)}&timeframe=${timeframe}`)
      .then((response) => response.json()).then((value) => setCandles(value.candles ?? [])).catch(() => setCandles([]));
  }, [selectedAccount, pair, timeframe]);

  async function resync() {
    if (!selectedAccount) return;
    setResyncing(true);
    await fetch(`${apiBase}/api/v1/broker-accounts/${selectedAccount}/market-data/resync?stream=markets`, { method: "POST" });
    setResyncing(false);
  }

  return (
    <main>
      <p>Trading Engine</p><h1>Markets</h1>
      <label>Account <input aria-label="selected account" value={selectedAccount} onChange={(event) => setSelectedAccount(event.target.value)} /></label>
      <label>Pair <input aria-label="pair" value={pair} onChange={(event) => setPair(event.target.value)} /></label>
      <label>Timeframe <select value={timeframe} onChange={(event) => setTimeframe(event.target.value)}><option>M15</option><option>H1</option><option>H4</option></select></label>
      <button type="button" onClick={resync} disabled={!selectedAccount || resyncing}>{resyncing ? "Resyncing…" : "Resync stream"}</button>
      <p aria-live="polite">{selectedAccount ? `Akun terpilih: ${selectedAccount}` : "Pilih satu akun"}</p>
      <MarketChart candles={candles} quoteTelemetry={quoteTelemetry} />
      <p>Quote telemetry bersifat non-canonical dan tidak mengubah MarketState.</p>
    </main>
  );
}
