---
trigger: always_on
---

# Strategy Dual-Sync Policy: Auto Trade & Backtest Parity

Whenever any trading strategy is added, modified, tuned, optimized, or refactored in this project, the changes **MUST be applied and synchronized symmetrically across BOTH Auto Trade (Live/Paper Execution) AND Backtest (Historical Simulation)**.

Zero logic drift between backtest simulation and real-world auto trade execution is non-negotiable.

---

## 1. Core Principle (জিরো প্যারিটি ড্রিফ্ট / Zero Parity Drift)

- **Never update Backtest without Auto Trade**: If an indicator, threshold, entry trigger, exit condition, risk filter, or parameter is added or adjusted for backtesting, the identical logic must be active and accessible in the auto trade engine.
- **Never update Auto Trade without Backtest**: If live/paper order execution, trailing stop rules, time-of-day cutoffs, or position sizing behaviors are adjusted in auto trade, the backtest engine must simulate those exact conditions.
- **Single Source of Truth**: Core strategy calculations and signal evaluations must reside in shared strategy classes (`bot/*strategy*.py`) and be directly consumed by both the backtest engine (`bot/*backtest*.py`) and the auto trader engine (`bot/*trader*.py`).

---

## 2. Architecture & File Mapping

When touching any strategy family, you must inspect and update the corresponding counterparts:

| Strategy Family | Core Strategy Logic | Auto Trade Engine | Backtest Engine | Presets File | UI Templates & Scripts |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SMA Crossover** | `bot/strategy.py` (`SmaCrossoverStrategy`) | `bot/trader.py`, `bot/multi_trader.py` | `bot/backtest.py` | `bot/sma_presets.py` | `web/auto-trade.html`, `web/backtest.html`, `web/static/js/auto-trade.js`, `web/static/js/backtest.js` |
| **Buy The Dip** | `bot/strategy.py` (`BuyTheDipStrategy`), `bot/dip_hunt.py` | `bot/trader.py`, `bot/multi_trader.py` | `bot/backtest.py` | `bot/dip_presets.py` | `web/auto-trade.html`, `web/backtest.html`, `web/static/js/auto-trade.js`, `web/static/js/backtest.js` |
| **Day Trading (VWAP/ORB/EMA/MR)** | `bot/day_strategy.py` | `bot/day_trader.py` | `bot/day_backtest.py` | `bot/day_presets.py` | `web/auto-trade.html`, `web/backtest.html`, `web/static/js/auto-trade.js`, `web/static/js/backtest.js` |
| **Long / Short (LS)** | `bot/ls_strategy.py`, `bot/ls_validate.py` | `bot/ls_trader.py` | `bot/ls_backtest.py` | N/A | `web/auto-trade.html`, `web/backtest.html`, `web/static/js/auto-trade.js`, `web/static/js/backtest.js` |
| **Pair Trading** | `bot/pair_strategy.py` | `bot/pair_trader.py` | `bot/pair_backtest.py` | `bot/pair_presets.py` | `web/auto-trade.html`, `web/backtest.html`, `web/static/js/auto-trade.js`, `web/static/js/backtest.js` |
| **AI Trader / LLM Brain** | `bot/ai_brain.py`, `bot/ai_risk.py` | `bot/ai_trader.py` | `bot/ai_backtest.py` | `bot/ai_presets.py` | `web/auto-trade.html`, `web/backtest.html`, `web/static/js/auto-trade.js`, `web/static/js/backtest.js` |
| **Metals / Options Overlay** | `bot/metals_intel.py`, `bot/options_overlay.py` | `bot/trader.py`, `bot/multi_trader.py` | `bot/backtest.py` | Relevant config | `web/auto-trade.html`, `web/backtest.html` |

---

## 3. Mandatory 5-Layer Synchronization Checklist

Whenever updating, adding, or modifying a strategy, verify and complete all 5 layers:

### Layer 1: Core Strategy Logic (`bot/*strategy*.py`)
- [ ] Strategy logic, indicators, signal enums (`Signal.BUY`, `Signal.SELL`, `Signal.HOLD`), and evaluation methods (`evaluate()`, `StrategyResult`) are updated.
- [ ] Indicator calculations (SMA, EMA, RSI, ADX, ATR, VWAP, Bollinger Bands) use the shared routines in `bot/analysis.py` consistently.
- [ ] Avoid duplicating logic across files. Ensure both backtest and auto trade import and call the same strategy class.

### Layer 2: Execution & Simulation Engines (`bot/*trader*.py` & `bot/*backtest*.py`)
- [ ] **Signal Ingestion**: Both engines receive and process signals from the strategy class without altering the core trigger rules.
- [ ] **Exit Conditions**: Stop Loss (SL), Take Profit (TP), Trailing Stops, Time-of-day square-offs (e.g. 15:55 ET intraday exit), and maximum hold periods must execute identically in simulation and live paper trading.
- [ ] **Position Sizing**: Sizing models (fixed share, fixed dollar sizing, risk-based sizing, ATR sizing) must calculate order quantities using the same formula and rounding rules.
- [ ] **Slippage & Fill Modeling**: Backtest fill rules must realistically reflect auto trade execution (e.g. market order fill at open of next candle vs limit order fills).

### Layer 3: Presets & Default Configurations (`bot/*presets*.py`)
- [ ] If strategy default values, periods, or multiplier thresholds change, update the corresponding preset files (e.g. `bot/sma_presets.py`, `bot/day_presets.py`, etc.).
- [ ] Ensure that default parameter values match between preset definitions and engine fallback defaults.

### Layer 4: Web API & State Management (`bot/webapp.py`, `bot/web_state.py`)
- [ ] **Request Parsing**: If new strategy parameters are introduced, ensure API handlers for both Auto Trade (`/api/auto-trade/...`) and Backtest (`/api/backtest/...`) accept, validate, and pass the new fields.
- [ ] **State Serialization**: Check that state persistence, active runner configs, and saved backtest results properly store and restore the updated parameters.

### Layer 5: Frontend UI & Forms (`web/*.html` & `web/static/js/*.js`)
- [ ] **Form Fields**: New or modified parameters must appear in both Auto Trade configuration forms (`web/auto-trade.html`, `web/static/js/auto-trade.js`) and Backtest configuration forms (`web/backtest.html`, `web/static/js/backtest.js`).
- [ ] **Default Values & Sliders**: Field IDs, ranges, default values, step increments, and tooltips must match across both interfaces.
- [ ] **Translatable Strings**: Follow the `translatable-strings` policy — use `data-i18n` and `t()` with default strings in English only.

---

## 4. Verification & Testing Protocol

Before considering any strategy update complete:

1. **Dual Test Suite Execution**:
   Run the test suites for both Auto Trade and Backtest to ensure neither side has broken or regressed:
   ```bash
   # Run both backtest and trading engine tests
   ./.venv/bin/pytest tests/test_*backtest*.py tests/test_*trade*.py tests/test_*strategy*.py -v
   ```
2. **Signal Parity Verification**:
   Verify that a known historical dataset fed into the backtest engine yields the identical signal outputs that the auto trade engine would trigger when encountering those identical bars.
3. **Automated Server Restart**:
   Per `server-restart.md`, execute `./restart_web.sh` and verify:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/login
   ```
   Check `.webapp.log` for any runtime errors or schema mismatches.
