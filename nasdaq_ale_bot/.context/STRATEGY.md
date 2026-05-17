# STRATEGY.md — Project Direction (distilled, post-Phase-4)

> Living source of truth for project intent. Distilled from PLAN.md,
> ASSUMPTIONS.md, the Phase 4 diagnostics, and the 2026-05-16 pivot
> decision. When this file and PLAN.md disagree, **this file wins** —
> PLAN.md is stale (QQQ/SPY/Alpaca era).

## Project

- **Name:** NasdaqAle Trading Bot (working title, no rebrand planned).
- **Repo path (actual):** `c:\Users\loren\Private\Workspace\Workspace\nasdaq_ale_bot\`
  (the context handoff and `settings.local.json` reference an older
  `c:\Users\Bernd\Workspace\nasdaq_ale_bot\` — see AI_INSIGHTS).
- **Owner:** Bernd — 4th-year HTL Weiz IT student (4AHIT), Austria.
- **Languages:** German for planning discussion, English for code and docs.

## Goal

Two goals, deliberately separated:

- **Goal A — make money:** pass a prop-firm evaluation (now Tradeify
  SELECT 50K) with a mechanical bot, then trade the funded account.
  **This is the only remaining focus.**
- **Goal B — learn algo-trading engineering:** already **achieved** —
  clean codebase, walk-forward methodology, 359 tests, look-ahead-proof
  detection. Goal B is done; it is not a reason to keep iterating.

If Goal A is not reachable in a realistic timeframe, **archiving the
project is a valid outcome, not a failure.**

## Current status (2026-05-16)

8 months of engineering produced a **definitive negative result** on the
NasdaqAle ICT replication: detection is clean (no look-ahead), parameters
are stable (variant V_A beats BASE), **but the gross edge of +$16.35 per
NQ contract is smaller than the $19 round-trip cost stack** ($9 commission
+ $10 slippage). Net edge per trade: **-$2.65**. No prop-firm path exists
for the ICT strategy as built.

This is a real, valuable finding — many retail traders pay hundreds in
challenge fees to learn it.

## Strategic decision — PIVOT

**From:** mechanical replication of the discretionary NasdaqAle ICT
strategy.
**To:** **15-minute Opening Range Breakout (ORB) on NQ.**

Rationale:
1. ICT is structurally too close to cost break-even; raising the edge
   needs either higher win rate or higher R:R, neither of which the ICT
   rules deliver.
2. ORB has a documented edge in published research (Cory Mitchell;
   Edgeful; TradingStats 12-year / 6,142-day study).
3. ORB is mechanically simpler — max 1 trade/day, clearly defined states.
4. ORB reuses much of the existing harness (backtest runner, ledger,
   gates, killzone, data loaders). See AI_INSIGHTS for a realistic
   reuse estimate (~60-70%, not 95%).

**Target prop firm changed:** Apex 50K → **Tradeify SELECT 50K**.

### Tradeify SELECT 50K rules (for reference — explain mechanics to owner)

- Cost: **$111** with code SOPF (vs Apex $195).
- Profit target: **+$3,000**.
- Drawdown: **$2,000 EOD trailing** — trails *end-of-day* balance, not
  intraday. Intraday dips do not fail the account; the peak and the DD
  check both happen at session close. (This is more forgiving than
  Apex's intraday trailing DD — explain this distinction explicitly.)
- **No time limit** on the evaluation.
- **No daily loss limit** during evaluation.
- **40% consistency rule:** no single day may exceed 40% of total
  profit at payout.
- Minimum 3 trading days.
- Bots allowed for proprietary strategies.
- Max **4 NQ contracts** on the 50K account.
- Platform: **Tradovate**.

## Non-goals

- No rebrand of the project name.
- No multi-strategy / multi-session parallelism before a single strategy
  is proven on out-of-sample data (see Meta-Observation Pattern 1 & 2).
- No prop-firm challenge purchase until the backtest shows **multiple**
  Tradeify passes on out-of-sample data (Pattern 3).
- No live-runner / GUI work (PLAN.md Phases 4/6) until Goal A has a
  mathematically demonstrated edge.

## Immediate next task

ORB build in progress (started 2026-05-16): light multi-strategy
refactor, then the 15-min Opening Range Breakout implementation,
tests, and validation runs.

---

## Decision Criteria — ORB Viability (LOCKED 2026-05-16)

These thresholds are **locked before any ORB test runs**. After the
Tradeify simulation, the verdict is read mechanically against these
numbers — no re-interpretation of thresholds after seeing results.
A NOT VIABLE outcome is a valid result to report plainly, not to
engineer around (see Meta-Observation Patterns 1 & 2).

| Verdict | Condition |
|---|---|
| **VIABLE** | avg net per trade **> $10** AND Tradeify 180-day WIN rate **> 60%** |
| **MARGINAL** | avg net per trade **$0–$10** AND WIN rate **40–60%** → one filter iteration allowed (VWAP trend filter only) |
| **NOT VIABLE** | avg net per trade **≤ $0** OR WIN rate **< 40%** |

All ORB backtests use the unified cost model ($19 round-trip per NQ
contract — see `.context/TECH_STACK.md` and `config/cost_model.yaml`).

### Walk-forward statistical verification (Step 2 — mandatory)

The Step 2 walk-forward must include two robustness checks per OOS split
and for the aggregate, written into `results/phase4_orb_tradeify_sim.json`:

- **CHECK A — Bootstrap CI:** resample OOS trade net-R-per-trade with
  replacement, 10 000 iterations; report the 95% CI (2.5/97.5 pct). If the
  CI spans zero → edge **not** statistically verified, regardless of the
  equity curve. Fully-positive CI → verified edge.
- **CHECK B — Null model:** for each real ORB entry, generate a null entry
  at a *random* time within 30 min after the same breakout signal, same
  stop/target logic; 1 000 null runs; report the p-value (fraction of null
  runs beating the real model). p < 0.05 → ORB timing carries real
  information; p > 0.05 → timing is no better than random entry.

If CHECK A spans zero **or** CHECK B gives p > 0.05, that is part of the
verdict — even if raw net numbers look positive.

---

## Working Style & Meta-Observations

Seven patterns distilled from 8 months of planning chats. Background
knowledge for every decision made on this project.

### Pattern 1 — "Trader's Spiral" risk
After a strategy fails, the owner tends, under stress, to jump straight
to the next strategy idea (NasdaqAle not profitable → multi-strategy →
multi-session → "own strategy"). **Mitigation:** decision criteria must
be defined *before* tests, never after seeing results. One strategy at a
time, never parallel without a single-strategy proof first.

### Pattern 2 — Multi-testing inflation
Over 8 months: 4-6 large detection refactors, multiple killzone
adjustments, several entry mechanics, an ML gate added and dropped,
multiple composite-score formulas. Each iteration is effectively a
multiple-testing increment that inflates the chance of a false positive.
**For the ORB build: NO iteration during the test phase.** Run as
specified, then accept the result.

### Pattern 3 — Student with a limited budget
No prop-firm challenge gets purchased until the backtest shows multiple
Apex/Tradeify passes on out-of-sample data. No "maybe it works" —
mathematical evidence first.

### Pattern 4 — Goal A vs Goal B
Goal A = make money (pass the challenge). Goal B = learn the engineering.
Goal B is **already complete**. Goal A is the sole focus. Project
archival is a valid option if Goal A is unreachable.

### Pattern 5 — Decision frameworks
A "Supreme Board of Advisors" pattern (cross-challenges between
perspectives) is used for major phase-gate decisions. "The one question
you are avoiding" is an especially valuable element. Routine prompts run
directly; major decisions go through the council.

### Pattern 6 — Owner technical background
Strong at coding and ML fundamentals (regression, gradient descent, AI).
**No professional trading experience** — explain trading-specific
concepts explicitly (e.g. EOD trailing-drawdown mechanics). Code
standards can be communicated at senior level.

### Pattern 7 — Working style
Mostly desktop with Claude Code in VSCode; occasionally mobile for
planning chats. German for planning discussion, English in code and
technical docs. Prefers minimal, direct solutions without
over-engineering.
