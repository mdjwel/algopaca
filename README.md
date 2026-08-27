<p align="center">
  <img src="web/static/img/algopaca-banner.svg" alt="AlgoPaca Quantitative Trading Desk" width="100%">
</p>

<p align="center">
  <strong>Autonomous Algorithmic Paper &amp; Live Trading Desk</strong><br>
  <em>5 Algorithmic Engines • Multi-Modal AI Reasoning • Automated ATR Risk Guardrails • Interactive Backtesting • Modern Web Desk</em>
</p>

<p align="center">
  <a href="https://algopaca.spiderdevs.xyz/"><img src="https://img.shields.io/badge/🚀_Live_Demo-algopaca.spiderdevs.xyz-orange.svg" alt="Live Demo"></a>
  <a href="https://x.com/ehjewelbd/status/2090872372731711646"><img src="https://img.shields.io/badge/𝕏_Post-Demo_%26_Announcement-black?logo=x&logoColor=white" alt="X Demo Post"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/trading-100%25_free_%26_commission--free-success.svg" alt="100% Free & Commission-Free Trading">
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg" alt="Python Versions">
  <img src="https://img.shields.io/badge/broker-Alpaca%20Markets-green.svg" alt="Alpaca Markets">
  <img src="https://img.shields.io/badge/AI-OpenAI%20%7C%20Gemini-8A2BE2.svg" alt="AI Models">
  <img src="https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker&logoColor=white" alt="Docker Ready">
  <img src="https://img.shields.io/badge/status-open%20source-success.svg" alt="Open Source">
</p>

<p align="center">
  <a href="https://algopaca.spiderdevs.xyz/">Live Demo</a> •
  <a href="#-watch-in-action-demo--walkthrough">Demo Video</a> •
  <a href="#-key-features">Features</a> •
  <a href="#-strategy-engines-overview">Strategies</a> •
  <a href="#%EF%B8%8F-system-architecture">Architecture</a> •
  <a href="#-quickstart">Quickstart</a> •
  <a href="#-mcp-server-claude-cursor-and-other-ai-assistants">MCP Server</a> •
  <a href="#-cli--terminal-execution">CLI Usage</a> •
  <a href="#%EF%B8%8F-configuration-env">Configuration</a> •
  <a href="#-risk-management--safety-guardrails">Risk Safety</a> •
  <a href="#-contributing">Contributing</a>
</p>

---

AlgoPaca is a **100% free, MIT-licensed, full-stack quantitative trading desk** built natively for [Alpaca Markets](https://alpaca.markets) with **$0 commission stock & ETF trading**. It democratizes algorithmic and AI-driven trading by replacing expensive monthly SaaS bot subscriptions with a self-hosted, institutional-grade platform featuring **5 algorithmic trading engines**, mechanical **risk guardrails**, an interactive **walk-forward backtesting suite**, and a clean, responsive **web trading desk**.

> [!NOTE]
> 💸 **100% Free Platform & Commission-Free Trading**:
> AlgoPaca is completely free & open source with **zero monthly subscription fees** and **zero paywalls**. Combined with Alpaca Markets' native API for US equities and ETFs, you get automated quant trading with **$0 platform costs and $0 brokerage commissions**.

---

## 🎬 Watch in Action (Demo & Walkthrough)

Check out the launch walkthrough video and feature demonstration on **𝕏 (Twitter)**:

<p align="center">
  <a href="https://x.com/ehjewelbd/status/2090872372731711646">
    <img src="https://img.shields.io/badge/▶_Watch_Live_Demo_on_𝕏-000000?style=for-the-badge&logo=x&logoColor=white" alt="Watch AlgoPaca Demo on X" height="40">
  </a>
</p>

> [!TIP]
> 📺 **Launch Announcement & Video Walkthrough**:  
> _"🚨 Introducing AlgoPaca — A fully autonomous AI Quantitative Trading Desk for @AlpacaMarkets 🦙🤖 Fuses Technicals + Live News + Nasdaq Earnings + Macro events with automated ATR risk guardrails."_  
> 🔗 **Direct Link:** [https://x.com/ehjewelbd/status/2090872372731711646](https://x.com/ehjewelbd/status/2090872372731711646)

---

## 🌟 Why AlgoPaca?

- 💸 **100% Free & Commission-Free Trading**: No recurring monthly subscription tiers, no hidden platform fees, and $0 commission US stock & ETF trading natively via Alpaca Markets.
- 🧠 **Multi-Factor AI Intelligence**: Leverages state-of-the-art LLMs (OpenAI GPT-4o / GPT-4o-mini and Google Gemini 2.0 Flash / Pro) to fuse quantitative technical indicators, financial news sentiment, earnings surprise history, and macroeconomic calendar events into reasoned trading decisions.
- 🛡️ **Mechanical Risk Protection**: Removes emotional decision-making with strict volatility-based ATR position sizing, automatic trailing stops, profit scaling, spread checks, and daily drawdown circuit breakers.
- 🔌 **MCP Server for AI Assistants**: A built-in [Model Context Protocol](https://modelcontextprotocol.io) server exposes AlgoPaca's engines as structured tools, so Claude, Cursor, and other AI assistants can check the account, run a strategy cycle, place/close orders, and backtest directly.
- ⚡ **Zero-Build Web Desk**: Ultra-fast, lightweight Vanilla JS/CSS web desk with real-time portfolio metrics, order execution, cycle audit history, and multi-language internationalization.
- 🐳 **1-Click Deployment**: Get up and running in 60 seconds with Docker Compose or standard Python virtual environments.

---

## ✨ Key Features

- 📈 **5 Algorithmic Trading Engines**:
  1. **Classic SMA Crossover** — Moving average trend-following across watchlist tickers with multiple proven presets (Golden Cross, Swing, Fibonacci, etc.).
  2. **Buy the Dip** — Oversold mean-reversion with RSI + lower Bollinger wash entries and recovery exits.
  3. **Long & Short Pair Rotator** — Dynamic regime-impulse rotator between two correlated/inverse symbols (e.g., QLD / QURL or TQQQ / SQQQ).
  4. **Regime Dual Momentum (L/S)** — Daily EMA + ADX regime gate with MACD histogram triggers and ATR-based risk sizing.
  5. **AI Quantitative Desk** — Multi-modal LLM engine synthesizing Technicals, News Sentiment, Nasdaq Earnings, and Forex Factory Economic Releases into structured JSON trading decisions.
- 🛡️ **Autonomous Mechanical Risk Engine**: Pre-execution risk gates enforcing position sizing (`equity × risk% ÷ ATR`), trailing stop-losses, take-profit scaling, max concurrent positions, daily drawdown circuit-breakers, spread checks, and post-loss cooldowns.
- 🧪 **Interactive Backtest & Comparison Suite**: Walk-forward simulations on historical daily/minute bars with side-by-side run comparisons (CAGR, Sharpe Ratio, Max Drawdown, Win Rate, Profit Factor, Equity Curve visualization).
- 🌐 **Modern Responsive Web Desk**: Real-time portfolio tracking, advanced order ticketing with risk guards, cycle audit logs, history analytics, and multi-language support (English, Bengali, Spanish, French, Hindi).
- 🔒 **Dual-Slot Paper & Live Trading Safety**: Strict separation of Paper and Live credentials with hard killswitches, context resets, and automatic fallback safeguards.

---

## 📊 Strategy Engines Overview

| Engine      | Strategy Type      | Core Logic                                        | Highlights & Presets                                                                                                                                                                                         |
| :---------- | :----------------- | :------------------------------------------------ | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SMA**     | Trend Following    | Moving average crossover filters across watchlist | **Classic** (10/30), **Short-term** (5/20), **Fibonacci** (8/21), **Swing** (20/50), **Golden Cross** (50/200), or **Custom**.                                                                               |
| **Dip**     | Mean Reversion     | Capitulation washouts & recovery bounces          | RSI threshold washouts + Bollinger lower band taps. Presets: **Deep**, **Mild pullback**, **Washout**, **Custom**.                                                                                           |
| **Pair**    | Regime Rotation    | Dynamic regime-impulse rotation across 2 symbols  | Holds long leg in bull regimes; rotates into short leg on confirmed bear impulses (e.g. 7-day drop ≤ -5% below SMA).                                                                                         |
| **LS**      | Dual Momentum      | Trend strength momentum (Long/Short)              | EMA fast/slow + ADX trend strength gate. Holds through chop, exits on signal flip, ATR stop, or R:R target.                                                                                                  |
| **AI Desk** | Multi-Factor Quant | LLM multi-modal market synthesis & reasoning      | TA (RSI, MACD, BB, ATR, ADX) + News + Earnings + Macro events → JSON decision → risk-sized orders. Presets: **Balanced**, **Conservative**, **Momentum**, **Mean Reversion**, **PEAD**, **ORB**, **Custom**. |

---

## 🚀 Quickstart

> [!TIP]
> 🌐 **Try it live first**: **[algopaca.spiderdevs.xyz](https://algopaca.spiderdevs.xyz/)** is a hosted instance of the multi-user desk — sign up and connect your own Alpaca paper keys without installing anything.

### Option 1: Run with Docker & Docker Compose (Recommended)

The easiest way to spin up AlgoPaca with zero manual Python environment configuration:

```bash
# 1. Clone the repository
git clone https://github.com/mdjwel/algopaca.git
cd algopaca

# 2. Copy and configure environment variables
cp .env.example .env

# 3. Start AlgoPaca container in background
docker compose up -d
```

Open your browser at **[http://localhost:8765](http://localhost:8765)**.

To stop the container:

```bash
docker compose down
```

---

### Option 2: Local Python Setup (macOS / Linux / Windows)

#### Prerequisites

- **Python 3.10, 3.11, or 3.12**
- **Git**
- Free [Alpaca Paper Trading Account](https://app.alpaca.markets/paper/dashboard/overview)

#### macOS & Linux:

```bash
# 1. Clone the repository
git clone https://github.com/mdjwel/algopaca.git
cd algopaca

# 2. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env

# 5. Launch the Web Trading Desk
./launch_web.sh
```

#### Windows (Command Prompt or PowerShell):

```cmd
:: 1. Clone and navigate to folder
git clone https://github.com/mdjwel/algopaca.git
cd algopaca

:: 2. Launch web desk (automatically sets up .venv & dependencies)
launch_web.bat
```

Open **[http://127.0.0.1:8765](http://127.0.0.1:8765)** to access your trading desk.

---

## 🔌 MCP Server (Claude, Cursor, and other AI assistants)

AlgoPaca ships its own [Model Context Protocol](https://modelcontextprotocol.io) server on top of the same `alpaca-py` Trading API client the CLI and web desk use. It exposes AlgoPaca's five strategy engines, mechanical risk gates, options overlay, and backtester as structured tools, so an AI assistant like Claude or Cursor can check the account, run a strategy cycle, place/close orders, and backtest — all through tool calls instead of the web UI.

```bash
# 1. Configure .env (paper credentials by default — see Configuration below)
cp .env.example .env

# 2. Launch the MCP server (stdio transport)
./run_mcp.sh          # macOS / Linux
run_mcp.bat           # Windows
```

**Available tools:**

| Tool                     | What it does                                                                 |
| :----------------------- | :---------------------------------------------------------------------------- |
| `get_account`             | Equity, cash, buying power, paper/live status, day P&L                       |
| `get_positions`           | All open positions (stocks, ETFs, and options)                               |
| `get_open_orders`         | Open orders grouped by symbol, with protective-stop metadata                 |
| `list_strategy_presets`   | Named parameter presets for an engine (e.g. `sma` → `golden_cross`)          |
| `run_strategy_cycle`      | Evaluate + (if a signal clears every risk gate) execute one engine's cycle, including its options overlay |
| `place_manual_order`      | User-directed market/limit/stop order, with bracket/OTO stop-loss & take-profit |
| `close_position`          | Liquidate a position fully or partially                                      |
| `run_backtest`            | Walk-forward backtest an SMA or dip strategy on historical bars (no orders)  |

Add it to Claude Desktop's `claude_desktop_config.json` (or Cursor's MCP settings) as a local stdio server:

```json
{
  "mcpServers": {
    "algopaca": {
      "command": "/absolute/path/to/algopaca/.venv/bin/python",
      "args": ["-m", "bot.mcp_server"],
      "cwd": "/absolute/path/to/algopaca"
    }
  }
}
```

> [!NOTE]
> The server inherits AlgoPaca's paper/live safety model straight from `.env` — every tool call runs against Alpaca paper by default, and live trading only activates when `ALPACA_PAPER=false` and `ALPACA_ALLOW_LIVE=true` are both set. There is no way to flip environments per tool call.

---

## 💻 CLI & Terminal Execution

AlgoPaca provides a full-featured terminal CLI for headless servers, automated cron jobs, or scriptable executions:

```bash
# Display connected Alpaca account status and buying power
./run.sh --account

# Run a single strategy evaluation cycle
./run.sh --once --mode sma --sma-preset golden_cross
./run.sh --once --mode dip --dip-preset deep
./run.sh --once --mode pair
./run.sh --once --mode ls
./run.sh --once --mode ai --provider openai --preset balanced
./run.sh --once --mode ai --provider gemini --preset momentum

# Run continuous autonomous loop (polls at configured intervals)
./run.sh --mode ai --provider gemini --preset balanced

# Override symbols for a cycle
./run.sh --once --mode sma --symbols AAPL,MSFT,NVDA,TSLA
```

_(On Windows, replace `./run.sh` with `run.bat`)_

---

## ⚙️ Configuration (`.env`)

AlgoPaca uses environment variables for default configuration. All settings can also be modified in real-time via the **Configuration** page in the Web Desk.

```env
# ==========================================
# Alpaca Paper Trading Credentials (Default)
# ==========================================
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true

# ==========================================
# Alpaca Live Trading Credentials (Safety Guarded)
# ==========================================
ALPACA_LIVE_API_KEY=AK...
ALPACA_LIVE_SECRET_KEY=...
ALPACA_ALLOW_LIVE=false

# ==========================================
# AI Model Providers & Keys
# ==========================================
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash
AI_PROVIDER=openai

# ==========================================
# Default Strategy Settings
# ==========================================
STRATEGY_MODE=sma
SYMBOLS=AAPL,MSFT,NVDA,SPY,QQQ
SMA_PRESET=classic
DIP_PRESET=deep
AI_PRESET=balanced

# ==========================================
# Mechanical Risk Engine
# ==========================================
AI_RISK_PCT=0.5             # Risk 0.5% equity per trade
AI_ATR_STOP_MULT=1.8        # ATR(14) trailing stop distance multiplier
AI_TAKE_PROFIT_R=2.0        # Take profit target (2.0x initial risk)
AI_TRAIL_AFTER_R=1.0        # Move stop to breakeven & trail after 1.0x R
AI_MAX_POSITIONS=3          # Max concurrent open positions
AI_DAILY_LOSS_LIMIT_PCT=3.0 # Circuit breaker: halt entries if daily P&L <= -3%
AI_COOLDOWN_MINUTES=60      # Cooldown before re-entering a stopped ticker
AI_MAX_ALLOC_PCT=25.0       # Max portfolio allocation per individual trade

# ==========================================
# Web Server Configuration
# ==========================================
ALGOPACA_HOST=127.0.0.1
ALGOPACA_PORT=8765
```

See [.env.example](.env.example) for the full list of options and default parameters.

---

## 🛡️ Risk Management & Safety Guardrails

AlgoPaca is architected from the ground up with mechanical risk controls that execute prior to every order:

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
└────────────────────────────────────────────────────────┘
```

### Paper vs. Live Safety Guarantees

1. **Default Paper Mode**: Fresh installations always start in Paper mode using simulated capital.
2. **Dedicated Credential Slots**: Paper keys (`ALPACA_API_KEY`) and Live keys (`ALPACA_LIVE_API_KEY`) are kept in separate slots to prevent accidental promotion.
3. **Hard Killswitch**: Live trading requires setting `ALPACA_ALLOW_LIVE=true` and explicit user confirmation in the Configuration menu.
4. **Environment Isolation**: Switching between Paper and Live immediately cancels armed triggers, stops active loops, and clears portfolio caches.
5. **Fail-Safe Fallback**: Any authentication or permission error on Live immediately reverts the system back to Paper mode.

---

## 🧪 Walk-Forward Backtesting

The interactive Backtester enables testing strategies across historical daily and minute bar datasets:

- **Metrics Computed**: Total Return (%), Buy & Hold Return (%), Benchmark Alpha, Sharpe Ratio, Max Drawdown (%), Win Rate (%), Profit Factor, Total Trades, and Average Trade Duration.
- **Side-by-Side Comparison**: Save and compare multiple parameter runs side-by-side to eliminate curve-fitting.
- **Visual Equity Curves**: Interactive charts depicting equity growth versus buy-and-hold benchmarks over time.

---

## 📁 Project Structure

```
algopaca/
├── bot/                     # Core Python trading engines & backend
│   ├── ai_brain.py          # AI prompt assembly & multi-modal context builder
│   ├── ai_models.py         # Supported LLM model catalog
│   ├── ai_presets.py        # Named AI strategy presets (Balanced, PEAD, etc.)
│   ├── ai_providers.py      # OpenAI & Gemini client integrations
│   ├── ai_risk.py           # Mechanical ATR sizing & risk rules
│   ├── ai_trader.py         # AI execution & post-trade reflection controller
│   ├── analysis.py          # Technical indicators (RSI, MACD, Bollinger, ATR, ADX)
│   ├── backtest.py          # Walk-forward backtesting simulation engine
│   ├── backtest_store.py    # Backtest results persistence
│   ├── client.py            # Alpaca API client & market data wrapper
│   ├── config.py            # Configuration loader & validator
│   ├── desk_risk.py         # Multi-engine risk verification
│   ├── dip_hunt.py          # Buy-the-dip engine
│   ├── earnings.py          # Nasdaq earnings calendar & EPS surprise parser
│   ├── econ_calendar.py     # Economic calendar reader (Forex Factory USD)
│   ├── history_insights.py  # Trade history attribution & performance analytics
│   ├── ls_strategy.py       # Regime Dual Momentum (L/S) engine
│   ├── mcp_server.py        # MCP server exposing engines as AI assistant tools
│   ├── pair_strategy.py     # Long/Short pair rotation engine
│   ├── strategy.py          # SMA crossover engine
│   ├── trader.py            # Execution dispatcher
│   └── webapp.py            # FastAPI web server
├── web/                     # Web Trading Desk frontend
│   ├── static/css/          # Responsive styling & dark theme
│   ├── static/js/           # Client-side trading desk interactions & charts
│   └── static/lang/         # i18n JSON language catalogs (EN, BN, ES, FR, HI)
├── tests/                   # Automated unit & integration test suite
├── scripts/                 # Analysis and strategy validation scripts
├── Dockerfile               # Production container definition
├── docker-compose.yml       # 1-command Docker Compose stack
├── launch_web.sh            # macOS / Linux web launcher
├── launch_web.bat           # Windows web launcher
├── run.sh                   # macOS / Linux CLI runner
├── run.bat                  # Windows CLI runner
├── run_mcp.sh                # macOS / Linux MCP server launcher
├── run_mcp.bat                # Windows MCP server launcher
├── requirements.txt         # Python dependencies
├── CONTRIBUTING.md          # Contribution guidelines
├── CODE_OF_CONDUCT.md       # Community code of conduct
├── SECURITY.md              # Security policy & reporting
└── LICENSE                  # MIT License
```

---

## 🧪 Testing

AlgoPaca includes automated unit and integration tests:

```bash
# Run all tests
python -m unittest discover -s tests

# Run specific test modules with verbose output
python -m unittest tests.test_desk -v
```

---

## 🤝 Contributing

We welcome community contributions, bug fixes, strategy improvements, and feedback!

1. Fork the repository on GitHub.
2. Create a feature branch (`git checkout -b feature/amazing-feature`).
3. Commit your changes (`git commit -m 'Add amazing feature'`).
4. Push to the branch (`git push origin feature/amazing-feature`).
5. Open a Pull Request.

Please review our [Contributing Guidelines](CONTRIBUTING.md) and [Code of Conduct](CODE_OF_CONDUCT.md) before submitting.

---

## 🔒 Security

Security is critical for trading applications. If you discover a security vulnerability, please consult our [Security Policy](SECURITY.md) to report it responsibly.

---

## ⚖️ Financial & Legal Disclaimer

> [!CAUTION]
> **DISCLAIMER**: AlgoPaca is an open-source software project provided for educational, research, and technical evaluation purposes only. **Nothing contained in this software, documentation, or repository constitutes financial, investment, legal, or tax advice.**
>
> Trading equities, securities, and cryptocurrencies involves a high degree of risk and the potential for substantial financial loss. Algorithmic trading systems are subject to market slippage, software bugs, connectivity loss, and unforeseen market conditions.
>
> The developers and contributors of AlgoPaca accept **NO RESPONSIBILITY OR LIABILITY** for any financial losses, damages, or unintended trades resulting from the use of this software. Always test strategies extensively in a **Paper Trading (simulated)** environment before considering live execution.

---

## 📄 License

AlgoPaca is open-source software licensed under the [MIT License](LICENSE).
