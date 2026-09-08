"use client";
import { useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type AccountSummary = {
  account_id: string;
  display_name: string;
  environment: string;
  bot_state: string;
  open_positions: number;
};

type CriticalAlert = {
  id: string;
  event_type: string;
  broker_account_id: string;
};

type EmergencyOperation = { id: string; status: string };

type Summary = {
  accounts: AccountSummary[];
  critical_alerts: CriticalAlert[];
  global_emergency: EmergencyOperation[];
  execution_available?: boolean;
};

export default function DashboardPage() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    fetch(`${apiBase}/api/v1/dashboard-summary-snapshot`)
      .then((response) =>
        response.ok
          ? response.json()
          : Promise.reject(new Error("summary")),
      )
      .then(setSummary)
      .catch(() => setUnavailable(true));
  }, []);

  return (
    <main>
      <h1>Dashboard</h1>
      <p>Watchlist dan status operasional account-aware.</p>
      {unavailable && <p role="alert">Backend: Tidak tersedia</p>}
      {!summary && !unavailable && (
        <p aria-live="polite">Memuat ringkasan…</p>
      )}
      {summary && (
        <>
          <p>Execution available: {summary.execution_available ? "Ya" : "Tidak"}</p>
          <section aria-label="Ringkasan Semua Akun">
            <h2>Ringkasan Semua Akun</h2>
            {summary.accounts.map((account) => (
              <article key={account.account_id}>
                <h3>
                  {account.display_name} · {account.environment}
                </h3>
                <p>
                  {account.bot_state} · Posisi terbuka: {account.open_positions}
                </p>
              </article>
            ))}
          </section>
          <section aria-label="Critical alerts">
            <h2>Critical alerts</h2>
            {summary.critical_alerts.length ? (
              summary.critical_alerts.map((alert) => (
                <p key={alert.id}>
                  {alert.event_type} · {alert.broker_account_id}
                </p>
              ))
            ) : (
              <p>Tidak ada alert kritis.</p>
            )}
          </section>
          <section aria-label="Global emergency">
            <h2>Global emergency</h2>
            {summary.global_emergency.length ? (
              summary.global_emergency.map((operation) => (
                <p key={operation.id}>
                  {operation.id}: {operation.status}
                </p>
              ))
            ) : (
              <p>Tidak ada operasi darurat.</p>
            )}
          </section>
        </>
      )}
    </main>
  );
}
