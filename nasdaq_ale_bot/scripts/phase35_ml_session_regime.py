#!/usr/bin/env python3
"""Phase 3.5 — Session-level ML regime classifier.

Predicts (before session start) whether a NY trading session will produce
at least one positive-EV trade. One prediction per day, NOT per trade.

Features (computed from NQ 1m bars only, no look-ahead):
  efficiency_ratio      — 10-day |net_move| / avg_daily_range (Kaufman ER)
  adr_pct               — 10-day rolling average daily range as % of price
  net_move_5d           — 5-day net % change (close-to-close)
  bias_direction        — sign of 10-day net move w/ ±1% threshold; LONG/SHORT/NONE
  day_of_week           — 0=Mon, 4=Fri
  prior_session_result  — 1 if prior session had a positive trade, else 0

Target: session_had_positive_trade (1=yes, 0=no — no trades counts as 0).

Train:  Split A IS  (2022-01-01..2023-09-30)
Test:   Split A OOS (2023-10-01..2024-02-29)  — validation
        Split C OOS (2024-10-01..2025-04-25)  — primary holdout

Decision: if Split C OOS accuracy > 55%, the gate is worth adding.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.preprocessing import StandardScaler

from nasdaq_ale_bot.backtest.runner import BacktestRunner
from nasdaq_ale_bot.bias.htf_bias import HTFBiasDetector
from nasdaq_ale_bot.core.account_ledger import AccountLedger
from nasdaq_ale_bot.execution.gates import GateList, load_strategy_config
from nasdaq_ale_bot.execution.mock_broker import MockBroker
from nasdaq_ale_bot.settings import load_instruments_config

logging.basicConfig(level=logging.CRITICAL)
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

REPO = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")

REPLAY_START = date(2022, 1, 1)
REPLAY_END = date(2025, 4, 30)

SPLITS = {
    "A_IS":  (date(2022, 1, 1),   date(2023, 9, 30)),
    "A_OOS": (date(2023, 10, 1),  date(2024, 2, 29)),
    "C_OOS": (date(2024, 10, 1),  date(2025, 4, 25)),
}


# ---------------------------------------------------------------------------
# Replay → trade list
# ---------------------------------------------------------------------------

def replay_and_collect_trades():
    """Single replay over REPLAY window; returns (trades, nq_bars)."""
    nq = BacktestRunner.load_bars_from_dbn(
        REPO / "data/historical/NQ_1m_2022_2026.dbn.zst", symbol_prefix="NQ"
    )
    es = BacktestRunner.load_bars_from_dbn(
        REPO / "data/historical/ES_1m_2022_2026.dbn.zst", symbol_prefix="ES"
    )
    nq = [b for b in nq if REPLAY_START <= b.ts.date() <= REPLAY_END]

    cfg = load_strategy_config(REPO / "config/strategy.yaml")
    inst = load_instruments_config(REPO / "config/instruments.yaml").primary
    cfg["_bias_detector"] = HTFBiasDetector(inst)
    cfg["_gate_list"] = GateList.base_list(cfg)
    cfg["risk_per_trade_usd"] = Decimal("750")
    cfg["entry_order_type"] = "LIMIT"
    cfg["entry_slippage_ticks"] = 0
    cfg.setdefault("default_qty", Decimal("1"))
    cfg["ifvg_tolerance_ticks"] = 2
    cfg["rr_cap"] = Decimal("1.1")
    cfg["cisd_lookback_bars"] = 20

    pv = Decimal(str(getattr(inst, "point_value", 1)))
    ledger = AccountLedger(session_start_equity=Decimal("50000"), today=nq[0].ts.date())
    broker = MockBroker(ledger=ledger, initial_equity=Decimal("50000"), point_value=pv)
    runner = BacktestRunner(
        bars_primary=nq, bars_correlated=es, mock_broker=broker, ledger=ledger,
        strategy_cfg=cfg, instrument_cfg=inst, param_set_hash="phase35_ml",
    )
    result = runner.run()
    return result.trades, nq


# ---------------------------------------------------------------------------
# Daily aggregation + feature engineering (no look-ahead)
# ---------------------------------------------------------------------------

def build_session_frame(nq_bars, trades) -> pd.DataFrame:
    """Aggregate 1m NQ bars to NY daily OHLC and compute per-session features.

    All features for session D use data ENDING on session D-1 (no look-ahead).
    The label `y` uses session D's trades.
    """
    # Convert NQ bars to a DataFrame with NY-date grouping
    df = pd.DataFrame(
        {
            "ts_utc": [b.ts for b in nq_bars],
            "open": [b.open for b in nq_bars],
            "high": [b.high for b in nq_bars],
            "low": [b.low for b in nq_bars],
            "close": [b.close for b in nq_bars],
        }
    )
    df["ny_date"] = pd.to_datetime(df["ts_utc"]).dt.tz_convert(NY).dt.date
    daily = df.groupby("ny_date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).reset_index()
    daily = daily.sort_values("ny_date").reset_index(drop=True)

    # Trade labels: 1 if any trade with realized_pnl > 0 on that NY-date
    trade_dates_pos: set[date] = set()
    trade_dates_any: set[date] = set()
    for t in trades:
        d = t.entry_ts.astimezone(NY).date()
        trade_dates_any.add(d)
        if float(t.realized_pnl) > 0:
            trade_dates_pos.add(d)
    daily["y"] = daily["ny_date"].apply(lambda d: 1 if d in trade_dates_pos else 0)
    daily["had_trade"] = daily["ny_date"].apply(lambda d: 1 if d in trade_dates_any else 0)

    # Daily range and returns (computed at session END, fed FORWARD for next-day features)
    daily["range_pct"] = (daily["high"] - daily["low"]) / daily["close"] * 100.0
    daily["ret_pct"] = daily["close"].pct_change() * 100.0
    daily["abs_ret_pct"] = daily["ret_pct"].abs()

    # Rolling features over 10-day window ending on PRIOR session
    # .shift(1) ensures features for session D only see sessions <= D-1
    daily["adr_pct"] = daily["range_pct"].rolling(10).mean().shift(1)
    daily["net_move_10d_abs"] = (
        daily["close"].pct_change(10).abs() * 100.0
    ).shift(1)
    daily["efficiency_ratio"] = daily["net_move_10d_abs"] / daily["adr_pct"]
    daily["net_move_5d"] = (daily["close"].pct_change(5) * 100.0).shift(1)
    net_move_10d = (daily["close"].pct_change(10) * 100.0).shift(1)
    daily["bias_direction"] = np.where(
        net_move_10d > 1.0, 1, np.where(net_move_10d < -1.0, -1, 0)
    )
    daily["day_of_week"] = pd.to_datetime(daily["ny_date"]).dt.dayofweek
    daily["prior_session_result"] = daily["y"].shift(1).fillna(0).astype(int)

    return daily


# ---------------------------------------------------------------------------
# Model train + evaluate (≤50 lines of model code)
# ---------------------------------------------------------------------------

FEATURES = [
    "efficiency_ratio",
    "adr_pct",
    "net_move_5d",
    "bias_direction",
    "day_of_week",
    "prior_session_result",
]


def train_and_eval(daily: pd.DataFrame) -> dict:
    """Fit LogisticRegression on Split A IS; evaluate on A OOS and C OOS.

    Also writes per-date predictions for the entire replay window to
    data/ml_session_predictions.csv so MLRegimeGate can consume them.
    """
    full = daily.dropna(subset=FEATURES).copy()

    def slice_split(name: str) -> pd.DataFrame:
        a, b = SPLITS[name]
        return full[(full["ny_date"] >= a) & (full["ny_date"] <= b)]

    train = slice_split("A_IS")
    val = slice_split("A_OOS")
    test = slice_split("C_OOS")

    X_train, y_train = train[FEATURES].values, train["y"].values
    X_val, y_val = val[FEATURES].values, val["y"].values
    X_test, y_test = test[FEATURES].values, test["y"].values

    scaler = StandardScaler().fit(X_train)
    model = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=0)
    model.fit(scaler.transform(X_train), y_train)

    # Predict for every dated row in `full` (replay window) and persist.
    X_full = full[FEATURES].values
    preds = model.predict(scaler.transform(X_full))
    probs = model.predict_proba(scaler.transform(X_full))[:, 1]
    pred_df = pd.DataFrame({
        "ny_date": full["ny_date"].astype(str).values,
        "ml_predict": preds.astype(int),
        "ml_prob_positive": probs.round(6),
    })
    pred_path = REPO / "data" / "ml_session_predictions.csv"
    pred_df.to_csv(pred_path, index=False)
    print(f"Saved predictions: {pred_path.relative_to(REPO)}  ({len(pred_df)} rows)")

    acc_val = accuracy_score(y_val, model.predict(scaler.transform(X_val)))
    acc_test = accuracy_score(y_test, model.predict(scaler.transform(X_test)))

    coefs = dict(zip(FEATURES, model.coef_[0]))
    top3 = sorted(coefs.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]

    base_rate_train = y_train.mean()
    base_rate_val = y_val.mean()
    base_rate_test = y_test.mean()

    return {
        "n_train": len(train),
        "n_val": len(val),
        "n_test": len(test),
        "base_rate_train_pct": round(100 * base_rate_train, 2),
        "base_rate_val_pct": round(100 * base_rate_val, 2),
        "base_rate_test_pct": round(100 * base_rate_test, 2),
        "acc_split_a_oos": round(acc_val, 4),
        "acc_split_c_oos": round(acc_test, 4),
        "coefs": {k: round(v, 4) for k, v in coefs.items()},
        "top3_by_abs_coef": [(k, round(v, 4)) for k, v in top3],
        "intercept": round(float(model.intercept_[0]), 4),
        "scaler_mean": dict(zip(FEATURES, [round(x, 4) for x in scaler.mean_])),
        "scaler_scale": dict(zip(FEATURES, [round(x, 4) for x in scaler.scale_])),
    }


def main() -> int:
    print("Replaying NQ/ES to collect trades...")
    trades, nq = replay_and_collect_trades()
    print(f"  Trades: {len(trades)}   NQ bars: {len(nq):,}")
    daily = build_session_frame(nq, trades)
    print(f"  Sessions: {len(daily)}  with trades: {int(daily['had_trade'].sum())}  "
          f"with positive trade: {int(daily['y'].sum())}")

    result = train_and_eval(daily)
    print()
    print("=== Phase 3.5 ML regime classifier — results ===")
    print(f"Sessions: train={result['n_train']}  val={result['n_val']}  test={result['n_test']}")
    print(f"Base rate (% positive sessions):")
    print(f"  Split A IS  : {result['base_rate_train_pct']}%")
    print(f"  Split A OOS : {result['base_rate_val_pct']}%")
    print(f"  Split C OOS : {result['base_rate_test_pct']}%")
    print()
    print(f"Accuracy Split A OOS (validation): {result['acc_split_a_oos']:.4f}")
    print(f"Accuracy Split C OOS (holdout)   : {result['acc_split_c_oos']:.4f}")
    print()
    print("Top-3 features (by |coef|):")
    for k, v in result["top3_by_abs_coef"]:
        print(f"  {k:25s}: {v:+.4f}")
    print()
    print("All coefficients:")
    for k, v in result["coefs"].items():
        print(f"  {k:25s}: {v:+.4f}")
    print(f"  {'intercept':25s}: {result['intercept']:+.4f}")

    decision_threshold = 0.55
    if result["acc_split_c_oos"] > decision_threshold:
        print(f"\nDECISION: Split C OOS accuracy "
              f"{result['acc_split_c_oos']:.4f} > {decision_threshold} — "
              f"ADD MLRegimeGate.")
        gate_decision = "ADD"
    else:
        print(f"\nDECISION: Split C OOS accuracy "
              f"{result['acc_split_c_oos']:.4f} <= {decision_threshold} — "
              f"ML not significant; TrendRegimeGate retained.")
        gate_decision = "REJECT"

    out = REPO / "results" / "phase35_ml_regime.json"
    out.write_text(json.dumps({"decision": gate_decision, **result}, indent=2))
    print(f"Saved: {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
