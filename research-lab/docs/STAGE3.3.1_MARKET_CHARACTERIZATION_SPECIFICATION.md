# Stage 3.3.1 — Market Characterization & Opportunity Detection Specification

Lab-only research intelligence for BTC/USDT paper scalping.  
**Not** an execution engine. **Not** a profitability or predictive-accuracy claim.

## Architecture

Package: `research_lab.characterization`

| Module | Responsibility |
| --- | --- |
| `candles.py` | Minimal OHLC(+optional volume) candle type |
| `config.py` | Provisional thresholds and cost defaults |
| `models.py` | Enums and immutable result dataclasses |
| `data_quality.py` | Explicit validation; no repair/fabrication |
| `features.py` | Deterministic feature calculation at close |
| `primary.py` | Primary regime raw labels |
| `secondary.py` | Overlapping secondary characteristics |
| `stability.py` | Persistence / hysteresis / gap reset |
| `opportunity.py` | Cost-adjusted research filter |
| `pipeline.py` | Sequential no-look-ahead orchestration |

Three separate concepts are always distinguished:

1. **Primary regime** — structural TRENDING / RANGING / TRANSITIONING / UNKNOWN  
2. **Secondary characteristics** — volatility, momentum, directional strength (overlapping)  
3. **Opportunity quality** — cost-adjusted movement proxy filter  

## Primary regimes

| Label | Meaning |
| --- | --- |
| `TRENDING` | High directional efficiency + EMA separation + meaningful range span |
| `RANGING` | Low efficiency + bounded rolling range width |
| `TRANSITIONING` | Intermediate structure, conflicting evidence, or awaiting confirmation |
| `UNKNOWN` | Insufficient / invalid / non-finite features |

R3 (high-vol expansion) and R4 (low-vol compression) are **not** primary regimes. They appear only as secondary volatility levels so they can coexist with TRENDING or RANGING.

## Secondary characteristics

Independent of primary:

- Volatility: `HIGH_VOLATILITY` / `NORMAL_VOLATILITY` / `LOW_VOLATILITY` / `UNKNOWN`  
- Momentum: `STRONG_UP` / `STRONG_DOWN` / `WEAK` / `UNKNOWN`  
- Directional strength: `STRONG` / `MODERATE` / `WEAK` / `UNKNOWN`  

NATR is the primary volatility signal; realized volatility confirms or conflicts. True NATR↔RVOL contradiction → `UNKNOWN` volatility (not forced).

## Feature definitions

All features use only information available at the current candle close (bars `0..i`).

### ATR / normalized ATR

- True range = max(high−low, |high−prev_close|, |low−prev_close|)  
- ATR = Wilder RMA over `atr_period` (default 14)  
- Normalized ATR = ATR / close  
- Warm-up: ≥ `atr_period + 1` closes  
- Units: price / fraction of price  
- Missing/non-finite → `None` / `non_finite_detected`

### Realized volatility

- StdDev of log returns over `realized_vol_period` (default 20)  
- Bar-scale (not annualized)  
- Units: dimensionless  

### Directional efficiency

- `|close − close_N| / Σ|Δclose|` over `efficiency_period` (default 20)  
- Units: [0, 1]  

### EMA structure

- EMA periods `ema_fast=8`, `ema_slow=21`, SMA seed  
- `ema_separation = |ema_fast − ema_slow| / close`  

### Rolling high/low / range width

- Lookback `range_lookback=20`  
- `range_width = (rolling_high − rolling_low) / mid`  

### Volume deviation (optional)

- Disabled by default (`use_volume=False`)  
- When enabled: `volume / SMA(volume) − 1`  

## Classification logic (provisional thresholds)

Defaults live in `config.py` and are labelled **provisional** (not historically optimized).

**Primary raw**

- TRENDING if efficiency ≥ `trend_efficiency_min` (0.35) **and** EMA separation ≥ `ema_separation_min` (0.0015) **and** range width ≥ `trend_range_width_min` (0.008)  
- RANGING if efficiency ≤ `range_efficiency_max` (0.20) **and** range width ≤ `range_width_max` (0.025)  
- Both true → TRANSITIONING + `conflicting=True`  
- Neither → TRANSITIONING  
- Missing features → UNKNOWN  

**Confirmed primary** (stability)

- Adopt raw label after `min_persistence_bars` (3) consecutive matches from UNKNOWN  
- Switch away from an established regime only after `min_persistence_bars + hysteresis_bars` (3+2)  
- Gap ≥ `gap_reset_intervals` (2) resets confirmed state to UNKNOWN  
- Past outputs are never rewritten  

While awaiting confirmation, the emitted primary regime is `TRANSITIONING`.

## Cost-adjusted opportunity methodology

Reuses paper research cost assumptions from `BacktestSettings` defaults:

| Component | Default | Role |
| --- | --- | --- |
| `fee_rate_per_side` | 0.001 | ×2 round trip |
| `spread_fraction` | 0.0002 | full synthetic spread once |
| `slippage_rate_per_side` | 0.0005 | ×2 round trip |

Gross move proxy = `normalized_atr * atr_move_multiplier` (default 1.0).  
This is **not** a forecast and **not** historical MFE.

Statuses:

- `OPPORTUNITY` — net ≥ `minimum_net_edge_fraction` (0.001)  
- `REJECTED_COSTS_TOO_HIGH` — gross/net insufficient after costs  
- `NO_OPPORTUNITY` — non-positive gross  
- `INCOMPLETE_COST_INFORMATION` — costs unavailable/invalid  
- `UNAVAILABLE` — features insufficient  

Incomplete costs **cannot** produce a confident positive opportunity.

## Data-quality handling

Checks: empty series, naive/invalid timestamps, duplicates, non-monotonic times, irregular intervals, missing intervals (gaps), invalid OHLC, non-positive/non-finite prices, optional volume, insufficient warm-up.

Invalid input → fail closed (`UNKNOWN` / `UNAVAILABLE`).  
No silent repair or fabrication.

## Known limitations

- Provisional thresholds are not empirically tuned.  
- ATR-based gross move is a filter proxy, not expected value.  
- Synthetic quotes ≠ live L2.  
- Volume features off unless candles carry volume and `use_volume=True`.  
- Does not generate orders or modify PaperBroker.  
- Does not establish profitability, predictive accuracy, or strategy superiority.  

## What this implementation does not establish

- Any claim that regimes predict future returns.  
- Optimal scalping parameters.  
- Authorization to run historical scoring or holdout evaluation.  
- Changes to B0/H1, Stage 2 replay, or production modules.  
