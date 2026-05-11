# nasdaq-ale-bot

Mechanical ICT trading bot replicating the NasdaqAle YouTube strategy via a deterministic 6-state engine. Primary instrument is QQQ with SPY as the correlated SMT pair, configurable via `config/instruments.yaml`. Detection layer is pure-function and look-ahead-proof; backtest and live paper share one `StateMachine.on_bar` code path.

```
pip install -e .[dev]
pytest
```

See [PLAN.md](PLAN.md) for the full implementation plan and acceptance criteria.
