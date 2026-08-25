"""FastAPI web application for AlgoPaca."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from bot.alpaca_errors import humanize_alpaca_error
from bot.auth import AUTH_STORE
from bot.backtest_store import summarize_entry
from bot.email_service import send_password_reset_email
from bot.web_state import AppState, get_user_state

log = logging.getLogger("algopaca-web")

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


class SignupIn(BaseModel):
    username: str
    email: str
    password: str
    display_name: Optional[str] = None


class LoginIn(BaseModel):
    identifier: str
    password: str
    remember_me: bool = False


class ForgotPasswordIn(BaseModel):
    identifier: str


class ResetPasswordIn(BaseModel):
    token: str
    password: str


SESSION_COOKIE_NAME = "algopaca_session"
SESSION_DAYS_DEFAULT = 7
SESSION_DAYS_REMEMBERED = 30

# Set ALGOPACA_COOKIE_SECURE=1 when serving over HTTPS so the session cookie is
# never sent in the clear. Off by default for local http:// development.
COOKIE_SECURE = os.getenv("ALGOPACA_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}

# Sign-in throttle: after this many consecutive failures for the same
# (client, identifier) pair, further attempts are refused until the window ends.
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_SECONDS = 300
_login_attempts: dict[tuple[str, str], tuple[int, float]] = {}


def _client_key(request: Request) -> str:
    return (request.client.host if request.client else "") or "unknown"


def _login_retry_after(request: Request, identifier: str) -> int:
    """Seconds the caller must wait, or 0 when the attempt is allowed."""
    key = (_client_key(request), (identifier or "").strip().lower())
    record = _login_attempts.get(key)
    if not record:
        return 0
    count, first_failure_at = record
    elapsed = time.monotonic() - first_failure_at
    if elapsed >= LOGIN_LOCKOUT_SECONDS:
        _login_attempts.pop(key, None)
        return 0
    if count < LOGIN_MAX_ATTEMPTS:
        return 0
    return max(1, int(LOGIN_LOCKOUT_SECONDS - elapsed))


def _record_login_failure(request: Request, identifier: str) -> None:
    key = (_client_key(request), (identifier or "").strip().lower())
    now = time.monotonic()
    count, first_failure_at = _login_attempts.get(key, (0, now))
    if now - first_failure_at >= LOGIN_LOCKOUT_SECONDS:
        count, first_failure_at = 0, now
    _login_attempts[key] = (count + 1, first_failure_at)

    # Opportunistic cleanup so the map cannot grow without bound.
    if len(_login_attempts) > 1024:
        stale = [k for k, (_, started) in _login_attempts.items() if now - started >= LOGIN_LOCKOUT_SECONDS]
        for k in stale:
            _login_attempts.pop(k, None)


def _clear_login_failures(request: Request, identifier: str) -> None:
    _login_attempts.pop((_client_key(request), (identifier or "").strip().lower()), None)


def _set_session_cookie(response: Response, token: str, days: int) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=days * 86400,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _get_session_token(request: Request) -> Optional[str]:
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return None


def _get_current_user(request: Request) -> Optional[dict[str, Any]]:
    token = _get_session_token(request)
    if not token:
        return None
    return AUTH_STORE.get_user_by_session(token)


def require_auth(request: Request) -> dict[str, Any]:
    """Dependency: require an authenticated user session for API endpoints."""
    user = _get_current_user(request)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please sign in to access your portfolio.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


PAGE_FILES = {
    "login": "login.html",
    "signup": "signup.html",
    "reset-password": "reset-password.html",
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


def _protected_page(request: Request, name: str) -> Response:
    user = _get_current_user(request)
    if not user:
        from urllib.parse import quote
        next_path = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(url=f"/login?next={next_path}", status_code=302)
    return _page_response(name)


@app.get("/")
def index(request: Request) -> RedirectResponse:
    user = _get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return RedirectResponse(url="/auto-trade", status_code=302)


@app.get("/login")
def page_login(request: Request) -> Response:
    user = _get_current_user(request)
    if user:
        next_url = request.query_params.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//") and not next_url.startswith("/login"):
            return RedirectResponse(url=next_url, status_code=302)
        return RedirectResponse(url="/auto-trade", status_code=302)
    return _page_response("login")


@app.get("/signup")
def page_signup(request: Request) -> Response:
    user = _get_current_user(request)
    if user:
        next_url = request.query_params.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//") and not next_url.startswith("/signup"):
            return RedirectResponse(url=next_url, status_code=302)
        return RedirectResponse(url="/auto-trade", status_code=302)
    return _page_response("signup")


@app.get("/reset-password")
def page_reset_password(request: Request) -> Response:
    return _page_response("reset-password")


@app.get("/auto-trade")
def page_auto_trade(request: Request) -> Response:
    return _protected_page(request, "auto-trade")


@app.get("/backtest")
def page_backtest(request: Request) -> Response:
    return _protected_page(request, "backtest")


@app.get("/backtest/history")
def page_backtest_history(request: Request) -> Response:
    return _protected_page(request, "backtest-history")


@app.get("/backtest/compare")
def page_backtest_compare(request: Request) -> Response:
    return _protected_page(request, "backtest-compare")


@app.get("/manual-order")
@app.get("/advanced-order")
def page_manual_order(request: Request) -> Response:
    return _protected_page(request, "manual-order")


@app.get("/positions")
def page_positions(request: Request) -> Response:
    return _protected_page(request, "positions")


@app.get("/orders")
def page_orders(request: Request) -> Response:
    return _protected_page(request, "orders")


@app.get("/history")
def page_history(request: Request) -> Response:
    return _protected_page(request, "history")


@app.get("/configuration")
def page_configuration(request: Request) -> Response:
    return _protected_page(request, "configuration")


@app.post("/api/auth/signup")
def api_auth_signup(body: SignupIn, request: Request, response: Response) -> dict:
    try:
        user = AUTH_STORE.register_user(
            username=body.username,
            email=body.email,
            password=body.password,
            display_name=body.display_name,
        )
        user_agent = request.headers.get("user-agent", "")
        token, _ = AUTH_STORE.create_session(user["id"], remember_me=True, user_agent=user_agent)
        _set_session_cookie(response, token, SESSION_DAYS_REMEMBERED)
        return {"ok": True, "user": user, "token": token}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Registration failed. Please try again.") from exc


@app.post("/api/auth/login")
def api_auth_login(body: LoginIn, request: Request, response: Response) -> dict:
    retry_after = _login_retry_after(request, body.identifier)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many sign-in attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        user = AUTH_STORE.authenticate_user(
            identifier=body.identifier,
            password=body.password,
        )
    except ValueError as exc:
        _record_login_failure(request, body.identifier)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Login failed. Please try again.") from exc

    try:
        _clear_login_failures(request, body.identifier)
        user_agent = request.headers.get("user-agent", "")
        token, _ = AUTH_STORE.create_session(
            user["id"],
            remember_me=body.remember_me,
            user_agent=user_agent,
        )
        days = SESSION_DAYS_REMEMBERED if body.remember_me else SESSION_DAYS_DEFAULT
        _set_session_cookie(response, token, days)
        return {"ok": True, "user": user, "token": token}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Login failed. Please try again.") from exc


@app.post("/api/auth/forgot-password")
def api_auth_forgot_password(body: ForgotPasswordIn, request: Request) -> dict:
    identifier = (body.identifier or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Username or email is required.")

    reset_data = AUTH_STORE.create_password_reset_token(identifier)
    if reset_data:
        base_url = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            base_url = str(request.base_url).rstrip("/")
        reset_url = f"{base_url}/reset-password?token={reset_data['token']}"
        lang = request.cookies.get("algopaca_lang", "en")
        try:
            send_password_reset_email(
                to_email=reset_data["email"],
                username=reset_data["username"],
                reset_url=reset_url,
                lang=lang,
            )
        except Exception:
            # Never surface the delivery failure: a 500 here only happens for
            # identifiers that exist, which would turn this endpoint into an
            # account-enumeration oracle. Log it and fall through to the same
            # generic reply an unknown identifier gets.
            log.exception("Password reset email could not be sent (check SMTP_* settings)")

    return {
        "ok": True,
        "message": "If an account matches that username or email, instructions have been sent.",
    }


@app.post("/api/auth/reset-password")
def api_auth_reset_password(body: ResetPasswordIn) -> dict:
    try:
        user = AUTH_STORE.verify_and_use_reset_token(body.token, body.password)
        return {
            "ok": True,
            "message": "Password reset successful. You can now sign in with your new password.",
            "user": user,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to reset password. Please try again.") from exc


@app.post("/api/auth/demo")
def api_auth_demo(request: Request, response: Response) -> dict:
    raise HTTPException(
        status_code=400,
        detail="Shared demo mode is disabled. Please create an account or sign in to trade in your own private portfolio.",
    )


@app.post("/api/auth/logout")
def api_auth_logout(request: Request, response: Response) -> dict:
    token = _get_session_token(request)
    if token:
        AUTH_STORE.delete_session(token)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    return {"ok": True}


@app.get("/api/auth/me")
def api_auth_me(request: Request) -> dict:
    token = _get_session_token(request)
    user = AUTH_STORE.get_user_by_session(token) if token else None
    if user:
        return {"ok": True, "authenticated": True, "user": user}
    return {"ok": True, "authenticated": False, "user": None}


@app.get("/api/positions")
def api_positions(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        data = state.positions_overview()
        return {"ok": True, **data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/positions/{symbol}/lots")
def api_position_lots(
    symbol: str, lookback_days: int = Query(365, ge=1, le=1825), user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        data = state.position_lots(symbol, lookback_days=lookback_days)
        return {"ok": True, **data}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/positions/{symbol}/close")
def api_close_position(
    symbol: str, body: Optional[ClosePositionIn] = None, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        qty = body.qty if body else None
        pct = body.percentage if body else None
        cancel = body.cancel_orders if body else None
        result = state.close_single_position(
            symbol=symbol, qty=qty, percentage=pct, cancel_orders=cancel
        )
        return {"ok": True, "result": result, "overview": state.positions_overview()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/positions/close-batch")
def api_close_batch_positions(
    body: CloseBatchPositionsIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        result = state.close_batch_positions(
            symbols=body.symbols, cancel_orders=body.cancel_orders
        )
        return {"ok": True, "result": result, "overview": state.positions_overview()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/positions/close-all")
def api_close_all_positions(
    body: Optional[CloseAllPositionsIn] = None, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        cancel = body.cancel_orders if body else True
        result = state.close_all_positions(cancel_orders=cancel)
        return {"ok": True, "result": result, "overview": state.positions_overview()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/status")
def status(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        state.refresh_quote(force=False)
    except Exception:
        pass
    return state.snapshot()


@app.get("/api/quote")
def api_quote(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        data = state.refresh_quote(force=True)
        if data is None:
            raise HTTPException(status_code=502, detail=state.error or "No quote")
        return {"ok": True, "quote": data}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/quotes")
def quotes(symbols: str = "", user: dict = Depends(require_auth)) -> dict:
    """Live marks for the watchlist."""
    state = get_user_state(user["id"])
    wanted = [part.strip().upper() for part in symbols.split(",") if part.strip()]
    if len(wanted) > 50:
        raise HTTPException(status_code=400, detail="Too many symbols (max 50)")
    try:
        return {"ok": True, "quotes": state.watch_quotes(wanted)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/manual-context")
def manual_context(symbol: str = "AAPL", user: dict = Depends(require_auth)) -> dict:
    """Mark, session, position, and buying power for the Advanced Order page."""
    state = get_user_state(user["id"])
    try:
        return {"ok": True, **state.manual_ticket_context(symbol)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/settings")
def save_settings(body: SettingsIn, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        state.update_settings(body.model_dump())
        return {
            "ok": True,
            "settings": state.snapshot()["settings"],
            "ai_key_status": state.snapshot()["ai_key_status"],
            "alpaca_key_status": state.snapshot()["alpaca_key_status"],
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/lang")
def save_lang(body: LangIn, user: dict = Depends(require_auth)) -> dict:
    """Persist the desk language alone."""
    state = get_user_state(user["id"])
    try:
        settings = state.update_settings({"lang": body.lang})
        return {"ok": True, "lang": settings.lang}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/keys")
def save_keys(body: ApiKeysIn, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        if not body.openai_api_key.strip() and not body.gemini_api_key.strip():
            raise ValueError("Paste at least one API key to save.")
        status = state.apply_api_keys(
            openai_api_key=body.openai_api_key or None,
            gemini_api_key=body.gemini_api_key or None,
            save_to_env=body.save_to_env,
        )
        return {"ok": True, "ai_key_status": status, "state": state.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/alpaca-keys")
def save_alpaca_keys(body: AlpacaKeysIn, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        status = state.apply_alpaca_keys(
            alpaca_api_key=body.alpaca_api_key or None,
            alpaca_secret_key=body.alpaca_secret_key or None,
            environment=body.environment,
            save_to_env=body.save_to_env,
        )
        ok = bool(status.get("set")) and not status.get("account_error")
        if body.environment:
            env = str(body.environment).strip().lower()
            slot = status.get("paper_keys" if env == "paper" else "live_keys") or {}
            ok = bool(slot.get("set")) and not status.get("account_error")
        return {
            "ok": ok,
            "alpaca_key_status": status,
            "trading_mode": state.snapshot().get("trading_mode"),
            "state": state.snapshot(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/alpaca-keys/clear")
def clear_alpaca_keys(
    body: ClearAlpacaKeysIn = ClearAlpacaKeysIn(), user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        environment = body.environment or "all"
        status = state.clear_alpaca_keys(environment=environment)
        return {
            "ok": True,
            "alpaca_key_status": status,
            "state": state.snapshot(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/trading-mode")
def set_trading_mode(body: TradingModeIn, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        result = state.set_trading_mode(body.mode, confirm=body.confirm)
        return {
            "ok": True,
            **result,
            "state": state.snapshot(),
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/trading-mode/authorize")
def authorize_live_session(
    body: LiveAuthorizeIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        result = state.authorize_live_session(confirm=body.confirm)
        return {
            "ok": True,
            **result,
            "state": state.snapshot(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/keys/clear")
def clear_keys(body: ClearAiKeysIn, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        status = state.clear_api_keys(
            openai=body.openai,
            gemini=body.gemini,
            clear_env=body.clear_env,
        )
        return {"ok": True, "ai_key_status": status, "state": state.snapshot()}
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
    user: dict = Depends(require_auth),
) -> dict:
    state = get_user_state(user["id"])
    try:
        data = state.trade_history(
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
def history_insights(
    body: HistoryInsightsIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        data = state.history_insights(
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
def history_query(body: HistoryQueryIn, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        return {
            "ok": True,
            "filter": state.history_query(
                body.text, symbols=body.symbols, lang=body.lang or None
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/history/lessons")
def list_lessons(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    return {"ok": True, "lessons": state.list_lessons()}


@app.post("/api/history/lessons")
def save_lesson(body: LessonIn, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        lesson = state.save_lesson(body.model_dump())
        return {"ok": True, "lesson": lesson, "lessons": state.list_lessons()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/history/lessons/{lesson_id}")
def toggle_lesson(
    lesson_id: int, body: LessonToggleIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        state.set_lesson_enabled(lesson_id, body.enabled)
        return {"ok": True, "lessons": state.list_lessons()}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/history/lessons/{lesson_id}")
def delete_lesson(lesson_id: int, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    if not state.delete_lesson(lesson_id):
        raise HTTPException(status_code=404, detail=f"no lesson with id {lesson_id}")
    return {"ok": True, "lessons": state.list_lessons()}


@app.post("/api/account")
def account(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        return {"ok": True, "account": state.refresh_account()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/run-once")
def run_once(
    body: SettingsIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        payload = body.model_dump()
        state.update_settings(payload)
        result = state.run_once()
        return {"ok": True, "result": result, "state": state.snapshot()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class BacktestCompareIn(BaseModel):
    ids: list[int] = Field(..., min_length=2, max_length=4)


@app.post("/api/backtest")
def backtest(body: BacktestIn, user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        result = state.run_strategy_backtest(**body.model_dump())
        entry = state.save_backtest_result(result)
        return {
            "ok": True,
            "result": result,
            "history_id": entry.get("id"),
            "history": state.list_backtest_history(),
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/backtest/history")
def backtest_history(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    return {"ok": True, "history": state.list_backtest_history()}


@app.get("/api/backtest/history/{entry_id}")
def backtest_history_entry(
    entry_id: int, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    entry = state.get_backtest_history_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return {"ok": True, "entry": entry}


@app.delete("/api/backtest/history/{entry_id}")
def delete_backtest_history_entry(
    entry_id: int, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    if not state.delete_backtest_history_entry(entry_id):
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return {"ok": True, "history": state.list_backtest_history()}


@app.delete("/api/backtest/history")
def clear_backtest_history(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    state.clear_backtest_history()
    return {"ok": True, "history": []}


@app.post("/api/backtest/compare")
def compare_backtests(
    body: BacktestCompareIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        entries = state.compare_backtests(body.ids)
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
def place_order(
    body: ManualOrderIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        result = state.place_manual_order(
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
        return {"ok": True, "result": result, "state": state.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # A broker rejection is the user's problem to fix, so it has to arrive
        # as a sentence rather than Alpaca's raw JSON body.
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.post("/api/position/stop")
def manage_position_stop(
    body: ManageStopIn, user: dict = Depends(require_auth)
) -> dict:
    """Move a position's protective stop — breakeven, a price, or a trail."""
    state = get_user_state(user["id"])
    try:
        result = state.manage_position_stop(
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
def order_status(
    order_id: str = Query(..., min_length=1), user: dict = Depends(require_auth)
) -> dict:
    """Poll one submitted ticket — accepted, filled, rejected, or resting."""
    state = get_user_state(user["id"])
    try:
        return {"ok": True, "order": state.manual_order_status(order_id)}
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
    user: dict = Depends(require_auth),
) -> dict:
    """Account-wide working or recently closed orders for the blotter."""
    state = get_user_state(user["id"])
    try:
        data = state.list_orders(
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
def cancel_order(
    body: CancelOrderIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        result = state.cancel_manual_order(
            order_id=body.order_id or "", symbol=body.symbol or ""
        )
        return {"ok": True, **result, "state": state.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.post("/api/orders/cancel-all")
def cancel_all_orders(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    try:
        result = state.cancel_all_open_orders()
        return {"ok": True, **result, "state": state.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.post("/api/order/replace")
def replace_order(
    body: ReplaceOrderIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        result = state.replace_manual_order(
            order_id=body.order_id,
            qty=body.qty,
            limit_price=body.limit_price,
            stop_price=body.stop_price,
            time_in_force=body.time_in_force,
            trail=body.trail,
        )
        return {"ok": True, **result, "state": state.snapshot()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=humanize_alpaca_error(exc)
        ) from exc


@app.get("/api/reinvest")
def reinvest_plans(symbol: str = "", user: dict = Depends(require_auth)) -> dict:
    """Armed and settled buy-backs, newest first."""
    state = get_user_state(user["id"])
    return {"ok": True, "plans": state.reinvest_plans_payload(symbol)}


@app.post("/api/reinvest/cancel")
def reinvest_cancel(
    body: CancelReinvestIn, user: dict = Depends(require_auth)
) -> dict:
    """Disarm a waiting buy-back. The sell order itself is left alone."""
    state = get_user_state(user["id"])
    try:
        plan = state.cancel_reinvest_plan(body.plan_id)
        return {"ok": True, "plan": plan, "plans": state.reinvest_plans_payload()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/followon")
def followon_plans(symbol: str = "", user: dict = Depends(require_auth)) -> dict:
    """Armed and settled next tickets, newest first."""
    state = get_user_state(user["id"])
    return {"ok": True, "plans": state.followon_plans_payload(symbol)}


@app.post("/api/followon/cancel")
def followon_cancel(
    body: CancelReinvestIn, user: dict = Depends(require_auth)
) -> dict:
    """Disarm a waiting next ticket. The close order itself is left alone."""
    state = get_user_state(user["id"])
    try:
        plan = state.cancel_followon_plan(body.plan_id)
        return {"ok": True, "plan": plan, "plans": state.followon_plans_payload()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/dip-hunt")
def dip_hunt_plans(symbol: str = "", user: dict = Depends(require_auth)) -> dict:
    """Armed and settled dip hunts, newest first."""
    state = get_user_state(user["id"])
    return {"ok": True, "plans": state.dip_hunt_plans_payload(symbol)}


@app.post("/api/dip-hunt/cancel")
def dip_hunt_cancel(
    body: CancelReinvestIn, user: dict = Depends(require_auth)
) -> dict:
    """Disarm a live dip hunt. A parked cheaper-buy is cancelled; the stop is not."""
    state = get_user_state(user["id"])
    try:
        plan = state.cancel_dip_hunt_plan(body.plan_id)
        return {"ok": True, "plan": plan, "plans": state.dip_hunt_plans_payload()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/loop/start")
def loop_start(
    body: SettingsIn, user: dict = Depends(require_auth)
) -> dict:
    state = get_user_state(user["id"])
    try:
        payload = body.model_dump()
        state.update_settings(payload)
        state.start_loop()
        return {"ok": True, "state": state.snapshot()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/loop/stop")
def loop_stop(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    state.stop_loop()
    return {"ok": True, "state": state.snapshot()}


@app.get("/api/loop/state")
def loop_state(user: dict = Depends(require_auth)) -> dict:
    """Cheap poll target while Stop drains the current cycle."""
    state = get_user_state(user["id"])
    return state.loop_state()


@app.post("/api/history/clear")
def clear_history(user: dict = Depends(require_auth)) -> dict:
    state = get_user_state(user["id"])
    state.clear_history()
    return {"ok": True, "state": state.snapshot()}


def main() -> None:
    import uvicorn

    host = os.getenv("ALGOPACA_HOST", "127.0.0.1")
    port = int(os.getenv("ALGOPACA_PORT", "8765"))
    uvicorn.run("bot.webapp:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
