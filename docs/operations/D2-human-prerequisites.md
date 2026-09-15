# D2 — Prasyarat operator dan bukti kesiapan DEMO

Dokumen ini adalah daftar persiapan untuk drill D2 pada satu `BrokerAccount` ber-Environment `DEMO`. Ini bukan otorisasi deployment, onboarding connector, atau pengiriman Order. Operator menjalankan setiap tindakan yang menyentuh VPS, terminal MT5, broker, DNS, atau layanan notifikasi; agent hanya dapat membantu meninjau bukti yang sudah disanitasi.

`BrokerAccount` dan `Pair` dipakai sesuai glossary proyek. Semua bukti harus menyebut account scope dan waktu (UTC). Jangan menyamarkan hasil yang belum converged sebagai berhasil: `UNKNOWN`, `ATTENTION_REQUIRED`, quarantine, atau operation `IN_PROGRESS` tetap merupakan hasil terbuka sampai reconciliation otoritatif selesai.

## Batas data

| Boleh dicatat dalam ticket/runbook/bukti tersanitasi | Rahasia — hanya masukkan lokal pada VPS/terminal/password manager |
| --- | --- |
| Nama DNS/domain, alamat VPS yang sudah disetujui, registry/image **digest**, versi release, ID `BrokerAccount`, provider dan broker server, environment `DEMO`, Pair, timestamp, status health/command/reconciliation, ID audit/command/order/position yang aman dibagikan | SSH private key/password, password Basic Auth, isi `deploy/secrets/*`, PostgreSQL/Redis/application secret, token registry, token/credential alert, password atau investor password MT5, connector secret, API key RSS/AI/Telegram |

Jangan mengirim rahasia lewat chat, Git, GitHub Issue, URL, screenshot, log, command line, environment variable yang tercatat shell history, atau artefak drill. Buat dan simpan rahasia pada VPS/Windows terminal sesuai hak akses lokal. Bila bukti perlu menunjukkan konfigurasi, redaksi nilainya dan tampilkan hanya nama field, permission, fingerprint/digest yang tidak dapat dipakai untuk autentikasi, serta status berhasil/gagal.

## Prasyarat yang perlu disediakan operator

| Area | Yang disediakan/dilakukan manusia | Bukti yang dicatat (tanpa rahasia) |
| --- | --- | --- |
| VPS dan DNS | Satu VPS tujuan, Docker Engine + Compose plugin, akses operator, DNS dashboard menunjuk ke VPS, dan firewall hanya membuka SSH (dibatasi IP operator bila mungkin), 80, dan 443. Database, Redis, dan backend tidak diberi host port. | Nama domain, provider/region, waktu DNS propagasi, ringkasan aturan firewall/port, versi Docker/Compose, dan hasil HTTPS `/healthz`. |
| Release immutable | Image backend yang mengimplementasikan `GET /health/live` pada port 8000 dan semua image pada `release.env` dipin ke `@sha256:` digest. Operator menempatkan release file di lokasi terlindungi, di luar Git. | Digest setiap image, lokasi release file (tanpa isi), waktu deploy, output tersanitasi `release deployed`, dan hasil smoke check. |
| Secret deployment | Di VPS, buat `app_secret_key`, `caddy_basic_auth_hash`, `postgres_password`, dan `redis_password` untracked dengan owner deployment user serta mode `0600`; plaintext Basic Auth disimpan di password manager dan file password smoke terpisah `0600` di luar repo. | Nama file, owner/mode `0600`, dan konfirmasi password manager; jangan lampirkan nilai, hash, atau output generator. |
| Backup dan rollback | Storage backup lokal dengan permission terbatas, tujuan encrypted off-host yang disetujui, jadwal host timer, dan satu release sebelumnya agar rollback bermakna. | Nama/artifact backup, checksum verification pass, restore-verification pass, jadwal/retensi, release digest sebelum/sesudah rehearsal rollback, dan smoke pass setelahnya. |
| MT5 DEMO dan connector | Akun DEMO yang dipilih, terminal MT5 Windows yang login pada server broker yang tepat, connector build yang kompatibel, serta satu binding yang terikat pada identitas immutable penuh `(provider, broker_server, external_account_id)`. Connector secret dibuat/masuk hanya di secure credential storage Windows dan hash-only binding server. | ID `BrokerAccount`, provider, broker server, external account ID yang telah disetujui untuk bukti, `DEMO`, generation/lease dan readiness/reconciliation status, connector build/version, timestamp handshake. Jangan bukti kredensial MT5/connector. |
| Konfigurasi trading DEMO | RiskLimits aktif, Pair mapping valid, StrategyConfig dan EnrichmentPolicy aktif, session policy, calendar coverage/blackout, serta keputusan ukuran order DEMO yang dibatasi. Akun mulai `STOPPED` dan `MANUAL`. | ID/versi tiap konfigurasi, Pair, limit yang telah disetujui (tanpa credential), status calendar, readiness gates, dan screenshot/export dashboard account-scoped. |
| Observability dan alert | Akses operator ke Dashboard/System, Docker/Caddy logs, host disk/memory, status backup, serta tujuan alert yang diuji. Dashboard menyediakan health API/connector/calendar, state/mode/freshness, recovery/reconciliation, audit timeline, dan critical alert routing; delivery Telegram nyata bukan implementasi V0. | URL/deep-link yang aman, daftar penerima/on-call tanpa token, timestamp test alert, incident key, status delivery/preview, dan bukti health untuk connector, calendar, risk, protection, reconciliation, disk, memory, serta backup. |
| Otorisasi order DEMO | Seorang operator yang berwenang menamai account, Pair, direction, maximum volume/risk, native SL/TP, jendela waktu, serta alasan order. Ia menyetujui langkah submit **setelah** preflight sehat. | Record persetujuan dengan scope, waktu, parameter non-rahasia, alasan, dan command/audit ID. Tidak ada order dikirim sebelum checkpoint ini. |

Rujukan deployment yang berlaku adalah [`deploy/README.md`](../../deploy/README.md): `scripts/release.sh` memvalidasi secret `0600`, image digest, Compose, health, dan smoke; backup/restore menggunakan `scripts/backup-postgres.sh` dan `scripts/verify-backup-restore.sh`. Urutan drill dan template bukti ada di [`D2-operator-drill-runbook.md`](D2-operator-drill-runbook.md). D2 tetap harus membuktikan drill pada lingkungan nyata; contract/unit test bukan pengganti.

## Stage wizard yang diusulkan (belum diimplementasikan atau dijalankan)

Wizard masa depan hanya mengumpulkan pilihan non-rahasia, menampilkan langkah lokal yang harus dijalankan operator, dan meminta bukti tersanitasi. Ia tidak pernah membaca, mencetak, atau mentransmisikan credential.

1. **VPS/DNS** — konfirmasi host tujuan, domain, Docker/Compose, firewall dan hasil DNS/HTTPS health; berhenti bila port internal diekspos atau DNS belum siap.
2. **Pembuatan secret** — tampilkan instruksi lokal dari `deploy/README.md`, lalu minta hanya konfirmasi file nonempty, owner, dan mode `0600`.
3. **Image immutable dan release** — minta digest image dan metadata public, validasi format digest, lalu arahkan operator menjalankan deploy/smoke di VPS dengan password smoke file terpisah.
4. **Onboarding connector MT5 DEMO** — minta identity non-secret account, environment `DEMO`, dan bukti handshake/reconciliation; connector secret tetap dibuat serta dimasukkan langsung di terminal/secure storage.
5. **Setup alert dan observability** — konfirmasi deep link Dashboard/System, on-call, health source, dan hasil test alert/preview tanpa token atau payload sensitif.
6. **Checkpoint otorisasi sebelum order DEMO** — tampilkan preflight account scope, `STOPPED`/`MANUAL` state awal, readiness/risk/calendar/mapping/connector/protection status, serta parameter order yang dibatasi. Wizard berhenti dan menunggu persetujuan eksplisit operator sebelum ada tindakan `approve` atau `execute`.

## Rekaman minimum per drill D2

Untuk connector onboarding, reconciliation, market data, MANUAL command, restart, connector loss, `UNKNOWN` recovery, emergency stop, dan explicit `close_all`, buat satu entri bukti berisi: account scope/`DEMO`, UTC start-end, tujuan dan langkah yang dilakukan operator, kondisi awal, command/audit ID, observasi authoritative dari MT5 dan dashboard, status akhir, serta link artefak tersanitasi. Emergency stop tidak otomatis menutup Position; catat `close_all` sebagai command eksplisit terpisah. Jika broker belum konvergen, catat hasil sebagai unresolved dan lanjutkan melalui reconciliation—jangan retry buta.

Sebelum D2 dinyatakan lengkap, catat pula daftar account DEMO/LIVE yang ditetapkan, RiskLimits, Pair mappings, session policies, dan event-blackout configuration yang disetujui. D3 tidak dibuka oleh dokumen ini: live unlock dan progres MANUAL → SEMI_AUTO → FULL_AUTO tetap memerlukan persetujuan manusia berdasarkan bukti D2.
