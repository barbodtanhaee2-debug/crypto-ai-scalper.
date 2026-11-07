# Research laboratory (Stage 3.1)

Isolated BTCUSDT Spot scalping **research** environment. It does not change production trading, frozen baselines, or original quote-tape captures.

Import package: `research_lab` (this folder is named `research-lab`).

```
research-lab/
  research_lab/     Python package (datasets, strategies, backtesting, metrics, experiments)
  datasets/         catalog pointers only — no tape copies
  configs/          frozen experiment JSON and future hypotheses
  experiments/runs/ isolated journals
  reports/          Stage 3.1 reports
  tests/
```

Decision clocks:

- `kline_event` — reproduces Stage 2.4 mixed kline `E` vs host quote receive.
- `host_receive` — lab default: freshness on the host clock. The 3 s threshold is **not** increased.

The demo strategy `demo_close_follow` only proves the interface. It is not a candidate and is not profitable evidence.
