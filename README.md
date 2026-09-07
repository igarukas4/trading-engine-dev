# Trading Engine V0

Repository ini adalah baseline bersih untuk implementasi Trading Engine V0.

## Current State

- Satu pintu masuk spesifikasi kanonik tersedia di [`docs/spec/trading-engine-v0.md`](docs/spec/trading-engine-v0.md).
- Kontrak backend, frontend, dan strategy di `docs/spec/` merupakan lampiran normatif dari spec tersebut, bukan proposal yang saling bersaing.
- Domain glossary dipertahankan di `CONTEXT.md`.
- D1 production deployment foundation sudah tersedia di `deploy/`, `scripts/`, dan `tests/`.
- Implementasi aplikasi trading berikutnya belum dimulai di `main`.

## Start New Work

Selalu mulai dari `main` terbaru dan gunakan satu branch yang fokus untuk setiap perubahan:

```bash
git checkout main
git pull --ff-only origin main
git checkout -b feat/<nama-perubahan>
```

Gunakan spesifikasi di `docs/spec/` sebagai source of truth. Ticket implementasi yang masih terbuka tetap tersedia di GitHub Issues.

## Verification

Jalankan seluruh kontrak D1 sebelum menggabungkan perubahan:

```bash
for test in tests/*.sh; do bash "$test"; done
for file in scripts/*.sh tests/*.sh; do bash -n "$file"; done
git diff --check
```

Tes kontrak D1 tidak menggantikan deployment nyata. Sebelum production, verifikasi Docker Compose, HTTPS, smoke check, rollback, dan backup/restore pada VPS sesuai `deploy/README.md`.

## Recovery Snapshot

State repository sebelum pembersihan disimpan pada GitHub Release `repo-cleanup-backup-20260907` sebagai Git bundle lengkap.
