"""FastAPI web application for AlgoPaca."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from bot.alpaca_errors import humanize_alpaca_error
from bot.backtest_store import summarize_entry
from bot.web_state import AppState

STATE = AppState()

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class FallbackStaticFiles(StaticFiles):
    def __init__(self, primary_dir: Path, fallback_dir: Path, *args, **kwargs):
        self.primary_dir = primary_dir
        self.fallback_dir = fallback_dir
        super().__init__(directory=str(primary_dir), *args, **kwargs)

    async def get_response(self, path: str, scope):
        p1 = self.primary_dir / path
        if p1.is_file():
            self.directory = str(self.primary_dir)
            return await super().get_response(path, scope)
        p2 = self.fallback_dir / path
        if p2.is_file():
            self.directory = str(self.fallback_dir)
            return await super().get_response(path, scope)
        self.directory = str(self.primary_dir)
        return await super().get_response(path, scope)


app = FastAPI(title="AlgoPaca", version="2.0.0")
if (FRONTEND_DIR / "static").is_dir():
    app.mount("/static", FallbackStaticFiles(primary_dir=FRONTEND_DIR / "static", fallback_dir=WEB_DIR / "static"), name="static")
else:
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


class SettingsIn(BaseModel):
    symbol: str = "AAPL"
    symbols: str = "AAPL"
    fast_sma: int = Field(10, ge=2)
    slow_sma: int = Field(30, ge=3)
    sma_preset: str = "classic"
    dip_preset: str = "deep"
    dip_rsi_buy: float = Field(30.0, gt=0, lt=100)
    dip_rsi_sell: float = Field(60.0, gt=0, lt=100)
    dip_skip_bearish: bool = True
    trade_qty: float = Field(1.0, gt=0)
    size_mode: str = "qty"
    trade_notional: float = Field(100.0, gt=0)
    bar_timeframe: str = "15Min"
    poll_seconds: int = Field(20, ge=10)
    strategy_mode: str = "sma"
    pair_preset: str = "research_max"
    pair_sma_period: int = Field(50, ge=2)
    pair_lookback: int = Field(7, ge=1)
    pair_impulse_pct: float = Field(5.0, gt=0, lt=100)
    pair_weak_side: str = "LONG"
    pair_long_symbol: str = ""
    pair_short_symbol: str = ""
    ls_ema_fast: int = Field(21, ge=2)
    ls_ema_slow: int = Field(55, ge=3)
    ls_adx_min: float = Field(20.0, ge=0)
    ls_atr_stop_mult: float = Field(1.5, gt=0)
    ls_risk_pct: float = Field(1.0, gt=0)
    ls_rr: float = Field(2.0, gt=0)
    ls_time_stop_bars: int = Field(15, ge=1)
    ai_provider: str = "openai"
    ai_preset: str = "balanced"
    ai_instructions: str = ""
    ai_min_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    openai_model: Optional[str] = None
    gemini_model: Optional[str] = None
    openai_api_key: str = ""
    gemini_api_key: str = ""
    save_keys_to_env: bool = False
    # None = leave unchanged (prevents older clients from wiping .env stop).
    stop_loss_pct: Optional[float] = Field(None, ge=0.0, le=50.0)
    # AI risk engine — None keeps the stored value, so a client that does not
    # send these cannot silently reset the desk's risk limits.
    ai_risk_pct: Optional[float] = Field(None, ge=0.0, le=10.0)
    ai_atr_stop_mult: Optional[float] = Field(None, ge=0.0, le=10.0)
    ai_take_profit_r: Optional[float] = Field(None, ge=0.0, le=20.0)
    ai_trail_after_r: Optional[float] = Field(None, ge=0.0, le=20.0)
    ai_max_positions: Optional[int] = Field(None, ge=0, le=50)
    ai_daily_loss_limit_pct: Optional[float] = Field(None, ge=0.0, le=100.0)
    ai_min_hold_minutes: Optional[int] = Field(None, ge=0, le=1440)
    ai_cooldown_minutes: Optional[int] = Field(None, ge=0, le=1440)
    ai_max_spread_bps: Optional[float] = Field(None, ge=0.0, le=1000.0)
    # 0 = stop-market; >0 = stop-limit cushion % past the trigger.
    stop_limit_offset_pct: Optional[float] = Field(None, ge=0.0, le=50.0)
    # Omit / null keeps the stored language rather than resetting to English.
    lang: Optional[str] = None
    options_enabled: Optional[bool] = None
    options_style: Optional[str] = None
    options_dte_min: Optional[int] = Field(None, ge=1, le=180)
    options_dte_max: Optional[int] = Field(None, ge=1, le=365)
    options_otm_pct: Optional[float] = Field(None, ge=0.5, le=25.0)
    options_max_contracts: Optional[int] = Field(None, ge=1, le=20)
    options_max_premium_pct: Optional[float] = Field(None, ge=0.0, le=10.0)


class LangIn(BaseModel):
    lang: str


class ApiKeysIn(BaseModel):
    openai_api_key: str = ""
    gemini_api_key: str = ""
    save_to_env: bool = True


class AlpacaKeysIn(BaseModel):
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    # Which credential slot to write. Defaults to the currently active mode.
    environment: Optional[str] = None
    save_to_env: bool = True


class TradingModeIn(BaseModel):
    mode: str = Field(..., pattern="(?i)^(paper|live)$")
    # Accepted for API compatibility; ignored (typed Live gate removed).
    confirm: Optional[str] = None


class LiveAuthorizeIn(BaseModel):
    # Accepted for API compatibility; ignored.
    confirm: str = ""


class ClearAlpacaKeysIn(BaseModel):
    environment: str = Field("all", pattern="(?i)^(paper|live|all)$")


class ClearAiKeysIn(BaseModel):
    openai: bool = False
    gemini: bool = False
    clear_env: bool = True


class ReinvestIn(BaseModel):
    """Buy the shares back after the sell on this ticket fills.

    Alpaca has no sell-then-buy order class, so the desk holds the plan and
    watches the sell order itself — see `AppState._reinvest_worker`.
    """

    enabled: bool = False
    # match = whatever the sell actually filled; custom = an exact share count.
    qty_mode: str = Field("match", pattern="(?i)^(match|custom)$")
    qty: Optional[float] = Field(None, gt=0)
    limit_price: Optional[float] = Field(None, gt=0)
    # How long the plan waits for the sell before it gives up.
    expire_minutes: Optional[float] = Field(None, gt=0, le=1440)


class DipHuntIn(BaseModel):
    """Re-enter cheaper after this buy's protective stop fills.

    The desk watches the stop. If price drops `dip_pct` further before
    `wait_minutes` is up, it buys immediately; otherwise it parks a limit at
    that same target. The cycle repeats until cancelled or a take-profit hits.
    """

    enabled: bool = False
    wait_minutes: Optional[float] = Field(None, gt=0, le=1440)
    dip_pct: Optional[float] = Field(None, gt=0, le=50)


class FollowOnIn(BaseModel):
    """Open the next ticket after this close fills.

    Reverse: close a long then short the same name, or close a short then buy
    it back. Rotate: close anything then buy a different symbol. The next
    ticket is a limit at the typed price, or a market fill. Alpaca has no
    close-then-open class, so the desk watches the close — see
    `AppState._followon_worker`.
    """

    enabled: bool = False
    kind: str = Field("reverse", pattern="(?i)^(reverse|rotate)$")
    target_symbol: Optional[str] = Field(None, max_length=12)
    qty_mode: str = Field("match", pattern="(?i)^(match|custom)$")
    qty: Optional[float] = Field(None, gt=0)
    order_type: str = Field("limit", pattern="(?i)^(market|limit)$")
    # Distinct from the close's ``order_type``. True means skip limit_price.
    market: bool = False
    limit_price: Optional[float] = Field(None, gt=0)
    expire_minutes: Optional[float] = Field(None, gt=0, le=1440)

    @model_validator(mode="before")
    @classmethod
    def coerce_market_ticket(cls, data: Any) -> Any:
        """Treat market next tickets as market even if ``order_type`` is missing.

        The close ticket also has ``order_type``. A client that only sends
        ``market: true`` / ``ticket_type`` must not fall back to a limit that
        then demands a price the form no longer shows.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        flag = data.get("market")
        ticket = str(
            data.get("ticket_type")
            or data.get("followon_order_type")
            or data.get("order_type")
            or ""
        ).strip().lower()
        is_market = (
            flag is True
            or str(flag or "").strip().lower() in {"true", "1", "yes"}
            or ticket == "market"
        )
        if not is_market:
            return data
        data["order_type"] = "market"
        data["market"] = True
        price = data.get("limit_price")
        try:
            missing = price in (None, "") or float(price) <= 0
        except (TypeError, ValueError):
            missing = True
        if missing:
            data.pop("limit_price", None)
        return data


class CancelReinvestIn(BaseModel):
    plan_id: str = Field(..., min_length=1)


class ManualOrderIn(BaseModel):
    symbol: str = "AAPL"
    # Four actions, not two broker sides — see `AppState.place_manual_order`
    # for why "sell" alone is ambiguous once shorting exists.
    side: str = Field(..., pattern="(?i)^(buy|sell|short|cover)$")
    order_type: str = Field(
        "market", pattern="(?i)^(market|limit|stop|stop_limit|trailing_stop)$"
    )
    time_in_force: str = Field("day", pattern="(?i)^(day|gtc|ioc|fok|opg|cls)$")
    # Opt in to extended-hours fills. Alpaca accepts limit DAY/GTC there.
    extended_hours: bool = False
    qty: Optional[float] = Field(None, gt=0)
    size_mode: str = "qty"
    notional: Optional[float] = Field(None, gt=0)
    limit_price: Optional[float] = Field(None, gt=0)
    # Trigger for stop and stop-limit entries.
    stop_price: Optional[float] = Field(None, gt=0)
    # Trailing stops take one dimension or the other, never both.
    trail_percent: Optional[float] = Field(None, gt=0, le=50)
    trail_price: Optional[float] = Field(None, gt=0)
    # None = use desk stop_loss_pct setting.
    stop_loss_pct: Optional[float] = Field(None, ge=0.0, le=50.0)
    # Risk engine — None keeps desk settings (same knobs as Auto Trade).
    ai_risk_pct: Optional[float] = Field(None, ge=0.0, le=10.0)
    ai_atr_stop_mult: Optional[float] = Field(None, ge=0.0, le=10.0)
    # Take-profit leg, priced in R (stop distances). 0 = stop only, no target.
    take_profit_r: Optional[float] = Field(None, ge=0.0, le=20.0)
    # 0 = sell/cover at market after the stop; >0 = stop-limit cushion %.
    stop_limit_offset_pct: Optional[float] = Field(None, ge=0.0, le=50.0)
    # Absolute sell/cover limit after the stop. Long: at or below stop; short: at or above.
    stop_limit_price: Optional[float] = Field(None, gt=0)
    preview: bool = False
    # When True, accept sell clamp / whole-share truncation and submit.
    confirm_adjusted_qty: bool = False
    # When True, submit even though the ticket crosses a desk risk limit. The
    # limits are advisory for a human, but never silent — see `manual_guards`.
    override_breaches: bool = False
    # Client-generated id for one click. Lets a retry of the same request be
    # recognised as a duplicate rather than placed as a second order.
    ticket_id: Optional[str] = Field(None, max_length=64)
    # Sell tickets only — buy the shares back once the sell fills.
    reinvest: Optional[ReinvestIn] = None
    # Sell or cover — reverse the same name, or buy a different one, after fill.
    followon: Optional[FollowOnIn] = None
    # Buy tickets only — hunt a cheaper re-entry after the stop fills.
    dip_hunt: Optional[DipHuntIn] = None

    @model_validator(mode="after")
    def one_trail_dimension(self) -> "ManualOrderIn":
        if self.trail_percent is not None and self.trail_price is not None:
            raise ValueError(
                "Give a trailing stop either a trail percent or a trail amount, "
                "not both"
            )
        return self


class ManageStopIn(BaseModel):
    """Move the protection on an open position without closing it."""

    symbol: str = Field(..., min_length=1, max_length=12)
    action: str = Field(..., pattern="(?i)^(breakeven|price|trail)$")
    stop_price: Optional[float] = Field(None, gt=0)
    stop_pct: Optional[float] = Field(None, gt=0, le=50)
    trail_percent: Optional[float] = Field(None, gt=0, le=50)


class CancelOrderIn(BaseModel):
    """One resting order by id, or every open order on a symbol."""

    order_id: Optional[str] = None
    symbol: Optional[str] = None


class ReplaceOrderIn(BaseModel):
    """Patch a resting limit/stop. Omit fields that should stay as they are."""

    order_id: str = Field(..., min_length=1)
    qty: Optional[int] = Field(None, gt=0)
    limit_price: Optional[float] = Field(None, gt=0)
    stop_price: Optional[float] = Field(None, gt=0)
    time_in_force: Optional[str] = Field(
        None, pattern="(?i)^(day|gtc|ioc|fok|opg|cls)$"
    )
    # Alpaca calls this replacement field `trail`; it keeps the order's
    # existing percent-vs-dollar trail dimension.
    trail: Optional[float] = Field(None, gt=0)

    @model_validator(mode="after")
    def at_least_one_change(self) -> "ReplaceOrderIn":
        if (
            self.qty is None
            and self.limit_price is None
            and self.stop_price is None
            and not self.time_in_force
            and self.trail is None
        ):
            raise ValueError(
                "Give a quantity, price, trail, or time-in-force to replace"
            )
        return self


class BacktestIn(BaseModel):
    mode: str = Field("sma", pattern="(?i)^(sma|dip|pair|ls)$")
    symbol: str = "AAPL"
    symbols: Optional[str] = None
    run_kind: str = Field(
        "per_symbol", pattern="(?i)^(per_symbol|portfolio|per-symbol)$"
    )
    days: int = Field(365, ge=30, le=1500)
    bar_timeframe: str = "1Day"
    qty: Optional[float] = Field(None, gt=0)
    initial_cash: float = Field(10_000.0, ge=100)
    sma_preset: Optional[str] = None
    fast_sma: Optional[int] = Field(None, ge=2)
    slow_sma: Optional[int] = Field(None, ge=3)
    dip_preset: str = "deep"
    dip_rsi_buy: Optional[float] = Field(None, gt=0, lt=100)
    dip_rsi_sell: Optional[float] = Field(None, gt=0, lt=100)
    dip_skip_bearish: Optional[bool] = None
    stop_loss_pct: Optional[float] = Field(None, ge=0.0, le=50.0)
    pair_preset: Optional[str] = None
    pair_sma_period: Optional[int] = Field(None, ge=2)
    pair_lookback: Optional[int] = Field(None, ge=1)
    pair_impulse_pct: Optional[float] = Field(None, gt=0, lt=100)
    pair_weak_side: Optional[str] = None
    pair_long_symbol: Optional[str] = None
    pair_short_symbol: Optional[str] = None
    slip_bps: Optional[float] = Field(None, ge=0, le=100)
    ls_ema_fast: Optional[int] = Field(None, ge=2)
    ls_ema_slow: Optional[int] = Field(None, ge=3)
    ls_adx_min: Optional[float] = Field(None, ge=0)
    ls_atr_stop_mult: Optional[float] = Field(None, gt=0)
    ls_risk_pct: Optional[float] = Field(None, gt=0)
    ls_rr: Optional[float] = Field(None, gt=0)
    ls_time_stop_bars: Optional[int] = Field(None, ge=1)
    ls_commission_pct: Optional[float] = Field(None, ge=0)
    ls_slippage_pct: Optional[float] = Field(None, ge=0)


class ClosePositionIn(BaseModel):
    qty: Optional[float] = Field(None, gt=0)
    percentage: Optional[float] = Field(None, gt=0, le=100)
    # None keeps the client default: cancel resting orders on a full close,
    # leave protection in place on a partial one.
    cancel_orders: Optional[bool] = None

    @model_validator(mode="after")
    def qty_xor_percentage(self) -> "ClosePositionIn":
        if self.qty is not None and self.percentage is not None:
            raise ValueError("Provide qty or percentage, not both")
        return self


class CloseBatchPositionsIn(BaseModel):
    symbols: list[str]
    cancel_orders: bool = True


class CloseAllPositionsIn(BaseModel):
    cancel_orders: bool = True


PAGE_FILES = {
    "auto-trade": "auto-trade.html",
    "backtest": "backtest.html",
    "backtest-history": "backtest-history.html",
    "backtest-compare": "backtest-compare.html",
    "manual-order": "manual-order.html",
    "positions": "positions.html",
    "orders": "orders.html",
    "history": "history.html",
    "configuration": "configuration.html",
}


def _page_response(name: str) -> FileResponse:
    filename = PAGE_FILES.get(name)
    if not filename:
        raise HTTPException(status_code=404, detail="Page not found")
    path = WEB_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(path)


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/auto-trade", status_code=302)


@app.get("/auto-trade")
def page_auto_trade() -> FileResponse:
    return _page_response("auto-trade")


@app.get("/backtest")
def page_backtest() -> FileResponse:
    return _page_response("backtest")


@app.get("/backtest/history")
def page_backtest_history() -> FileResponse:
    return _page_response("backtest-history")


@app.get("/backtest/compare")
def page_backtest_compare() -> FileResponse:
    return _page_response("backtest-compare")


@app.get("/manual-order")
def page_manual_order() -> FileResponse:
    return _page_response("manual-order")


@app.get("/positions")
def page_positions() -> FileResponse:
    return _page_response("positions")


@app.get("/orders")
def page_orders() -> FileResponse:
    return _page_response("orders")


@app.get("/history")
def page_history() -> FileResponse:
    return _page_response("history")


@app.get("/configuration")
def page_configuration() -> FileResponse:
    return _page_response("configuration")


@app.get("/api/positions")
def api_positions() -> dict:
    try:
        data = STATE.positions_overview()
        return {"ok": True, **data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/positions/{symbol}/lots")
def api_position_lots(
    symbol: str, lookback_days: int = Query(365, ge=1, le=1825)
) -> dict:
    try:
        data = STATE.position_lots(symbol, lookback_days=lookback_days)
        return {"ok": True, **data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/positions/{symbol}/close")
def api_close_position(symbol: str, body: Optional[ClosePositionIn] = None) -> dict:
    try:
        qty = body.qty if body else None
        pct = body.percentage if body else None
        cancel = body.cancel_orders if body else None
        result = STATE.close_single_position(
            symbol=symbol, qty=qty, percentage=pct, cancel_orders=cancel
        )
        return {"ok": True, "result": result, "overview": STATE.positions_overview()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/positions/close-batch")
def api_close_batch_positions(body: CloseBatchPositionsIn) -> dict:
    try:
        result = STATE.close_batch_positions(
            symbols=body.symbols, cancel_orders=body.cancel_orders
        )
        return {"ok": True, "result": result, "overview": STATE.positions_overview()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/positions/close-all")
def api_close_all_positions(body: Optional[CloseAllPositionsIn] = None) -> dict:
    try:
        cancel = body.cancel_orders if body else True
        result = STATE.close_all_positions(cancel_orders=cancel)
        return {"ok": True, "result": result, "overview": STATE.positions_overview()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/status")
def status() -> dict:
    try:
        STATE.refresh_quote(force=False)
    except Exception:
        pass
    return STATE.snapshot()


@app.get("/api/quote")
def api_quote() -> dict:
    try:
        data = STATE.refresh_quote(force=True)
        if data is None:
            raise HTTPException(status_code=502, detail=STATE.error or "No quote")
        return {"ok": True, "quote": data}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/quotes")
def quotes(symbols: str = "") -> dict:
    """Live marks for the watchlist.

    `symbols` is an optional comma-separated list; empty means the desk's own
    evaluate list. Marks are cached per symbol in the service layer, so polling
    this is cheap between refreshes.
    """
    wanted = [part.strip().upper() for part in symbols.split(",") if part.strip()]
    if len(wanted) > 50:
        raise HTTPException(status_code=400, detail="Too many symbols (max 50)")
    try:
        return {"ok": True, "quotes": STATE.watch_quotes(wanted)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/manual-context")
def manual_context(symbol: str = "AAPL") -> dict:
    """Mark, session, position, and buying power for the Manual Order page."""
    try:
        return {"ok": True, **STATE.manual_ticket_context(symbol)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/settings")
def save_settings(body: SettingsIn) -> dict:
    try:
        STATE.update_settings(body.model_dump())
        return {
            "ok": True,
            "settings": STATE.snapshot()["settings"],
            "ai_key_status": STATE.snapshot()["ai_key_status"],
            "alpaca_key_status": STATE.snapshot()["alpaca_key_status"],
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/lang")
def save_lang(body: LangIn) -> dict:
    """Persist the desk language alone.

    The switcher sits on every page, but only Auto Trade has the settings form
    that POSTs /api/settings — without this the AI would keep writing in the
    previously saved language. Unknown codes fall back to English.
    """
    try:
        settings = STATE.update_settings({"lang": body.lang})
        return {"ok": True, "lang": settings.lang}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/keys")
def save_keys(body: ApiKeysIn) -> dict:
    try:
        if not body.openai_api_key.strip() and not body.gemini_api_key.strip():
            raise ValueError("Paste at least one API key to save.")
        status = STATE.apply_api_keys(
            openai_api_key=body.openai_api_key or None,
            gemini_api_key=body.gemini_api_key or None,
            save_to_env=body.save_to_env,
        )
        return {"ok": True, "ai_key_status": status, "state": STATE.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/alpaca-keys")
def save_alpaca_keys(body: AlpacaKeysIn) -> dict:
    try:
        status = STATE.apply_alpaca_keys(
            alpaca_api_key=body.alpaca_api_key or None,
            alpaca_secret_key=body.alpaca_secret_key or None,
            environment=body.environment,
            save_to_env=body.save_to_env,
        )
        ok = bool(status.get("set")) and not status.get("account_error")
        # Saving a non-active slot reports set=false for the active mode — still OK.
        if body.environment:
            env = str(body.environment).strip().lower()
            slot = status.get("paper_keys" if env == "paper" else "live_keys") or {}
            ok = bool(slot.get("set")) and not status.get("account_error")
        return {
            "ok": ok,
            "alpaca_key_status": status,
            "trading_mode": STATE.snapshot().get("trading_mode"),
            "state": STATE.snapshot(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/alpaca-keys/clear")
def clear_alpaca_keys(body: ClearAlpacaKeysIn = ClearAlpacaKeysIn()) -> dict:
    try:
        environment = body.environment or "all"
        status = STATE.clear_alpaca_keys(environment=environment)
        return {
            "ok": True,
            "alpaca_key_status": status,
            "state": STATE.snapshot(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/trading-mode")
def set_trading_mode(body: TradingModeIn) -> dict:
    try:
        result = STATE.set_trading_mode(body.mode, confirm=body.confirm)
        return {
            "ok": True,
            **result,
            "state": STATE.snapshot(),
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/trading-mode/authorize")
def authorize_live_session(body: LiveAuthorizeIn) -> dict:
    try:
        result = STATE.authorize_live_session(confirm=body.confirm)
        return {
            "ok": True,
            **result,
            "state": STATE.snapshot(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/keys/clear")
def clear_keys(body: ClearAiKeysIn) -> dict:
    try:
        status = STATE.clear_api_keys(
            openai=body.openai,
            gemini=body.gemini,
            clear_env=body.clear_env,
        )
        return {"ok": True, "ai_key_status": status, "state": STATE.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/trades")
def trades(
    range: str = "month",
    symbol: str = "",
    side: str = "",
    limit: int = 100,
    start: str = "",
    end: str = "",
) -> dict:
    try:
        data = STATE.trade_history(
            range_key=range,
            symbol=symbol or None,
            side=side or None,
            limit=limit,
            start=start or None,
            end=end or None,
        )
        return {"ok": True, **data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class HistoryInsightsIn(BaseModel):
    """A History range to review, and how deep a read to take of it.

    No symbol/side here on purpose: the review is a read of the whole range,
    not of the filtered fill list. See ``WebState.history_insights``.
    """

    range: str = "month"
    start: str = ""
    end: str = ""
    # The language on screen right now. The saved desk language is only a
    # fallback: the switcher lives in the browser and does not reach the server
    # until someone actually changes it, so a page loaded straight into Bangla
    # would otherwise be narrated in English.
    lang: str = ""
    scope: str = Field("debrief", pattern="(?i)^(debrief|postmortem|audit)$")
    # False returns the deterministic fact sheet only — no model call, no cost.
    narrate: bool = True


class HistoryQueryIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=400)
    symbols: list[str] = Field(default_factory=list, max_length=200)
    lang: str = ""


class LessonIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=280)
    text_en: str = ""
    scope: str = Field("global", pattern="(?i)^(global|symbol|preset)$")
    target: str = ""
    lang: str = "en"
    source_range: str = ""


class LessonToggleIn(BaseModel):
    enabled: bool = True


@app.post("/api/history/insights")
def history_insights(body: HistoryInsightsIn) -> dict:
    try:
        data = STATE.history_insights(
            range_key=body.range,
            start=body.start or None,
            end=body.end or None,
            lang=body.lang or None,
            scope=body.scope.lower(),
            narrate_range=body.narrate,
        )
        return {"ok": True, **data}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.post("/api/history/query")
def history_query(body: HistoryQueryIn) -> dict:
    try:
        return {
            "ok": True,
            "filter": STATE.history_query(
                body.text, symbols=body.symbols, lang=body.lang or None
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/history/lessons")
def list_lessons() -> dict:
    return {"ok": True, "lessons": STATE.list_lessons()}


@app.post("/api/history/lessons")
def save_lesson(body: LessonIn) -> dict:
    try:
        lesson = STATE.save_lesson(body.model_dump())
        return {"ok": True, "lesson": lesson, "lessons": STATE.list_lessons()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/history/lessons/{lesson_id}")
def toggle_lesson(lesson_id: int, body: LessonToggleIn) -> dict:
    try:
        STATE.set_lesson_enabled(lesson_id, body.enabled)
        return {"ok": True, "lessons": STATE.list_lessons()}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/history/lessons/{lesson_id}")
def delete_lesson(lesson_id: int) -> dict:
    if not STATE.delete_lesson(lesson_id):
        raise HTTPException(status_code=404, detail=f"no lesson with id {lesson_id}")
    return {"ok": True, "lessons": STATE.list_lessons()}


@app.post("/api/account")
def account() -> dict:
    try:
        return {"ok": True, "account": STATE.refresh_account()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/run-once")
def run_once(body: SettingsIn) -> dict:
    try:
        payload = body.model_dump()
        STATE.update_settings(payload)
        result = STATE.run_once()
        return {"ok": True, "result": result, "state": STATE.snapshot()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class BacktestCompareIn(BaseModel):
    ids: list[int] = Field(..., min_length=2, max_length=4)


@app.post("/api/backtest")
def backtest(body: BacktestIn) -> dict:
    try:
        result = STATE.run_strategy_backtest(**body.model_dump())
        entry = STATE.save_backtest_result(result)
        return {
            "ok": True,
            "result": result,
            "history_id": entry.get("id"),
            "history": STATE.list_backtest_history(),
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/backtest/history")
def backtest_history() -> dict:
    return {"ok": True, "history": STATE.list_backtest_history()}


@app.get("/api/backtest/history/{entry_id}")
def backtest_history_entry(entry_id: int) -> dict:
    entry = STATE.get_backtest_history_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return {"ok": True, "entry": entry}


@app.delete("/api/backtest/history/{entry_id}")
def delete_backtest_history_entry(entry_id: int) -> dict:
    if not STATE.delete_backtest_history_entry(entry_id):
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return {"ok": True, "history": STATE.list_backtest_history()}


@app.delete("/api/backtest/history")
def clear_backtest_history() -> dict:
    STATE.clear_backtest_history()
    return {"ok": True, "history": []}


@app.post("/api/backtest/compare")
def compare_backtests(body: BacktestCompareIn) -> dict:
    try:
        entries = STATE.compare_backtests(body.ids)
        return {
            "ok": True,
            "runs": [
                {
                    "id": e.get("id"),
                    "created_at": e.get("created_at"),
                    "summary": summarize_entry(e),
                    "result": e.get("result"),
                }
                for e in entries
            ],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/order")
def place_order(body: ManualOrderIn) -> dict:
    try:
        result = STATE.place_manual_order(
            symbol=body.symbol,
            side=body.side,
            order_type=body.order_type,
            qty=body.qty,
            size_mode=body.size_mode,
            notional=body.notional,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
            trail_percent=body.trail_percent,
            trail_price=body.trail_price,
            time_in_force=body.time_in_force,
            extended_hours=body.extended_hours,
            stop_loss_pct=body.stop_loss_pct,
            ai_risk_pct=body.ai_risk_pct,
            ai_atr_stop_mult=body.ai_atr_stop_mult,
            take_profit_r=body.take_profit_r,
            stop_limit_offset_pct=body.stop_limit_offset_pct,
            stop_limit_price=body.stop_limit_price,
            preview=body.preview,
            confirm_adjusted_qty=body.confirm_adjusted_qty,
            override_breaches=body.override_breaches,
            ticket_id=body.ticket_id,
            reinvest=body.reinvest.model_dump() if body.reinvest else None,
            followon=body.followon.model_dump() if body.followon else None,
            dip_hunt=body.dip_hunt.model_dump() if body.dip_hunt else None,
        )
        return {"ok": True, "result": result, "state": STATE.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # A broker rejection is the user's problem to fix, so it has to arrive
        # as a sentence rather than Alpaca's raw JSON body.
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.post("/api/position/stop")
def manage_position_stop(body: ManageStopIn) -> dict:
    """Move a position's protective stop — breakeven, a price, or a trail."""
    try:
        result = STATE.manage_position_stop(
            symbol=body.symbol,
            action=body.action,
            stop_price=body.stop_price,
            stop_pct=body.stop_pct,
            trail_percent=body.trail_percent,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.get("/api/order/status")
def order_status(order_id: str = Query(..., min_length=1)) -> dict:
    """Poll one submitted ticket — accepted, filled, rejected, or resting."""
    try:
        return {"ok": True, "order": STATE.manual_order_status(order_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.get("/api/orders")
def list_orders(
    status: str = Query("open", pattern="(?i)^(open|closed)$"),
    symbol: str = "",
    side: str = "",
    limit: int = Query(200, ge=1, le=500),
    after: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
    until: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
) -> dict:
    """Account-wide working or recently closed orders for the blotter.

    Includes desk queues (buy-backs, next tickets, dip hunts) so the page
    can show plans that are not yet resting at the broker. ``after``/``until``
    bound the closed window; the KPI counts stay account-wide regardless.
    """
    try:
        data = STATE.list_orders(
            status=status,
            symbol=symbol,
            side=side,
            limit=limit,
            after=after,
            until=until,
        )
        return {"ok": True, **data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.post("/api/order/cancel")
def cancel_order(body: CancelOrderIn) -> dict:
    try:
        result = STATE.cancel_manual_order(
            order_id=body.order_id or "", symbol=body.symbol or ""
        )
        return {"ok": True, **result, "state": STATE.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.post("/api/orders/cancel-all")
def cancel_all_orders() -> dict:
    try:
        result = STATE.cancel_all_open_orders()
        return {"ok": True, **result, "state": STATE.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.post("/api/order/replace")
def replace_order(body: ReplaceOrderIn) -> dict:
    try:
        result = STATE.replace_manual_order(
            order_id=body.order_id,
            qty=body.qty,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
            time_in_force=body.time_in_force,
            trail=body.trail,
        )
        return {"ok": True, **result, "state": STATE.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.get("/api/reinvest")
def reinvest_plans(symbol: str = "") -> dict:
    """Armed and settled buy-backs, newest first."""
    return {"ok": True, "plans": STATE.reinvest_plans_payload(symbol)}


@app.post("/api/reinvest/cancel")
def reinvest_cancel(body: CancelReinvestIn) -> dict:
    """Disarm a waiting buy-back. The sell order itself is left alone."""
    try:
        plan = STATE.cancel_reinvest_plan(body.plan_id)
        return {"ok": True, "plan": plan, "plans": STATE.reinvest_plans_payload()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/followon")
def followon_plans(symbol: str = "") -> dict:
    """Armed and settled next tickets, newest first."""
    return {"ok": True, "plans": STATE.followon_plans_payload(symbol)}


@app.post("/api/followon/cancel")
def followon_cancel(body: CancelReinvestIn) -> dict:
    """Disarm a waiting next ticket. The close order itself is left alone."""
    try:
        plan = STATE.cancel_followon_plan(body.plan_id)
        return {"ok": True, "plan": plan, "plans": STATE.followon_plans_payload()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/dip-hunt")
def dip_hunt_plans(symbol: str = "") -> dict:
    """Armed and settled dip hunts, newest first."""
    return {"ok": True, "plans": STATE.dip_hunt_plans_payload(symbol)}


@app.post("/api/dip-hunt/cancel")
def dip_hunt_cancel(body: CancelReinvestIn) -> dict:
    """Disarm a live dip hunt. A parked cheaper-buy is cancelled; the stop is not."""
    try:
        plan = STATE.cancel_dip_hunt_plan(body.plan_id)
        return {"ok": True, "plan": plan, "plans": STATE.dip_hunt_plans_payload()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/loop/start")
def loop_start(body: SettingsIn) -> dict:
    try:
        payload = body.model_dump()
        STATE.update_settings(payload)
        STATE.start_loop()
        return {"ok": True, "state": STATE.snapshot()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/loop/stop")
def loop_stop() -> dict:
    STATE.stop_loop()
    return {"ok": True, "state": STATE.snapshot()}


@app.get("/api/loop/state")
def loop_state() -> dict:
    """Cheap poll target while Stop drains the current cycle."""
    return STATE.loop_state()


@app.post("/api/history/clear")
def clear_history() -> dict:
    STATE.clear_history()
    return {"ok": True, "state": STATE.snapshot()}


def main() -> None:
    import uvicorn

    host = os.getenv("ALGOPACA_HOST", "127.0.0.1")
    port = int(os.getenv("ALGOPACA_PORT", "8765"))
    uvicorn.run("bot.webapp:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
