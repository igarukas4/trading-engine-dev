"use client";

import Link from "next/link";
import { ReactNode, useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
type Account = { id: string; display_name: string; environment: "DEMO" | "LIVE"; lifecycle_status: string; bot_state: string; execution_mode: string };
const links = [["Dashboard", "/"], ["Markets", "/markets"], ["Opportunities", "/opportunities"], ["Positions", "/positions"], ["Strategies", "/strategies"], ["System", "/system"]] as const;

export function OperatorShell({ children }: { children: ReactNode }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selected, setSelected] = useState("");
  const [emergencyOpen, setEmergencyOpen] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    const saved = window.localStorage.getItem("selected-broker-account") ?? "";
    setSelected(saved);
    fetch(`${apiBase}/api/v1/broker-accounts`).then((response) => response.ok ? response.json() : Promise.reject(new Error("accounts"))).then((value) => {
      const available = (value.accounts ?? []) as Account[]; setAccounts(available);
      if (!saved || available.some((account) => account.id === saved)) return;
      setSelected("");
    }).catch(() => setAccounts([]));
  }, []);
  function chooseAccount(value: string) { setSelected(value); if (value) window.localStorage.setItem("selected-broker-account", value); else window.localStorage.removeItem("selected-broker-account"); }
  async function emergency(kind: "STOP_ONLY" | "CLOSE_ALL") {
    if (!accounts.length) return;
    const response = await fetch(`${apiBase}/api/v1/global-emergency`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": `dashboard-${Date.now()}` }, body: JSON.stringify({ account_ids: accounts.map((account) => account.id), kind, reason: "Tindakan darurat operator" }) });
    setMessage(response.ok ? "Diproses — status final mengikuti operasi darurat." : "Perintah darurat ditolak oleh backend."); setEmergencyOpen(false);
  }
  const query = selected ? `?account=${encodeURIComponent(selected)}` : "";
  return <div><header aria-label="Konteks operator"><Link href="/">Trading Engine</Link><label>Akun <select aria-label="selected account" value={selected} onChange={(event) => chooseAccount(event.target.value)}><option value="">Ringkasan Semua Akun</option>{accounts.map((account) => <option key={account.id} value={account.id}>{account.display_name} · {account.environment}</option>)}</select></label>{selected && <span data-testid="account-context">{accounts.find((account) => account.id === selected)?.environment ?? ""}</span>}<button type="button" aria-label="Emergency" onClick={() => setEmergencyOpen(true)}>Emergency</button>{emergencyOpen && <aside role="dialog" aria-label="Konfirmasi darurat"><p>Konfirmasi scope akun dan dampak close-all.</p><button type="button" onClick={() => emergency("STOP_ONLY")}>Stop exposure</button><button type="button" onClick={() => emergency("CLOSE_ALL")}>Stop + close all</button><button type="button" onClick={() => setEmergencyOpen(false)}>Batal</button></aside>}{message && <p role="status">{message}</p>}</header><nav aria-label="Navigasi operator">{links.map(([label, href]) => <Link key={label} href={`${href}${query}`}>{label}</Link>)}</nav>{children}</div>;
}
