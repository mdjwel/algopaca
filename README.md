<p align="center">
  <img src="web/static/img/algopaca-banner.svg" alt="AlgoPaca Quantitative Trading Desk" width="100%">
</p>

<p align="center">
  <strong>Autonomous Algorithmic Paper &amp; Live Trading Desk</strong><br>
  <em>6 Algorithmic &amp; Intraday Engines • Gold &amp; Silver Metals Intelligence • Exit Strategies &amp; Bracket Stops • Post-Exit Automation • Custom Blueprints • Options Overlay • 4 AI Providers • AI Desk Review &amp; Lessons Loop • Auto-Trade Approval Queue • Multi-User Auth &amp; Admin • Mobile PWA &amp; Web Desk</em>
</p>

<p align="center">
  <a href="https://algopaca.spiderdevs.xyz/"><img src="https://img.shields.io/badge/🚀_Live_Demo-algopaca.spiderdevs.xyz-orange.svg" alt="Live Demo"></a>
  <a href="https://x.com/ehjewelbd/status/2090872372731711646"><img src="https://img.shields.io/badge/𝕏_Post-Demo_%26_Announcement-black?logo=x&logoColor=white" alt="X Demo Post"></a>
  <a href="https://github.com/mdjwel/algopaca/actions/workflows/ci.yml"><img src="https://github.com/mdjwel/algopaca/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/trading-100%25_free_%26_commission--free-success.svg" alt="100% Free & Commission-Free Trading">
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg" alt="Python Versions">
  <img src="https://img.shields.io/badge/tests-254%20passing-brightgreen.svg" alt="254 Tests Passing">
  <img src="https://img.shields.io/badge/broker-Alpaca%20Markets-green.svg" alt="Alpaca Markets">
  <img src="https://img.shields.io/badge/AI-OpenAI%20%7C%20Gemini%20%7C%20Claude%20%7C%20xAI-8A2BE2.svg" alt="AI Models">
  <img src="https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker&logoColor=white" alt="Docker Ready">
  <img src="https://img.shields.io/badge/status-open%20source-success.svg" alt="Open Source">
</p>

<p align="center">
  <a href="https://algopaca.spiderdevs.xyz/">Live Demo</a> •
  <a href="#-watch-in-action-demo--walkthrough">Demo Video</a> •
  <a href="#-key-highlights">Highlights</a> •
  <a href="#-strategy-engines--options-overlay">Engines &amp; Options</a> •
  <a href="#-intraday-day-trading-presets">Day Presets</a> •
  <a href="#-mobile-pwa--web-trading-desk">Mobile &amp; Web Desk</a> •
  <a href="#-history-desk-review--the-lessons-loop">Desk Review</a> •
  <a href="#-post-exit-automation-crash-safe-plans">Post-Exit Automation</a> •
  <a href="#-quickstart">Quickstart</a> •
  <a href="#-mcp-server-claude-cursor--ai-agents">MCP Server</a> •
  <a href="#-cli--terminal-execution">CLI Usage</a> •
  <a href="#%EF%B8%8F-configuration-env">Configuration</a> •
  <a href="#-risk-management--safety-guardrails">Risk Safety</a>
</p>

---

AlgoPaca is a **100% free, MIT-licensed quantitative trading desk** built natively for [Alpaca Markets](https://alpaca.markets) with **$0 commission stock & ETF trading**. It replaces expensive monthly SaaS bot subscriptions with a self-hosted, institutional-grade platform featuring **6 algorithmic & intraday trading engines**, **Gold & Silver macro intelligence**, **automated position exit strategies**, **crash-safe post-exit automation**, **custom strategy blueprints**, an **Alpaca options overlay**, **multi-provider AI reasoning** (OpenAI, Gemini, Claude, xAI), **mechanical risk guardrails**, an **approval queue**, an **AI desk review that grades your own trade log**, and a fast **mobile PWA & web desk**.

> [!NOTE]
> 💸 **100% Free & Commission-Free**:
> Zero subscription fees, zero paywalls. Combined with Alpaca Markets' API, automate your quantitative strategies with **$0 platform costs and $0 brokerage commissions**.

---

## 🎬 Watch in Action (Demo & Walkthrough)

Check out the launch walkthrough and feature demonstration on **𝕏 (Twitter)**:

<p align="center">
  <a href="https://x.com/ehjewelbd/status/2090872372731711646">
    <img src="https://img.shields.io/badge/▶_Watch_Live_Demo_on_𝕏-000000?style=for-the-badge&logo=x&logoColor=white" alt="Watch AlgoPaca Demo on X" height="40">
  </a>
</p>

> [!TIP]
> 📺 **Announcement on 𝕏**:  
> _"🚨 Introducing AlgoPaca — A fully autonomous AI Quantitative Trading Desk for @AlpacaMarkets 🦙🤖 Fuses Technicals + Live News + Nasdaq Earnings + Macro events with automated ATR risk guardrails."_  
> 🔗 **Direct Link:** [https://x.com/ehjewelbd/status/2090872372731711646](https://x.com/ehjewelbd/status/2090872372731711646)

---

## 🌟 Key Highlights

- 🪙 **Precious Metals & Macro Intelligence**: Real-time cross-asset data fusion across the full Alpaca metals universe — gold (`GLD`, `IAU`, `UGL`, `GLL`, `PHYS`), silver (`SLV`, `AGQ`, `ZSL`, `SIL`, `SILJ`, `PSLV`), and miners (`GDX`, `GDXJ`). Blends Treasury yield direction (weight 0.40), a 1-year z-scored **Gold-to-Silver Ratio (GSR)** (0.35), the 200-day trend regime (0.15), and the US Dollar (0.10) into a single macro bias fed to the AI desk. Weights come from measured predictive power against 2007-2025 forward GLD returns rather than convention — the resulting score is monotonic in forward returns (mean 20-day return climbs from −0.29% in the most bearish band to +1.14% in the most bullish, Spearman IC +0.11, stable across both halves of the sample). Miner relative strength is reported for context but carries **zero** weight: its measured information coefficient was −0.01.
- ⚡ **Intraday & Two-Way Execution**: 1m/5m/15m/1h day trading engine with VWAP trend riding, VWAP mean-reversion fades, 15m Opening Range Breakout & Breakdown (ORB), and momentum scalping. Full support for **Long Only**, **Short Only**, and **Two-Way (Long & Short)** trading, per-day trade caps, an opening-bell buffer, and automatic End-of-Day (EOD) square-off.
- 🛡️ **Automated Position Exit Strategies**: One-click protection right from your Positions desk:
  - **Stop Loss**: Quick risk chips (1%, 2%, 3%, 5%, 8%, 10%) or custom dollar levels.
  - **Zero-Risk Breakeven**: Instant ratchet moving stop to entry price ($entry ± $0.01) once in profit.
  - **Trailing Stop Loss (%)**: Dynamic trailing stop locking in gains as price advances.
  - **Take Profit & Brackets**: Automated target limits and synchronized Take Profit / Stop Loss brackets.
  - **Partial Liquidation**: Scale out by percentage (25%, 50%, 75%) or exact shares with live P&L estimates.
- 🔁 **Crash-Safe Post-Exit Automation**: Arm what happens *after* an exit fills — **Dip Hunt** re-entry below a stop-out, **Re-investment** buy-back plans, and **Next-Ticket** follow-ons that flip a position or rotate into another name. Every plan is persisted to disk and scoped per broker account, so a restart resumes it instead of silently dropping it.
- 🧠 **Multi-Provider AI Intelligence**: Seamlessly switch between **OpenAI** (GPT-5.6 Sol / Terra / Luna, GPT-5.4), **Google Gemini** (3.6 Flash, 3.5 Flash / Flash-Lite, 3.1 Pro), **Anthropic Claude** (Opus 5, Sonnet 5, Fable 5, Haiku 4.5), and **xAI Grok** (Grok 4, Grok 4 Fast, Grok 3) to synthesize technical indicators, live financial news, earnings surprises, economic calendars, and metals intel into structured JSON decisions.
- 📓 **AI Desk Review & Lessons Loop**: Every number on the History review — win rates, P&L attribution by engine, confidence calibration, hold times, execution blocks — is computed **deterministically in Python**; the LLM is handed the finished figures and asked only to narrate them. Approve a lesson and it feeds back into the live AI prompt, gated behind an explicit human click.
- 🛠️ **Custom Engines & Starter Blueprints**: Save any active desk setup — parameters, custom AI prompts, risk limits, and indicators — as an isolated **Custom Engine**, or launch immediately from **10 pre-built blueprints** (e.g. *AI Real-Time Gold & Silver Macro Momentum*, *AI Intraday VWAP & ORB Sniper*, *Quantitative RSI Dip Hunter*).
- 🎯 **Alpaca Options Overlay**: Automatically translates equity signals into defined-risk options contracts — **Vertical Debit Spreads**, **Long Options** (ATM calls/puts), and **Covered Hedges** with automated DTE (21–45) and strike selection.
- 🚦 **Auto-Trade Approval Queue & Instant Alerts**: Run 100% autonomously or in **Approval Mode** (human-in-the-loop review staging candidates with confidence scores and reasoning). Receive instant **Desktop Browser Push Notifications** and branded **HTML Email Alerts** with 1-click deep links.
- 📋 **Advanced Orders & Lot Batch Liquidation**: Execute Market, Limit, Stop, Stop-Limit, Trailing Stops, Bracket, OCO, and OTO orders. Inspect tax lots, batch-liquidate specific lots with resting-order conflict safeguards, and replace or cancel resting orders in place.
- 🧯 **Advisory Guards on Hand-Typed Tickets**: Manual orders are measured against the same daily-loss, max-position, and per-trade risk budget as the bots — plus live **portfolio heat** (total open risk across every position). A human sees every breach at once and must explicitly override it; the bots are refused outright.
- 📱 **Mobile-First PWA & Web Desk**: Ultra-fast Vanilla JS/CSS terminal with a sticky masthead, live ticker search, bottom navigation bar, touch-friendly order ticketing, **4 terminal themes** (Obsidian Night, Midnight Slate, Emerald Forest, Daylight Desk), and multi-language support (English, Bengali, Spanish, French, Hindi).
- 👥 **Multi-User Isolation & Admin Suite**: Secure signup/login, onboarding setup wizard, role-based permissions (**Owner**, **Admin**, **Member**), user-scoped API keys, session/device management, GDPR-style data export & account deletion, SMTP configuration, email logs, and comprehensive audit trails.
- 🧪 **Walk-Forward Daily & Intraday Backtesting**: Realistic simulations on historical daily and minute bars with zero lookahead, conservative stop-first intra-bar resolution, slippage modeling, saved run history, and side-by-side run comparison. Ships with an **in-sample / out-of-sample validation harness** for the Regime L/S engine.
- 🔌 **Built-in MCP Server**: Native [Model Context Protocol](https://modelcontextprotocol.io) integration allowing Claude Desktop, Cursor, and AI agents to check balances, place orders, run strategy cycles, and backtest via natural language.
- 🐳 **1-Click 60-Second Setup**: Fast deployment with Docker Compose or standard Python virtual environments, backed by a 254-test suite running on Python 3.10 / 3.11 / 3.12 in CI.

---

## 📊 Strategy Engines & Options Overlay

| Engine | Strategy Type | Core Logic | Highlights & Presets |
| :--- | :--- | :--- | :--- |
| **Day Trading** | Intraday & Scalping | High-frequency minute execution (1m/5m/15m/1h) using VWAP, Opening Range Breakout / Breakdown (ORB), and 9/21 EMA momentum | 13 presets across long-only, short-only, and two-way — see [Intraday Day-Trading Presets](#-intraday-day-trading-presets). |
| **SMA** | Trend Following | Moving average crossover filters across watchlist | **Classic** (10/30), **Short-term** (5/20), **Fibonacci** (8/21), **Swing** (20/50), **Golden Cross** (50/200), **Gold Momentum** (50/150), or **Custom**. |
| **Dip** | Mean Reversion | Capitulation washouts & recovery bounces | RSI threshold washouts + Bollinger lower band taps. Presets: **Deep dip**, **Mild pullback**, **Washout**, **Gold Bullion Dip Hunter**, **Custom**. |
| **Pair** | Regime Rotation | Dynamic regime-impulse rotation across 2 symbols | **Gold / Silver Rotation** (GLD/SLV), **Gold / Miners High-Beta Rotator** (GLD/GDX), **Gold / Inverse Gold Bear-Proof** (GLD/GLL), **Research max**, **Research strict**, **Cash when weak**, **Custom**. |
| **LS** | Dual Momentum | Trend strength momentum (Long/Short) | EMA fast/slow + ADX trend strength gate. Holds through chop, exits on signal flip or ATR stop. Ships with an IS/OOS validation harness. |
| **AI Desk** | Multi-Factor Quant | LLM multi-modal market synthesis & reasoning | TA (RSI, MACD, BB, ATR, ADX) + News + Earnings + Macro events + Metals Intel + approved desk lessons → JSON decision. Presets: **Balanced**, **Conservative**, **Momentum**, **Mean reversion**, **News-aware**, **Trend + ATR trail**, **Earnings drift (PEAD)**, **Opening range breakout**, **AI Gold & Silver Macro Momentum**, **Custom**. |
| **Custom Engines** | User-Defined | Composable strategies & starter templates | Save, duplicate, and switch custom rules, AI instructions, and risk parameters across any base engine. Includes 10 starter blueprints. |
| **Options Overlay** | Defined-Risk Derivatives | Maps equity signals directly to Alpaca options | **Vertical Debit Spreads** (Bull Call / Bear Put), **Long Options** (ATM calls/puts), **Covered Hedges** (Protective puts / Covered calls). |

---

## ⚡ Intraday Day-Trading Presets

The day engine ships with 13 tuned presets. Every one square-offs before the close by default and respects a per-day trade cap.

| Preset ID | Label | Side | Sub-mode | Target / Stop |
| :--- | :--- | :--- | :--- | :--- |
| `ai_vwap_momentum` | AI Institutional VWAP & Momentum | Long | VWAP trend | 2.0R / 1.5 ATR |
| `ai_orb_breakout` | AI Opening Range Sniper (15m) | Long | ORB | 2.5R / 1.8 ATR |
| `ai_adaptive_scalp` | AI Adaptive Intraday Scalper | Long | Momentum scalp | 1.5R / 1.2 ATR |
| `ai_metals_breakout` | AI Gold & Silver Intraday Breakout | Long & Short | VWAP trend | 2.8R / 1.3 ATR |
| `ai_vwap_momentum_ls` | AI Two-Way VWAP & Momentum | Long & Short | VWAP trend | 2.0R / 1.5 ATR |
| `ai_orb_breakout_ls` | AI Opening Range Sniper (Long & Short) | Long & Short | ORB | 2.5R / 1.8 ATR |
| `ai_orb_breakdown_short` | AI Opening Range Breakdown | Short only | ORB | 2.5R / 1.8 ATR |
| `vwap_trend` | VWAP Trend Rider | Long | VWAP trend | 2.0R / 1.5 ATR |
| `vwap_trend_short` | VWAP Downtrend Rider | Short only | VWAP trend | 2.0R / 1.5 ATR |
| `orb_breakout` | Opening Range Breakout (15m) | Long | ORB | 2.5R / 1.8 ATR |
| `momentum_scalp` | Intraday Momentum Scalp | Long | Momentum scalp | 1.5R / 1.2 ATR |
| `vwap_fade` | VWAP Mean Reversion (Fade) | Long | VWAP fade | 1.8R / 1.5 ATR |
| `custom` | Custom | Your choice | Your choice | Your choice |

> Presets prefixed `ai_` add an LLM confirmation gate (default minimum confidence 0.65–0.70) on top of the mechanical signal — the model can veto a setup, never invent one.

---

## 📱 Mobile PWA & Web Trading Desk

AlgoPaca delivers an institutional-grade experience on both desktop and mobile screens:

- **Mobile Shell (PWA)**: Bottom navigation (`Desk`, `Orders`, `Auto-Trade`, `Positions`, `Menu`), sticky top bar, slide-out drawer, and mobile touch tickets.
- **Masthead Ticker Search**: Scored search across US equities and ETFs by symbol or company name, augmented with live broker assets and ranked by your own holdings and watchlist.
- **Positions & Exit Desk**: Real-time mark-to-market prices (including pre-market and after-hours quotes), one-click Breakeven ratchet, trailing stops, bracket attachments, and partial scaling.
- **Tax Lots & Batch Liquidation**: Drill into individual lots (timestamp, fill price, unrealized P&L) and batch-liquidate selected lots with automatic resting-order conflict protection.
- **4 Terminal Themes**: Obsidian Night, Midnight Slate, Emerald Forest, and Daylight Desk — switchable from the masthead or User Settings, remembered per device.
- **Backtest History & Compare**: Every run is saved; reopen a past run or put two runs side by side on the same metric grid.
- **Multi-Tenant Security**: User data, Alpaca paper/live keys, and AI keys are strictly isolated per account. Includes an onboarding Setup Wizard, RBAC roles, active-session management, account data export, and SMTP email services.

---

## 📓 History, Desk Review & the Lessons Loop

The History page is more than a fill ledger — it grades the desk against its own record.

- **Deterministic Analytics First**: Win rate, realized P&L attribution by engine *and* preset, confidence calibration (do high-confidence entries actually pay better?), hold-time distribution, concentration, execution blocks and skip reasons, and rule-based flags are all computed in Python from the same ledger the page renders.
- **The Model Only Narrates**: The LLM receives those finished numbers and writes the prose. It is never asked to add, divide, or recall a figure — a hallucinated P&L on a trading desk is worse than no prose at all.
- **Natural-Language History Query**: Ask for a slice of the log in plain language and get back a structured filter applied to the table.
- **Post-Mortems**: Each closed trade is shown with its original entry thesis beside what the position actually did.
- **Approved Lessons → Live Prompt**: A lesson only reaches the AI engine because a human read it and pressed Save. Saved lessons are injected into the AI desk prompt for the symbols they apply to — and they can never override a mechanical risk rule. This is the one History feature that changes live behavior, so it is gated behind an explicit click, and every lesson can be toggled off or deleted.

---

## 🔁 Post-Exit Automation (Crash-Safe Plans)

Alpaca has no "close-then-open" order class, so AlgoPaca holds the plan itself and watches the exit fill. All three plan types are written to disk, keyed to the broker account, and scoped by trading mode — a paper plan is never resumed against live credentials.

| Plan | What it promises | Behavior |
| :--- | :--- | :--- |
| **Dip Hunt** | "If my stop fills, re-enter lower" | After the protective stop fills at `P`, watches for a further `dip_pct` drop. If it arrives before `wait_minutes` elapses, it buys immediately; otherwise it parks a limit at the same target. The new entry carries the same stop — and the same hunt — so a second stop-out repeats the cycle. |
| **Re-investment** | "When this sell fills, put the cash back to work at $X" | The wait clock starts at the sell fill, not the order placement. Resumable states are re-read from the broker on restart rather than guessed. |
| **Next-Ticket (Follow-On)** | "When this close fills, open the other way / rotate into another name" | Flip long→short, short→long, or close-and-buy a different symbol. A plan interrupted mid-placement is reloaded as `interrupted` rather than blindly retried. |

---

## 🚀 Quickstart

> [!TIP]
> 🌐 **Try it live**: Test the hosted multi-user desk at **[algopaca.spiderdevs.xyz](https://algopaca.spiderdevs.xyz/)** — sign up and connect your Alpaca paper keys with zero installation.

### Option 1: Run with Docker Compose (Recommended)

```bash
# 1. Clone the repository
git clone https://github.com/mdjwel/algopaca.git
cd algopaca

# 2. Copy and configure environment variables
cp .env.example .env

# 3. Start AlgoPaca container in background
docker compose up -d
```
Open **[http://localhost:8765](http://localhost:8765)** in your browser. (To stop: `docker compose down`).

Your SQLite database and per-user workspaces live in `./data`, which is mounted as a volume — your accounts and armed plans survive container rebuilds.

---

### Option 2: Local Python Setup (macOS / Linux / Windows)

**Prerequisites:** Python 3.10–3.12, Git, and a free [Alpaca Paper Account](https://app.alpaca.markets/paper/dashboard/overview).

#### macOS & Linux:
```bash
git clone https://github.com/mdjwel/algopaca.git
cd algopaca
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./launch_web.sh
```

#### Windows:
```cmd
git clone https://github.com/mdjwel/algopaca.git
cd algopaca
launch_web.bat
```

Open **[http://127.0.0.1:8765](http://127.0.0.1:8765)** to complete the quick setup wizard.

---

## 🔌 MCP Server (Claude, Cursor & AI Agents)

AlgoPaca includes a built-in [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server, exposing portfolio tools and strategy engines directly to AI assistants:

```bash
./run_mcp.sh          # macOS / Linux
run_mcp.bat           # Windows
```

| Tool | Capability |
| :--- | :--- |
| `get_account` | Equity, cash, buying power, paper/live mode, day P&L |
| `get_positions` | Open stock, ETF, and options positions |
| `get_open_orders` | Active orders with stop/limit metadata |
| `list_strategy_presets` | Parameter presets for an engine (`sma`, `dip`, `pair`, `ai`) |
| `run_strategy_cycle` | Evaluate and execute one engine cycle — `sma`, `dip`, `pair`, `ls`, or `ai` — with full risk gates and options overlay |
| `place_manual_order` | Market/limit/stop/trailing orders with bracket protection |
| `close_position` | Full or partial position liquidation |
| `run_backtest` | Walk-forward simulation on historical bars (`sma`, `dip`) |

> [!IMPORTANT]
> `run_strategy_cycle` and `place_manual_order` submit **real orders** to whichever Alpaca account your `.env` points at. Keep `ALPACA_PAPER=true` while wiring up an assistant.

**Claude Desktop / Cursor Configuration:**
```json
{
  "mcpServers": {
    "algopaca": {
      "command": "/path/to/algopaca/.venv/bin/python",
      "args": ["-m", "bot.mcp_server"],
      "cwd": "/path/to/algopaca"
    }
  }
}
```

---

## 💻 CLI & Terminal Execution

Run single evaluations, headless cycles, or automated cron jobs:

```bash
# Check connected Alpaca account status
./run.sh --account

# Run a single strategy evaluation cycle
./run.sh --once --mode ai --provider gemini --preset balanced
./run.sh --once --mode sma --sma-preset golden_cross
./run.sh --once --mode dip --dip-preset washout
./run.sh --once --mode pair
./run.sh --once --mode ls

# Run continuous autonomous loop
./run.sh --mode ai --provider gemini --preset balanced
```
*(On Windows, use `run.bat`)*

**Available flags**

| Flag | Values |
| :--- | :--- |
| `--once` | Evaluate (and optionally trade) a single cycle, then exit |
| `--account` | Print the account summary and exit |
| `--mode` | `sma`, `dip`, `ai`, `pair`, `ls` |
| `--provider` | `openai`, `gemini`, `anthropic`, `xai` |
| `--preset` | `balanced`, `conservative`, `momentum`, `mean_reversion`, `news_aware`, `custom` |
| `--sma-preset` | `classic`, `short_term`, `fibonacci`, `swing`, `golden_cross`, `custom` |
| `--dip-preset` | `deep`, `mild`, `washout`, `custom` |

> [!NOTE]
> The **day-trading engine** runs from the CLI too, but has no `--mode day` flag — select it with `STRATEGY_MODE=day` and `DAY_PRESET=<preset id>` in `.env`, then run `./run.sh` or `./run.sh --once`. The web desk exposes every engine, preset, and blueprint (including the metals and PEAD/ORB AI presets that the CLI's `--preset` shortlist omits).

---

## ⚙️ Configuration (`.env`)

AlgoPaca uses environment variables for default settings. All parameters can also be configured live via the **API Keys** and **Settings** pages in the Web Desk — and per-user keys set there take precedence over the file.

```env
# Alpaca Paper Trading Credentials (Default)
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true

# Alpaca Live Trading Credentials (Safety Guarded)
ALPACA_LIVE_API_KEY=AK...
ALPACA_LIVE_SECRET_KEY=...
ALPACA_ALLOW_LIVE=false

# AI Model Providers (OpenAI, Gemini, Anthropic, xAI)
AI_PROVIDER=gemini           # openai | gemini | anthropic | xai
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.5-flash-lite

# Default Strategy & Risk Settings
STRATEGY_MODE=day            # day | ai | sma | dip | pair | ls
SYMBOLS=AAPL,MSFT,NVDA,SPY,QQQ,GLD,SLV
AI_RISK_PCT=0.5              # Risk 0.5% equity per trade
AI_DAILY_LOSS_LIMIT_PCT=3.0  # Circuit breaker: halt if daily loss >= 3%
AI_MAX_POSITIONS=3           # Concurrent open position cap (0 = unlimited)
AI_COOLDOWN_MINUTES=60       # Anti-revenge cooldown after a stop-out

# Intraday Day-Trading Engine
DAY_PRESET=ai_vwap_momentum  # See "Intraday Day-Trading Presets" above
DAY_SIDE=long_only           # long_only | short_only | long_short
DAY_MAX_TRADES_PER_DAY=5
DAY_EOD_FLATTEN=true         # Square off before the close

# Web Desk & Localization
ALGOPACA_HOST=127.0.0.1
ALGOPACA_PORT=8765
LANG_CODE=en                 # en | bn | es | fr | hi
```

Leaving a model variable blank picks a cheap, fast default per provider (currently `gpt-5.6-luna`, `gemini-3.5-flash-lite`, `claude-haiku-4-5-20251001`, `grok-3-mini`); the full curated catalog is selectable from the API Keys page.

👉 *`Config` exposes 83 tunable fields — see [.env.example](.env.example) for the documented core set and advanced knobs.*

---

## 🛡️ Risk Management & Safety Guardrails

Every automated order must clear strict mechanical pre-trade gates before execution:

```
[Signal Generated]
       │
       ▼
┌────────────────────────────────────────────────────────┐
│             Pre-Execution Risk Guardrails              │
├────────────────────────────────────────────────────────┤
│  1. Daily Loss Circuit Breaker (Halt if Day P&L ≤ -X%) │
│  2. Max Concurrent Open Positions Gate                 │
│  3. Spread & Liquidity Sanity Check                    │
│  4. Post-Loss Cooldown Gate (Anti-Revenge Trading)     │
│  5. Volatility-Adjusted ATR Sizing:                    │
│     Shares = (Equity × Risk%) / (ATR14 × Multiplier)   │
│  6. Max Allocation Cap (e.g., max 25% portfolio)       │
│  7. Portfolio Heat Check (total open risk vs budget)   │
└────────────────────────────────────────────────────────┘
       │
       ▼
  [Order Sent to Alpaca API]
       │
       ▼
┌────────────────────────────────────────────────────────┐
│             Active Position Management                 │
├────────────────────────────────────────────────────────┤
│  • Automatic ATR Trailing Stop-Loss                    │
│  • Dynamic Breakeven Ratchet at +1.0R                  │
│  • Partial Profit Scaling at +2.0R                     │
│  • EOD Square-Off (intraday engine)                    │
└────────────────────────────────────────────────────────┘
```

### Automated vs. Manual Tickets

The same gates run on both paths, with one deliberate difference:

- **Automated entries are refused.** A bot that breaches a gate does not trade — it logs the reason and moves to the next symbol.
- **Manual entries are advised.** A hand-typed ticket reports **every** breach at once (not just the first) alongside live portfolio heat, and requires an explicit override. The person is looking at the screen and may see something the rules cannot.

### Safety Guarantees:
1. **Paper by Default**: Fresh setups always start in Paper mode with simulated capital.
2. **Dedicated Credential Slots**: Paper and Live credentials remain completely separate.
3. **Hard Live Killswitch**: Live trading requires `ALPACA_ALLOW_LIVE=true` and explicit user confirmation.
4. **Environment Isolation**: Switching between modes halts active loops and clears cached state. Armed plans (dip hunts, buy-backs, follow-ons) and staged approvals are stored per broker account and never cross the paper/live boundary.
5. **Fail-Safe Fallback**: Any authentication or permission error on Live immediately reverts to Paper mode.

---

## 📁 Project Architecture

```
algopaca/
├── bot/                        # Trading backend & strategy engines
│   ├── ai_brain.py             # AI prompt assembly, multi-modal context & desk lessons
│   ├── ai_models.py            # Curated LLM model catalog per provider
│   ├── ai_providers.py         # OpenAI, Gemini, Anthropic & xAI integrations
│   ├── ai_risk.py              # Entry gates, ATR sizing & circuit breakers
│   ├── auth.py                 # Multi-user auth, session JWTs & RBAC
│   ├── day_strategy.py         # Intraday VWAP, ORB & EMA momentum signals
│   ├── desk_risk.py            # Desk-level risk accounting
│   ├── dip_hunt.py             # Buy-the-dip re-entry after a stop-out
│   ├── followon_store.py       # Next-ticket plans, persisted per account
│   ├── history_insights.py     # Deterministic review analytics + LLM narration
│   ├── lessons_store.py        # Human-approved lessons fed back to the AI desk
│   ├── live_quote.py           # Pre-market / after-hours quotes with fallbacks
│   ├── ls_validate.py          # In-sample / out-of-sample L/S validation harness
│   ├── manual_guards.py        # Advisory pre-trade guards & portfolio heat
│   ├── mcp_server.py           # Model Context Protocol server for AI assistants
│   ├── metals_intel.py         # Gold & Silver macro intelligence & GSR engine
│   ├── options_overlay.py      # Options debit spreads, long options & hedges
│   ├── reinvest_store.py       # Armed buy-back plans, persisted per account
│   ├── ticker_search.py        # Scored symbol & company search
│   └── webapp.py               # FastAPI server & 99 REST endpoints
├── web/                        # Web & Mobile PWA frontend
│   ├── auto-trade.html         # Auto-trade desk & approval queue
│   ├── positions.html          # Position desk, exit strategies & lot liquidation
│   ├── manual-order.html       # Advanced order ticketing & next-ticket chaining
│   ├── history.html            # Fill ledger, desk review & lessons
│   ├── backtest.html           # Walk-forward daily & intraday backtester
│   ├── backtest-compare.html   # Side-by-side run comparison
│   ├── admin.html              # Multi-user directory, RBAC & SMTP settings
│   ├── static/css/             # Responsive desktop & mobile PWA stylesheets (4 themes)
│   └── static/lang/            # i18n translations (EN, BN, ES, FR, HI)
├── tests/                      # 254 automated unit & integration tests
├── scripts/run_ls_validation.py# IS/OOS validation runner for the L/S engine
├── .github/workflows/ci.yml    # CI matrix on Python 3.10 / 3.11 / 3.12
├── Dockerfile                  # Production container definition
├── docker-compose.yml          # 1-command Docker Compose stack
└── requirements.txt            # Core dependencies
```

---

## 🧪 Testing

The suite runs 254 tests across 17 modules with no network access and no Alpaca credentials required.

```bash
# Run all automated tests
python -m unittest discover -s tests

# Run specific modules
python -m unittest tests.test_metals_strategy -v
python -m unittest tests.test_day_trading -v
python -m unittest tests.test_exit_strategy -v
python -m unittest tests.test_multiuser_isolation -v

# Validate the Regime L/S engine out-of-sample (requires Alpaca data keys)
python -m scripts.run_ls_validation
```

CI compiles `bot`, `scripts`, and `tests`, then runs the full suite on Python 3.10, 3.11, and 3.12 for every push and pull request.

---

## 🤝 Contributing & Security

Contributions, strategy ideas, and bug fixes are warmly welcomed! Please review our [Contributing Guidelines](CONTRIBUTING.md) and [Code of Conduct](CODE_OF_CONDUCT.md). For vulnerability disclosures, refer to our [Security Policy](SECURITY.md).

---

## ⚖️ Financial & Legal Disclaimer

> [!CAUTION]
> **DISCLAIMER**: AlgoPaca is an open-source project provided for educational, research, and technical evaluation purposes only. **Nothing contained in this software constitutes financial, investment, legal, or tax advice.** Trading involves substantial risk of loss. Always test thoroughly in **Paper Trading (simulated)** before committing live capital.

---

## 📄 License

AlgoPaca is open-source software licensed under the [MIT License](LICENSE).
