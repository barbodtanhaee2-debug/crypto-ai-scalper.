# Stage 3.1 — Stale-decision investigation

Paper-only. Production freshness remains **3.0 s**. Original capture files were not modified.

## Dataset

- dataset_id: `20261030T191728Z-4751556f` (catalog bind of Stage 2.3 extended session)
- candles: 725
- quotes: 2787240
- reconnect gaps: 1
- median(kline E − host receive): 0.053371

## Counts

| Clock | Stale candle decisions |
| --- | ---: |
| kline_event (Stage 2.4 policy) | 69 |
| host_receive (lab correction) | 1 |

Stage 2.4 reported **69** stale decisions on this session. This investigation's kline_event count is **69**.

| Classification | Count |
| --- | ---: |
| Negative age (E before last quote host time) | 68 |
| Age > 3 s versus kline E | 1 |
| Stale on E but fresh on host clock | 68 |
| Stale on E and still stale on host clock | 1 |
| Candles with E before host receive | 138 |

Stale age versus kline E: `{'count': 69, 'min': -2.138994, 'median': -0.045598, 'max': 6.943652}`

Host-clock age on those same bars: `{'count': 69, 'min': 3.4e-05, 'median': 7.4e-05, 'max': 6.829929}`

## Root cause

The 3 s freshness rule is applied to (kline E − host quote received_at). Those clocks are not the same. Negative ages fail closed; ages above 3 s also fail. This is mixed-clock replay synchronization, not proof that the quote stream was idle for 3 s, and not a reason to widen the production freshness threshold.

## Correction (lab only)

Default research clock is **host_receive**. Do not widen the 3 s threshold. Do not apply the kline median offset to quotes. Do not invent bookTicker E. Do not drop stale decisions.

Residual host-clock stale bars are treated as true missing/delayed quotes (including the preserved reconnect gap), not as a clock artefact.
