"use client";
import { useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type Status = { status: string; version: string; services: Record<string, string>; execution_available: boolean; trading_enabled: boolean; message: string; account?: { account_id: string; state: string; mode: string; lifecycle_status: string; live_execution_enabled: boolean; runtime_interlock: string; version: number }; freshness?: Record<string, string> };
type AuditEvent = { id: string; event_type: string; reason: string; created_at: string };
type Recovery = { kind: string; order_id: string; status: string; reason: string; recovery_legal: boolean };
type Target = { account_id: string; status: string; detail?: string | null };
type Emergency = { id: string; version: number; requested_kind?: string; status: string; target_account_ids: string[]; targets: Record<string, Target | undefined> };
type Pairing = { session_id: string; device_code?: string; status: string; candidate?: { provider: string; broker_server: string; external_account_id: string; environment: string } | null; account_id?: string | null };

function label(value: string) {
  if (value === "healthy") return "Sehat";
  if (value === "unknown") return "Tidak diketahui";
  if (value === "unavailable") return "Tidak tersedia";
  return value;
}

const HEALTH_KEYS = ["dashboard_stream", "connector", "calendar"] as const;
const UNKNOWN_STATUS = "unknown"; // Backend unresolved status: UNKNOWN.
const ATTENTION_REQUIRED = "ATTENTION_REQUIRED";

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return response.json() as Promise<T>;
}

export default function SystemPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [recovery, setRecovery] = useState<Recovery[]>([]);
  const [emergencies, setEmergencies] = useState<Emergency[]>([]);
  const [message, setMessage] = useState("");
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [pairingName, setPairingName] = useState("");

  useEffect(() => {
    const account = new URLSearchParams(window.location.search).get("account");
    const query = account ? `?account_id=${encodeURIComponent(account)}` : "";
    Promise.all([
      fetchJson<Status>(`${apiBase}/api/v1/system/status${query}`),
      fetchJson<{ global_emergency?: Emergency[] }>(`${apiBase}/api/v1/dashboard-summary-snapshot`),
      account
        ? fetchJson<{ audit_events?: AuditEvent[]; recovery?: Recovery[] }>(
            `${apiBase}/api/v1/broker-accounts/${encodeURIComponent(account)}/dashboard-snapshot`,
          )
        : Promise.resolve({ audit_events: [], recovery: [] }),
    ]).then(([nextStatus, summary, snapshot]) => {
      setStatus(nextStatus);
      setEmergencies(summary.global_emergency ?? []);
      setAudit(snapshot.audit_events ?? []);
      setRecovery(snapshot.recovery ?? []);
    }).catch(() => setStatus(null));
  }, []);

  async function resume(operation: Emergency) {
    setMessage("Diproses");
    const response = await fetch(`${apiBase}/api/v1/global-emergency-operations/${operation.id}/resume-reconcile`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": `system-reconcile-${operation.id}-${operation.version}`,
      },
      body: JSON.stringify({
        expected_version: operation.version,
        reason: "Melanjutkan rekonsiliasi operasi darurat",
      }),
    });
    setMessage(response.ok ? "Diproses — status final mengikuti operasi darurat." : "Recovery ditolak oleh backend.");
  }

  async function startPairing() {
    const response = await fetch(`${apiBase}/api/v1/pairing-sessions`, { method: "POST" });
    if (response.ok) setPairing(await response.json() as Pairing);
  }

  async function pairingAction(action: "cancel" | "confirm") {
    if (!pairing) return;
    const response = await fetch(`${apiBase}/api/v1/pairing-sessions/${pairing.session_id}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: action === "confirm" ? JSON.stringify({ display_name: pairingName || undefined }) : undefined,
    });
    if (response.ok) {
      const next = await response.json() as Pairing & { session?: Pairing };
      setPairing(next.session ?? next);
      setMessage(action === "confirm" ? "Akun terhubung; kunci dikirim melalui sesi connector." : "Pairing dibatalkan.");
    }
  }

  async function changeControl(path: string, body: Record<string, unknown>) {
    const account = status?.account;
    if (!account) return;
    const response = await fetch(`${apiBase}/api/v1/broker-accounts/${encodeURIComponent(account.account_id)}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, expected_version: account.version }),
    });
    if (response.ok) {
      const result = await response.json() as { execution_mode?: string; live_execution_enabled?: boolean; runtime_interlock?: string; version?: number };
      setStatus({ ...status!, account: { ...account, mode: result.execution_mode ?? account.mode, live_execution_enabled: result.live_execution_enabled ?? account.live_execution_enabled, runtime_interlock: result.runtime_interlock ?? account.runtime_interlock, version: result.version ?? account.version } });
    }
  }

  return (
    <main>
      <h1>System</h1>
      {!status && <p role="alert">Backend: Tidak tersedia</p>}
      {status && (
        <section aria-label="Status sistem">
          <p>Status: {status.status}</p>
          <p>Versi: {status.version}</p>
          {Object.entries(status.services).map(([name, value]) => (
            <p key={name}>
              {name}: {label(value)}
            </p>
          ))}
          {Object.entries(status.freshness ?? {}).map(([name, value]) => (
            <p key={name}>freshness · {name}: {label(value)}</p>
          ))}
          {HEALTH_KEYS.map((name) => (
            <p key={`health-${name}`}>
              calendar/stream health · {name}: {label(status.freshness?.[name] ?? UNKNOWN_STATUS)}
            </p>
          ))}
          {status.account && <p>State: {status.account.state} · Mode: {status.account.mode} · Lifecycle: {status.account.lifecycle_status}</p>}
          <p>Execution available: {status.execution_available ? "Ya" : "Tidak"}</p>
          <p>{status.message}</p>
        </section>
      )}
      <section aria-label="Global emergency progress">
        <h2>Global emergency</h2>
        {emergencies.length ? emergencies.map((operation) => (
          <article key={operation.id}>
            <p>
              <strong>{operation.requested_kind ?? "EMERGENCY"}</strong> · {operation.status === "COMPLETE" ? "COMPLETED" : "IN_PROGRESS"} · v{operation.version}
            </p>
            {operation.target_account_ids.map((id) => {
              const target = operation.targets[id];
              const unresolved = !target || target.status !== "CONVERGED";
              return (
                <p key={id} className={unresolved ? "warning" : ""}>
                  Target {id}: {target ? target.status : ATTENTION_REQUIRED}
                  {target?.detail ? ` — ${target.detail}` : ""}
                </p>
              );
            })}
            {operation.status !== "COMPLETE" && (
              <button type="button" onClick={() => resume(operation)}>Resume reconcile</button>
            )}
          </article>
        )) : <p>Tidak ada operasi darurat aktif.</p>}
      </section>
      <section aria-label="Recovery and reconciliation">
        <h2>Recovery & reconciliation</h2>
        {recovery.length ? recovery.map((item) => (
          <p key={item.order_id} className="warning">
            {item.kind} · {item.order_id} · {item.status} · {item.reason}{" "}
            {item.recovery_legal ? "· Recovery tersedia" : `· ${ATTENTION_REQUIRED}`}
          </p>
        )) : <p>Tidak ada recovery yang dinyatakan legal oleh backend.</p>}
      </section>
      <section aria-label="Account Discovery Pairing">
        <h2>Account Discovery Pairing</h2>
        {!pairing && <button type="button" onClick={startPairing}>Mulai pairing 10 menit</button>}
        {pairing && <article>
          {pairing.device_code && <p>Device code untuk connector: <strong>{pairing.device_code}</strong></p>}
          <p>Status pairing: {pairing.status}</p>
          {pairing.candidate && <p>Candidate: {pairing.candidate.provider} · {pairing.candidate.broker_server} · {pairing.candidate.external_account_id} · {pairing.candidate.environment}</p>}
          {pairing.status === "CANDIDATE_SUBMITTED" && <>
            <label>Nama akun <input value={pairingName} onChange={event => setPairingName(event.target.value)} placeholder="Nama tampilan" /></label>
            <button type="button" onClick={() => pairingAction("confirm")}>Konfirmasi akun</button>
            <button type="button" onClick={() => pairingAction("cancel")}>Batalkan</button>
          </>}
        </article>}
      </section>
      {status?.account && <section aria-label="Custodian controls">
        <h2>Custodian controls</h2>
        <p>Interlock: {status.account.runtime_interlock}</p>
        <p>LIVE unlock: {status.account.live_execution_enabled ? "ON" : "OFF"}</p>
        <div>
          {(["MANUAL", "SEMI_AUTO", "FULL_AUTO"] as const).map(mode => <button key={mode} type="button" onClick={() => changeControl("/execution-mode", { mode })}>{mode}</button>)}
          <button type="button" onClick={() => changeControl(`/live-execution/${status.account?.live_execution_enabled ? "disable" : "enable"}`, {})}>{status.account.live_execution_enabled ? "Disable LIVE" : "Enable LIVE"}</button>
        </div>
      </section>}
      <section aria-label="Audit timeline">
        <h2>AuditEvent / command timeline</h2>
        {audit.length ? (
          audit.map((event) => (
            <p key={event.id}>
              {new Date(event.created_at).toLocaleString("id-ID")} · {event.event_type}: {event.reason}
            </p>
          ))
        ) : (
          <p>Pilih account untuk melihat audit kronologis.</p>
        )}
      </section>
      {message && <p role="status">{message}</p>}
    </main>
  );
}
