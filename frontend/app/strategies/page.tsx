"use client";
import { useEffect, useState } from "react";

const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type StrategyConfig = {
  id: string;
  pair: string;
  strategy: string;
  activation_status: string;
};

export default function StrategiesPage() {
  const [configs, setConfigs] = useState<StrategyConfig[]>([]);

  useEffect(() => {
    const account = new URLSearchParams(window.location.search).get("account");
    if (account) {
      fetch(`${apiBase}/api/v1/broker-accounts/${account}/strategy-configs`)
        .then((response) => response.json())
        .then((value) => setConfigs(value.configs ?? []))
        .catch(() => setConfigs([]));
    }
  }, []);

  return (
    <main>
      <h1>Strategies</h1>
      {configs.map((config) => (
        <article key={config.id}>
          <h2>
            {config.pair} · {config.strategy}
          </h2>
          <p>
            Versi {config.id}: {config.activation_status}
          </p>
        </article>
      ))}
    </main>
  );
}
