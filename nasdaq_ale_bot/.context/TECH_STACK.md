# TECH_STACK.md — Technologies & Rationale (distilled)

> What is actually installed and used as of 2026-05-16. Distilled from
> `pyproject.toml` and the working state of the repo. Where the stack
> diverged from PLAN.md, the divergence is noted.

## Runtime

| Component | Version | Why |
|---|---|---|
| Python | 3.11+ (3.12 in use) | Type hints, `zoneinfo`, pattern matching. |
| pandas | >=2.2,<3.0 | Bar data frames, DBN → DataFrame. |
| numpy | >=1.26,<3.0 | Numeric ops in metrics / features. |
| pydantic | >=2.6,<3.0 | `Candle` model with tz-aware + OHLC validators. |
| pydantic-settings | >=2.2,<3.0 | Env-driven secrets (`AlpacaSettings`). |
| structlog | >=24.1,<25.0 | Structured JSON logging on every state transition. |
| pyyaml | >=6.0,<7.0 | `strategy.yaml`, `instruments.yaml`. |
| pyarrow | >=15.0,<20.0 | Parquet caching (legacy QQQ/SPY path). |
| pandas_market_calendars | >=4.4,<5.0 | NYSE early-close handling for killzones. |
| tzdata | >=2024.1 | `zoneinfo` on Windows. |
| **databento** | **>=0.78.0** | **DBN data loading — added Phase 3.5.** Not in original PLAN.md. |
| scikit-learn | 1.8.0 (ad hoc) | Phase 3.5 ML regime gate experiment. Used by `phase35_ml_session_regime.py`; **not** a declared dependency in `pyproject.toml`. |

## Dev / test

| Component | Version | Why |
|---|---|---|
| pytest | >=8.0,<9.0 | 359 tests, unit + integration. |
| pytest-cov | >=5.0,<6.0 | Branch coverage; `fail_under = 90`. |
| pytest-asyncio | >=0.23,<1.0 | Async broker-stream tests. |
| hypothesis | >=6.100,<7.0 | Property-based CISD fuzzing (optional). |
| ruff | >=0.4,<1.0 | Lint, 100-char line length, py311 target. |
| mypy | >=1.10,<2.0 | Static typing on core + detection. |

## Data

- **Primary:** `data/historical/NQ_1m_2022_2026.dbn.zst` — Databento
  DBN, NQ E-mini 1-minute OHLCV, 2022-01-02 .. 2026-05-12.
- **Correlated (SMT):** `data/historical/ES_1m_2022_2026.dbn.zst` — ES
  E-mini, same range.
- Loaded via `BacktestRunner.load_bars_from_dbn(path, symbol_prefix)`:
  filters to outright front-month (no `-` calendar spreads), picks the
  highest-volume contract per timestamp, returns UTC tz-aware `Candle`s.
- SHA-256 + row counts recorded in `data/historical/manifest.json`.
- **Legacy (removed):** Alpaca QQQ/SPY parquet, Kaggle
  `NQ_1min_2022_2025.csv`, Databento `mes1123.csv`. All loaders and
  references deleted in Phase 3.5.

## Architecture (one-liner per layer)

- `core/` — `Candle`, `CandleView` (look-ahead guard), `StateMachine`
  (6-state ICT engine), `AccountLedger`, `SMTTracker`, `logging_sink`.
- `detection/` — pure, stateless ICT signal functions (cisd, fvg, ifvg,
  sweep, equilibrium, smt_pure). **ICT-specific — not reusable for ORB.**
- `bias/` — HTF 4H-FVG bias detector + timeframe aggregators.
- `filters/` — killzone, news, trend. `killzone` is strategy-agnostic.
- `execution/` — `BrokerProtocol`, `MockBroker`, `GateList` (8 gates).
- `backtest/` — `BacktestRunner`, `MetricsCalculator`, `GridHarness`,
  `WalkForwardController`. **Strategy-agnostic harness — reusable.**
- `live/` — empty (`__init__.py` only). PLAN.md's "Phase 4 Paper Live
  Runner" was never built.

## Stack divergences from PLAN.md

- PLAN.md stack line says "alpaca-py, ..." and "QQQ primary + SPY
  correlated". **Superseded:** the project trades NQ/ES futures on
  Databento data. `alpaca-py` is still a declared dependency but the
  live Alpaca path is dead code for the current direction.
- PLAN.md "Phase 3.5 = vectorbt metrics cross-check." **Reused number:**
  the *actual* Phase 3.5 was the Databento data migration. Two unrelated
  meanings of "3.5" exist in project history — see AI_INSIGHTS.

## Cost model (canonical — use everywhere for ORB)

Realistic per-NQ-contract round-trip cost:
- Commission: **$9.00** ($4.50/side × 2).
- Slippage: **$10.00** (1 tick/side × 2; NQ tick value $5).
- **Total: $19.00 per NQ contract round-trip.**

⚠️ `MockBroker` only deducts **$4.50/contract** internally (one charge
at exit). Any analysis that uses raw `MockBroker` realized PnL is
~$14.50/trade too optimistic. See AI_INSIGHTS — this must be reconciled
before the ORB walk-forward.
