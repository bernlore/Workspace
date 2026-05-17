# LESSONS_LEARNED.md — Risks & Hard-Won Insights

> What was identified as risky or difficult across 8 months and the
> Phase 4 diagnostics. Distilled — not a duplicate of PLAN.md §7.
> Ordered by how much they should shape the ORB build.

## L1 — Costs decide viability, not gross edge (THE central lesson)

The NasdaqAle ICT strategy has a *real* gross edge (+$16.35/NQ contract)
and still fails, because the realistic cost stack ($19 round-trip) is
larger. 8 months of clean engineering did not surface this until the
final viability test, because intermediate backtests used the lenient
`MockBroker` cost model ($4.50/contract).

**For ORB:** apply the full $19/NQ round-trip cost from the very first
backtest. A strategy that is not clearly profitable *after* realistic
costs is not profitable. Gross PF means nothing on its own.

## L2 — Look-ahead bias is the #1 inflator; enforce it at runtime

`CandleView` raises `LookAheadError` on any access `k > i`. The Phase 4
audit replayed 1.18M bars with zero violations — the guard works. But
the guard only covers *detection functions*. Stateful accumulators
(`Bucketed4HAggregator`, `DailyAggregator`, `SMTTracker`) enforce
no-look-ahead by *only emitting a closed bar when the next bar of the
next bucket arrives*.

**For ORB:** the opening-range high/low is exactly such an accumulator.
It must follow the same pattern — the range is only "closed" once the
first bar *after* the 15-minute window arrives. Never evaluate a
breakout against an in-flight range. (See AI_INSIGHTS for detail.)

## L3 — The SMT positional-join bug — silent, expensive, found late

The SMT correlated-bar join was *positional* (`bars_correlated[i]`)
instead of *timestamp-based*. NQ and ES have slightly different
missing-minute patterns, so positional indexing drifted up to 53 hours
apart by mid-replay — only 0.38% of bar pairs actually matched. The fix
(timestamp-keyed lookup) was a few lines and moved OOS PnL by +$15k.

**Lesson:** any two-series join must be on timestamp, never on index.
ORB is single-instrument so this specific bug cannot recur, but the
principle holds for any future correlated-data feature.

## L4 — News-feed staleness fail-closes the whole strategy

`NewsBlackoutGate` is fail-closed: if `data/news_events.csv` is older
than 24h, *every* trade is blocked. Every backtest in recent sessions
needed `touch data/news_events.csv` first, or it reported 0 trades.

**Lesson / risk:** in live trading this is a silent kill-switch. If the
news feed is not refreshed daily, the bot stops trading and looks
"healthy". Decide explicitly whether ORB uses a news gate; if yes, the
freshness mechanism must be robust, not a stub CSV.

## L5 — Walk-forward discipline: define criteria before the test

The project used a single replay partitioned into three IS/OOS splits
(A/B/C). This is sound *only* if the pass/fail criterion is fixed before
looking at results. The ICT work repeatedly adjusted parameters after
seeing OOS numbers (the ML gate, composite scores, killzone tweaks) —
each adjustment is a multiple-testing increment.

**For ORB:** write the decision rule (win rate / PF / net-per-trade
thresholds) into the prompt *before* running anything. Run once. Accept
the result. No iteration during the test phase.

## L6 — MockBroker fill model: SL wins on intrabar collision (A22)

When a single backtest bar's range contains both stop and target,
`MockBroker` resolves it as a **stop-out** (conservative — without tick
data we cannot know which was hit first). This is correct and must be
preserved for ORB; it keeps backtests honest rather than optimistic.

## L7 — Phase numbering and PLAN.md have drifted from reality

PLAN.md describes a QQQ/SPY/Alpaca project with a "Phase 4 Paper Live
Runner" and "Phase 5 Apex compliance". Reality: futures + Databento, no
live runner built, Apex replaced by Tradeify. "Phase 3.5" means two
different things in project history (vectorbt cross-check in PLAN.md vs
the Databento migration in practice).

**Lesson:** treat `.context/` as the source of truth from now on; do not
trust PLAN.md for current direction. Keep `.context/` updated as the
project moves.

## L8 — The strategy edge must come from structure, not parameter search

The ICT grid harness swept CISD windows, IFVG tolerance, R:R caps. The
best variant (V_A, `cisd_lookback=15`) only marginally beat BASE. No
parameter setting produced a viable edge because the *structure* of the
strategy (tight stops, RR capped at 1.1, ~36% win rate) sits at
cost-break-even by construction.

**For ORB:** the edge must be visible in the raw, default-parameter
backtest. If ORB needs a parameter search to look profitable, that is
the same overfitting trap — treat it as a red flag, not a fix.
