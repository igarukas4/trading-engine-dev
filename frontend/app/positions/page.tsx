"use client";
import { useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type Position = {
  position_id: string;
  pair: string;
  direction: string;
  remaining_volume: string;
  entry_price: string;
  current_pnl: string;
  protection_status: string;
  protection_confirmed: boolean;
  order_status: string;
  data_status: string;
};

export default function PositionsPage() {
  const [account, setAccount] = useState("");
  const [positions, setPositions] = useState<Position[]>([]);
  const [selected, setSelected] = useState<Position | null>(null);
  const [volume, setVolume] = useState("");
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const selectedAccount = new URLSearchParams(window.location.search).get("account") ?? "";
    setAccount(selectedAccount);
    if (selectedAccount) {
      fetch(`${apiBase}/api/v1/broker-accounts/${selectedAccount}/dashboard-snapshot`)
        .then((response) => {
          if (!response.ok) return Promise.reject();
          return response.json();
        })
        .then((value) => setPositions(value.positions ?? []))
        .catch(() => setPositions([]));
    }
  }, []);

  async function requestExit(kind: "close" | "reduce") {
    if (!selected || !confirmed || !reason.trim()) {
      setMessage("Pilih volume, isi alasan, dan konfirmasi account/pair terlebih dahulu.");
      return;
    }
    const requestedVolume = kind === "close" ? "ALL" : volume;
    const response = await fetch(`${apiBase}/api/v1/broker-accounts/${account}/positions/${selected.position_id}/${kind}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idempotency_key: `${kind}-${selected.position_id}-${Date.now()}`, volume: requestedVolume, pair_confirmation: selected.pair, reason, confirmed }),
    });
    const value = await response.json();
    if (response.ok) {
      setMessage("Diproses — menunggu status final dari backend/broker.");
      setSelected({ ...selected, order_status: "SUBMITTED" });
    } else {
      setMessage(`Perintah ditolak: ${value.detail?.code ?? "UNKNOWN"}`);
    }
  }

  return (
    <main>
      <div className="eyebrow">Exposure control · Account scoped</div><h1>Positions</h1>
      {!account && <section className="empty-state"><h2>Pilih account terlebih dahulu</h2><p>Exposure tidak ditampilkan lintas account.</p></section>}
      {account && <p>Account: <strong>{account}</strong> · Data UNKNOWN atau STALE tidak mengaktifkan exposure-increasing controls.</p>}
      {account && positions.length ? (
        positions.map((position) => (
          <article key={position.position_id}>
            <h2>Pair: {position.pair} · Direction: {position.direction}</h2>
            <p>Volume: {position.remaining_volume} · Entry: {position.entry_price} · Current PnL: {position.current_pnl}</p>
            <p>Protection: {position.protection_status} · OrderStatus: {position.order_status} · Data: {position.data_status}</p>
            <button type="button" onClick={() => setSelected(position)}>Close / Reduce-only</button>
          </article>
        ))
      ) : (
        account && <p>Tidak ada Position pada account ini.</p>
      )}
      {selected && <section aria-label="Position exit confirmation">
        <h2>Konfirmasi exit: {selected.pair}</h2>
        <p>Target Position: {selected.position_id}. Tidak ada edit harga dari tabel.</p>
        <label>Volume reduce-only <input aria-label="exit volume" value={volume} onChange={(event) => setVolume(event.target.value)} /></label>
        <label>Alasan <input aria-label="exit reason" value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        <label><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> Saya mengonfirmasi account {account} dan Pair {selected.pair}</label>
        <button type="button" onClick={() => requestExit("reduce")} disabled={selected.data_status !== "CONFIRMED" && selected.protection_status !== "CONFIRMED"}>Reduce-only</button>
        <button type="button" onClick={() => requestExit("close")}>Close All</button>
        <button type="button" onClick={() => setSelected(null)}>Batal</button>
      </section>}
      {message && <p role="status">{message}</p>}
    </main>
  );
}
