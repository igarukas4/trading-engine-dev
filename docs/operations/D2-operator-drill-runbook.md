# D2 — Runbook drill operasional DEMO

Gunakan satu salinan runbook ini untuk satu `BrokerAccount` dengan Environment `DEMO`. Ini adalah panduan operator dan template bukti, bukan otorisasi untuk mengakses VPS, MT5, atau broker. Jangan masukkan credential, token, isi secret file, alamat yang tidak boleh dibagikan, atau password ke dokumen, shell history, ticket, maupun chat.

Rujukan deployment: [`deploy/README.md`](../../deploy/README.md). Prasyarat dan batas bukti: [`D2-human-prerequisites.md`](D2-human-prerequisites.md). Istilah domain memakai [`CONTEXT.md`](../../CONTEXT.md).

## Identitas drill

| Field | Nilai operator |
| --- | --- |
| `BrokerAccount` / account scope | |
| Provider / broker server / external account ID | |
| Environment | `DEMO` |
| Pair yang diuji | |
| Operator dan on-call | |
| Mulai–selesai (UTC) | |
| Release image digest dan connector build | |
| Dashboard evidence link / lokasi artefak tersanitasi | |

## Aturan berhenti

- Jangan lanjut ke command atau order bila account bukan `DEMO`, identity connector tidak cocok, readiness/reconciliation tidak sehat, atau `RiskLimits`, Pair mapping, session policy, dan event-blackout belum aktif.
- `UNKNOWN`, `ATTENTION_REQUIRED`, quarantine, atau operasi `IN_PROGRESS` adalah hasil terbuka. Catat command/audit ID, lakukan recovery/reconciliation yang dijelaskan sistem, dan jangan melakukan retry broker secara buta.
- `EMERGENCY_STOP` menolak entry baru; ia tidak menutup Position secara otomatis. `close_all` adalah command eksplisit dan harus memiliki otorisasi scope tersendiri.
- Tidak ada order DEMO dikirim sampai operator berwenang menyetujui **pada saat itu**: account, Pair, direction, volume maksimum, native SL/TP, jendela waktu, dan hasil uji yang diharapkan.
- Jika ada perbedaan antara dashboard dan MT5, MT5 adalah sumber kebenaran untuk Order, Fill, dan Position; tandai drill belum selesai sampai reconciliation konvergen.

## Sebelum memulai

Untuk connector MVP satu akun DEMO, ikuti `connector/README.md`. Provisioning secret dilakukan lewat Windows Credential Manager pada user yang menjalankan connector; config memuat `credential-manager://<target>`. Jika Credential Manager tidak tersedia, gunakan file UTF-8 di luar repository dengan ACL Windows yang tidak memberi akses baca/tulis ke principal luas, atau mode `0600` pada host POSIX. Provider membuka file tanpa mengikuti symlink/reparse point, memeriksa ACL dan tipe file pada handle yang sama, lalu membaca handle itu. File kosong, invalid, symlink/reparse point, atau ACL luas harus gagal. Jalankan `--preflight-only` lebih dulu. Perintah ini hanya membaca terminal, journal, dan status WSS/reconciliation. Catat hasil `PASS`/`FAIL` tanpa secret. `FAIL`, journal `UNKNOWN`, identity atau generation/epoch mismatch, terminal trade permission mati, atau backend gate tertutup berarti berhenti. Jangan mengubah flag config untuk melewati kegagalan.

Sesudah `PASS`, issue #68 tetap memerlukan approval manusia tersendiri untuk satu order DEMO tertentu. Baru sesudah approval itu, operator boleh menjalankan `--run --enable-manual-demo` dengan `execution_disabled=false`. Perubahan config saja tidak mengaktifkan command. Jika sesi putus, connector menonaktifkan dispatch sampai handshake dan reconciliation berikutnya tervalidasi. Untuk berhenti, hentikan penerimaan command, tunggu queue idle, dan simpan journal. Jangan hapus journal saat `INVOKING` atau `UNKNOWN`; gunakan observasi broker dan recovery, tanpa resend buta. Inspeksi health memakai account ID, status terminal, generation/epoch, heartbeat, jumlah journal per state, watermark, dan ticket eksternal yang sudah disanitasi.

Catat bukti tersanitasi berikut.

- VPS/DNS/firewall sesuai `deploy/README.md`; hanya SSH, 80, dan 443 terekspos.
- Semua image pada release file dipin ke digest; secret VPS-lokal serta smoke-password file terpisah ada, tidak tracked, owner deployment user, dan mode `0600`.
- `scripts/release.sh deploy` beserta authenticated smoke check berhasil; dashboard, host logs, dan alert destination dapat diakses operator.
- Backup berhasil, checksum lolos, restore ephemeral berhasil, dan satu release sebelumnya tersedia untuk rollback rehearsal.
- Account mulai `STOPPED` dan `MANUAL`; connector identity immutable cocok; handshake, generation/lease, market-data freshness, calendar, risk, native-protection, dan reconciliation menunjukkan sehat.

Jika salah satu butir gagal, hentikan dan buka incident record. Untuk deployment/release incident, kumpulkan log tersanitasi lalu gunakan rollback hanya menurut `deploy/README.md`; jangan mengubah secret atau image digest untuk “mencoba lagi”.

## Matriks drill

Jalankan berurutan bila preflight tetap sehat. Untuk setiap baris, isi waktu UTC, kondisi awal/akhir, command/audit ID, observasi MT5 dan dashboard, alert/log terkait, serta tautan bukti tersanitasi. Status selesai hanya `PASS` jika keadaan broker dan domain telah konvergen.

| # | Drill | Langkah operator tingkat tinggi | Bukti keberhasilan | Kondisi gagal/berhenti |
| --- | --- | --- | --- | --- |
| 1 | Onboarding connector | Bind satu connector pada full identity `BrokerAccount`, lalu lakukan handshake. | Generation/lease aktif dan identity yang tampil cocok. | Identity mismatch, credential error, atau lease/generation ambigu. |
| 2 | Reconciliation awal | Jalankan reconciliation sebelum exposure dibuka. | Snapshot Order/Fill/Position dan watermark konvergen dengan MT5. | `UNKNOWN`, position/order tak cocok, atau snapshot stale. |
| 3 | Market data | Verifikasi Pair mapping dan freshness feed untuk Pair scope tersebut. | Candle/MarketState bertimestamp benar dan health hijau. | Mapping ambigu, feed stale, atau data silang-account. |
| 4 | MANUAL command | Dengan `MANUAL`, uji alur Signal → approve → execute sesuai preflight. Bila command ini akan membuat order, tunggu otorisasi eksplisit terpisah dahulu. | Audit menunjukkan dua aksi berbeda dan RiskAssessment terbaru. | State/mode salah, risk/calendar/protection gate gagal, atau otorisasi belum ada. |
| 5 | Restart | Restart komponen connector menurut prosedur operator, tanpa mengubah account scope. | Restart, handshake, dan reconciliation selesai tanpa duplicate send. | Journal/command ambigu atau recovery belum konvergen. |
| 6 | Connector loss | Induksi/observasi loss secara terkontrol dan pastikan alert muncul. | Entry diblokir, alert tercatat, account lain (jika ada) tidak terpengaruh. | Exposure tetap terbuka untuk entry atau alert tidak tampak. |
| 7 | `UNKNOWN` recovery | Uji outcome ambigu yang aman atau gunakan kejadian drill yang tercatat; inspeksi journal lalu reconcile ke MT5. | Command/Order final berasal dari broker truth; tidak ada resend buta. | Bukti invocation tidak cukup atau MT5 belum konvergen. |
| 8 | Emergency stop | Kirim emergency stop yang disetujui operator. | Entry baru ditolak; monitoring, protection, dan reconciliation tetap berjalan. | Sistem mengklaim Position tertutup tanpa command eksplisit atau state belum fenced. |
| 9 | Explicit `close_all` | Hanya setelah otorisasi eksplisit yang memuat scope order/position; jalankan dan tunggu convergence. | Semua target yang disetujui final di MT5 dan dashboard, command/audit lengkap. | Pending/open/UNKNOWN Position atau efek broker belum terselesaikan. |

## Catatan per drill

Salin blok berikut untuk setiap nomor pada matriks.

```text
Drill #:                         Status: PASS / FAIL / UNRESOLVED
Tujuan dan kondisi awal:
Mulai–selesai UTC:
BrokerAccount / Pair / DEMO:
Tindakan operator (tanpa rahasia):
Command / audit / order / position IDs:
Observasi MT5 (otoritatif):
Observasi dashboard, health, log, alert:
Kondisi akhir dan watermark/reconciliation:
Link bukti tersanitasi:
Incident / tindak lanjut (jika ada):
```

## Penutupan dan gate D3

Setelah seluruh drill konvergen, catat konfigurasi yang disetujui untuk account DEMO dan LIVE: `RiskLimits`, Pair mappings, session policies, event-blackout configuration, designated accounts, bukti backup/restore/rollback, dan health/alert coverage. Lampirkan seluruh record ke issue D2 setelah disanitasi.

Dokumen ini tidak membuka D3. LIVE, `SEMI_AUTO`, atau `FULL_AUTO` tetap memerlukan persetujuan manusia eksplisit yang terpisah dan bukti D2 lengkap.
