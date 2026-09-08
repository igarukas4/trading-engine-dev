"use client";

import { useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type RiskAssessment = { approved: boolean; reason_codes: string[] };
type Signal = {
  id: string;
  pair: string;
  direction: string;
  status: string;
  expires_at: string;
  reason_codes: string[];
  risk_assessment: RiskAssessment;
};
type Opportunity = { id?: string; pair: string; direction: string; confidence: string; reason_codes: string[]; signals?: Signal[] };

export default function OpportunitiesPage() {
  const [account, setAccount] = useState("");
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);

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

  return (
    <main>
      <p>Trading Engine</p>
      <h1>Opportunities</h1>
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
            </section>
          ))}
        </article>
      ))}
    </main>
  );
}
