"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
type Account = { id: string; display_name: string; environment: "DEMO" | "LIVE"; lifecycle_status: string; bot_state: string; execution_mode: string };
const links = [["⌂", "Dashboard", "/"], ["⌁", "Markets", "/markets"], ["◇", "Opportunities", "/opportunities"], ["▣", "Positions", "/positions"], ["◈", "Strategies", "/strategies"], ["⚙", "System", "/system"]] as const;
function accountQuery(id: string) { return id ? `?account=${encodeURIComponent(id)}` : ""; }

export function OperatorShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedAccount = searchParams.get("account") ?? "";
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selected, setSelected] = useState(requestedAccount);
  const [emergencyOpen, setEmergencyOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const saved = window.localStorage.getItem("selected-broker-account") ?? "";
    fetch(`${apiBase}/api/v1/broker-accounts`)
      .then(response => response.ok ? response.json() : Promise.reject())
      .then(value => {
        const available = (value.accounts ?? []) as Account[];
        const candidate = requestedAccount || saved;
        setAccounts(available);
        if (candidate && !available.some(account => account.id === candidate)) {
          setSelected("");
          setMessage("Konteks akun tidak tersedia; menampilkan Ringkasan Semua Akun (read-only).");
          if (requestedAccount) router.replace(pathname);
          return;
        }
        setSelected(candidate);
      })
      .catch(() => {
        setAccounts([]);
        setSelected("");
        setMessage("Konteks akun tidak tersedia; menampilkan Ringkasan Semua Akun (read-only).");
      });
  }, [pathname, requestedAccount, router]);

  function chooseAccount(value: string) {
    setSelected(value);
    if (value) window.localStorage.setItem("selected-broker-account", value);
    else window.localStorage.removeItem("selected-broker-account");
    router.push(`${pathname}${accountQuery(value)}`);
  }

  async function emergency(kind: "STOP_ONLY" | "CLOSE_ALL") {
    const account_ids = selected ? [selected] : accounts.map(account => account.id);
    if (!account_ids.length) return;
    const response = await fetch(`${apiBase}/api/v1/global-emergency`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": `dashboard-${Date.now()}` },
      body: JSON.stringify({ account_ids, kind, reason: "Tindakan darurat operator" }),
    });
    setMessage(response.ok ? "Diproses — status final mengikuti operasi darurat." : "Perintah darurat ditolak oleh backend.");
    setEmergencyOpen(false);
  }

  const query = accountQuery(selected);
  const selectedAccount = accounts.find(account => account.id === selected);
  const contextLabel = selectedAccount ? `${selectedAccount.display_name} · ${selectedAccount.environment}` : "Ringkasan Semua Akun · read-only";

  return <div className="app-shell">
    <aside className="sidebar"><Link className="brand" href="/"><span className="brand-mark">T</span><span>TRADING<br/><strong>ENGINE</strong><br/><small>By O-O</small></span></Link><div className="sidebar-label">WORKSPACE</div><nav className="side-nav" aria-label="Navigasi operator">{links.map(([icon, label, href]) => <Link key={label} href={`${href}${query}`} className={pathname === href ? "active" : ""}><span className="nav-icon">{icon}</span>{label}</Link>)}</nav><div className="sidebar-footer"><span className="status-dot"/> Sistem operasional</div></aside>
    <div className="app-content"><header className="topbar" aria-label="Konteks operator"><div className="mobile-brand"><span className="brand-mark">T</span> Trading Engine</div><label className="account-picker"><span className="muted-label">AKUN AKTIF</span><select aria-label="selected account" value={selected} onChange={event => chooseAccount(event.target.value)}><option value="">Ringkasan Semua Akun</option>{accounts.map(account => <option key={account.id} value={account.id}>{account.display_name} · {account.environment}</option>)}</select></label><span className="mobile-account-context" data-testid="mobile-account-context">{contextLabel}</span><div className="topbar-spacer"/><span className="connection"><span className="status-dot"/> LIVE DATA <small>Just now</small></span>{selected && <span data-testid="account-context" className={`env-badge ${selectedAccount?.environment === "LIVE" ? "live" : "demo"}`}>{selectedAccount?.environment ?? ""}</span>}<button className="emergency-button" type="button" aria-label="Emergency" onClick={() => setEmergencyOpen(true)}>♢ Emergency</button>{emergencyOpen && <aside className="dialog" role="dialog" aria-label="Konfirmasi darurat"><div className="dialog-icon">!</div><h2>Emergency control</h2><p>{selected ? `Hentikan exposure untuk ${contextLabel}.` : "Tidak ada akun terpilih: tindakan mencakup semua akun yang tersedia."} Tindakan ini diproses oleh backend.</p><button type="button" onClick={() => emergency("STOP_ONLY")}>Stop exposure <small>Hentikan entry baru</small></button><button type="button" onClick={() => emergency("CLOSE_ALL")}>Stop + close all <small>Hentikan dan tutup posisi</small></button><button type="button" onClick={() => setEmergencyOpen(false)}>Batal</button></aside>}{message && <p className="toast" role="status">{message}</p>}</header>{children}<nav className="mobile-nav" aria-label="Navigasi mobile">{links.slice(0, 4).map(([icon, label, href]) => <Link key={label} href={`${href}${query}`}><span>{icon}</span>{label}</Link>)}<button type="button" aria-expanded={moreOpen} aria-controls="mobile-more-menu" onClick={() => setMoreOpen(open => !open)}><span>•••</span>Lainnya</button>{moreOpen && <div id="mobile-more-menu" className="mobile-more-menu" aria-label="Navigasi lainnya"><Link href={`/strategies${query}`} onClick={() => setMoreOpen(false)}>Strategies</Link><Link href={`/system${query}`} onClick={() => setMoreOpen(false)}>System</Link></div>}</nav></div>
  </div>;
}
