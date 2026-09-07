# Trading Engine V0 — Canonical Specification

**Status:** Accepted  
**Canonical source:** Dokumen ini adalah satu-satunya entry point untuk scope dan kontrak implementasi Trading Engine V0.  
**Accepted destination:** [GitHub Issue #24](https://github.com/igarukas4/trading-engine-dev/issues/24)

## 1. Purpose

Trading Engine V0 memantau beberapa `BrokerAccount` MT5 secara terisolasi, mengubah market data menjadi `Opportunity` dan `Signal` yang dapat dijelaskan, menerapkan risk gate deterministik, lalu mengirim serta merekonsiliasi order pasar nyata melalui connector MT5. Operator mengendalikan sistem dari dashboard realtime berbahasa Indonesia.

MT5 tetap menjadi sumber kebenaran untuk Order, Fill, dan Position. PostgreSQL adalah domain truth; Redis hanya dipakai untuk cache dan realtime fanout.

## 2. Canonical Contract Set

Spec ini sengaja modular. Semua dokumen berikut adalah bagian normatif dari **satu** Trading Engine V0 specification:

1. [`backend-v0.md`](backend-v0.md) — domain model, account isolation, connector, market data, strategy pipeline, risk, execution, persistence, REST, WebSocket, recovery, dan acceptance contract.
2. [`frontend-v0.md`](frontend-v0.md) — dashboard operator, account context, enam area aplikasi, command UX, realtime/resync, alert, dan safety presentation.
3. [`strategy-templates-v0.md`](strategy-templates-v0.md) — template dan fixture deterministik untuk XAUUSD, USDJPY, dan WTI serta kontrak bersama strategy plugin.
4. [`strategy-eurusd-snd-ao-qm-v0.md`](strategy-eurusd-snd-ao-qm-v0.md) — override normatif EURUSD menggunakan Supply/Demand + AO + Quasimodo.
5. [`fixtures/`](fixtures/) — executable specification untuk strategy scenarios dan mutation boundaries.
6. [`../../CONTEXT.md`](../../CONTEXT.md) — bahasa domain kanonik.

Dokumen di atas bukan proposal alternatif. Mereka adalah bab terpisah dari satu kontrak implementasi yang dirujuk melalui file ini.

## 3. Conflict Precedence

Jika ditemukan konflik, gunakan urutan berikut:

1. Safety invariants, account isolation, broker truth, idempotency, fencing, reconciliation, dan risk gates pada `backend-v0.md`.
2. Aturan instrument-specific pada `strategy-eurusd-snd-ao-qm-v0.md` untuk EURUSD.
3. Kontrak strategy bersama dan template lain pada `strategy-templates-v0.md`.
4. Presentasi dan interaction contract pada `frontend-v0.md`.
5. Ringkasan pada dokumen root ini dan GitHub Issues.

`CONTEXT.md` menentukan arti istilah domain; ia tidak boleh digunakan untuk melemahkan safety invariant yang lebih spesifik.

## 4. Current V0 Strategy Set

| Pair | Strategy | Entry model | Default |
|---|---|---|---|
| XAUUSD | `TrendPullbackContinuationStrategy@0.1.0` | Market after deterministic M15 trigger | Disabled |
| USDJPY | `TrendPullbackContinuationStrategy@0.1.0` | Market after deterministic M15 trigger | Disabled |
| EURUSD | `SupplyDemandAOQMStrategy@0.1.0` | Pending Limit after H4/M30 confirmation | Disabled |
| WTI | `TrendFilteredBreakoutStrategy@0.1.0` | Market after deterministic M15 trigger | Disabled |

EURUSD **tidak** menggunakan template TrendPullback pada V0. Strategy EURUSD yang berlaku adalah override SND/AO/QM.

## 5. Delivery State

- D1 production deployment foundation telah diimplementasikan pada `main` di `deploy/`, `scripts/`, dan `tests/`.
- Implementasi aplikasi trading berikutnya belum dimulai pada clean baseline.
- Ticket T1–T11 serta D2–D3 tetap menjadi backlog implementasi.
- Nilai parameter strategy adalah editable test seeds, bukan klaim profitabilitas atau hasil kalibrasi broker.
- LIVE/FULL_AUTO tidak boleh dianggap aman hanya karena unit atau contract tests lulus; rollout harus melalui DEMO dan operational gates.

## 6. Implementation Rule

Setiap implementasi harus:

- dimulai dari `main` terbaru;
- menyebut bagian kontrak yang dipenuhi;
- mempertahankan account scope dan fail-closed behavior;
- menyertakan test untuk perilaku eksternal dan failure path;
- tidak mengubah dokumen kontrak secara diam-diam untuk menyesuaikan implementasi.

Perubahan scope atau safety contract harus memperbarui file root ini dan lampiran terkait dalam commit yang sama.
