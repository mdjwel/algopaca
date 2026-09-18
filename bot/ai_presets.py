"""Named AI strategy presets for AlgoPaca.

Each preset carries two things: a playbook (what the model reads) and a risk
profile (what the bot enforces). The playbook states numeric gates rather than
adjectives — "ADX >= 22" is checkable, "avoid choppy tapes" is not — and every
preset defines how it exits, not just how it enters.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AiPreset:
    id: str
    label: str
    summary: str
    min_confidence: float
    instructions: str
    # Risk profile applied when the preset is selected. None = leave as configured.
    atr_stop_mult: float | None = None
    take_profit_r: float | None = None
    trail_after_r: float | None = None
    max_positions: int | None = None
    risk_pct: float | None = None


_SHARED_EXIT = (
    "EXITS: the bot moves the stop to breakeven and trails it past trail_after_r, "
    "and scales out at take_profit_r. Do not sell a winner early just to bank a "
    "small gain, and do not exit a loser manually — the stop defines the loss. "
    "Exit only when the reason for the entry is gone."
)


_PRESETS: tuple[AiPreset, ...] = (
    AiPreset(
        id="balanced",
        label="Balanced",
        summary="Hold when mixed; long or short when trend, momentum, and news align.",
        min_confidence=0.55,
        atr_stop_mult=1.8,
        take_profit_r=2.0,
        trail_after_r=1.0,
        max_positions=3,
        instructions=(
            "Balanced long/short desk. Open only when at least two of trend, "
            "momentum, and news agree, and none of them clearly disagrees.\n"
            "LONG gates: trend_bias bullish, regime 'trending' (ADX >= 20), price "
            "above SMA20, dist_sma50_atr between -1 and +3, RSI 40-70.\n"
            "SHORT gates: the mirror image — trend_bias bearish, ADX >= 20, price "
            "below SMA20, dist_sma50_atr between -3 and +1, RSI 30-60.\n"
            "Skip when regime is 'chop', when atr_pct is above 6 (too wild to size), "
            "or within 30 minutes of a High-impact USD event unless the thesis is "
            "unusually clear. Do not open against higher_timeframe.bias.\n"
            "No new position into an earnings blackout; flattening is allowed. After "
            "a print: a beat can open or hold a long and should cover a short; a "
            "miss can open a short and should exit a long.\n"
            + _SHARED_EXIT
        ),
    ),
    AiPreset(
        id="conservative",
        label="Conservative",
        summary="Rare, high-quality trades only; wide stop, small size, sits out risk events.",
        min_confidence=0.62,
        atr_stop_mult=2.2,
        take_profit_r=2.5,
        trail_after_r=1.0,
        max_positions=2,
        risk_pct=0.35,
        instructions=(
            "Trade rarely and only on full alignment. Default to hold — a skipped "
            "trade costs nothing.\n"
            "Require ALL of: trend_bias agrees with the direction, regime "
            "'trending' with ADX >= 25, higher_timeframe.bias agrees, news_bias not "
            "against you, spread_bps <= 15, and atr_pct <= 4.\n"
            "Never initiate with RSI > 70 for a long or RSI < 30 for a short, never "
            "with |dist_sma50_atr| > 2.5 (already extended), never inside an "
            "earnings blackout, and never with a High-impact USD event in the next "
            "2 hours.\n"
            "After an earnings miss, exit longs or stay flat; a short needs a clean "
            "breakdown below SMA20 on above-average volume. After a beat, cover "
            "shorts.\n"
            "Set confidence honestly — it scales size, and this preset is meant to "
            "trade small unless the setup is textbook.\n"
            + _SHARED_EXIT
        ),
    ),
    AiPreset(
        id="momentum",
        label="Momentum",
        summary="Follow strength — long uptrends, short breakdowns, let winners run.",
        min_confidence=0.55,
        atr_stop_mult=2.0,
        take_profit_r=3.0,
        trail_after_r=1.0,
        max_positions=3,
        instructions=(
            "Follow momentum in both directions. The edge here lives in the few "
            "large winners, so the trailing stop matters more than the entry.\n"
            "LONG gates: trend_bias bullish, ADX >= 22, price above SMA10 and "
            "SMA20, MACD histogram positive and not shrinking, volume_ratio >= 1.0.\n"
            "SHORT gates: trend_bias bearish, ADX >= 22, price below SMA10 and "
            "SMA20, MACD histogram negative and not shrinking.\n"
            "HARD SKIP when regime is 'chop' (ADX < 20) — momentum setups fail "
            "there and it is the main way this playbook loses money. Also skip when "
            "dist_sma50_atr is beyond +3.5 (long) or -3.5 (short): that is chasing.\n"
            "Do not buy a falling knife: a bounce needs price back above SMA10, not "
            "just an oversold RSI.\n"
            "After a beat, prefer continuation longs and cover shorts while the tape "
            "holds. After a miss, exit longs; short only on a clean breakdown.\n"
            + _SHARED_EXIT
            + " For this playbook specifically: hold while the trend structure is "
            "intact even if the move looks extended — the trail handles the giveback."
        ),
    ),
    AiPreset(
        id="mean_reversion",
        label="Mean reversion",
        summary="Fade stretched moves back to the mean; wide stop, quick target, time-limited.",
        min_confidence=0.6,
        atr_stop_mult=2.5,
        take_profit_r=1.2,
        trail_after_r=0.0,
        max_positions=2,
        instructions=(
            "Fade stretched moves — the only playbook that WANTS regime 'chop'. In a "
            "strong trend (ADX >= 30) do not fade; stand aside.\n"
            "LONG gates: RSI <= 32 or bollinger.pct_b <= 0.05, dist_sma50_atr <= "
            "-2.0, and price action stabilising (the last bar is not a fresh low on "
            "expanding volume).\n"
            "SHORT gates: RSI >= 70 or bollinger.pct_b >= 0.95, dist_sma50_atr >= "
            "+2.0, and upside momentum stalling.\n"
            "Target is the mean, not a trend move: exit into SMA20 / bollinger.mid "
            "even if the bounce looks strong. take_profit_r is deliberately low and "
            "trailing is off — this playbook takes many small wins.\n"
            "TIME STOP: if the position is older than roughly 10 bars and "
            "r_multiple is still between -0.5 and +0.5, the mean reversion did not "
            "happen — close it and move on. A stalled fade is a failed fade.\n"
            "Never fade a fresh catalyst: skip when news_bias is strongly against "
            "the fade, during an earnings blackout, or in the react window after a "
            "miss (long) or a beat (short). Skip High-impact USD events.\n"
        ),
    ),
    AiPreset(
        id="news_aware",
        label="News-aware",
        summary="Trade fresh catalysts; wide stop for gap risk, flat when headlines are stale.",
        min_confidence=0.58,
        atr_stop_mult=2.5,
        take_profit_r=2.0,
        trail_after_r=1.0,
        max_positions=2,
        instructions=(
            "Trade catalysts, not chatter. Weight news, earnings, and the economic "
            "calendar above quiet technicals — but only when the news is FRESH.\n"
            "A headline older than roughly 4 hours is context, not a catalyst. If "
            "the newest relevant headline is stale, or headlines conflict, or there "
            "are fewer than two on-topic items, hold.\n"
            "Go long only when news_bias is clearly supportive (a fresh EPS beat "
            "counts) and technicals are not strongly against it. Go short / exit "
            "longs when headlines are clearly negative or the name just missed.\n"
            "Because news moves gap risk, use size conservatively and expect a wide "
            "stop — do not tighten it mentally by exiting early on noise.\n"
            "Do not open into an earnings blackout or a High-impact USD release "
            "unless the context is already priced and confidence is high.\n"
            "Never infer a headline that is not in the context.\n"
            + _SHARED_EXIT
        ),
    ),
    AiPreset(
        id="trend_atr",
        label="Trend + ATR trail",
        summary="Pure trend following: no fixed target, ride the trail until it breaks.",
        min_confidence=0.55,
        atr_stop_mult=2.5,
        take_profit_r=0.0,
        trail_after_r=1.0,
        max_positions=3,
        instructions=(
            "Pure trend following. Accept a low win rate: most trades are small "
            "losers and a few are large winners. Never cut a winner early — that "
            "single habit is what destroys this playbook's expectancy.\n"
            "LONG gates: trend_bias bullish, ADX >= 22 and rising, price above "
            "SMA20, higher_timeframe.bias bullish or neutral, MACD histogram > 0.\n"
            "SHORT gates: the exact mirror.\n"
            "Entry timing: prefer a pullback toward SMA10/SMA20 in an intact trend "
            "(dist_sma50_atr between 0 and +2 for a long) over a fresh breakout "
            "extension beyond +3.5.\n"
            "Skip regime 'chop' entirely.\n"
            "EXITS: there is no profit target — take_profit_r is 0 on purpose. The "
            "ATR trailing stop is the only exit. Answer 'sell' on an open long ONLY "
            "when the trend structure genuinely breaks: price closes below SMA20 "
            "with MACD histogram flipping negative, or trend_bias turns bearish. "
            "Otherwise hold, no matter how large the open gain looks.\n"
        ),
    ),
    AiPreset(
        id="pead",
        label="Earnings drift (PEAD)",
        summary="Trade the post-earnings drift after a surprise, in the direction of the print.",
        min_confidence=0.6,
        atr_stop_mult=2.5,
        take_profit_r=2.0,
        trail_after_r=1.0,
        max_positions=2,
        instructions=(
            "Trade post-earnings announcement drift: prices tend to keep moving in "
            "the direction of a genuine surprise for several sessions.\n"
            "ONLY act when earnings.stance is 'react' — a print has just landed. If "
            "stance is 'clear' or 'wait', or blackout is true, hold. Never trade "
            "this playbook on technicals alone, and never invent an EPS figure.\n"
            "LONG: earnings.last_result is 'beat' and price is holding above the "
            "post-print level (trend_bias not bearish). SHORT: last_result is "
            "'miss' and price is failing (trend_bias not bullish).\n"
            "Do not chase the first violent move — if dist_sma50_atr is already "
            "beyond +4 (beat) or -4 (miss), the drift is spent; hold.\n"
            "Fade nothing here: never short a beat, never buy a miss.\n"
            "Expect to hold for several sessions. Do not exit on the first red bar "
            "— exit when the drift stalls, meaning price loses SMA10 (long) or "
            "reclaims SMA10 (short), or when a fresh contradicting catalyst lands.\n"
            + _SHARED_EXIT
        ),
    ),
    AiPreset(
        id="orb",
        label="Opening range breakout",
        summary="Intraday: trade the break of the opening range, flat by the close.",
        min_confidence=0.58,
        atr_stop_mult=1.5,
        take_profit_r=2.0,
        trail_after_r=1.0,
        max_positions=2,
        instructions=(
            "Intraday opening-range breakout. Use with an intraday bar_timeframe "
            "(5Min or 15Min); on daily bars this playbook has no meaning — hold.\n"
            "The opening range is the high and low of roughly the first 30 minutes "
            "of the regular session — read it from recent_bars and range_20.\n"
            "LONG: price breaks and holds above the opening-range high with "
            "volume_ratio >= 1.2. SHORT: breaks and holds below the opening-range "
            "low with the same volume confirmation. A wick through the level that "
            "closes back inside is a failed break — hold.\n"
            "Only trade the break in the direction of higher_timeframe.bias when "
            "that bias is not neutral.\n"
            "Do not open a new position after roughly 14:30 ET — too little time "
            "left for the move to work.\n"
            "END OF DAY: this playbook does not hold overnight. If a position is "
            "open and the session is close to the 16:00 ET close, answer with the "
            "closing action (sell a long, buy back a short) regardless of "
            "r_multiple.\n"
            "Skip entirely when session is not 'open' — pre/post-market has no "
            "opening range.\n"
            + _SHARED_EXIT
        ),
    ),
    AiPreset(
        id="gold_silver_macro",
        label="AI Gold & Silver Macro Momentum",
        summary="Calibrated macro & Gold/Silver Ratio playbook: buys pullbacks in confirmed gold uptrends across GLD, SLV, GDXU & inverse short ETFs (GLL, GDXD), with a wide ATR stop that lets trends run.",
        min_confidence=0.70,
        # Calibrated risk management: 1.6 ATR stop gives breathing room without excessive loss.
        # Trailing stop armed at 2.0R ratchets to breakeven, and take-profit scale-out target at 4.0R
        # secures partial profits while the runner trails for multi-week trend capture.
        atr_stop_mult=1.6,
        take_profit_r=4.0,
        trail_after_r=2.0,
        max_positions=2,
        risk_pct=1.8,
        instructions=(
            "Specialized Gold & Silver (GLD, SLV, GDXU, GLL, GDXD) macro playbook.\n"
            "This playbook is calibrated against 2007-2025 forward-return studies on GLD, not on folklore. "
            "Three findings drive it, and they overrule intuition when the two conflict:\n"
            "(a) Gold's SHORT-TERM momentum is mean-reverting. 5-20 day momentum has a NEGATIVE correlation "
            "with the next month's return. Buying a fresh breakout in gold is a measured losing edge — buy "
            "pullbacks inside an established uptrend instead.\n"
            "(b) Falling real yields and a stretched Gold/Silver ratio are the only macro factors with a "
            "durable edge. The US Dollar is far weaker than commonly claimed, and gold-miner leadership has "
            "NO measurable predictive power at all — treat miners_signal as colour, never as a reason. "
            "GDX, GDXJ, DUST, and UGL are strictly excluded from this playbook to avoid equity beta and leveraged drag.\n"
            "(c) Overtrading is what destroys returns here. Every fast timing rule tested underperformed "
            "simply holding gold. Time in the trend beats frequency of trades. Never scalp or take small micro-profits.\n"
            "LONG gates (all must hold):\n"
            "1. Regime: precious_metals_intel.trend_regime is 'bullish_above_sma200'. This is the single "
            "participation gate — it is what keeps you out of multi-year bear markets like 2012-2015.\n"
            "2. Macro: macro_composite_score >= 0.0 (constructive/neutral macro bias). The "
            "score is a calibrated -3..+3 blend; scores below -0.5 preceded NEGATIVE average forward returns, "
            "so treat that band as a hard no-buy, not a discount.\n"
            "3. Entry timing: prefer entering on a PULLBACK — RSI between 38 and 58, or price at or below "
            "SMA20 while the higher-timeframe trend stays up. Do NOT require price above EMA9 or a positive "
            "MACD histogram to go long; demanding short-term strength is the mean-reversion trap in (a).\n"
            "4. Vehicle: default to GLD. Only step up to GDXU (3x miners) when macro_composite_score is "
            ">= +1.5 (ADX >= 20.0) AND trend_regime is bullish — GDX, GDXJ, DUST, and UGL are strictly excluded. "
            "GDXU is a long-only bull vehicle (unshortable at Alpaca); NEVER initiate a short/sell to open on it.\n"
            "5. Relative value: when gsr_z_score >= +0.8 or macro_composite_score >= +0.2 the ratio is stretched or supportive. "
            "Historically this marked a risk-off bid that lifted the whole complex AND set up silver catch-up, so SLV is the higher-beta "
            "expression of a bullish call (size boosted). When gsr_z_score <= -1.2 the ratio is compressed — a risk-on tell "
            "that preceded below-average bullion returns. Do not read a low ratio as 'gold is cheap'.\n"
            "SHORT & INVERSE ETF gates:\n"
            "1. Regime: trend_regime is 'bearish_below_sma200' and macro_composite_score <= -0.5.\n"
            "2. Confirmation: rising yields (yield_trend 'rising_yields') is the factor that matters most.\n"
            "3. Shorting gold fights a positive long-run drift. Require a clearly bearish macro score and "
            "size smaller than an equivalent long. Inverse ETFs (GLL, GDXD) express the bearish view without margin "
            "borrow by BUYING long (action='buy'). DUST is strictly excluded. Never short GDXD or GDXU directly, and never hold opposing pairs like GDXU and GDXD simultaneously. "
            "Do NOT initiate or maintain shorts when US Dollar Index economic data is mixed, dollar_trend is 'neutral', or gold_dollar_divergence is True (bullish decoupling). "
            "Skip late-session entry (hour >= 19 UTC) on inverse ETFs to avoid overnight gap risk.\n"
            "RISK & VOLATILITY GATES:\n"
            "- PRE-RELEASE FREEZE: If macro_risk_level is 'imminent_release' (high-impact unreleased FOMC / CPI within 45 minutes), HOLD to avoid spread whipsaws before the binary announcement.\n"
            "- POST-RELEASE CATALYST POSITIONING (MANDATORY): When macro_risk_level is 'post_release_catalyst' (e.g. FOMC Federal Funds Rate decision or statement has been released):\n"
            "  The imminent event freeze is LIFTED. Formulate an active directional position based on the released data:\n"
            "  * Rate Hike / Hawkish: Check market digestion and yields! If Treasury yields fall (TLT rises) or USD pulls back post-hike (the hike was priced in), this is a BULLISH REBOUND (e.g. Sept 17, 2026 dynamic); initiate or add to LONG bullion (GLD/SLV). Only buy inverse ETFs (GLL/GDXD) or short GLD if yields and USD actively surge post-announcement (hawkish follow-through).\n"
            "  * Rate Cut / Dovish Surprise: Lower rates weaken USD and yields, igniting bullish demand for bullion. Initiate or add to LONG positions (GLD, SLV).\n"
            "  * 3-Confirmation Matrix: When three_confirmation_state is 'all_three_aligned' (DXY down, Yields down, Gold bullish), size up long positions. When 'divergent_warning', defend longs and strictly prohibit shorts.\n"
            "  * Crude Oil (USO) Inflation Tell: When oil_trend is 'cooling_inflation' (oil sharp drop), it eases inflation and yield pressure, acting as an additional bullish catalyst for gold.\n"
            "  * In-line / As Expected: Trade the technical breakout or trend continuation without the pre-release hold restriction.\n"
            "- HARD SKIP when spread_bps > 25.\n"
            "- Do not chase overextended moves when dist_sma50_atr is beyond +3.2 (long) or -3.2 (short).\n"
            "EXITS & RUNNERS:\n"
            "- Gold's edge is captured by holding trends, so the default answer on an open, working position "
            "is HOLD. Never scalp or cut winners early. Ratchet the stop to breakeven after 2.0R and let the ATR trail do the work for large multi-week gains.\n"
            "- Scale out at the 4.0R take-profit target; let the remainder trail for multi-week commodity upside.\n"
            "- INVERSE DECAY GUARD: For inverse ETFs (GLL, GDXD), never hold longer than ~5 trading days or when inverse RSI reaches >= 65.0 to guard against leveraged volatility decay.\n"
            "- Exit early ONLY on a confirmed regime flip (loss of trend_regime), a macro score that has "
            "crossed below -1.0, or a major contradicting catalyst. Do not exit on short-term weakness alone "
            "— shallow pullbacks inside an uptrend are entries, not exits.\n"
            "- REVERSAL RULE: If analysis shows economic data for the US Dollar Index is mixed or neutral "
            "(dollar_trend 'neutral' or dollar_mixed is True) OR gold_dollar_divergence is True, immediately CLOSE/COVER any open sell/short "
            "position and reverse into a LONG position (action='buy'). Shorting into mixed dollar data or bullish decoupling carries "
            "severe squeeze risk; flip to long exposure instead.\n"
            + _SHARED_EXIT
        ),
    ),
    AiPreset(
        id="custom",
        label="Custom",
        summary="Write your own instructions below.",
        min_confidence=0.55,
        instructions="",
    ),
)

_BY_ID = {p.id: p for p in _PRESETS}
DEFAULT_PRESET_ID = "balanced"


def list_presets() -> list[dict[str, Any]]:
    return [asdict(p) for p in _PRESETS]


def get_preset(preset_id: str | None) -> AiPreset:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return _BY_ID.get(key, _BY_ID[DEFAULT_PRESET_ID])


def resolve_preset_id(preset_id: str | None) -> str:
    key = (preset_id or DEFAULT_PRESET_ID).strip().lower()
    return key if key in _BY_ID else DEFAULT_PRESET_ID


def instructions_for(preset_id: str | None, override: str | None = None) -> str:
    """Return instructions to send the model.

    Named presets supply their playbook unless the caller already set text
    (UI always writes the playbook when a preset is chosen). Custom uses override.
    """
    text = (override or "").strip()
    preset = get_preset(preset_id)
    if preset.id == "custom":
        return text
    return text or preset.instructions


def risk_profile_for(preset_id: str | None) -> dict[str, float | int]:
    """Risk knobs a named preset wants, skipping the ones it leaves alone.

    A mean-reversion book and a trend book need opposite stop/target geometry,
    so the preset owns those defaults rather than every preset inheriting one
    global setting.
    """
    preset = get_preset(preset_id)
    fields = {
        "ai_atr_stop_mult": preset.atr_stop_mult,
        "ai_take_profit_r": preset.take_profit_r,
        "ai_trail_after_r": preset.trail_after_r,
        "ai_max_positions": preset.max_positions,
        "ai_risk_pct": preset.risk_pct,
    }
    return {k: v for k, v in fields.items() if v is not None}
