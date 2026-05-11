# Assumptions Reference Index (A1-A24)

Full details in `ASSUMPTIONS.md`. This page provides a one-line summary for quick lookup.

| # | Topic | Summary |
|---|-------|---------|
| A1 | CISD reference (bullish) | Walk backward through contiguous down-leg; run-terminator (first break) is reference if up-close; 20-bar cap |
| A1b | CISD reference (bearish) | Mirror of A1: walk backward through up-leg, reference must be down-close |
| A2 | CISD confirmation window | Forward-scan up to 15 bars after sweep; body-close beyond reference.high/low confirms |
| A3 | Sweep minimum penetration | Per-instrument tick from `instruments.yaml`; default 2 ticks minimum; wick must pierce, body must reclaim |
| A4 | IFVG count filter | 0 IFVGs -> reject, 1 -> ideal, 2 -> pick closer to sweep, >=3 -> reject |
| A5 | Equilibrium check leg | Fib 0.5 of CISD-move leg (sweep_idx..confirm_idx); entry must be in discount (long) or premium (short) |
| A6 | SL construction | All 3 SL inputs (IFVG.bottom, sweep_low, swing_low) from 1m timeframe; SL = min - 2*tick |
| A7 | TP and R:R gating | Floor 1:1.1, cap 1:1.3, target avg 1:1.2; logs `natural_rr_at_signal` and `executed_rr` |
| A8 | HTF bias break | 4H unmitigated FVG body-close + two-bar confirmation; requires 1H structure + Daily close agreement |
| A9 | Secondary killzone | Any AM order disables PM killzone for the session |
| A10 | News filter source | CSV stub in Phase 1; `NewsFeedStale` if missing or mtime > 24h; live feed in Phase 4 |
| A11 | Position sizing skip | SL > max_stop -> skip trade entirely; never shrink position or widen stop |
| A12 | SMT aggregation | Clock-anchored 5m bars (09:30, 09:35...); `SMTTracker` owns 1m->5m; `smt_pure.py` is stateless |
| A13 | Missing bar handling | Forward-fill max 1 bar; >=2 consecutive missing -> SMT UNAVAILABLE -> **blocks entries** (fail-closed) |
| A14 | Risk parameter | `fixed` ($750) or `percent` (0.5%); percent queries equity at session start only |
| A15 | Daily loss limit | -$1500 realized triggers halt; new-entry gate uses projected_pnl (realized + unrealized - risk) |
| A16 | Session boundary | America/New_York calendar day; all counters reset at 00:00 ET |
| A17 | Breakeven trigger | Touch-based (exempt from body-close principle); SL moved to entry +/- 1 tick |
| A18 | Hard time exit | Market order at 11:00 ET (AM) / 15:45 ET (PM); shifts on NYSE early-close days |
| A19 | Trade idempotency | `client_order_id = sha1(bar_ts_iso\|direction\|strategy_version)`; prevents duplicates on reconnect |
| A20 | BrokerProtocol | 9-method `typing.Protocol`; Alpaca impl today, Tradovate/IBKR tomorrow |
| A21 | Threading contract | State machine is single-threaded; WS -> queue -> engine-thread actor model |
| A22 | Intrabar SL+TP collision | SL wins (conservative); no tick data available to determine order |
| A23 | Clock drift guard | Startup + hourly check; \|delta\| > 2s -> halt new entries |
| A24 | Look-ahead enforcement | `CandleView.__getitem__` raises `LookAheadError` if k > i; runtime, not convention |
