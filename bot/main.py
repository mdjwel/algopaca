#!/usr/bin/env python3
"""Run AlgoPaca (SMA, dip, pair, regime L/S, or AI)."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from bot.ai_trader import AiTradingBot
from bot.config import Config, live_allowed_from_env
from bot.ls_trader import LsTradingBot
from bot.pair_trader import PairTradingBot
from bot.trader import TradingBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("alpaca-bot")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AlgoPaca (SMA, dip, pair, regime L/S, or AI)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate and optionally trade once, then exit",
    )
    parser.add_argument(
        "--account",
        action="store_true",
        help="Print paper account summary and exit",
    )
    parser.add_argument(
        "--mode",
        choices=("sma", "dip", "ai", "pair", "ls"),
        default=None,
        help="Override STRATEGY_MODE from .env",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "gemini"),
        default=None,
        help="Override AI_PROVIDER from .env",
    )
    parser.add_argument(
        "--preset",
        choices=(
            "balanced",
            "conservative",
            "momentum",
            "mean_reversion",
            "news_aware",
            "custom",
        ),
        default=None,
        help="Override AI_PRESET from .env",
    )
    parser.add_argument(
        "--sma-preset",
        choices=(
            "classic",
            "short_term",
            "fibonacci",
            "swing",
            "golden_cross",
            "custom",
        ),
        default=None,
        help="Override SMA_PRESET from .env (sets FAST_SMA/SLOW_SMA)",
    )
    parser.add_argument(
        "--dip-preset",
        choices=("deep", "mild", "washout", "custom"),
        default=None,
        help="Override DIP_PRESET from .env (sets DIP_RSI_BUY/SELL)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = Config.from_env()
        if (
            args.mode
            or args.provider
            or args.preset
            or args.sma_preset
            or args.dip_preset
        ):
            config = config.override(
                strategy_mode=args.mode,
                ai_provider=args.provider,
                ai_preset=args.preset,
                sma_preset=args.sma_preset,
                dip_preset=args.dip_preset,
            )
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    if not config.paper:
        if not live_allowed_from_env():
            logger.error(
                "Refusing to start: live mode requires ALPACA_ALLOW_LIVE=true."
            )
            return 1
        logger.warning("LIVE TRADING MODE — real money.")

    if config.strategy_mode == "ai":
        if config.ai_provider == "openai" and not config.openai_api_key:
            logger.error("OPENAI_API_KEY is missing — required for AI mode with openai.")
            return 1
        if config.ai_provider == "gemini" and not config.gemini_api_key:
            logger.error("GEMINI_API_KEY is missing — required for AI mode with gemini.")
            return 1
        bot: TradingBot | AiTradingBot | PairTradingBot | LsTradingBot = AiTradingBot(config)
        service = bot.service
    elif config.strategy_mode == "pair":
        bot = PairTradingBot(config)
        service = bot.service
    elif config.strategy_mode == "ls":
        bot = LsTradingBot(config)
        service = bot.service
    else:
        bot = TradingBot(config)
        service = bot.service

    summary = service.account_summary()
    logger.info(
        "Connected | paper=%s status=%s equity=$%.2f cash=$%.2f buying_power=$%.2f",
        summary["paper"],
        summary["status"],
        summary["equity"],
        summary["cash"],
        summary["buying_power"],
    )
    if config.strategy_mode == "ai":
        logger.info(
            "AI strategy | provider=%s preset=%s symbols=%s size=%s conf>=%.2f",
            config.ai_provider,
            config.ai_preset,
            ",".join(config.primary_symbols()),
            config.size_summary(),
            config.ai_min_confidence,
        )
    elif config.strategy_mode == "pair":
        logger.info(
            "Pair strategy | preset=%s %s/%s SMA%d lookback=%dd impulse≤−%.1f%% "
            "weak=%s size=%s tf=%s",
            config.pair_preset,
            config.pair_long_symbol,
            config.pair_short_symbol,
            config.pair_sma_period,
            config.pair_lookback,
            config.pair_impulse_pct,
            config.pair_weak_side,
            config.size_summary(),
            config.bar_timeframe,
        )
    elif config.strategy_mode == "ls":
        logger.info(
            "LS strategy | EMA(%d/%d) ADX≥%.0f ATR×%.1f risk=%.1f%% RR=%.1f "
            "time=%dbars tf=%s",
            config.ls_ema_fast,
            config.ls_ema_slow,
            config.ls_adx_min,
            config.ls_atr_stop_mult,
            config.ls_risk_pct,
            config.ls_rr,
            config.ls_time_stop_bars,
            config.bar_timeframe,
        )
    elif config.strategy_mode == "dip":
        logger.info(
            "Dip strategy | preset=%s symbols=%s RSI(buy≤%.0f/sell≥%.0f) "
            "skip_bearish=%s size=%s tf=%s",
            config.dip_preset,
            ",".join(config.primary_symbols()),
            config.dip_rsi_buy,
            config.dip_rsi_sell,
            config.dip_skip_bearish,
            config.size_summary(),
            config.bar_timeframe,
        )
    else:
        logger.info(
            "SMA strategy | preset=%s symbols=%s SMA(%d/%d) size=%s tf=%s",
            config.sma_preset,
            ",".join(config.primary_symbols()),
            config.fast_sma,
            config.slow_sma,
            config.size_summary(),
            config.bar_timeframe,
        )

    if args.account:
        return 0

    def tick() -> None:
        bot.run_once()

    if args.once:
        tick()
        return 0

    logger.info("Looping every %ss (Ctrl+C to stop)", config.poll_seconds)
    while True:
        try:
            tick()
        except Exception:
            logger.exception("iteration failed; will retry")
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
        sys.exit(0)
