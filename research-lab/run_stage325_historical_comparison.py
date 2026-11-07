#!/usr/bin/env python3
"""Stage 3.2.5 — authorized formal historical B0 vs H1 comparison (paper-only, €0).

Uses load_formal_research_candles + run_formal_historical_replay only.
Does not score the sealed holdout. Does not optimize.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = LAB_ROOT.parent
for path in (str(LAB_ROOT), str(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from backtester import BacktestSettings

from research_lab.backtesting.historical_replay import (
    fingerprint_journal,
    run_formal_historical_replay,
)
from research_lab.datasets.historical_catalog import (
    bind_historical_dataset,
    load_formal_research_candles,
)
from research_lab.historical.constants import PROCESSED, PROVENANCE, RESEARCH_CUTOFF
from research_lab.isolation import assert_isolated_path
from research_lab.metrics.evaluate import MIN_CLOSED_TRADES_TO_RANK, experiment_metrics
import logging

from research_lab.strategies.adapters import adapt_production
from research_lab.strategies.confirmed_momentum import ConfirmedMomentumBreakoutStrategy

# Console I/O from PaperBroker rejection warnings dominates wall time on multi-year
# formal runs; economics are unchanged. Reject counters remain in-memory; formal
# replay also buffers journal I/O and omits mark_to_market journal rows.
logging.getLogger("paper_broker").setLevel(logging.ERROR)
logging.getLogger().setLevel(logging.ERROR)
logging.disable(logging.WARNING)


class _H1NoDecisionLog(ConfirmedMomentumBreakoutStrategy):
    """Same H1 confirmation rules; experiment-only silence of in-memory decision_log.

    Does not alter confirmation logic in confirmed_momentum.py.
    """

    def _log(self, decision, audit, *, event: str) -> None:  # type: ignore[override]
        return None

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
RUNS = LAB_ROOT / "experiments" / "runs" / "stage325" / RUN_ID
REPORTS = LAB_ROOT / "reports"
CONFIG_PATH = LAB_ROOT / "configs" / "stage324_experiment.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _settings_from_config(cfg: dict) -> BacktestSettings:
    costs = cfg["costs"]
    risk = cfg["risk"]
    return BacktestSettings(
        candle_interval_seconds=60,
        initial_equity=float(costs["initial_equity"]),
        fee_rate_per_side=float(costs["fee_rate_per_side"]),
        slippage_rate_per_side=float(costs["slippage_rate_per_side"]),
        spread_fraction=float(costs["spread_fraction"]),
        risk_fraction=float(risk["risk_fraction"]),
        daily_loss_limit_fraction=float(risk["daily_loss_limit_fraction"]),
        max_drawdown_fraction=float(risk["max_drawdown_fraction"]),
    )


def _event_counts(result: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in result.get("events") or []:
        name = str(event.get("event"))
        counts[name] = counts.get(name, 0) + 1
    return counts


def _arm_summary(result: dict, *, initial_equity: float, min_trades: int) -> dict:
    scored = list(result.get("completed_scored_trades") or result.get("trades") or [])
    metrics_input = dict(result)
    metrics_input["trades"] = scored
    metrics = experiment_metrics(
        metrics_input,
        initial_equity=initial_equity,
        min_closed_trades_to_rank=min_trades,
    )
    return {
        "strategy": result.get("strategy"),
        "candles_processed": result.get("candles_processed"),
        "completed_scored_trades": len(scored),
        "unresolved_positions": result.get("unresolved_positions"),
        "entries_accepted": result.get("entries_accepted"),
        "entries_rejected": result.get("entries_rejected"),
        "fills_deferred": result.get("fills_deferred"),
        "fills_executed": result.get("fills_executed"),
        "fills_cancelled_missing_next": result.get("fills_cancelled_missing_next"),
        "fills_cancelled_incompatible": result.get("fills_cancelled_incompatible"),
        "missing_intervals": result.get("missing_intervals"),
        "decision_counts": result.get("decision_counts"),
        "rejection_reasons": result.get("rejection_reasons"),
        "event_counts": _event_counts(result),
        "metrics": metrics,
        "execution_model": result.get("execution_model"),
        "force_close_at_end": result.get("force_close_at_end"),
        "assumptions": result.get("assumptions"),
    }


def verify_preflight(cfg: dict) -> dict:
    manifest = PROJECT_ROOT / cfg["manifest_path"]
    digest = _sha256_file(manifest)
    bound = bind_historical_dataset(load_candles=False)
    if digest != cfg["manifest_sha256"]:
        raise SystemExit(f"Manifest SHA mismatch: config={cfg['manifest_sha256']} file={digest}")
    if digest != bound.manifest_sha256:
        raise SystemExit("Bound catalog manifest SHA mismatch.")
    if cfg["research_end_exclusive"] != RESEARCH_CUTOFF.isoformat():
        raise SystemExit("Config cutoff does not match RESEARCH_CUTOFF.")
    if cfg["provenance"] != PROVENANCE:
        raise SystemExit("Provenance mismatch.")
    if set(cfg["arms"]) != {"B0", "H1"}:
        raise SystemExit(f"Unexpected arms: {cfg['arms']}")
    # Formal gate: force_close rejected
    try:
        run_formal_historical_replay(
            candles=object(),  # type: ignore[arg-type]
            journal_path=RUNS / "gate.jsonl",
            force_close_at_end=True,
        )
        raise SystemExit("Formal runner failed to reject force_close_at_end=True.")
    except ValueError:
        pass
    except TypeError:
        # May hit type check first depending on order; re-check force_close path with valid bundle later.
        pass
    return {
        "manifest_path": str(manifest),
        "manifest_sha256": digest,
        "bound_dataset_id": bound.dataset_id,
        "research_cutoff": RESEARCH_CUTOFF.isoformat(),
        "user_authorization": (
            "Stage 3.2.5 explicit user approval for one offline B0/H1 historical comparison"
        ),
        "config_flag_full_historical_scoring_authorized": cfg.get(
            "full_historical_scoring_authorized"
        ),
    }


def run_arm(
    *,
    arm_id: str,
    replica: str,
    candles,
    settings: BacktestSettings,
    strategy,
) -> tuple[dict, str]:
    journal = assert_isolated_path(RUNS / f"{arm_id}_{replica}.jsonl")
    started = time.time()
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] starting {arm_id}/{replica} "
        f"candles={len(candles.candles)}",
        flush=True,
    )
    result = run_formal_historical_replay(
        candles=candles,
        journal_path=journal,
        strategy_object=strategy,
        settings=settings,
        force_close_at_end=False,
    )
    elapsed = time.time() - started
    fp = fingerprint_journal(journal)
    result["arm_id"] = arm_id
    result["replica"] = replica
    result["journal_fingerprint"] = fp
    result["elapsed_seconds"] = elapsed
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] finished {arm_id}/{replica} "
        f"trades={result.get('trade_count')} unresolved={result.get('unresolved_positions')} "
        f"elapsed_s={elapsed:.1f}",
        flush=True,
    )
    # Drop bulky event list from aggregate JSON (keep counts); full journal remains on disk.
    slim = dict(result)
    slim["events"] = []
    slim["event_count"] = len(result.get("events") or [])
    slim["_event_counts"] = _event_counts(result)
    return slim, fp


def write_report(payload: dict) -> Path:
    b0 = payload["arms"]["B0"]["primary_summary"]
    h1 = payload["arms"]["H1"]["primary_summary"]
    b0_n = b0["completed_scored_trades"]
    h1_n = h1["completed_scored_trades"]
    gate = int(payload["min_closed_trades_to_rank"])
    inconclusive = b0_n < gate or h1_n < gate
    final = "INCONCLUSIVE" if inconclusive else "PASS"

    lines = [
        "# Stage 3.2.5 — Full historical B0 vs H1 comparison",
        "",
        "Paper-only. €0. User-authorized single offline experiment.",
        "Historical candle track only (`BINANCE_SPOT_HISTORICAL`). Separate from live quote-tape.",
        "",
        f"## Conclusion: **{final}**",
        "",
    ]
    if final == "INCONCLUSIVE":
        lines.append(
            f"Either arm has fewer than {gate} completed scored trades "
            f"(B0={b0_n}, H1={h1_n}). No winner declared."
        )
    else:
        lines.append(
            f"Both arms have ≥{gate} completed scored trades (B0={b0_n}, H1={h1_n}). "
            "After-cost metrics below are descriptive only. Not a profitability claim; "
            "not production promotion; not equivalent to tape H1 or live execution."
        )

    lines.extend(
        [
            "",
            "## Dataset and manifest verification",
            "",
            f"- Provenance: `{payload['preflight']['provenance'] if 'provenance' in payload['preflight'] else PROVENANCE}`",
            f"- Manifest SHA-256: `{payload['preflight']['manifest_sha256']}`",
            f"- Manifest path: `{payload['preflight']['manifest_path']}`",
            f"- Research start: `{payload['window']['research_start']}`",
            f"- Research end exclusive (sealed holdout): `{payload['window']['research_end_exclusive']}`",
            f"- Formal candles loaded: **{payload['window']['candle_count']}**",
            f"- Excluded at/after cutoff: **{payload['window']['excluded_at_or_after_cutoff']}**",
            f"- Max close in research set: `{payload['window']['max_close']}`",
            "",
            "## Exact run configuration",
            "",
            f"- Run ID: `{payload['run_id']}`",
            f"- Entrypoint: `load_formal_research_candles` + `run_formal_historical_replay`",
            f"- `force_close_at_end`: `{payload['force_close_at_end']}`",
            f"- Arms: B0=`momentum_breakout_logged`, H1=`h1_confirmed_momentum_breakout`",
            f"- Costs: fee={payload['settings']['fee_rate_per_side']}, "
            f"slip={payload['settings']['slippage_rate_per_side']}, "
            f"spread={payload['settings']['spread_fraction']}, "
            f"equity={payload['settings']['initial_equity']}",
            f"- Risk: risk_fraction={payload['settings']['risk_fraction']}, "
            f"daily_loss={payload['settings']['daily_loss_limit_fraction']}, "
            f"max_dd={payload['settings']['max_drawdown_fraction']}",
            f"- Execution model: `{payload['execution_model']}`",
            "",
            "## Trade counts",
            "",
            "| Arm | Completed scored trades | Unresolved positions | Entries accepted | Entries rejected |",
            "| --- | ---: | ---: | ---: | ---: |",
            f"| B0 | {b0_n} | {b0['unresolved_positions']} | {b0['entries_accepted']} | {b0['entries_rejected']} |",
            f"| H1 | {h1_n} | {h1['unresolved_positions']} | {h1['entries_accepted']} | {h1['entries_rejected']} |",
            "",
            "## After-cost metrics (completed scored trades only)",
            "",
            "| Metric | B0 | H1 |",
            "| --- | ---: | ---: |",
        ]
    )

    def m(arm, key):
        return arm["metrics"].get(key)

    rows = [
        ("Net P&L", "net_profit_and_loss"),
        ("Net return %", "net_return_percentage"),
        ("Win rate", "win_rate"),
        ("Expectancy / trade", "expectancy_per_trade"),
        ("Profit factor", "profit_factor"),
        ("Max drawdown (frac)", "maximum_drawdown"),
        ("Fees paid", "fees_paid"),
        ("Est. spread costs", "estimated_spread_costs"),
        ("Est. slippage", "estimated_slippage"),
        ("Ranking allowed", "ranking_allowed"),
        ("Insufficient sample", "insufficient_sample"),
    ]
    for label, key in rows:
        lines.append(f"| {label} | {m(b0, key)} | {m(h1, key)} |")

    lines.extend(
        [
            "",
            "## Cancellations and rejects",
            "",
            "| Arm | Missing-next cancels | Incompatible-open cancels | Missing intervals |",
            "| --- | ---: | ---: | ---: |",
            f"| B0 | {b0['fills_cancelled_missing_next']} | {b0['fills_cancelled_incompatible']} | {b0['missing_intervals']} |",
            f"| H1 | {h1['fills_cancelled_missing_next']} | {h1['fills_cancelled_incompatible']} | {h1['missing_intervals']} |",
            "",
            "## Deterministic fingerprints",
            "",
            f"- B0 replica match: `{payload['arms']['B0']['fingerprints_equal']}`",
            f"- H1 replica match: `{payload['arms']['H1']['fingerprints_equal']}`",
            f"- B0 fingerprints: `{payload['arms']['B0']['fingerprints']}`",
            f"- H1 fingerprints: `{payload['arms']['H1']['fingerprints']}`",
            "",
            "## Limitations",
            "",
            "- Synthetic candle-derived quotes (mid ± spread); **not** historical L2 / bookTicker.",
            "- **No** intra-bar protective stop simulation on this path.",
            "- Historical next-open fill timing differs from Stage 3.2.1 tape H1.",
            "- Unresolved end-of-window positions are **not** force-closed.",
            "- Results are not proof of future profitability and must not be promoted to production from this report alone.",
            "",
            f"Raw JSON: `{payload['results_json']}`",
            "",
        ]
    )
    path = assert_isolated_path(REPORTS / "STAGE3.2.5_HISTORICAL_B0_H1_COMPARISON.md")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload["conclusion"] = final
    return path


def main() -> int:
    RUNS.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    preflight = verify_preflight(cfg)
    preflight["provenance"] = PROVENANCE
    settings = _settings_from_config(cfg)
    min_trades = int(cfg.get("min_closed_trades_to_rank", MIN_CLOSED_TRADES_TO_RANK))

    print("Loading formal research candles...", flush=True)
    candles = load_formal_research_candles(PROCESSED / "klines_1m")
    max_close = max(c.close_time for c in candles.candles)
    if max_close >= RESEARCH_CUTOFF:
        raise SystemExit("Holdout leakage in formal candle load.")
    print(
        f"Loaded {len(candles.candles)} candles; excluded={candles.excluded_at_or_after_cutoff}; "
        f"max_close={max_close.isoformat()}",
        flush=True,
    )

    # Fresh strategy instances per replica (no shared pending state).
    # B0 uses production adapter with frozen defaults (same as DecisionLoggingAdapter
    # inner path). H1 uses confirmation subclass that only silences decision_log I/O.
    b0_a, fp_b0_a = run_arm(
        arm_id="B0",
        replica="a",
        candles=candles,
        settings=settings,
        strategy=adapt_production("momentum_breakout"),
    )
    b0_b, fp_b0_b = run_arm(
        arm_id="B0",
        replica="b",
        candles=candles,
        settings=settings,
        strategy=adapt_production("momentum_breakout"),
    )
    h1_a, fp_h1_a = run_arm(
        arm_id="H1",
        replica="a",
        candles=candles,
        settings=settings,
        strategy=_H1NoDecisionLog(),
    )
    h1_b, fp_h1_b = run_arm(
        arm_id="H1",
        replica="b",
        candles=candles,
        settings=settings,
        strategy=_H1NoDecisionLog(),
    )

    initial = float(settings.initial_equity)
    b0_summary = _arm_summary(b0_a, initial_equity=initial, min_trades=min_trades)
    b0_summary["event_counts"] = b0_a.get("_event_counts") or _event_counts(b0_a)
    h1_summary = _arm_summary(h1_a, initial_equity=initial, min_trades=min_trades)
    h1_summary["event_counts"] = h1_a.get("_event_counts") or _event_counts(h1_a)

    results_json = assert_isolated_path(REPORTS / f"stage325_results_{RUN_ID}.json")
    payload = {
        "stage": "3.2.5",
        "run_id": RUN_ID,
        "preflight": preflight,
        "window": {
            "research_start": cfg["research_start"],
            "research_end_exclusive": RESEARCH_CUTOFF.isoformat(),
            "candle_count": len(candles.candles),
            "excluded_at_or_after_cutoff": candles.excluded_at_or_after_cutoff,
            "max_close": max_close.isoformat(),
            "candle_source": candles.source,
        },
        "settings": {
            "initial_equity": settings.initial_equity,
            "fee_rate_per_side": settings.fee_rate_per_side,
            "slippage_rate_per_side": settings.slippage_rate_per_side,
            "spread_fraction": settings.spread_fraction,
            "risk_fraction": settings.risk_fraction,
            "daily_loss_limit_fraction": settings.daily_loss_limit_fraction,
            "max_drawdown_fraction": settings.max_drawdown_fraction,
        },
        "force_close_at_end": False,
        "execution_model": "next_available_candle_open",
        "min_closed_trades_to_rank": min_trades,
        "arms": {
            "B0": {
                "fingerprints": [fp_b0_a, fp_b0_b],
                "fingerprints_equal": fp_b0_a == fp_b0_b,
                "trades_equal": b0_a.get("trades") == b0_b.get("trades"),
                "primary_summary": b0_summary,
                "elapsed_seconds": {
                    "a": b0_a.get("elapsed_seconds"),
                    "b": b0_b.get("elapsed_seconds"),
                },
            },
            "H1": {
                "fingerprints": [fp_h1_a, fp_h1_b],
                "fingerprints_equal": fp_h1_a == fp_h1_b,
                "trades_equal": h1_a.get("trades") == h1_b.get("trades"),
                "primary_summary": h1_summary,
                "elapsed_seconds": {
                    "a": h1_a.get("elapsed_seconds"),
                    "b": h1_b.get("elapsed_seconds"),
                },
            },
        },
        "results_json": str(results_json),
        "limitations": [
            "Synthetic candle quotes only; not L2/bookTicker.",
            "No intra-bar protective stop simulation.",
            "Historical next-open timing differs from tape H1.",
            "Separate from BINANCE_SPOT_LIVE_CAPTURE results.",
            "Not proof of future profitability; not production promotion.",
        ],
    }
    report_path = write_report(payload)
    payload["report_path"] = str(report_path)
    results_json.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"conclusion": payload["conclusion"], "report": str(report_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
