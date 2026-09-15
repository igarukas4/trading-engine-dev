"use client";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type StrategyConfig = {
  id: string;
  pair: string;
  strategy: string;
  activation_status: string;
};

function StrategiesContent() {
  const searchParams = useSearchParams();
  const account = searchParams.get("account");
  const [configs, setConfigs] = useState<StrategyConfig[]>([]);
  const [loadedAccount, setLoadedAccount] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    if (!account) {
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setFailed(false);

    fetch(`${apiBase}/api/v1/broker-accounts/${encodeURIComponent(account)}/strategy-configs`, {
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error("Strategy request failed");
        return response.json();
      })
      .then((value) => {
        if (!Array.isArray(value.configs)) throw new Error("Invalid Strategy response");
        setConfigs(value.configs);
        setLoadedAccount(account);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setFailed(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [account, retryCount]);

  const showLoading = loading || (!!account && loadedAccount !== account && !failed);

  return (
    <main>
      <h1>Strategies</h1>
      {showLoading ? (
        <section className="empty-state" role="status" aria-live="polite">
          <h2>Memuat konfigurasi Strategy</h2>
        </section>
      ) : !account ? (
        <section className="empty-state" aria-live="polite">
          <h2>Pilih account terlebih dahulu</h2>
          <p>Konfigurasi Strategy ditampilkan untuk satu account. Pilih account dari header.</p>
        </section>
      ) : failed ? (
        <section className="empty-state" role="alert">
          <h2>Gagal memuat konfigurasi Strategy</h2>
          <p>Periksa koneksi lalu coba lagi.</p>
          <button className="resync-button" type="button" onClick={() => setRetryCount((count) => count + 1)}>
            Coba lagi
          </button>
        </section>
      ) : configs.length === 0 ? (
        <section className="empty-state" aria-live="polite">
          <h2>Belum ada konfigurasi Strategy</h2>
          <p>Account ini belum memiliki konfigurasi Strategy.</p>
        </section>
      ) : (
        configs.map((config) => (
          <article key={config.id}>
            <h2>
              {config.pair} · {config.strategy}
            </h2>
            <p>
              Versi {config.id}: {config.activation_status}
            </p>
          </article>
        ))
      )}
    </main>
  );
}

export default function StrategiesPage() {
  return (
    <Suspense
      fallback={(
        <main>
          <h1>Strategies</h1>
          <section className="empty-state" role="status" aria-live="polite">
            <h2>Memuat konfigurasi Strategy</h2>
          </section>
        </main>
      )}
    >
      <StrategiesContent />
    </Suspense>
  );
}
