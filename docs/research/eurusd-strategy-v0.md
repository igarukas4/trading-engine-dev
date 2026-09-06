# Riset kandidat strategi utama EURUSD untuk V0

**Status:** rekomendasi riset, bukan keputusan strategi final

> **ADDENDUM — Decision B (override):** After this research recommended the trend-pullback/channel-continuation candidate, the user supplied a QM (Quasimodo) + AO (Awesome Oscillator) + supply/demand creator framework they wanted as the EURUSD V0 strategy. That framework was never examined in this document. On review the user selected it as the EURUSD source of truth (decision B). The accepted EURUSD strategy is now the **supply/demand + AO + Quasimodo** plugin, specified deterministically in [`docs/spec/strategy-eurusd-snd-ao-qm-v0.md`](../spec/strategy-eurusd-snd-ao-qm-v0.md). The trend-pullback recommendation below remains valid research but does **not** define the active EURUSD V0 entry; it may be revisited as a challenger (see §9.4 of that spec). XAUUSD and USDJPY still use the trend-pullback template.

**Ticket:** [#14 — Riset: Kandidat strategi utama EURUSD untuk V0](https://github.com/igarukas4/trading-engine-dev/issues/14)

**Batas:** strategi rule-based deterministic untuk closed candle H4/H1/M15; tidak mengimplementasikan engine dan tidak mengklaim profit.

## Ringkasan eksekutif

Kandidat utama yang paling masuk akal untuk **dibawa ke eksperimen dan grilling**, bukan langsung dikunci sebagai template produksi, adalah:

> **H4 trend/regime filter → H1 pullback atau konsolidasi searah tren → M15 closed-candle continuation breakout**, dengan session gate, spread/slippage gate, dan event-risk blackout di luar plugin Strategy.

Alasannya:

1. Ia cocok langsung dengan kontrak H4 filter + H1 setup + M15 trigger dan dapat dievaluasi secara murni/deterministic dari snapshot closed candle.
2. Literatur memberi dasar lintas-aset untuk continuation/trend, tetapi horizon bukti terkuat jauh lebih panjang daripada H4/M15; jadi bukti itu hanya prior penelitian, bukan validasi template intraday ini.[8]
3. Literatur FX mendefinisikan moving-average dan channel breakout sebagai rule mekanis yang lazim diteliti, sekaligus memperingatkan bahwa pemilihan parameter tanpa dasar membuka data-snooping/data-mining.[3]
4. Breakout sebagai pemicu searah H4 mengurangi konflik regime dibanding breakout murni. Mean-reversion tetap layak sebagai challenger khusus `RANGING`, bukan strategi utama lintas-regime.
5. Bukti intraday FX menunjukkan hasil rule sangat sensitif terhadap biaya transaksi dan jam perdagangan; predictability harga tidak sama dengan profit yang dapat dieksekusi.[4]

**Urutan kandidat untuk diuji:**

1. **Utama:** trend-aligned pullback/continuation breakout.
2. **Challenger A:** volatility/session-gated breakout murni.
3. **Challenger B:** range-gated mean-reversion.
4. **Bukan strategi mandiri:** session/time-of-day; gunakan sebagai gate dan dimensi evaluasi lebih dulu.

## 1. Kontrak repo yang membatasi desain

Rekomendasi ini mengikuti [`CONTEXT.md`](../../CONTEXT.md) dan [`docs/spec/backend-v0.md`](../spec/backend-v0.md):

- `Strategy.evaluate(state, config)` harus pure/deterministic, maksimal satu `Opportunity`, dan tidak membaca news/AI, mutable account state, atau broker.
- Semua keputusan memakai candle **closed** dan snapshot lengkap; gap, open candle, revisi tidak cocok, atau lookback kurang harus menghasilkan skip yang diaudit.
- Trigger evaluasi adalah M15; H4 dan H1 yang dipakai adalah candle terakhir yang sudah closed pada `trigger_time` yang sama.
- Session, spread, slippage, volatility, event blackout, account risk, dan expiry akhir tetap merupakan gate pipeline/RiskEngine. Strategy boleh mengeluarkan reason code teknikal, tetapi tidak boleh mengambil dependency broker/news tersembunyi.
- Native SL/TP MT5 wajib ada saat entry. Dalam satu Position MT5 hanya ada satu TP native; target parsial harus menjadi durable reduce-only `PositionCommand` dan trailing harus capability-gated.
- `signal_ttl` merupakan konfigurasi immutable; Signal expired tidak boleh dieksekusi.

Konsekuensinya: “strategi” di dokumen ini adalah aturan teknikal yang reproducible. Safety dan eksekusi broker tetap mengikuti spec, bukan diduplikasi di plugin.

## 2. Bukti yang relevan dan batas transfernya

### 2.1 Karakter EURUSD dan sesi

EURUSD layak sebagai pair pertama dari sisi likuiditas relatif: BIS mencatat USD berada pada satu sisi 88% transaksi FX April 2022 dan euro adalah mata uang kedua paling aktif; tabel pasangan menempatkan USD/EUR pada 22,7% turnover global.[1] Ulasan struktur pasar BIS menyebut EURUSD sekitar 23% volume dan aktivitas biasanya memuncak saat jam London dan New York overlap, sedangkan akhir sore New York/awal pagi Asia relatif tipis.[2] Literatur klasik DM/USD juga memisahkan pola aktivitas intraday dan pengumuman makro saat memodelkan volatilitas, sehingga evaluasi lintas-session tidak boleh diabaikan.[7]

Implikasi yang boleh ditarik:

- baseline spread harus **pair + BrokerAccount + session specific**, bukan angka pip universal;
- eksperimen harus dipecah per sesi dan kondisi likuiditas;
- entry baru pada jam tipis/rollover sebaiknya fail closed, bukan diasumsikan sama dengan overlap.

Implikasi yang **tidak** boleh ditarik: pair paling likuid tidak otomatis membuat suatu rule profitable, dan statistik wholesale/interdealer tidak sama dengan fill retail MT5.

### 2.2 Trend-following dan breakout

Kajian Federal Reserve St. Louis merumuskan tiga keluarga rule mekanis: filter, double moving-average, dan channel; channel masuk long/short ketika harga melewati maksimum/minimum `n` periode sebelumnya. Kajian itu juga menegaskan bahwa parameter lazim dipilih berdasarkan praktik tanpa panduan kuat, sehingga data-snooping merupakan risiko utama.[3]

Bukti time-series momentum menemukan persistence 1–12 bulan pada futures/forward lintas aset termasuk currency, lalu partial reversal pada horizon lebih panjang.[8] Ini mendukung **prior** bahwa continuation layak diuji, tetapi tidak membuktikan momentum H4/H1/M15 pada spot EURUSD retail.

Bukti mikrostruktur New York Fed menemukan tren dapat bergerak cepat setelah level tempat stop-loss berkelompok tersentuh, respons stop-loss lebih besar dan lebih lama daripada take-profit, dengan banyak hasil signifikan selama jam tetapi tidak hari.[9] Ini masuk akal sebagai alasan menguji breakout buffer dan slippage guard; bukan alasan mengejar harga atau menaruh SL tepat di angka bulat.

### 2.3 Mean-reversion

Bukti yang mendukung reversal intraday lebih rapuh dan sangat cost-sensitive. Studi intraday FX St. Louis Fed menemukan komponen yang dapat diprediksi, tetapi tidak menemukan excess return setelah biaya realistis dan jam perdagangan diperhitungkan.[4] Hasil itu konsisten dengan reversal/autocorrelation jangka sangat pendek yang dapat habis oleh spread.

Karena itu mean-reversion harus diuji hanya ketika H4/H1 menyatakan range, dengan target lebih pendek dan gate biaya lebih ketat. Ia tidak layak menjadi default ketika H4 trending atau saat price discovery akibat news sedang berlangsung.

### 2.4 Session/time-of-day

Studi Swiss National Bank dengan data EBS 1997–2007 menemukan pola time-of-day dan order flow. Sebagian besar strategi sesi sederhana tidak profitable setelah biaya, meskipun EURUSD merupakan pengecualian pada sampel tersebut.[10] Sampel lama dan venue interdealer membuat temuan ini hipotesis segmentasi, bukan template live saat ini.

Rekomendasi: untuk V0, session adalah **gate/stratum**, bukan directional signal. Jangan hardcode bias “morning short/afternoon long” sebelum bukti point-in-time broker sendiri lolos out-of-sample.

### 2.5 News, spread, dan slippage

Riset macro-announcement menunjukkan surprise menghasilkan jump pada conditional mean exchange rate dan reaksi dapat asimetris.[5] Riset 20 tahun data EBS menemukan peningkatan lebih jelas pada kecepatan price discovery daripada liquidity recovery; keduanya tidak boleh dianggap pulih bersamaan.[6]

Implikasi:

- news/event tidak dimasukkan ke Strategy; gunakan `EnrichmentPolicy` dan PRE_ORDER `RiskAssessment` sesuai spec;
- setelah blackout berakhir, jangan langsung menganggap spread/slippage normal; minta quote fresh, spread guard lolos, dan minimal satu M15 closed candle pasca-blackout;
- breakout paling sensitif terhadap gap/slippage saat news; mean-reversion paling rentan melawan repricing fundamental; trend continuation dapat ikut repricing tetapi tetap berisiko entry terlambat.

MT5 menerima `sl`, `tp`, dan `deviation` pada trade request; `deviation` adalah deviasi maksimum yang diterima dalam points.[11] Namun setting request bukan bukti fill atau proteksi aktif—reconciliation spec tetap authoritative.

## 3. Perbandingan kandidat

| Keluarga | Regime cocok | Regime buruk | Kecocokan H4→H1→M15 | Biaya & execution | Risiko utama | Putusan riset |
|---|---|---|---|---|---|---|
| Trend-aligned pullback/continuation | `TRENDING`, volatilitas normal–tinggi tetapi tidak ekstrem | `SIDEWAYS`, whipsaw, trend sangat extended | Sangat tinggi: H4 arah, H1 setup, M15 continuation | Frekuensi moderat; buffer membantu tetapi breakout tetap slippage-sensitive | late entry, whipsaw, parameter EMA/ADX terlalu banyak | **Kandidat utama** |
| Breakout murni | kompresi lalu ekspansi; London/overlap | range noisy, thin session, announcement jump | Tinggi: H4 regime opsional, H1 channel, M15 confirm | Spread dan stop-entry slippage paling kritis | false breakout, gap, stop cascade | Challenger A |
| Mean-reversion | `RANGING` stabil, ATR/spread normal | trend kuat, structural break, news | Cukup: H4 non-trend, H1 extreme, M15 reversal | Target pendek membuat cost share besar | menangkap “falling knife”, average berubah | Challenger B |
| Session/time-of-day directional | pola order-flow yang stabil | perubahan venue/regime/DST/holiday | Secara teknis mudah | Sangat sensitif spread dan timestamp | decay, data-mining kalender | Gate/stratum dulu |

Tidak ada baris yang merupakan klaim profit.

## 4. Kandidat utama: aturan deterministic yang diusulkan

Nama kerja: `EURUSDTrendContinuationV0`.

### 4.1 Input dan alignment

Pada close M15 `t`:

1. Pilih M15 terakhir dengan `close_time = t`.
2. Pilih H1 dan H4 terakhir dengan `close_time <= t`; tidak boleh menggunakan candle yang masih open.
3. Seluruh candle dan indikator harus berasal dari `BrokerAccount`, Pair mapping, source revision, dan config version yang sama.
4. Bila dua arah sekaligus lolos, return `None` + `CONFLICTING_SETUPS`.

### 4.2 H4 trend/regime filter

**LONG** hanya bila semua benar; SHORT simetris:

- `EMA_fast(H4) > EMA_slow(H4)`;
- slope `EMA_slow` selama `slope_bars` positif;
- close H4 di atas `EMA_slow`;
- optional `ADX(H4) >= adx_min`;
- jarak `abs(EMA_fast - EMA_slow) / ATR(H4)` minimal `ema_separation_atr`;
- tidak `VOLATILE` ekstrem menurut regime/volatility gate pipeline.

Filter ADX dan separation harus diuji sebagai **ablation**; jangan wajibkan keduanya hanya karena lebih banyak filter terlihat lebih “aman”.

### 4.3 H1 setup

Uji dua varian di bawah satu keluarga, tetapi jangan campur hasilnya:

**P — pullback/resumption**

- dalam `setup_age_h1` candle terakhir, low (LONG) menyentuh atau mendekati `EMA_pullback(H1)` dalam `pullback_tolerance_atr × ATR(H1)`;
- tidak ada H1 close menembus H4 slow trend boundary;
- H1 terakhir kembali close searah tren dan body bukan doji menurut `min_body_fraction`.

**C — consolidation/channel**

- range H1 `channel_h1` bar berada di bawah `max_channel_width_atr × ATR(H1)`;
- channel tidak membatalkan arah H4;
- setup hanya hidup `setup_age_h1` bar agar konsolidasi lama tidak dipakai ulang tanpa batas.

Varian P adalah baseline; C adalah challenger di dalam keluarga utama.

### 4.4 M15 trigger

**LONG**:

- M15 terakhir close di atas high dari `channel_m15` closed bars sebelumnya;
- breakout buffer minimum adalah maksimum dari `buffer_atr × ATR(M15)` dan `buffer_spread_multiple × spread_at_evaluation`;
- body close searah tren dan memenuhi `min_body_fraction`;
- trigger masih berada dalam session yang diizinkan.

SHORT simetris. Trigger intrabar/tick tidak valid untuk Opportunity; stop order yang kemudian dipakai ExecutionEngine tidak mengubah fakta bahwa keputusan Strategy lahir dari M15 closed.

### 4.5 TTL

Grid awal `signal_ttl_m15 = {1, 2, 4}` closed M15 bars (15/30/60 menit), baseline **2 bars / 30 menit**.

TTL harus dipendekkan ke batas terdekat berikut bila lebih awal:

- session entry window berakhir;
- event blackout dimulai;
- StrategyConfig/config revision diganti;
- MarketState atau risk input menjadi stale menurut spec.

Setelah TTL, engine wajib mengevaluasi setup baru; tidak “menghidupkan kembali” Signal lama.

## 5. Rentang parameter untuk eksperimen

Rentang ini adalah **ruang eksperimen terbatas**, bukan angka proven atau default live final.

| Lapisan | Parameter | Rentang/grid awal | Baseline riset |
|---|---|---:|---:|
| H4 | `ema_fast` | 20, 34, 50 | 34 |
| H4 | `ema_slow` | 100, 150, 200; selalu > fast | 150 |
| H4 | `slope_bars` | 3, 5, 8 | 5 |
| H4 | `adx_period` | 10, 14, 20 | 14 |
| H4 | `adx_min` | 18, 22, 25 | 22 |
| H4 | `ema_separation_atr` | 0, 0,10, 0,20 | 0,10 |
| H1 | `ema_pullback` | 20, 34, 50 | 34 |
| H1 | `atr_period` | 10, 14, 20 | 14 |
| H1 | `pullback_tolerance_atr` | 0,25; 0,50; 0,75 | 0,50 |
| H1 | `setup_age_h1` | 1, 2, 3 | 2 |
| H1 | `channel_h1` (varian C) | 12, 20, 24 | 20 |
| H1 | `max_channel_width_atr` | 1,5; 2,0; 2,5 | 2,0 |
| M15 | `channel_m15` | 8, 12, 20, 24 | 12 |
| M15 | `atr_period` | 10, 14, 20 | 14 |
| M15 | `buffer_atr` | 0,05; 0,10; 0,15 | 0,10 |
| M15 | `buffer_spread_multiple` | 1,0; 1,5; 2,0 | 1,5 |
| H1/M15 | `min_body_fraction` | 0,40; 0,55; 0,70 | 0,55 |
| Signal | `signal_ttl_m15` | 1, 2, 4 | 2 |

Gunakan desimal canonical (`0.10`, bukan float biner) di config implementasi kelak. Jangan menjalankan full Cartesian product. Mulai dari baseline + one-factor ablation, lalu hanya tetangga parameter yang stabil.

### Minimum lookback/warm-up

EMA secara matematis memiliki memori tak hingga; periodenya bukan jumlah warm-up yang cukup. Aturan operasional awal:

- **H4:** minimal `3 × ema_slow` closed bars; baseline slow 150 → **450 H4**, maksimum grid 200 → **600 H4**.
- **H1:** minimal maksimum dari `3 × ema_pullback`, `3 × atr_period`, dan `channel_h1 + 1`; baseline → **102 H1** bila EMA 34, tetapi provision **150 H1** agar grid EMA 50 valid.
- **M15:** minimal maksimum dari `3 × atr_period` dan `channel_m15 + 1`; provision **60 M15** untuk grid ATR 20.

Eksperimen warm-up wajib membandingkan multiplier 3× vs 5× pada boundary data. Snapshot kurang satu bar pun harus skip; jangan mengisi gap atau mengulang indikator stale.

## 6. Session/regime policy

### 6.1 Session

Grid session yang diuji secara terpisah:

1. London daytime.
2. London–New York overlap.
3. Gabungan London open sampai akhir overlap.
4. Semua jam likuid sebagai control, dengan rollover/“witching hour” dikecualikan.

Gunakan IANA timezone (`Europe/London`, `America/New_York`) dan konversi ke UTC per tanggal agar DST benar. Holiday dan early-close harus eksplisit. Baseline riset adalah **gabungan London daytime + overlap**, bukan jam UTC hardcoded sepanjang tahun.

### 6.2 Regime

- Kandidat utama eligible pada `TRENDING`; `VOLATILE` hanya bila volatility guard tetap lolos.
- Breakout challenger eligible pada kompresi H1 dan ekspansi M15, bukan semata label `VOLATILE`.
- Mean-reversion eligible pada `RANGING`/`SIDEWAYS`, `ADX(H4)` rendah, dan tidak ada H4 EMA separation kuat.
- Jika klasifikasi MarketRegime dan filter internal berkonflik, baseline fail closed dan audit `REGIME_CONFLICT`; eksperimen boleh menguji salah satu sebagai advisory, tetapi harus versioned.

## 7. Spread, slippage, dan news sensitivity

Ikuti default accepted RiskLimits sebagai floor desain: spread maksimum 2× median Pair+session, slippage maksimum 0,15R, dan volatility maksimum 2,5× median ATR, dengan baseline sample/freshness eksplisit. Tambahan eksperimen:

- median spread rolling: 20, 40, 60 sesi sejenis;
- minimum sample: 20, 40 observasi session-day;
- skip bila spread saat evaluasi > min(`2× session median`, `0,10 × initial_stop_distance`);
- catat spread saat evaluasi, saat order dibuat, requested price, fill price, dan slippage dalam points serta R;
- market-vs-stop entry diuji terpisah karena fill semantics berbeda;
- jangan mengubah `deviation` untuk memaksa fill setelah rejection; Signal harus expire/re-evaluate.

Event-risk grid untuk high-impact EUR atau USD:

- pre-blackout: 30, 60, 90 menit;
- post-blackout: 30, 60, 120 menit;
- baseline riset: 60 menit sebelum dan 60 menit sesudah;
- entry baru kembali eligible hanya setelah official calendar healthy, satu M15 pasca-blackout closed, quote fresh, spread dan volatility guard lolos.

Pengaturan event final milik `EnrichmentPolicy`/RiskEngine dan masih memerlukan persetujuan manusia.

## 8. Exit architecture yang kompatibel dengan V0

### 8.1 Initial protection

- **Native SL wajib saat entry**, berdasarkan H1 invalidation swing + buffer `0,10/0,25/0,50 × ATR(H1)`.
- Guard jarak awal: uji `0,8/1,2/1,6 × ATR(H1)`; bila swing membutuhkan jarak di luar guard, skip, jangan memindahkan SL agar position size terlihat menarik.
- Native SL hanya boleh bergerak mengurangi risiko setelah fill; tidak pernah dilebarkan.
- Native TP awal tetap wajib karena spec. Untuk perilaku yang sama pada netting dan hedging, gunakan **final safety cap** native di `2,5/3,0/4,0R`, bukan berpura-pura MT5 mendukung beberapa TP pada satu Position.

### 8.2 TP1, TP2, dan trailing sisa

Grid awal:

- **TP1:** `0,75/1,00/1,25R`, reduce-only `25/40/50%` (baseline 1R, 40%).
- **TP2:** `1,50/2,00/2,50R`, reduce-only `25/30/40%` dari volume awal (baseline 2R, 30%).
- **Sisa trailing:** `20–50%` volume awal (baseline 30%).
- Trailing aktif setelah TP2; distance `1,5/2,0/2,5 × ATR(M15)` atau low/high `3/5/8` closed M15 bars, diuji sebagai dua keluarga terpisah.
- Update trailing hanya pada M15 close, monotonic, melewati broker minimum stop distance, dan capability/reconciliation gate lolos.

Pada netting, TP1/TP2 adalah durable reduce-only commands; native TP final cap menutup seluruh sisa bila tercapai sebelum/selama trailing. Pada hedging, jangan split menjadi beberapa Position untuk “meniru” TP tanpa keputusan arsitektur terpisah—baseline harus berperilaku sama pada dua accounting mode.

Jika partial reduction atau trailing tidak dapat dibuktikan atomic/exclusive sesuai capability, fail closed ke exit native SL/TP yang sudah terkonfirmasi. Jangan mendekati safety dengan command best-effort.

## 9. Challenger definitions

### 9.1 Breakout murni

- H4: tidak berlawanan dengan EMA slow; ADX boleh netral.
- H1: channel `12/20/24/48` bars dan compression width `1,5–3,0 ATR`.
- M15: close di luar H1 channel + buffer yang sama.
- TTL: 1–2 M15 bars.
- Fokus evaluasi: false-break rate, entry slippage, MAE setelah gap, dan performa tanpa announcement window.

### 9.2 Mean-reversion range-gated

- H4: `ADX(10/14/20) <= 15/18/22`, EMA separation <= `0/0,10/0,20 ATR`.
- H1: deviation dari EMA 20/34/50 sebesar `1,0/1,5/2,0 ATR`, atau RSI 10/14/20 pada `25–35` / `65–75`; dua keluarga jangan digabung dalam satu sweep.
- M15: closed reversal kembali ke dalam range, bukan limit order buta saat extreme pertama.
- TP lebih pendek `0,5–1,25R`, TTL 1–2 bars, no entry saat event blackout/volatility shock.
- Kill condition: satu H1 close di luar range atau H4 trend filter aktif.

Mean-reversion wajib dinilai net of spread dengan lebih keras karena target lebih dekat.

## 10. Rancangan eksperimen sebelum angka dikunci

V0 tidak memiliki backtest engine. Validasi ini adalah pekerjaan riset/offline terpisah atau tooling setelah scope disetujui; dokumen ini tidak meminta implementasi backtest di engine.

### 10.1 Data

- data point-in-time broker/account sendiri: M1 bid/ask atau OHLC + observed spread, lalu bangun M15/H1/H4 persis seperti backend;
- minimal mencakup beberapa regime volatilitas, siklus DST, tahun kalender, announcement EUR/USD, dan kondisi stress;
- simpan revision dan larang look-ahead; signal hanya pada close M15;
- biaya: spread aktual, commission, swap bila posisi melewati rollover, slippage empirical per order/session/volatility bucket.

### 10.2 Protokol

1. Bekukan definisi rule dan grid kecil sebelum melihat test set.
2. Gunakan chronological walk-forward; training untuk memilih neighborhood, validation untuk pruning, test terakhir hanya sekali.
3. Purge/embargo window yang overlap karena satu setup dapat hidup beberapa bars.
4. Bandingkan dengan no-trade dan rule sederhana; jangan memilih berdasarkan gross PnL saja.
5. Jalankan stress cost 1×/1,5×/2× observed spread+slippage.
6. Lakukan parameter-neighborhood test; kandidat harus tidak runtuh karena perubahan satu step.
7. Laporkan per year, session, regime, long/short, event proximity, dan broker account.
8. Setelah offline, forward-evaluate di DEMO dengan order/fill/reconciliation nyata sebelum pertimbangan LIVE. Ini bukan klaim atau jaminan hasil LIVE.

### 10.3 Output wajib

- jumlah evaluation, Opportunity, eligible Signal, order, fill, dan exit lengkap;
- coverage/skip reasons: gap, warm-up, spread, volatility, blackout, stale, conflict;
- gross vs net result, turnover, hit rate **bersama** average win/loss, expectancy dalam R, MAE/MFE;
- drawdown, tail loss, time-in-market, holding time, adverse slippage percentile;
- TP1/TP2/trailing command success, UNKNOWN/reconciliation, dan protection confirmation failures;
- confidence interval/bootstrap per trade cluster/day, bukan asumsi trade independen;
- seluruh variasi yang dicoba agar multiple-testing terlihat.

Tidak ada threshold “profitable” yang dikunci di sini. Minimum sample/trade count, drawdown cap, net expectancy margin di atas stress cost, dan operational error budget harus diputuskan manusia pada grilling.

## 11. Keputusan yang masih memerlukan persetujuan manusia

1. Apakah kandidat utama memakai H1 pullback (P), consolidation (C), atau keduanya sebagai StrategyConfig terpisah.
2. Session entry window final dan holiday/DST calendar implementation.
3. ADX/separation mandatory atau advisory; definisi `MarketRegime` authoritative saat konflik.
4. Grid event blackout per event kind EUR/USD.
5. Market vs pending-stop entry dan TTL final.
6. Persentase TP1/TP2/sisa, final native TP cap, serta kapan trailing diaktifkan.
7. Apakah trailing/partial exit diaktifkan pada V0 sejak awal atau hanya setelah capability fixtures lulus.
8. Dataset, periode, broker/account, stress-cost, minimum sample, dan acceptance thresholds.
9. Apakah hasil DEMO diwajibkan sebelum StrategyConfig dapat diaktifkan pada LIVE; backend spec saat ini tidak mensyaratkan masa DEMO minimum.

## 12. Kesimpulan

Bukti yang tersedia tidak membuktikan satu template EURUSD intraday profitable. Ia mendukung menguji rule mekanis yang sederhana, membatasi parameter, memperlakukan session/regime sebagai kondisi eksplisit, dan menilai seluruh hasil setelah biaya serta execution failure.

Rekomendasi inti untuk grilling berikutnya adalah **trend-aligned H4 filter + H1 pullback + M15 closed continuation breakout** sebagai kandidat utama, dengan breakout murni dan mean-reversion range-gated sebagai challengers. Baseline angka di dokumen ini adalah titik eksperimen, bukan keputusan final dan bukan izin aktivasi LIVE.

## Sources

[1] https://www.bis.org/publications/202210-commentary-otc-derivatives.pdf — OTC foreign exchange turnover in April 2022
[2] https://www.bis.org/publications/working-paper-1094-foreign-exchange-market.pdf — The foreign exchange market
[3] https://files.stlouisfed.org/files/htdocs/wp/2011/2011-001.pdf — Technical Analysis in the Foreign Exchange Market
[4] https://files.stlouisfed.org/files/htdocs/wp/1999/99-016.pdf — Intraday Technical Trading in the Foreign Exchange Market
[5] https://www.nber.org/papers/w8959 — Micro Effects of Macro Announcements: Real-Time Price Discovery in Foreign Exchange | NBER
[6] https://www.nber.org/papers/w27036 — Price Discovery and Liquidity Recovery: Forex Market Reactions to Macro Announcements | NBER
[7] https://www.nber.org/papers/w5783 — DM-Dollar Volatility: Intraday Activity Patterns, Macroeconomic Announcements, and Longer Run Dependencies | NBER
[8] https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf — Time series momentum
[9] https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr150.html — Stop-loss orders and price cascades in currency markets
[10] https://www.snb.ch/public/asset/en/www-snb-ch/publications/research/working-papers/2011/working_paper_2011_04/publications0_en/working_paper_2011_04.n.pdf — Intraday patterns in FX returns and order flow
[11] https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py — order_send - Python Integration - MQL5 Reference
