"use client";

import { useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type RiskAssessment = { approved: boolean; reason_codes: string[] };
type SignalOpportunity = {
  pair: string;
  direction: string;
  confidence: string;
  reason_codes: string[];
};
type Signal = {
  id: string;
  revision: number;
  opportunity: SignalOpportunity;
  status: string;
  expires_at: string;
  reason_codes: string[];
  risk_assessment: RiskAssessment;
};
type Opportunity = {
  id?: string;
  pair: string;
  direction: string;
  confidence: string;
  reason_codes: string[];
  signals?: Signal[];
};

export default function OpportunitiesPage() {
  const [account, setAccount] = useState("");
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const selected = new URLSearchParams(window.location.search).get("account") ?? "";
    setAccount(selected);
  }, []);

  useEffect(() => {
    if (!account) return;
    fetch(`${apiBase}/api/v1/broker-accounts/${account}/opportunities`)
      .then((response) => response.json())
      .then((value) => setOpportunities(value.opportunities ?? []))
      .catch(() => setOpportunities([]));
  }, [account]);

  async function action(signal: Signal, operation: "approve" | "execute") {
    if (!reason.trim() || !confirmed) {
      setMessage("Konfirmasi dan alasan wajib diisi.");
      return;
    }
    const response = await fetch(`${apiBase}/api/v1/broker-accounts/${account}/signals/${signal.id}/${operation}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idempotency_key: `${operation}-${signal.id}-${signal.revision ?? 1}`, reason, confirmed, signal_revision: signal.revision ?? 1,
        ...(operation === "execute" ? { order_payload: { stop_loss: "native", take_profit: ["native"] } } : {}) }),
    });
    const value = await response.json();
    setMessage(response.ok ? `Perintah ${operation} diterima.` : `Perintah ditolak: ${value.detail?.code ?? "tidak aman"}`);
    if (response.ok) setOpportunities((current) => current.map((item) => ({ ...item, signals: item.signals?.map((entry) => entry.id === signal.id ? { ...entry, ...(value.signal ?? {}), status: operation === "approve" ? "APPROVED" : entry.status } : entry) }))));
  }

  return (
    <main>
      <p>Trading Engine</p>
      <h1>Opportunities</h1>
      <label>Alasan tindakan <input aria-label="alasan tindakan" value={reason} onChange={(event) => setReason(event.target.value)} /></label>
      <label><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> Saya mengonfirmasi tindakan ini</label>
      {message && <p role="status">{message}</p>}
      <label>
        Account
        <input aria-label="selected account" value={account} onChange={(event) => setAccount(event.target.value)} />
      </label>
      {!account && <p>Pilih satu akun untuk melihat peluang.</p>}
      {account && opportunities.length === 0 && <p>Tidak ada Opportunity.</p>}
      {opportunities.map((opportunity, index) => (
        <article key={opportunity.id ?? `${opportunity.pair}-${index}`}>
          <h2>{opportunity.pair} · {opportunity.direction}</h2>
          <p>Confidence: {opportunity.confidence}</p>
          <p>Alasan Opportunity: {opportunity.reason_codes.join(", ") || "Tidak ada"}</p>
          {(opportunity.signals ?? []).map((signal) => (
            <section key={signal.id} aria-label={`Signal ${signal.id}`}>
              <p>Status Signal: {signal.status}</p>
              <p>Berakhir: {signal.expires_at}</p>
              <p>Alasan risiko: {signal.risk_assessment.reason_codes.join(", ") || "Lolos"}</p>
              {signal.status === "ELIGIBLE" && <button onClick={() => action(signal, "approve")}>Setujui</button>}
              {signal.status === "APPROVED" && <button onClick={() => action(signal, "execute")}>Eksekusi</button>}
            </section>
          ))}
        </article>
      ))}
    </main>
  );
}
