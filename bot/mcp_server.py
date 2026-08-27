"""AlgoPaca's Model Context Protocol (MCP) server.

Exposes AlgoPaca's Alpaca-backed engines — SMA, buy-the-dip, long/short pair
rotation, regime dual-momentum, and the multi-modal AI quant desk, plus the
shared mechanical risk engine, options overlay, and walk-forward backtester —
as MCP tools. This lets an AI assistant such as Claude or Cursor drive real
Alpaca orders (paper by default) through structured tool calls instead of the
web desk, on top of the same ``alpaca-py`` Trading API client AlgoPaca's CLI
and web app already use.

Credentials, strategy mode, and the paper/live killswitch all come from this
process's ``.env`` (see ``bot/config.py``) — every tool call reuses the same
guardrails (ATR risk sizing, drawdown circuit breaker, cooldowns) as running
AlgoPaca headless or through the desk.

Run with stdio transport, the mode Claude Desktop / Cursor / the Alpaca CLI
expect for a local MCP server::

    python -m bot.mcp_server
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from alpaca.trading.enums import OrderSide
from mcp.server.mcpserver import MCPServer

from bot import ai_presets, dip_presets, pair_presets, sma_presets
from bot.ai_trader import AiTradingBot
from bot.backtest import build_strategy, run_backtest as _run_backtest, summary_from_result
from bot.client import AlpacaService
from bot.config import Config
from bot.ls_trader import LsTradingBot
from bot.pair_trader import PairTradingBot
from bot.trader import TradingBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("algopaca-mcp")

server = MCPServer(
    "algopaca",
    version="1.0.0",
    instructions=(
        "AlgoPaca is an autonomous algorithmic trading desk built on Alpaca's "
        "Trading API. Use get_account / get_positions / get_open_orders to "
        "read portfolio state, list_strategy_presets to see named parameter "
        "presets, run_strategy_cycle to evaluate and (if a signal clears "
        "every risk gate) execute one of AlgoPaca's five strategy engines "
        "with its options overlay, place_manual_order / close_position for "
        "direct control, and run_backtest to validate a strategy on history "
        "before trading it live. Trading defaults to Alpaca paper accounts; "
        "live trading only runs if this server's .env has ALPACA_PAPER=false "
        "and ALPACA_ALLOW_LIVE=true."
    ),
)

_PRESET_LISTERS = {
    "sma": sma_presets.list_presets,
    "dip": dip_presets.list_presets,
    "pair": pair_presets.list_presets,
    "ai": ai_presets.list_presets,
}


def _config() -> Config:
    return Config.from_env()


def _bot_for(config: Config) -> TradingBot | AiTradingBot | PairTradingBot | LsTradingBot:
    if config.strategy_mode == "ai":
        return AiTradingBot(config)
    if config.strategy_mode == "pair":
        return PairTradingBot(config)
    if config.strategy_mode == "ls":
        return LsTradingBot(config)
    return TradingBot(config)


@server.tool()
def get_account() -> dict[str, Any]:
    """Alpaca account summary: equity, cash, buying power, paper/live status, day P&L."""
    return AlpacaService(_config()).account_summary()


@server.tool()
def get_positions() -> list[dict[str, Any]]:
    """All open Alpaca positions, stocks/ETFs and options alike."""
    return AlpacaService(_config()).get_all_positions()


@server.tool()
def get_open_orders() -> dict[str, list[dict[str, Any]]]:
    """Open Alpaca orders grouped by symbol, including protective-stop metadata."""
    return AlpacaService(_config()).get_open_orders_summary()


@server.tool()
def list_strategy_presets(mode: Literal["sma", "dip", "pair", "ai"]) -> list[dict[str, Any]]:
    """Named parameter presets for a strategy engine (e.g. sma -> golden_cross)."""
    return _PRESET_LISTERS[mode]()


@server.tool()
def run_strategy_cycle(
    mode: Literal["sma", "dip", "pair", "ls", "ai"],
    symbols: str | None = None,
    preset: str | None = None,
    ai_provider: Literal["openai", "gemini", "anthropic", "xai"] | None = None,
) -> dict[str, Any]:
    """Run one live evaluation cycle of an AlgoPaca strategy engine.

    Reads current bars and indicators — plus news, earnings, and macro
    context for AI mode — applies AlgoPaca's mechanical ATR risk gates
    (position sizing, max positions, daily loss circuit breaker, cooldowns),
    and submits a real Alpaca order and its options overlay when a signal
    clears every guardrail. Every symbol gets a decision with a `reason`,
    even when it results in no trade.

    `symbols` overrides the configured watchlist (comma-separated, e.g.
    "AAPL,MSFT"). `preset` selects a named preset for the engine — see
    `list_strategy_presets`. `ai_provider` only applies when mode="ai".
    """
    config = _config().override(
        strategy_mode=mode,
        symbols=symbols,
        sma_preset=preset if mode == "sma" else None,
        dip_preset=preset if mode == "dip" else None,
        pair_preset=preset if mode == "pair" else None,
        ai_preset=preset if mode == "ai" else None,
        ai_provider=ai_provider,
    )
    return _bot_for(config).run_once()


@server.tool()
def place_manual_order(
    symbol: str,
    side: Literal["buy", "sell"],
    qty: float,
    order_type: Literal["market", "limit", "stop", "stop_limit", "trailing_stop"] = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    stop_loss_pct: float | None = None,
    take_profit_price: float | None = None,
    time_in_force: Literal["day", "gtc", "ioc", "fok"] = "day",
) -> dict[str, Any]:
    """Submit a user-directed Alpaca order, bypassing AlgoPaca's strategy engines.

    Still goes through AlgoPaca's own order-submission path, so a
    `stop_loss_pct` and/or `take_profit_price` attach as bracket/OTO
    protection rather than needing separate orders. Paper by default; the
    server's .env controls whether this reaches a live account.
    """
    service = AlpacaService(_config())
    order, attached = service.submit_manual_order(
        symbol,
        qty,
        OrderSide.BUY if side == "buy" else OrderSide.SELL,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        stop_loss_pct=stop_loss_pct,
        take_profit_price=take_profit_price,
        time_in_force=time_in_force,
    )
    return {
        "order": service.serialize_blotter_order(order),
        "attached_exit": attached,
    }


@server.tool()
def close_position(
    symbol: str,
    qty: float | None = None,
    percentage: float | None = None,
) -> dict[str, Any]:
    """Liquidate an open position — fully, or partially by qty or percentage (not both)."""
    return AlpacaService(_config()).close_position(symbol, qty=qty, percentage=percentage)


@server.tool()
def run_backtest(
    mode: Literal["sma", "dip"],
    symbol: str,
    days: int = 365,
    bar_timeframe: str = "1Day",
    fast_sma: int | None = None,
    slow_sma: int | None = None,
    dip_rsi_buy: float | None = None,
    dip_rsi_sell: float | None = None,
    initial_cash: float = 10_000.0,
) -> dict[str, Any]:
    """Walk-forward backtest an SMA or dip strategy on historical bars. Places no orders.

    Returns the same comparison-row metrics as the web desk's Backtest page:
    total return, buy & hold return, alpha, max drawdown, win rate, and
    round-trip count.
    """
    config = _config()
    service = AlpacaService(config)
    bars = service.get_bars(symbol.upper(), max(120, int(days)), timeframe=bar_timeframe)
    strategy = build_strategy(
        mode,
        fast_sma=fast_sma or config.fast_sma,
        slow_sma=slow_sma or config.slow_sma,
        dip_rsi_buy=dip_rsi_buy or config.dip_rsi_buy,
        dip_rsi_sell=dip_rsi_sell or config.dip_rsi_sell,
    )
    result = _run_backtest(bars, strategy, initial_cash=initial_cash)
    result["symbol"] = symbol.upper()
    return summary_from_result(result)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
