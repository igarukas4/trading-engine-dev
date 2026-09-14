"use client";

import { useEffect, useMemo, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
type DataStatus = {
  status: "HEALTHY" | "STALE" | "UNAVAILABLE" | "RESYNCING";
  can_approve: boolean;
  reason_code: string | null;
};
type RiskAssessment = { approved: boolean; reason_codes: string[] };
type Signal = {
  id: string;
  revision: number;
  opportunity: { pair: string; direction: string };
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
type Account = { display_name: string; execution_mode: string; bot_state: string };
type Action = "approve" | "execute";
type SignalBucket = "Perlu tindakan" | "Diblokir / expired" | "Riwayat";

const signalBuckets: SignalBucket[] = ["Perlu tindakan", "Diblokir / expired", "Riwayat"];

function signalBucket(signal: Signal): SignalBucket {
  if (signal.status === "ELIGIBLE" || signal.status === "APPROVED") return "Perlu tindakan";
  if (signal.status.startsWith("BLOCKED") || signal.status === "EXPIRED") return "Diblokir / expired";
  return "Riwayat";
}

function removePending(pending: Record<string, string>, signalId: string) {
  const next = { ...pending };
  delete next[signalId];
  return next;
}

export default function OpportunitiesPage() {
  const [accountId, setAccountId] = useState("");
  const [account, setAccount] = useState<Account | null>(null);
  const [dataStatus, setDataStatus] = useState<DataStatus | null>(null);
  const [opportunities, setOpportunities] = useState<Opportunity[]>([]);
  const [filter, setFilter] = useState<ReturnType<typeof signalBucket>>("Perlu tindakan");
  const [dialog, setDialog] = useState<{ signal: Signal; action: Action } | null>(null);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");

  useEffect(() => setAccountId(new URLSearchParams(window.location.search).get("account") ?? ""), []);
  useEffect(() => {
    if (!accountId) return;
    let cancelled = false;
    fetch(`${apiBase}/api/v1/broker-accounts/${encodeURIComponent(accountId)}/opportunities`)
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((value) => {
        if (cancelled) return;
        setAccount(value.account ?? null);
        setDataStatus(value.account_data_status ?? null);
        setOpportunities(value.opportunities ?? []);
      })
      .catch(() => {
        if (cancelled) return;
        setDataStatus({ status: "UNAVAILABLE", can_approve: false, reason_code: "ACCOUNT_DATA_UNAVAILABLE" });
        setOpportunities([]);
      });
    return () => { cancelled = true; };
  }, [accountId]);

  const visible = useMemo(
    () => opportunities.filter((opportunity) =>
      (opportunity.signals ?? []).some((signal) => signalBucket(signal) === filter),
    ),
    [filter, opportunities],
  );

  function openConfirmation(signal: Signal, action: Action) {
    setReason("");
    setConfirmed(false);
    setDialog({ signal, action });
  }

  async function submitAction() {
    if (!dialog || !accountId || !reason.trim() || !confirmed) return;
    const { signal, action } = dialog;
    const idempotencyKey = `${action}-${accountId}-${signal.id}-${signal.revision}`;
    setPending((current) => ({ ...current, [signal.id]: idempotencyKey }));
    setDialog(null);
    const payload: Record<string, unknown> = {
      idempotency_key: idempotencyKey,
      reason: reason.trim(),
      confirmed: true,
      signal_revision: signal.revision,
    };
    if (action === "execute") {
      payload.order_payload = {
        stop_loss: "native",
        take_profit: ["native"],
        signal_revision: signal.revision,
      };
    }
    try {
      const response = await fetch(
        `${apiBase}/api/v1/broker-accounts/${encodeURIComponent(accountId)}/signals/${signal.id}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
          body: JSON.stringify(payload),
        },
      );
      const value = await response.json();
      if (!response.ok) {
        setPending((current) => removePending(current, signal.id));
        setMessage(`Perintah ditolak: ${value.detail?.code ?? "tidak aman"}`);
        return;
      }
      setMessage(`Diproses · perintah ${value.command_id ?? "diterima"}. Status final mengikuti sumber otoritatif.`);
      if (value.command_id) {
        const poll = window.setInterval(async () => {
          const commandResponse = await fetch(`${apiBase}/api/v1/commands/${value.command_id}`);
          if (!commandResponse.ok) return;
          const command = await commandResponse.json();
          if (command.status !== "ACCEPTED") {
            window.clearInterval(poll);
            setPending((current) => removePending(current, signal.id));
            setMessage(`Status perintah: ${command.status}. Dashboard diperbarui dari sumber otoritatif.`);
            fetch(`${apiBase}/api/v1/broker-accounts/${encodeURIComponent(accountId)}/opportunities`)
              .then((latest) => latest.ok ? latest.json() : Promise.reject())
              .then((latest) => {
                setOpportunities(latest.opportunities ?? []);
                setDataStatus(latest.account_data_status ?? null);
              })
              .catch(() => undefined);
          }
        }, 1000);
        window.setTimeout(() => window.clearInterval(poll), 30000);
      }
    } catch {
      setPending((current) => removePending(current, signal.id));
      setMessage("Perintah belum dapat dikirim; status akun tetap aman.");
    }
  }

  const canAct = Boolean(dataStatus?.can_approve && account?.execution_mode === "MANUAL");

  return (
    <main>
      <p>Account-scoped operator flow · Mode {account?.execution_mode ?? "MANUAL"}</p>
      <h1>Opportunities</h1>

      {!accountId && <p>Pilih satu akun untuk melihat peluang.</p>}

      {accountId && (
        <>
          <div className="opportunity-toolbar">
            <strong>{account?.display_name ?? accountId}</strong>
            <span className={`data-state ${dataStatus?.status.toLowerCase()}`}>
              Data akun: {dataStatus?.status ?? "UNAVAILABLE"}
            </span>
          </div>

          {!dataStatus?.can_approve && (
            <p role="alert">
              Tindakan MANUAL dinonaktifkan: {dataStatus?.reason_code ?? "ACCOUNT_DATA_UNAVAILABLE"}.
            </p>
          )}

          <div role="tablist" aria-label="Filter Opportunities">
            {signalBuckets.map((name) => (
              <button
                type="button"
                role="tab"
                aria-selected={filter === name}
                key={name}
                onClick={() => setFilter(name)}
              >
                {name}
              </button>
            ))}
          </div>

          {visible.length === 0 && <p>Tidak ada Opportunity pada filter ini.</p>}

          {visible.map((opportunity, index) => (
            <article className="card" key={opportunity.id ?? `${opportunity.pair}-${index}`}>
              <h2>{opportunity.pair} · {opportunity.direction}</h2>
              <p>Confidence: {opportunity.confidence}</p>
              <p>Alasan backend: {opportunity.reason_codes.join(", ") || "Tidak ada"}</p>

              {(opportunity.signals ?? [])
                .filter((signal) => signalBucket(signal) === filter)
                .map((signal) => (
                  <section key={signal.id} aria-label={`Signal ${signal.id}`}>
                    <p>
                      <strong>{signal.opportunity.pair} · {signal.opportunity.direction}</strong>
                      {" · Status Signal: "}{signal.status}
                    </p>
                    <p>Berakhir: {signal.expires_at}</p>
                    <p>
                      RiskAssessment: {signal.risk_assessment.approved ? "APPROVED" : "REJECTED"}
                      {" · Reason codes: "}
                      {signal.reason_codes.concat(signal.risk_assessment.reason_codes).join(", ") || "Lolos"}
                    </p>

                    {pending[signal.id] && (
                      <button type="button" disabled>
                        Diproses · {pending[signal.id]}
                      </button>
                    )}
                    {!pending[signal.id] && signal.status === "ELIGIBLE" && (
                      <button
                        type="button"
                        disabled={!canAct}
                        onClick={() => openConfirmation(signal, "approve")}
                      >
                        Setujui
                      </button>
                    )}
                    {!pending[signal.id] && signal.status === "APPROVED" && (
                      <button
                        type="button"
                        disabled={!canAct}
                        onClick={() => openConfirmation(signal, "execute")}
                      >
                        Eksekusi
                      </button>
                    )}
                  </section>
                ))}
            </article>
          ))}
        </>
      )}

      {message && <p role="status">{message}</p>}

      {dialog && (
        <aside className="dialog" role="dialog" aria-label="Konfirmasi MANUAL">
          <h2>Konfirmasi {dialog.action === "approve" ? "persetujuan" : "eksekusi"}</h2>
          <p>
            Akun: {account?.display_name ?? accountId}<br />
            Pair: {dialog.signal.opportunity.pair}<br />
            Tindakan: {dialog.action}<br />
            Alasan wajib diisi sebelum perintah dikirim.
          </p>
          <label>
            Alasan operator
            <input
              aria-label="alasan operator"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            Saya mengonfirmasi tindakan ini
          </label>
          <button
            type="button"
            disabled={!reason.trim() || !confirmed || !dataStatus?.can_approve}
            onClick={submitAction}
          >
            Kirim perintah
          </button>
          <button type="button" onClick={() => setDialog(null)}>Batal</button>
        </aside>
      )}
    </main>
  );
}
