> **SUPERSEDED — jangan gunakan sebagai rencana atau spesifikasi implementasi.**
>
> Dokumen AI-generated ini diarsipkan sebagai konteks historis. Sumber normatif V0 adalah [`docs/spec/backend-v0.md`](../spec/backend-v0.md), [`docs/spec/frontend-v0.md`](../spec/frontend-v0.md), [`docs/spec/strategy-templates-v0.md`](../spec/strategy-templates-v0.md), dan [`CONTEXT.md`](../../CONTEXT.md). Istilah, scope, dan usulan di bawah dapat bertentangan dengan kontrak V0 saat ini.

* AI Generated Output. Historical reference only.

Untuk V1, saya sarankan **buat dashboard sendiri sebagai command center**, lalu TradingView/MT5/Binance tetap dipakai sebagai alat bantu eksternal.

Alasannya sederhana: kalau kamu hanya mengandalkan MT5 atau Binance, kamu bisa melihat posisi dan chart, tetapi kamu tidak bisa melihat dengan jelas **kenapa bot mengambil keputusan**, skor AI-nya, strategi mana yang aktif, news apa yang memengaruhi keputusan, risk state, atau log internal sistem.

Struktur frontend yang saya sarankan:

```text
                YOUR TRADING DASHBOARD
                         │
        ┌────────────────┼────────────────┐
        │                │                │
     MARKET           STRATEGY          ACCOUNT
     MONITOR          CONTROL           CONTROL
        │                │                │
     charts           activate         positions
     signals          strategy         PnL
     news             parameters       risk
        │                │                │
        └────────────────┼────────────────┘
                         ↓
                  TRADING BACKEND
                         ↓
              MT5 / Binance / Broker
```

## Dashboard utama

Saya akan membuat homepage seperti cockpit.

```text
┌─────────────────────────────────────────────────────────────┐
│ BOT STATUS ● RUNNING     ACCOUNT $10,420      PNL +2.4%    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│                   BTCUSDT CHART                             │
│                                                             │
│                TradingView-style chart                      │
│                                                             │
├───────────────────────┬─────────────────────────────────────┤
│ ACTIVE OPPORTUNITIES  │ AI MARKET CONTEXT                   │
│                       │                                     │
│ BTC LONG       78%    │ Bias: Bullish                       │
│ ETH LONG       64%    │ News Risk: Low                      │
│ XAUUSD SHORT   71%    │ Macro: Neutral                      │
│                       │                                     │
├───────────────────────┴─────────────────────────────────────┤
│ POSITIONS                                                   │
│ BTCUSDT LONG | +1.8R | SL 63,500 | TP 66,100               │
└─────────────────────────────────────────────────────────────┘
```

Yang penting bukan tampilannya dulu, tetapi **informasi yang kamu lihat dalam 5 detik**.

Saya ingin kamu langsung tahu:

```text
Bot hidup atau mati?
↓
Market sedang seperti apa?
↓
Ada opportunity apa?
↓
Bot sedang pegang posisi apa?
↓
Risk exposure berapa?
↓
Kenapa bot masuk posisi tersebut?
```

---

## 1. Market Monitor

Bagian ini seperti TradingView mini.

Tampilkan:

* candlestick
* timeframe selector
* indicators
* entry
* stop loss
* take profit
* current positions
* strategy signals

TradingView punya Advanced Charts/Datafeed API yang memungkinkan kita memasukkan market data kita sendiri ke chart; TradingView sendiri tidak menyediakan market data melalui library tersebut. ([TradingView][1])

Jadi chart bisa terlihat seperti TradingView, tetapi sumber datanya tetap backend kita:

```text
Binance / MT5
      ↓
Backend
      ↓
WebSocket
      ↓
TradingView Chart
```

Ini menurut saya UX terbaik.

---

# 2. Opportunity Feed

Ini justru fitur yang menurut saya akan menjadi bagian paling berharga.

Contoh:

```text
BTCUSDT
LONG

Confidence          82%
Technical           87
Fundamental         71
AI Reasoning        78
Risk                LOW

Timeframes
D1     bullish
H4     bullish
H1     pullback
M15    breakout

Entry
64,120 - 64,320

SL
63,450

TP1
65,800

TP2
67,100
```

Kemudian ada tombol:

```text
[ VIEW ANALYSIS ]
[ EXECUTE ]
[ REJECT ]
```

Pada mode full-auto, execute terjadi otomatis.

Pada mode semi-auto:

```text
BOT DETECTS
↓
BOT ANALYZES
↓
YOU APPROVE
↓
TRADE
```

Ini bahkan menurut saya mode terbaik ketika kita masih menguji V1.

---

# 3. Explainability / "Why this trade?"

Ini wajib kalau ada AI.

Misalnya kamu klik posisi BTC.

Dashboard menampilkan:

```text
WHY BOT ENTERED

Technical
✓ H4 EMA20 > EMA50
✓ H1 trend bullish
✓ M15 breakout
✓ volume +34%

Fundamental
✓ ETF inflow positive
✓ funding neutral

AI Context
✓ News sentiment positive
✓ no major bearish catalyst detected

Risk
Position size      2%
Account risk       0.8%
Risk/Reward        1 : 2.4
```

Ini jauh lebih berguna daripada hanya:

```text
BOT BUY BTC
```

Karena kalau bot mulai rugi, kita bisa melakukan diagnosis.

---

# 4. Strategy Control Panel

Ini sesuai dengan konsep modular strategy yang kita bahas.

Misalnya:

```text
ACTIVE STRATEGIES

Trend Pullback V2
Status          ON
Assets          BTC ETH SOL
Risk            0.75%
Timeframe       H4/H1/M15

Breakout V1
Status          OFF

Mean Reversion
Status          ON
Assets          BTC
```

Kemudian parameter bisa diedit:

```text
EMA Fast          [20]
EMA Slow          [50]

Minimum ADX       [25]

Risk / Trade      [0.75%]

Minimum Score     [72]

News Filter       [ON]

AI Validation     [ON]
```

Jadi kalau mau mengubah:

```text
EMA 20
```

menjadi:

```text
EMA 30
```

tidak perlu edit source code.

Parameter masuk database/config.

Ini penting sekali jika nanti sistem mulai berkembang.

---

# 5. Risk Control

Kalau saya hanya boleh memilih satu panel yang benar-benar penting, ini salah satunya.

```text
RISK MONITOR

Account equity
$10,430

Risk currently open
2.1%

Daily PNL
+1.4%

Daily drawdown
-0.3%

Maximum daily loss
3%

Open positions
4 / 6
```

Kemudian controls:

```text
Maximum risk / trade
1%

Maximum daily drawdown
3%

Maximum simultaneous positions
5

Maximum BTC exposure
15%

Stop trading after consecutive losses
4
```

Dan tombol besar:

```text
┌──────────────────────┐
│ EMERGENCY STOP BOT   │
└──────────────────────┘
```

Kalau dipencet:

```text
NO NEW TRADES
```

dan bisa ada pilihan:

```text
Stop new trades only

atau

Stop + close positions
```

---

# 6. AI Intelligence Panel

Saya tidak akan membuat AI sebagai chatbot utama.

Lebih baik:

```text
AI MARKET INTELLIGENCE
```

Misalnya:

```text
Market bias
MODERATELY BULLISH

Confidence
74%

Main Drivers

+ US risk sentiment improving
+ BTC ETF inflows positive
+ H4 momentum strong

Risks

- Funding increasingly positive
- Resistance near $68,200
- CPI tomorrow

AI Recommendation

Prefer long setups.
Avoid chasing breakout above resistance.
```

Baru di bawahnya boleh ada chat:

```text
Ask AI:

"Kenapa kita tidak entry ETH tadi?"

"Apakah news CPI mempengaruhi posisi sekarang?"

"Kenapa BTC confidence turun?"
```

Chatbot jadi **interface untuk interrogate system**, bukan engine utama.

---

# 7. News / Event Monitor

Kira-kira:

```text
LIVE EVENTS

16:32
Bitcoin ETF flows increase
Impact: BTC
Sentiment: +0.72
Severity: Medium

16:18
Fed governor comments on inflation
Impact: USD / GOLD
Sentiment: Hawkish
Severity: High

15:55
Ethereum protocol upgrade announcement
Impact: ETH
Sentiment: Positive
Severity: Low
```

Kemudian kalau event berbahaya:

```text
⚠ HIGH IMPACT EVENT

US CPI
18 minutes

New entries temporarily disabled
```

Ini bagus karena risk engine bisa punya:

```python
if high_impact_event_within(minutes=15):
    disable_new_positions()
```

---

# 8. Position Manager

Seperti Binance/MT5:

| Asset | Side |  Entry | Current |    PNL |     SL |     TP | Strategy |
| ----- | ---: | -----: | ------: | -----: | -----: | -----: | -------- |
| BTC   | Long | 64,200 |  64,820 | +0.96% | 63,400 | 66,100 | Trend V2 |
| ETH   | Long |  3,420 |   3,450 | +0.87% |  3,370 |  3,560 | Breakout |

Tapi klik satu posisi memberikan:

```text
Signal
↓
AI reasoning
↓
execution
↓
order fills
↓
SL adjustments
↓
exit reason
```

Jadi punya audit trail lengkap.

---

# 9. Bot Mode

Saya akan membuat tiga mode.

```text
MANUAL
```

Bot hanya memberikan opportunity.

```text
SEMI AUTO
```

Bot menganalisis dan mempersiapkan order, kamu approve.

```text
FULL AUTO
```

Bot execute sendiri.

Untuk awal:

> **SEMI AUTO**

adalah pilihan saya.

Karena kita masih bisa mengobservasi kualitas engine.

Setelah mungkin:

```text
500–1,000 paper trades
+
forward testing
```

baru kita mempertimbangkan full auto.

---

# Lalu apakah masih perlu TradingView?

**Ya.**

Saya akan menggunakan kombinasi:

### TradingView

Untuk:

```text
deep chart analysis
drawing
manual technical analysis
alerts
strategy prototyping
```

### MT5/Binance

Untuk:

```text
broker/exchange account
execution verification
manual emergency trading
account reconciliation
```

### Dashboard kita

Untuk:

```text
BOT CONTROL
AI
STRATEGIES
RISK
NEWS
OPPORTUNITIES
LOGS
AUTOMATION
```

Jadi bukan:

```text
Dashboard OR TradingView
```

melainkan:

```text
Dashboard
+
TradingView
+
Broker Platform
```

TradingView bahkan menyediakan Trading Platform APIs untuk quote/order-ticket/DOM/broker integration, tetapi implementasinya memerlukan Datafeed dan Broker API sendiri. ([TradingView][2])

---

# Tech stack frontend

Untuk proyek seperti ini saya akan memilih:

```text
Next.js
React
TypeScript

Tailwind
shadcn/ui

TradingView Advanced Charts
atau
Lightweight Charts

WebSocket
```

Realtime state:

```text
Backend websocket
       ↓
Frontend store
       ↓
Charts
Positions
Signals
PNL
News
```

Tidak perlu reload halaman.

Binance sendiri menggunakan user-data streams via WebSocket untuk event account/order real-time, jadi model realtime seperti ini sangat natural untuk connector crypto kita. ([Binance Developers][3])

---

# Layout V1 yang saya pilih

Saya bahkan akan membatasi V1 ke **6 halaman** saja:

```text
01 Dashboard
02 Markets
03 Opportunities
04 Positions
05 Strategies
06 System
```

Dashboard:

```text
overview
risk
PNL
opportunities
AI context
positions
```

Markets:

```text
chart
indicators
news
AI analysis
```

Opportunities:

```text
all candidate trades
```

Positions:

```text
active + history
```

Strategies:

```text
strategy configs
backtest results
enable/disable
```

System:

```text
MT5 connection
Binance connection
AI status
data feed status
logs
emergency controls
```

Itu sudah cukup solid tanpa menjadi monster dashboard.

---

## Satu prinsip penting

Frontend **tidak boleh menjadi tempat logic trading berada**.

Jangan:

```text
React
↓
if RSI < 30
↓
BUY
```

Semua logic tetap backend.

Frontend hanya:

```text
VIEW
CONTROL
CONFIGURE
APPROVE
```

Sehingga bahkan kalau browser mati:

```text
Backend trading engine
        ↓
tetap berjalan
```

Dan kalau frontend error:

```text
trading system tetap aman
```

### Jadi desain keseluruhannya

```text
                 YOUR DASHBOARD
                Next.js / React
                      │
             REST + WebSocket
                      ↓
              TRADING BACKEND
                      │
       ┌──────────────┼───────────────┐
       ↓              ↓               ↓
   Strategies        AI            Risk Engine
       │              │               │
       └──────────────┼───────────────┘
                      ↓
                Execution Engine
                 ↙           ↘
               MT5          Binance
```

Untuk V1, **custom dashboard + TradingView-style chart + semi-auto execution** menurut saya sweet spot terbaik: tidak terlalu kompleks, tetapi fondasinya sudah benar untuk nantinya berkembang menjadi sistem trading yang jauh lebih sophisticated.

[1]: https://www.tradingview.com/charting-library-docs/latest/connecting_data/datafeed-api/?utm_source=chatgpt.com "Datafeed API | Advanced Charts Documentation"
[2]: https://www.tradingview.com/charting-library-docs/latest/connecting_data/datafeed-api/trading-platform-methods/?utm_source=chatgpt.com "Trading Platform methods | Advanced Charts Documentation"
[3]: https://developers.binance.com/docs/binance-spot-api-docs?utm_source=chatgpt.com "CHANGELOG for Binance's API | Binance Developer Docs"
