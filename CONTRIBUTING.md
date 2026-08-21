# Contributing to AlgoPaca

Thank you for your interest in contributing to **AlgoPaca**! We welcome contributions from developers, quantitative researchers, traders, and open-source enthusiasts.

---

## Code of Conduct

All contributors and maintainers are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please treat everyone with respect and empathy.

---

## Getting Started

### 1. Fork and Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/algopaca.git
cd algopaca
```

### 2. Set Up a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Environment

Copy the example environment file and add your Alpaca Paper credentials:

```bash
cp .env.example .env
```

> [!TIP]
> Always use **Paper Trading credentials** during development. Never test changes against Live trading accounts.

---

## Development Workflow

### Project Structure

- `bot/`: Core Python trading engine, strategies, risk controls, API connectors, and web backend.
  - `strategy.py`: Classic SMA crossover engine.
  - `dip_hunt.py`: Buy-the-dip oversold/washout engine.
  - `pair_strategy.py`: Long/Short pair rotation engine.
  - `ls_strategy.py`: Regime Dual Momentum engine.
  - `ai_brain.py` & `ai_trader.py`: LLM-based autonomous trading and risk wrapper.
  - `desk_risk.py`: Mechanical position sizing and risk guardrails.
  - `webapp.py`: FastAPI server and route handlers.
- `web/`: Frontend templates, styles, and UI JavaScript.
  - `web/static/css/`: CSS design system.
  - `web/static/js/`: Vanilla JavaScript frontend interactions.
  - `web/static/lang/`: Internationalization JSON translation catalogs.
- `tests/`: Automated unit and integration test suite.
- `scripts/`: Validation and analysis scripts.

### Running the Test Suite

Before submitting any code, ensure all tests pass:

```bash
python -m unittest discover -s tests
```

### Adding New Strategies or AI Presets

1. Strategy definitions belong in `bot/` with matching presets in `bot/*_presets.py`.
2. Any new parameters must be reflected in `bot/config.py` and `.env.example`.
3. Add corresponding unit tests in `tests/`.
4. If modifying the UI, ensure translations in `web/static/lang/` and `web/static/i18n.js` are updated.

---

## Submitting Pull Requests

1. **Create a branch**: Use a descriptive branch name (e.g., `feat/trailing-stop-preset` or `fix/alpaca-order-reconnect`).
2. **Write clean code**: Follow PEP 8 style standards with type hints.
3. **Add tests**: Include unit tests for new features and bug fixes.
4. **Commit messages**: Use concise, conventional commit messages (e.g., `feat: add bollinger squeeze preset`).
5. **Open a PR**: Fill out the provided pull request template detailing changes and testing steps.

---

## Reporting Issues

- **Bug reports**: Please open an issue using the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.yml) with reproduction steps and logs (ensure no private API keys are included).
- **Feature proposals**: Use the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.yml) to outline use cases and design proposals.
