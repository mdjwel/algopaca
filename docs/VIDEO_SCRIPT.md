# AlgoPaca — Demo Walkthrough Script

**Version** v2 (short cut) · 04 Sep 2026
**Runtime** 3:45 · **Shots** 9 · **VO** ≈ 550 words @ 150 wpm
**Format** 1920 × 1080, 60 fps · screen capture + voice-over
**Account** Paper only

---

## Running order

| # | In | Dur | Shot |
|:--|:--|:--|:--|
| 01 | 0:00 | 0:28 | Hook |
| 02 | 0:28 | 0:17 | Setup |
| 03 | 0:45 | 0:35 | Engines |
| 04 | 1:20 | 0:30 | AI desk |
| 05 | 1:50 | 0:30 | Risk gates |
| 06 | 2:20 | 0:18 | Approval queue |
| 07 | 2:38 | 0:30 | Exits & post-exit plans |
| 08 | 3:08 | 0:22 | History & backtest |
| 09 | 3:30 | 0:15 | Close |

---

## SHOT 01 — Hook
**0:00 · 0:28 · 70 words**

**ON SCREEN**
Black frame. One mono line types out: `./run.sh --once --mode ai --provider gemini`

Hard cut to the Auto-Trade desk mid-cycle. The signal wall paints a live decision — symbol, side, confidence `0.78`, ATR stop, share count — and the written thesis fills in underneath. Hold two seconds so the reasoning is genuinely readable, then title card.

**VO**

> A trading bot that just says *buy* is asking for trust it hasn't earned.
>
> AlgoPaca says buy — then shows the indicator that fired, the news it read, the risk it sized against, and the gate it had to clear first. It's a self-hosted quant desk for Alpaca Markets, where stock and ETF trades are commission-free. Six engines, four AI providers, MIT licensed. No subscription, no paywall.

**LOWER THIRD** — AlgoPaca · Autonomous paper & live trading desk

---

## SHOT 02 — Setup
**0:28 · 0:17 · 43 words**

**ON SCREEN**
Terminal: `git clone` → `docker compose up -d`, speed-ramped to eight seconds. Cut to the setup wizard at `localhost:8765` — paper keys pasted (secret blurred), AI provider picked, watchlist set.

Push in on the **Paper trading** pill in the masthead on the last line.

**VO**

> Setup is two commands: clone the repo, bring up the container. Open port 8765 and the wizard takes over — paper keys, an AI provider, a watchlist.
>
> Fresh installs always start in paper. Live trading needs a flag *and* an explicit confirmation.

---

## SHOT 03 — Engines
**0:45 · 0:35 · 85 words**

**ON SCREEN**
Open the **Engine** dropdown and hold it open for a beat — SMA, Dip, Pair, Regime L/S, Day trading, AI desk. Then one quick cut per engine as it's named, landing on its preset chips.

On the intraday line, switch to **Day trading (VWAP & ORB)**: show the side selector (**Long only · Short only · Long & Short**), max trades per day, and the EOD flatten toggle. Finish on **Save as engine**.

**VO**

> Pick an engine. SMA crossover, from a five–twenty short-term to the fifty–two-hundred golden cross. Dip buys RSI washouts at the lower Bollinger band. Pair rotates between two symbols. Regime long-short is dual momentum with an ADX gate.
>
> There's a dedicated intraday engine too — VWAP trend, opening range breakouts and breakdowns, momentum scalps. Long, short or two-way, capped per day, squared off before the close. And any setup you like saves as your own engine.

**LOWER THIRD** — 6 engines · 40+ presets · 10 starter blueprints

---

## SHOT 04 — AI desk
**1:20 · 0:30 · 74 words**

**ON SCREEN**
AI desk selected, preset **Balanced**. Press **Run once** and let the signal wall paint in real time — no speed ramp here, the wait is the point.

Cut to the metals panel: the gold/silver/miner universe, then a built graphic of the four weights as a stacked bar (0.40 / 0.35 / 0.15 / 0.10), with miner strength sitting outside it, greyed, labelled `weight 0.00 · IC −0.01`.

**VO**

> Then the AI desk. It fuses RSI, MACD, Bollinger, ATR and ADX with live news, earnings surprises and the economic calendar into one structured decision — confidence score and written thesis.
>
> Including a gold and silver macro read: yield direction, a z-scored gold-to-silver ratio, the two-hundred-day regime, the dollar. Those weights were measured against forward returns, not chosen by convention. Miner strength scored zero — so it carries zero weight.

**LOWER THIRD** — Weights fitted on 2007–2025 forward returns

---

## SHOT 05 — Risk gates
**1:50 · 0:30 · 73 words**

**ON SCREEN**
Animated graphic: seven gates stack in one at a time as they're named, a signal dropping through them. Then a real refusal in the desk log — `blocked: daily loss limit` — held long enough to read.

Cut to the Advanced Order ticket with a deliberately oversized quantity: *every* advisory breach listed at once, portfolio-heat bar in the red, override checkbox unticked. Leave it unticked.

**VO**

> Nothing reaches the broker without clearing seven gates: daily-loss circuit breaker, position cap, spread check, post-loss cooldown, ATR sizing, allocation cap, and portfolio heat — total open risk across the book. Volatility decides how big you get to be.
>
> And note the asymmetry. A bot that trips a gate is refused outright. A human typing a ticket is *advised* — every breach at once, and you override it on purpose.

**LOWER THIRD** — Bots are refused · humans are advised

---

## SHOT 06 — Approval queue
**2:20 · 0:18 · 44 words**

**ON SCREEN**
Toggle **Approval mode**. Two candidates stage in with confidence and reasoning. Approve one, reject the other. Picture-in-picture: the browser push arriving, then the email alert on a phone.

**VO**

> You don't have to hand over the keys on day one. Approval mode stages every candidate instead of firing it — side, size, confidence, reasoning — and waits. Approve, reject, or clear the queue. You get a push notification and an email either way.

---

## SHOT 07 — Exits & post-exit plans
**2:38 · 0:30 · 74 words**

**ON SCREEN**
Positions desk, marks ticking. Open one position and run the exits in the order named: risk chips, **Breakeven** ratchet, trailing stop, bracket, then a 50 % scale-out with the P&L estimate updating live.

Arm a **Dip Hunt**, then the money shot in one unbroken take: kill the process in the terminal, restart it, cut back — the plan is still armed. No edit inside that sequence.

**VO**

> Positions mark to market live, pre-market included. Protect anything in one click — a stop from a risk chip, a breakeven ratchet, a trailing stop, a bracket, or a partial scale-out with the P&L estimated before you commit.
>
> And arm what happens *after* the exit: a dip-hunt re-entry, a buy-back, a flip into another name. Every plan is written to disk, so a restart resumes it instead of dropping it.

**LOWER THIRD** — Plans persist across restarts · scoped per broker account

---

## SHOT 08 — History & backtest
**3:08 · 0:22 · 55 words**

**ON SCREEN**
History page: win rate, P&L by engine, the confidence-calibration chart, blocked executions. Then the narrated paragraph appearing *beneath* those numbers — the ordering carries the claim.

Click **Save** on a lesson, cut to it appearing in the live AI prompt. Finish on a backtest equity curve drawing.

**VO**

> History grades the desk against its own record — win rate, P&L by engine, confidence calibration, every blocked execution. All computed in Python; the model only narrates the finished numbers.
>
> Approve a lesson and it feeds back into the live prompt. And you can walk-forward backtest any of it first, with no lookahead.

**LOWER THIRD** — Numbers in Python · prose from the model

---

## SHOT 09 — Close
**3:30 · 0:15 · 42 words**

**ON SCREEN**
Repo page, CI badge green, test badge reading 254 passing. End card: repo URL, `algopaca.spiderdevs.xyz`, MIT.

The disclaimer card holds statically for the whole closing line — not a fast super. Music out before the final sentence so it lands dry.

**VO**

> MIT licensed, two hundred and fifty-four tests, and a hosted demo if you want to try it before you clone it.
>
> This is educational software, not financial advice — trading carries a real risk of loss. Start in paper. Link's in the description.

**END CARD** — github.com/mdjwel/algopaca · algopaca.spiderdevs.xyz

---

## Production notes

### Voice & pace
- **Dry, engineer-to-engineer.** No hype adjectives. At this length the restraint *is* the pitch.
- Target **150 wpm** and hold it — there's no slack in this cut. If a shot overruns, trim the picture, not the line.
- Shots 05 and 07 carry the argument. Everything else can be read briskly.

### Capture checklist
- Paper account seeded with **4–6 open positions** and **20+ closed trades**, so History has something real to show.
- Blur every API secret — check the wizard frame in shot 02 twice.
- Shot 07's restart must be one unbroken take; a cut there kills the claim.
- Record more footage than the timings need. Picture gets trimmed to the VO, never the reverse.

### Non-negotiables
- The **Paper trading** pill stays visible for the entire runtime.
- Never show a live account, live keys, or a real dollar balance.
- The disclaimer is spoken *and* shown. It doesn't get cut for time.
- No returns, no performance claims — the backtest is shown as a tool, not a track record.

### Cut from the long version
- Options overlay, MCP server, multi-user admin, themes and the mobile PWA — all dropped.
- Put them in a **pinned comment** or a follow-up short rather than compressing them into a passing clause here.
- Restoring any one of them costs ~20 seconds of VO.

---

## Required on-screen disclaimer — shot 09

> AlgoPaca is open-source software provided for educational, research and technical evaluation purposes only. Nothing in this video constitutes financial, investment, legal or tax advice. Trading involves substantial risk of loss. Test in paper trading before committing live capital.

---

*AlgoPaca walkthrough · script v2 · 9 shots · 3:45 · paper account only*
