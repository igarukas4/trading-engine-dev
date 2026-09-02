# Trading Engine

Algorithmic trading engine — modular pipeline dari market data ingestion sampai eksekusi order, dengan AI-assisted market reasoning. Forex via MT5 (eksekusi pasar nyata), crypto/Binance sebagai arah pengembangan.

## Language

**Pair**:
Instrument yang diperdagangkan: `EURUSD`, `BTCUSDT`, `XAUUSD`.
_Avoid_: Instrument, Asset, Symbol (Symbol = identifier native broker, e.g. MT5).

**Symbol**:
Identifier pair di broker tertentu (e.g. MT5: `EURUSD`, Binance: `BTCUSDT`). Tidak dipakai sebagai istilah domain kanonik.

**MarketState**:
Snapshot multi-timeframe pasar saat ini — OHLCV bars (M5/M15/H1/H4/D1) + indikator terkomputasi (EMA/ATR/RSI/dll). INPUT yang di-feed ke `Strategy.evaluate()`. Selalu punya timestamp dan `pair`.
_Avoid_: MarketContext (itu broader intelligence).

**MarketRegime**:
Klasifikasi turunan dari MarketState via statistical engine: `TRENDING`, `RANGING`, `VOLATILE`, `SIDEWAYS`. Dipakai untuk adaptasi strategi & risk sizing.

**MarketContext**:
Kondisi pasar luas di luar data teknikal: news sentiment, macro bias, correlation antar pair, event risk. Output AI/news pipeline, bukan derivasi dari bar harga.

### Strategy & Signal

**Strategy**:
Interface/algoritma evaluasi pasar: `EMATrendStrategy`, `BreakoutStrategy`, `MeanReversionStrategy`. Plug-and-play — semua strategi mengikuti interface `Strategy.evaluate(state: MarketState) → Opportunity`.

**StrategyConfig**:
Instance terparameterisasi & aktif dari Strategy. Contoh: "EMA Trend, pair BTCUSDT+ETHUSDT, EMA 20/50, risk 0.75%, min score 72, timeframe H4/H1/M15". Muncul di dashboard Active Strategies, bisa enable/disable/edit. Bisa banyak StrategyConfig aktif bersamaan.

**Opportunity**:
Output mentah dari `Strategy.evaluate()`: `{pair, strategy_id, direction, confidence}`. Belum punya harga entry/SL/TP — belum siap dieksekusi.

**Signal**:
Opportunity yang sudah di-enrich oleh pipeline: `{pair, direction, entry_zone, stop_loss, take_profit[], technical_score, fundamental_score, ai_score, confidence, timestamp}`. Output Decision Engine setelah melalui enrichment + risk check.

### Order & Position

**Order**:
Instruksi ke broker (belum tentu terisi). Dua tipe utama: **Market Order** (eksekusi langsung di harga pasar) dan **Pending Order** (eksekusi saat harga mencapai level tertentu).

**Pending Order Types** (standar MT5):
- **Buy Limit** — beli di bawah harga pasar (expecting bounce up).
- **Sell Limit** — jual di atas harga pasar (expecting bounce down).
- **Buy Stop** — beli di atas harga pasar (expecting breakout up).
- **Sell Stop** — jual di bawah harga pasar (expecting breakout down).
- **Buy Stop Limit** — kombinasi: saat harga capai stop price, pasang Buy Limit.
- **Sell Stop Limit** — kombinasi: saat harga capai stop price, pasang Sell Limit.

**Fill**:
Eksekusi (parsial atau penuh) dari Order — konfirmasi broker bahwa order terisi. Punya `fill_price`, `volume`, `timestamp`.

**Position**:
Posisi terbuka hasil Fill — punya `pair`, `direction` (LONG/SHORT), `entry_price`, `current_pnl`, `stop_loss`, `take_profit`. Alur: Order → Fill → Position → Exit (close).

**Direction**:
Niat trade: `LONG`, `SHORT`, `NONE`. Dipakai di Opportunity, Signal, Order, Position.
_Avoid_: Side (ambigu — bisa buy-side/sell-side liquidity).

**StopLoss**:
Level harga untuk auto-exit jika pasar bergerak berlawanan — batas rugi per posisi.

**TakeProfit**:
Level harga untuk auto-exit jika profit mencapai target. Bisa punya beberapa level (TP1, TP2).

### Risk

**RiskEngine**:
Gatekeeper terakhir sebelum eksekusi: memvalidasi position size, max risk/trade, daily loss limit, max open positions, correlation exposure.

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

**Bias**:
Pandangan analitis tentang arah pasar: `bullish`, `bearish`, `neutral`. Dipakai di AI/news output, berbeda dari Direction (yang adalah niat trade).

### Bot Lifecycle

**Mode**:
Siapa yang approve eksekusi: `MANUAL` (bot hanya kasih opportunity), `SEMI_AUTO` (bot siapkan order, user approve), `FULL_AUTO` (bot eksekusi tanpa approval).

**State**:
Status hidup sistem: `RUNNING`, `STOPPED`, `EMERGENCY_STOP`. `EMERGENCY_STOP` = no new trades + opsional close all positions.

### Pipeline

**DataEngine**:
Ingestion & normalisasi dari Market Source (MT5 connector) → menyimpan ke database + cache Redis.

**FeatureEngine**:
Komputasi indikator teknikal dari MarketState: EMA, ATR, RSI, MACD, ADX, dsb. Output: MarketState dengan indikator terisi.

**StrategyEngine**:
Iterasi semua StrategyConfig aktif → `evaluate(MarketState)` → kumpulan Opportunity.

**DecisionEngine**:
Enrichment pipeline: Opportunity → tambah entry zone/SL/TP + skor technical/fundamental/AI + risk check → Signal (kalau lolos) atau rejected.

**ExecutionEngine**:
Menerjemahkan Signal → Order ke broker via MT5 connector. Monitor Fill & Position lifecycle. Handle exit (TP/SL/manual).