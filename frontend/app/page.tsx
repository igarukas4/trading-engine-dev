"use client";

import { useEffect, useState } from "react";

type ServiceStatus = {
  api: string;
  database: string;
  connector: string;
};

type Status = {
  status: string;
  version: string;
  services: ServiceStatus;
  execution_available: boolean;
  trading_enabled: boolean;
  message: string;
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

function displayServiceStatus(value: string): string {
  return value === "unavailable" ? "Tidak tersedia" : value;
}

export default function SystemPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    fetch(`${apiBase}/api/v1/system/status`)
      .then((response) => {
        if (!response.ok) {
          throw new Error("status");
        }
        return response.json();
      })
      .then((value: Status) => setStatus(value))
      .catch(() => setUnavailable(true));
  }, []);

  return (
    <main>
      <p>Trading Engine</p>
      <h1>System</h1>
      {!status && !unavailable && <p aria-live="polite">Memuat status…</p>}
      {unavailable && <p role="alert">Backend: Tidak tersedia</p>}
      {status && (
        <section aria-label="Status sistem">
          <p>Status: {status.status}</p>
          <p>Versi: {status.version}</p>
          <p>API: {status.services.api}</p>
          <p>Database: {displayServiceStatus(status.services.database)}</p>
          <p>Konektor: {displayServiceStatus(status.services.connector)}</p>
          <p>Trading aktif: {status.trading_enabled ? "Ya" : "Tidak"}</p>
          <p>Execution available: {status.execution_available ? "Ya" : "Tidak"}</p>
          <p>{status.message}</p>
        </section>
      )}
    </main>
  );
}
