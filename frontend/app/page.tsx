"use client";

import { useEffect, useState } from "react";

type Status = {
  status: string;
  version: string;
  services: Record<string, string>;
  execution_available: boolean;
  trading_enabled: boolean;
  message: string;
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export default function SystemPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    fetch(`${apiBase}/api/v1/system/status`)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("status")))
      .then((value: Status) => setStatus(value))
      .catch(() => setUnavailable(true));
  }, []);

  return (
    <main>
      <p>Trading Engine</p>
      <h1>System</h1>
      {!status && !unavailable && <p aria-live="polite">Memuat status…</p>}
      {unavailable && <p role="alert">Backend: Tidak tersedia</p>}
      {status && <section aria-label="Status sistem">
        <p>Status: {status.status}</p>
        <p>Versi: {status.version}</p>
        <p>API: {status.services.api}</p>
        <p>Database: {status.services.database === "unavailable" ? "Tidak tersedia" : status.services.database}</p>
        <p>Konektor: {status.services.connector === "unavailable" ? "Tidak tersedia" : status.services.connector}</p>
        <p>Trading aktif: {status.trading_enabled ? "Ya" : "Tidak"}</p>
        <p>Execution available: {status.execution_available ? "Ya" : "Tidak"}</p>
        <p>{status.message}</p>
      </section>}
    </main>
  );
}
