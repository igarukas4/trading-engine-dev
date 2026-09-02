# Trading Engine

Algorithmic trading engine — modular pipeline dari market data ingestion sampai eksekusi order, dengan AI-assisted market reasoning. Forex via MT5 (eksekusi pasar nyata), crypto/Binance sebagai arah pengembangan.

## Language

**Pair**:
Instrument yang diperdagangkan: `EURUSD`, `BTCUSDT`, `XAUUSD`.
_Avoid_: Instrument, Asset, Symbol (Symbol = identifier native broker, e.g. MT5).

**Symbol**:
Identifier pair di broker tertentu (e.g. MT5: `EURUSD`, Binance: `BTCUSDT`). Tidak dipakai sebagai istilah domain kanonik.

**BrokerAccount**:
Identitas akun trading pada broker yang menjadi pemilik Order, Fill, Position, RiskLimits, dan State. Berbeda dari akun pengguna dashboard.

**MarketState**:
Snapshot multi-timeframe pasar saat ini — OHLCV bars (M5/M15/H1/H4/D1) + indikator terkomputasi (EMA/ATR/RSI/dll). INPUT yang di-feed ke `Strategy.evaluate()`. Selalu punya timestamp dan `pair`.
_Avoid_: MarketContext (itu broader intelligence).

**MarketStateSnapshot**:
Rekaman immutable MarketState yang benar-benar dipakai dalam satu Strategy evaluation. Mereferensikan Candle dan IndicatorValue agar Opportunity dapat direproduksi dan diaudit.

**IndicatorValue**:
Nilai indikator terkomputasi untuk Pair, timeframe, candle, definisi/versi indikator, dan parameter tertentu. Disimpan sebagai time-series; berbeda dari parameter indikator yang berada di StrategyConfig.

**MarketRegime**:
Klasifikasi turunan dari MarketState via statistical engine: `TRENDING`, `RANGING`, `VOLATILE`, `SIDEWAYS`. Dipakai untuk adaptasi strategi & risk sizing.

**MarketContext**:
Kondisi pasar luas di luar data teknikal: news sentiment, macro bias, correlation antar pair, event risk. Output AI/news pipeline, bukan derivasi dari bar harga.

### Strategy & Signal

**Strategy**:
Interface/algoritma evaluasi pasar: `EMATrendStrategy`, `BreakoutStrategy`, `MeanReversionStrategy`. Plug-and-play — semua strategi mengikuti `Strategy.evaluate(state: MarketState, config: StrategyConfig) → Opportunity | None`. Evaluasi bersifat deterministic: input dan config yang sama menghasilkan output yang sama. Jika setup LONG dan SHORT konflik, Strategy mengembalikan `None` dan mencatat alasan `CONFLICTING_SETUPS` di evaluation log.

**StrategyConfig**:
Versi immutable dari instance Strategy terparameterisasi yang dapat dipilih sebagai versi aktif. Satu identitas config logis memiliki urutan versi immutable; Opportunity selalu menunjuk versi persis yang dievaluasi. Contoh: "EMA Trend, pair BTCUSDT+ETHUSDT, EMA 20/50, risk 0.75%, min score 72, timeframe H4/H1/M15". Bisa banyak StrategyConfig aktif bersamaan. Setiap StrategyConfig memiliki EnrichmentPolicy sendiri; plugin Strategy tetap tidak bergantung pada news/AI.

**EnrichmentPolicy**:
Aturan versioned per StrategyConfig yang menentukan sumber konteks `REQUIRED`, `ADVISORY`, atau `DISABLED`, freshness TTL, fallback yang eksplisit, dan event-risk rule. Versi yang dipakai dicatat pada Signal/AuditEvent agar keputusan lama dapat direproduksi setelah policy berubah.

**Opportunity**:
Candidate tunggal output mentah dari `Strategy.evaluate()`: `{pair, strategy_config_version_id, direction, confidence, evaluation_key}`. Belum punya harga entry/SL/TP — belum siap dieksekusi. Ketiadaan setup direpresentasikan sebagai `None`, bukan Opportunity dengan Direction `NONE`; `evaluation_key` mencegah duplikasi evaluasi pada versi config dan snapshot yang sama.

**Signal**:
Opportunity yang sudah di-enrich oleh pipeline menjadi proposal trade: `{pair, direction, entry_zone, stop_loss, take_profit[], technical_score, fundamental_score, ai_score, confidence, timestamp, expires_at}`. Signal bersifat immutable; re-score membuat revisi baru dan membatalkan approval revisi lama. Signal expired tidak dapat di-approve atau dieksekusi. RiskAssessment adalah gate terpisah dan harus diperbarui tepat sebelum Order dibuat.

**RiskAssessment**:
Hasil immutable dari RiskEngine untuk sebuah Signal pada snapshot akun/pasar tertentu: approved/rejected, reason codes, calculated PositionSize, limits dan versi state/context yang dipakai, serta masa valid. Assessment awal dibuat saat Signal lahir dan wajib dihitung ulang tepat sebelum Order dikirim.

**RiskReservation**:
Alokasi risiko sementara yang mencegah beberapa Signal memakai kapasitas exposure yang sama secara bersamaan. Dikonsumsi oleh Fill atau dilepas saat Order ditolak, dibatalkan, expired, atau selesai direkonsiliasi.

### Order & Position

**Order**:
Instruksi ke broker (belum tentu terisi). Dua tipe utama: **Market Order** (eksekusi langsung di harga pasar) dan **Pending Order** (eksekusi saat harga mencapai level tertentu).

**OrderStatus**:
Lifecycle canonical Order: `INTENT`, `CHECKED`, `DISPATCHING`, `SUBMITTED`, `UNKNOWN`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`. `UNKNOWN` hanya diselesaikan lewat reconciliation, bukan retry buta. Dashboard boleh mengelompokkan status menjadi `PENDING`, `SUCCESS`, atau `FAILED` disertai label detail; summary UI tidak boleh dipakai untuk logika domain.

**Pending Order Types** (standar MT5):
- **Buy Limit** — beli di bawah harga pasar (expecting bounce up).
- **Sell Limit** — jual di atas harga pasar (expecting bounce down).
- **Buy Stop** — beli di atas harga pasar (expecting breakout up).
- **Sell Stop** — jual di bawah harga pasar (expecting breakout down).
- **Buy Stop Limit** — kombinasi: saat harga capai stop price, pasang Buy Limit.
- **Sell Stop Limit** — kombinasi: saat harga capai stop price, pasang Sell Limit.

**Fill**:
Catatan milik BrokerAccount yang append-only atas deal broker: eksekusi parsial/penuh, reversal, close-by, charge, atau koreksi. Satu Order dapat memiliki beberapa Fill; koreksi tidak mengubah Fill lama.

**Position**:
State exposure broker hasil Fill—punya `pair`, `direction` (LONG/SHORT), `entry_price`, `current_pnl`, `stop_loss`, `take_profit`, dan external position ID. BrokerAccount menentukan accounting mode: netting mengagregasi per Pair, hedging mempertahankan Position independen; backend mendukung keduanya dan merekonsiliasi sesuai laporan MT5. Alur: Order → Fill → Position → Exit (close).

**Direction**:
Niat trade: `LONG` atau `SHORT`. Dipakai di Opportunity, Signal, Order, Position. “Tidak ada trade” adalah absence (`None`), bukan Direction.
_Avoid_: Side (ambigu — bisa buy-side/sell-side liquidity).

**StopLoss**:
Level harga untuk auto-exit jika pasar bergerak berlawanan—batas rugi per posisi. Untuk eksekusi V0, StopLoss native wajib dikirim ke MT5 agar proteksi tetap aktif saat backend/connector putus.

**TakeProfit**:
Level harga untuk auto-exit jika profit mencapai target. Strategi bisa punya beberapa target (TP1, TP2), tetapi satu Position MT5 hanya punya satu TP native; target tambahan diwujudkan sebagai perintah reduce-only yang direkonsiliasi. Proteksi native MT5 adalah sumber eksekusi.

### Risk

**RiskEngine**:
Gatekeeper deterministic sebelum eksekusi: memvalidasi position size, max risk/trade, daily loss limit, max open positions, correlation exposure. Menghasilkan RiskAssessment saat Signal dibuat dan wajib mengevaluasi ulang tepat sebelum Order dikirim.

**Equity**:
Nilai total akun saat ini (balance + floating PnL).

**Exposure**:
Total risiko terbuka sebagai persentase equity.

**Drawdown**:
Penurunan equity dari peak — diukur intraday (`DailyDrawdown`) dan keseluruhan.

**PositionSize**:
Ukuran lot/volume per posisi, dihitung oleh RiskEngine berdasarkan risk/trade.

### AI / News

**NewsEvent**:
Artikel/input mentah dari news feed (Google News RSS, Investing.com RSS).

**NewsAnalysis**:
Structured output AI dari NewsEvent: `{pair[], directional_bias, sentiment, severity, confidence, trade_impact, reason, expires_at}`.

**EconomicEvent**:
Jadwal event ekonomi terstruktur dari calendar resmi first-party untuk currency yang dipakai StrategyConfig aktif. Revision disimpan immutable; event menentukan blackout melalui EnrichmentPolicy, bukan melalui scraping Forex Factory.

**ManualEconomicEventOverride**:
Event operasional ber-expiry yang dibuat user untuk menambah atau memperpanjang blackout ketika source resmi belum lengkap/bermasalah. Selalu menghasilkan AuditEvent; bukan cara untuk memendekkan blackout secara diam-diam.

**Bias**:
Pandangan analitis tentang arah pasar: `bullish`, `bearish`, `neutral`. Dipakai di AI/news output, berbeda dari Direction (yang adalah niat trade).

### Bot Lifecycle

**Mode**:
Siapa yang mengizinkan pembuatan Order dari Signal: `MANUAL` (approve lalu execute melalui dua command user yang terpisah), `SEMI_AUTO` (approval user menjadwalkan Order), `FULL_AUTO` (Signal eligible dapat dijadwalkan tanpa approval). Perubahan Mode tidak mengeksekusi Signal lama secara otomatis.

**State**:
Status hidup engine per BrokerAccount: `RUNNING`, `STOPPED`, `EMERGENCY_STOP`. `STOPPED` menolak entry baru tetapi monitoring dan exit posisi terbuka tetap aktif. `EMERGENCY_STOP` selalu menolak entry baru; close-all hanya dijalankan bila diminta secara eksplisit.

**Command**:
Permintaan idempotent dan auditable untuk mengubah state domain atau broker. Command memiliki identitas, target, payload canonical, status, dan hasil; hasil ambigu tetap `UNKNOWN` sampai reconciliation.

### Pipeline

**DataEngine**:
Ingestion & normalisasi dari Market Source (MT5 connector) → menyimpan ke database + cache Redis.

**FeatureEngine**:
Komputasi indikator teknikal dari MarketState: EMA, ATR, RSI, MACD, ADX, dsb. Output: MarketState dengan indikator terisi.

**StrategyEngine**:
Pada candle pemicu yang closed, iterasi StrategyConfig aktif → `evaluate(MarketState, StrategyConfig)` → maksimal satu Opportunity per evaluasi atau `None`.

**DecisionEngine**:
Enrichment pipeline: Opportunity → tambah entry zone/SL/TP + skor technical/fundamental/AI → Signal. RiskEngine menilai Signal secara terpisah sebelum eksekusi.

**ExecutionEngine**:
Menerjemahkan Signal yang lolos RiskAssessment terbaru menjadi Order ke broker via MT5 connector. Monitor Fill & Position lifecycle. Handle exit (TP/SL/manual).

**Broker**:
Interface capability-based untuk market/account snapshots, validasi dan pengiriman Order, modifikasi/cancel/close, serta reconciliation Order/Fill/Position. Adapter MT5 mengimplementasikannya melalui connector remote; detail package MetaTrader5 tidak bocor ke core domain.

**AuditEvent**:
Catatan append-only tentang keputusan dan transisi penting—evaluasi Strategy, enrichment, RiskAssessment, command, Order, Fill, Position, perubahan State, dan reconciliation—agar setiap trade atau rejection dapat dijelaskan kembali.