<p align="center">
  <img src="web/static/img/algopaca-banner.svg" alt="AlgoPaca Quantitative Trading Desk" width="100%">
</p>

<p align="center">
  <strong>Autonomous Algorithmic Paper &amp; Live Trading Desk</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg" alt="Python Versions">
  <img src="https://img.shields.io/badge/broker-Alpaca%20Markets-green.svg" alt="Alpaca Markets">
  <img src="https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker&logoColor=white" alt="Docker Ready">
  <img src="https://img.shields.io/badge/status-open%20source-success.svg" alt="Open Source">
</p>

---

AlgoPaca is an open-source, full-stack quantitative trading application designed for [Alpaca Markets](https://alpaca.markets). It combines **5 algorithmic trading engines**, mechanical **risk guardrails**, an interactive **walk-forward backtesting suite**, and a clean, responsive **web trading desk**.

---

## Key Features

- 📈 **5 Algorithmic Trading Engines**:
  1. **Classic SMA Crossover** — Moving average trend-following across watchlist tickers with multiple presets.
  2. **Buy the Dip** — Oversold mean-reversion with RSI + lower Bollinger wash entries and recovery exits.
  3. **Long & Short Pair Rotator** — Dynamic regime-impulse rotator between two correlated/inverse symbols (e.g. QLD / QURL).
  4. **Regime Dual Momentum (L/S)** — Daily EMA + ADX regime gate with MACD histogram triggers and ATR-based risk sizing.
  5. **AI Quantitative Desk** — Multi-modal LLM engine (OpenAI GPT-4o / Google Gemini 2.0) fusing technical analysis (RSI, MACD, SMA, Bollinger, ATR, ADX), Yahoo Finance news feeds, Nasdaq earnings calendar (EPS surprises), and Forex Factory USD macroeconomic events into autonomous trading decisions.
- 🛡️ **Autonomous Mechanical Risk Engine**: Pre-execution risk gates that enforce position sizing (`equity × risk% ÷ ATR`), trailing stop-losses, take-profit scaling, max concurrent positions, daily drawdown circuit-breakers, spread checks, and post-loss cooldowns.
- 🧪 **Interactive Backtest & Comparison Suite**: Walk-forward simulations on historical daily/minute bars with side-by-side run comparisons (metrics, equity curves, drawdown, win-rate, profit factor).
- 🌐 **Modern Responsive Web Desk**: Fast, lightweight vanilla CSS/JS UI with real-time portfolio tracking, manual order ticketing, cycle audit logs, history analytics, and multi-language support (English, Bangla, Spanish, French, Hindi).

---

## Strategy Engines Overview

| Mode | Core Logic | Highlights & Presets |
|------|------------|----------------------|
| **SMA** | Trend-following moving average crossovers | Classic (10/30), Short-term (5/20), Fibonacci (8/21), Swing (20/50), Golden Cross (50/200), or Custom. |
| **Dip** | Oversold capitulation & mean-reversion | RSI threshold washouts + Bollinger lower band taps. Presets: Deep, Mild pullback, Washout, Custom. |
| **Pair** | Regime-impulse rotation across 2 symbols | Holds long leg in bull regimes; rotates into short leg on confirmed bear impulses (e.g. 7-day drop ≤ -5% below SMA). |
| **LS** | Dual momentum long/short execution | EMA fast/slow + ADX trend strength gate. Holds through chop, exits on signal flip, ATR stop, or R:R target. |
| **AI Desk** | Multi-factor quantitative LLM analysis | TA + News + Earnings + Macro events → JSON decision → risk-sized orders. Presets: Balanced, Conservative, Momentum, Mean Reversion, PEAD, ORB, Custom. |

---

## Quickstart

### Option 1: Run with Docker & Docker Compose (Recommended)

The easiest way to spin up AlgoPaca with zero manual Python environment configuration:

```bash
# 1. Clone the repository
git clone https://github.com/your-username/algopaca.git
cd algopaca

# 2. Copy and configure environment variables
cp .env.example .env

# 3. Start AlgoPaca container
docker compose up -d
```

Open your browser at **[http://localhost:8765](http://localhost:8765)**.

---

### Option 2: Local Python Setup (macOS / Linux / Windows)

#### Prerequisites
- Python 3.10, 3.11, or 3.12
- Git
- Free [Alpaca Paper Trading Account](https://app.alpaca.markets/paper/dashboard/overview)

#### macOS & Linux:

```bash
# 1. Clone the repository
git clone https://github.com/your-username/algopaca.git
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
git clone https://github.com/your-username/algopaca.git
cd algopaca

:: 2. Launch web desk (automatically sets up .venv & dependencies)
launch_web.bat
```

Open **[http://127.0.0.1:8765](http://127.0.0.1:8765)** to access your trading desk.

---

## CLI & Terminal Execution

AlgoPaca also provides a full-featured terminal CLI for headless servers or automated cron loops:

```bash
# Display connected account information
./run.sh --account

# Run a single strategy cycle
./run.sh --once --mode sma --sma-preset golden_cross
./run.sh --once --mode dip --dip-preset deep
./run.sh --once --mode pair
./run.sh --once --mode ls
./run.sh --once --mode ai --provider openai --preset balanced
./run.sh --once --mode ai --provider gemini --preset momentum

# Run continuous trading loop
./run.sh --mode ai --provider gemini
```

On Windows, replace `./run.sh` with `run.bat`.

---

## Configuration (`.env`)

AlgoPaca uses environment variables for default settings. All keys can also be configured directly in the Web UI under the **Configuration** page.

```env
# --- Alpaca Paper Trading (Default) ---
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true

# --- Alpaca Live Trading (Safety Guarded) ---
ALPACA_LIVE_API_KEY=AK...
ALPACA_LIVE_SECRET_KEY=...
ALPACA_ALLOW_LIVE=false

# --- AI Providers (OpenAI or Gemini) ---
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash
AI_PROVIDER=openai

# --- Strategy Defaults ---
STRATEGY_MODE=sma
SYMBOLS=AAPL,MSFT,NVDA,SPY,QQQ
SMA_PRESET=classic
DIP_PRESET=deep
AI_PRESET=balanced

# --- Mechanical Risk Engine ---
AI_RISK_PCT=0.5             # Risk 0.5% equity per trade
AI_ATR_STOP_MULT=1.8        # ATR14 trailing stop distance
AI_TAKE_PROFIT_R=2.0        # Trim 50% at 2.0x initial risk
AI_TRAIL_AFTER_R=1.0        # Breakeven & trail after 1.0x risk
AI_MAX_POSITIONS=3          # Max concurrent open positions
AI_DAILY_LOSS_LIMIT_PCT=3.0 # Halt entries if day P&L <= -3%
AI_COOLDOWN_MINUTES=60      # Cooldown before re-entering stopped ticker

# --- Web Server ---
ALGOPACA_HOST=127.0.0.1
ALGOPACA_PORT=8765
```

See [.env.example](.env.example) for a complete list of configuration parameters.

---

## Paper vs. Live Trading Safety

1. **Paper Trading is Default**: Every installation starts in Paper mode using simulated capital.
2. **Dedicated Credential Slots**: Paper keys (`ALPACA_API_KEY`) and Live keys (`ALPACA_LIVE_API_KEY`) are stored in isolated slots to prevent accidental promotion.
3. **Hard Killswitch**: Live trading requires `ALPACA_ALLOW_LIVE=true` and explicit user confirmation in the Configuration menu.
4. **Context Reset on Switch**: Switching environments immediately stops active loops, cancels armed buy-back triggers, and resets portfolio caches.
5. **Fail-Safe Fallback**: Any authorization failure on Live immediately reverts the desk back to Paper mode.

---

## Project Structure

```
algopaca/
├── bot/                     # Core Python trading engines & backend
│   ├── ai_brain.py          # AI decision making & market context builder
│   ├── ai_models.py         # Supported LLM models & defaults
│   ├── ai_presets.py        # Named AI strategy presets
│   ├── ai_providers.py      # OpenAI & Gemini API clients
│   ├── ai_risk.py           # Mechanical sizing & position risk rules
│   ├── ai_trader.py         # AI execution controller
│   ├── analysis.py          # Technical indicators (RSI, MACD, Bollinger, ATR, ADX)
│   ├── backtest.py          # SMA & Buy-the-Dip walk-forward engine
│   ├── backtest_store.py    # Backtest result persistence
│   ├── client.py            # Alpaca API client & market data wrapper
│   ├── config.py            # Configuration loader & validator
│   ├── desk_risk.py         # Shared multi-engine risk logic
│   ├── dip_hunt.py          # Buy-the-dip engine
│   ├── earnings.py          # Nasdaq earnings calendar & EPS surprise parser
│   ├── econ_calendar.py     # Economic calendar reader
│   ├── history_insights.py  # Performance attribution & trade review engine
│   ├── ls_strategy.py       # Regime Dual Momentum (L/S) engine
│   ├── pair_strategy.py     # Long/Short pair rotation engine
│   ├── strategy.py          # SMA crossover engine
│   ├── trader.py            # Execution dispatcher
│   └── webapp.py            # FastAPI web server
├── web/                     # Web Trading Desk frontend (HTML, CSS, JS)
│   ├── static/css/          # Responsive styling & dark theme
│   ├── static/js/           # Client-side trading desk interactions
│   └── static/lang/         # i18n JSON language catalogs (EN, BN, ES, FR, HI)
├── tests/                   # Automated unit & integration test suite
├── scripts/                 # Analysis and strategy validation scripts
├── Dockerfile               # Production container definition
├── docker-compose.yml       # 1-command Docker Compose stack
├── launch_web.sh            # macOS / Linux web launcher
├── launch_web.bat           # Windows web launcher
├── run.sh                   # macOS / Linux CLI runner
├── run.bat                  # Windows CLI runner
├── requirements.txt         # Python dependencies
└── LICENSE                  # MIT License
```

---

## Testing

AlgoPaca includes a comprehensive unit and integration test suite:

```bash
# Run all tests
python -m unittest discover -s tests

# Run specific test modules with verbose output
python -m unittest tests.test_desk -v
```

---

## Contributing

We welcome community contributions! Please review our [Contributing Guidelines](CONTRIBUTING.md) and [Code of Conduct](CODE_OF_CONDUCT.md) before submitting a pull request.

To report a bug or request a feature, please use our [GitHub Issue Templates](.github/ISSUE_TEMPLATE/).

---

## Security

Security is critical for trading applications. If you discover a vulnerability, please consult our [Security Policy](SECURITY.md) to report it responsibly.

---

## Financial & Legal Disclaimer

> [!CAUTION]
> **DISCLAIMER**: AlgoPaca is an open-source software project provided for educational, research, and technical evaluation purposes only. **Nothing contained in this software, documentation, or repository constitutes financial, investment, legal, or tax advice.**
>
> Trading equities, securities, and cryptocurrencies involves a high degree of risk and the potential for substantial financial loss. Algorithmic trading systems are subject to market slippage, software bugs, connectivity loss, and unforeseen market conditions.
>
> The developers and contributors of AlgoPaca accept **NO RESPONSIBILITY OR LIABILITY** for any financial losses, damages, or unintended trades resulting from the use of this software. Always test strategies extensively in a **Paper Trading (simulated)** environment before considering live execution.

---

## License

AlgoPaca is released under the [MIT License](LICENSE).
