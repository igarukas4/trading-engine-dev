"use client";
import Link from "next/link";
import { useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
type AccountSummary = { account_id: string; display_name: string; environment: string; bot_state: string; open_positions: number };
type CriticalAlert = { id: string; event_type: string; broker_account_id: string };
type EmergencyOperation = { id: string; status: string };
type Summary = { accounts: AccountSummary[]; critical_alerts: CriticalAlert[]; global_emergency: EmergencyOperation[]; execution_available?: boolean };

export default function DashboardPage() {
  const [summary, setSummary] = useState<Summary | null>(null); const [unavailable, setUnavailable] = useState(false); const [kpiOpen, setKpiOpen] = useState(true);
  useEffect(() => { fetch(`${apiBase}/api/v1/dashboard-summary-snapshot`).then(r => r.ok ? r.json() : Promise.reject()).then(setSummary).catch(() => setUnavailable(true)); }, []);
  const accounts = summary?.accounts ?? []; const positions = accounts.reduce((total, account) => total + account.open_positions, 0);
  return <main><div className="eyebrow">Operator overview · 14 Sep 2026</div><h1>Good morning, Chief</h1><p>Berikut ringkasan kondisi market dan operasional akun Anda.</p>
    {unavailable && <p role="alert" className="alert-list">Backend: Tidak tersedia</p>}{!summary && !unavailable && <p aria-live="polite">Memuat ringkasan…</p>}
    {summary && <>{kpiOpen && <section className="grid summary-grid" aria-label="KPI dashboard"><div className="card"><div className="card-label">PNL HARI INI</div><div className="metric green">—</div><div className="account-meta">Belum ada data PnL</div></div><div className="card"><div className="card-label">TOTAL EXPOSURE</div><div className="metric">—</div><div className="account-meta">Per akun terpilih</div></div><div className="card"><div className="card-label">RISK BUDGET</div><div className="metric amber">—</div><div className="account-meta">Backend controlled</div></div><div className="card"><div className="card-label">OPEN POSITIONS</div><div className="metric">{positions}</div><div className="account-meta">Across {accounts.length} account{accounts.length === 1 ? "" : "s"}</div></div></section>}
      <div className="section-heading"><div><div className="eyebrow">Watchlist</div><h2>Account overview</h2><span className="sr-only">Ringkasan Semua Akun</span></div><button className="subtle-button" type="button" onClick={() => setKpiOpen(!kpiOpen)}>{kpiOpen ? "Hide KPI" : "Show KPI"}</button></div>
      <section className="grid dashboard-grid"><div className="account-list">{accounts.length ? accounts.map(account => <Link className="account-row" key={account.account_id} href={`/?account=${encodeURIComponent(account.account_id)}`}><div><div className="account-name">{account.display_name}</div><div className="account-meta">{account.environment} · Bot {account.bot_state} · {account.open_positions} posisi terbuka</div></div><span className="pill">{account.bot_state === "RUNNING" ? "READY" : account.bot_state}</span></Link>) : <div className="card empty">Belum ada akun yang tersedia.</div>}</div><section className="card alert-card" aria-label="Critical alerts"><div className="section-heading"><h2>Critical alerts</h2><span>{summary.critical_alerts.length} ACTIVE</span></div>{summary.critical_alerts.length ? <div className="alert-list">{summary.critical_alerts.map(alert => <p key={alert.id}>● {alert.event_type}<br/><small>Akun: {alert.broker_account_id}</small></p>)}</div> : <p className="empty">Tidak ada alert kritis. Semua sistem terlihat normal.</p>}<div className="section-heading" style={{ marginTop: 22 }}><h2>Global emergency</h2><span>SAFETY STATE</span></div>{summary.global_emergency.length ? summary.global_emergency.map(operation => <p className="alert-list" key={operation.id}>{operation.id}: {operation.status}</p>) : <p className="empty">Tidak ada operasi darurat.</p>}</section></section>
    </>}
  </main>;
}
