"""measure.py — measurement harness for the cached static-core architecture.

Loads three workload fixtures (synthetic, in fixtures/), and for each
(fixture, model) pair runs 5 sequential calls via cached_client.complete().
Aggregates cache metrics, latency, and computes per-call cost using
published Anthropic rates. Writes MEASUREMENT_REPORT.md.

Run: python3 measure.py
Cost: ~30 API calls; rough estimate < $0.30 total at the configured max_tokens.
"""
import json
import pathlib
import statistics
import time
from typing import Any

from cached_client import complete

_HERE = pathlib.Path(__file__).resolve().parent
_FIXTURES_DIR = _HERE / "fixtures"
_REPORT_FILE = _HERE / "MEASUREMENT_REPORT.md"

# Published Anthropic rates (per million tokens), as of 2026.
RATES = {
    "claude-opus-4-7":   {"input": 5.0,  "output": 25.0},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0},
}
# Multipliers on the input rate.
CACHE_WRITE_MULT = 1.25  # 5-min ephemeral cache writes.
CACHE_READ_MULT  = 0.10  # 90% discount on cache reads.

FIXTURES = ["podcast_snippet", "briefing_email_batch", "deal_screen"]
MODELS = ["claude-sonnet-4-6", "claude-opus-4-7"]
N_CALLS = 5
MAX_TOKENS = 1024  # cap to keep wall time + spend bounded.


def _load_fixture(name: str) -> dict[str, str]:
    path = _FIXTURES_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _cost(metrics: dict[str, int], model: str) -> dict[str, float]:
    """Compute USD cost for one call, broken down by component."""
    rates = RATES[model]
    inp = rates["input"] / 1_000_000
    out = rates["output"] / 1_000_000
    return {
        "uncached_input_cost": metrics["uncached_input"] * inp,
        "cache_creation_cost": metrics["creation"]      * inp * CACHE_WRITE_MULT,
        "cache_read_cost":     metrics["read"]          * inp * CACHE_READ_MULT,
        "output_cost":         metrics["output"]        * out,
        "total":               (metrics["uncached_input"] * inp
                                + metrics["creation"]    * inp * CACHE_WRITE_MULT
                                + metrics["read"]        * inp * CACHE_READ_MULT
                                + metrics["output"]      * out),
    }


def _uncached_baseline_cost(metrics: dict[str, int], model: str) -> float:
    """Hypothetical cost if every cached token had been a fresh input token."""
    rates = RATES[model]
    inp = rates["input"] / 1_000_000
    out = rates["output"] / 1_000_000
    total_input = metrics["uncached_input"] + metrics["creation"] + metrics["read"]
    return total_input * inp + metrics["output"] * out


def run_pair(fixture: dict[str, str], model: str) -> list[dict[str, Any]]:
    """Run N_CALLS sequential calls for one (fixture, model) pair."""
    rows = []
    for i in range(N_CALLS):
        t0 = time.monotonic()
        result = complete(
            user_query=fixture["user_query"],
            source_content=fixture["source_content"],
            tenant_bundle="ignored-bundle-arg",  # placeholder absent; arg is dead.
            model=model,
            max_tokens=MAX_TOKENS,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        metrics = result["cache_metrics"]
        cost = _cost(metrics, model)
        baseline = _uncached_baseline_cost(metrics, model)
        rows.append({
            "call_idx": i,
            "metrics": metrics,
            "cost": cost,
            "uncached_baseline_cost": baseline,
            "latency_ms": latency_ms,
        })
    return rows


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate metrics across N_CALLS for one (fixture, model) pair."""
    creations = [r["metrics"]["creation"] for r in rows]
    reads     = [r["metrics"]["read"] for r in rows]
    uncached  = [r["metrics"]["uncached_input"] for r in rows]
    outputs   = [r["metrics"]["output"] for r in rows]
    totals    = [r["cost"]["total"] for r in rows]
    baselines = [r["uncached_baseline_cost"] for r in rows]
    latencies = [r["latency_ms"] for r in rows]
    cache_total = [r["metrics"]["read"] + r["metrics"]["creation"] for r in rows]
    hit_rates = [
        (reads[i] / cache_total[i]) if cache_total[i] > 0 else 0.0
        for i in range(len(rows))
    ]
    return {
        "first_call_creation": creations[0],
        "first_call_read": reads[0],
        "subsequent_creation_mean": statistics.mean(creations[1:]) if len(creations) > 1 else 0,
        "subsequent_read_mean": statistics.mean(reads[1:]) if len(reads) > 1 else 0,
        "uncached_input_mean": statistics.mean(uncached),
        "output_mean": statistics.mean(outputs),
        "cost_per_call_mean": statistics.mean(totals),
        "uncached_baseline_per_call_mean": statistics.mean(baselines),
        "savings_pct": (1 - statistics.mean(totals) / statistics.mean(baselines)) * 100
                       if statistics.mean(baselines) > 0 else 0,
        "latency_ms_mean": statistics.mean(latencies),
        "latency_ms_p50": statistics.median(latencies),
        "cache_hit_rate_mean_after_first": (
            statistics.mean(hit_rates[1:]) if len(hit_rates) > 1 else 0
        ),
    }


def fmt_money(x: float) -> str:
    return f"${x:.4f}"


def fmt_pct(x: float) -> str:
    return f"{x:.1f}%"


def write_report(results: dict[tuple[str, str], dict[str, Any]]) -> None:
    """Render MEASUREMENT_REPORT.md."""
    lines = []
    lines.append("# Measurement Report — Static-Core Cached Prompt")
    lines.append("")
    lines.append("Generated by `measure.py`. Each (fixture, model) pair ran 5 sequential "
                 "calls; first call writes cache, calls 2–5 should read.")
    lines.append("")
    lines.append(f"- Models tested: `{MODELS[0]}`, `{MODELS[1]}`")
    lines.append(f"- Fixtures: {', '.join(FIXTURES)}")
    lines.append(f"- N_CALLS per pair: {N_CALLS}")
    lines.append(f"- max_tokens cap: {MAX_TOKENS}")
    lines.append(f"- Pricing: Opus 4.7 ${RATES['claude-opus-4-7']['input']}/${RATES['claude-opus-4-7']['output']} per M; "
                 f"Sonnet 4.6 ${RATES['claude-sonnet-4-6']['input']}/${RATES['claude-sonnet-4-6']['output']} per M; "
                 f"cache writes {CACHE_WRITE_MULT}×, cache reads {CACHE_READ_MULT}×")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Per-fixture / per-model summary")
    lines.append("")
    lines.append("| Fixture | Model | First-call cache_creation | Subseq cache_read mean | Cost/call (cached avg) | Cost/call (uncached baseline) | Savings | Hit rate (calls 2-5) | Latency p50 (ms) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for fname in FIXTURES:
        for model in MODELS:
            agg = results[(fname, model)]
            lines.append(
                f"| {fname} | {model} | "
                f"{agg['first_call_creation']:.0f} | "
                f"{agg['subsequent_read_mean']:.0f} | "
                f"{fmt_money(agg['cost_per_call_mean'])} | "
                f"{fmt_money(agg['uncached_baseline_per_call_mean'])} | "
                f"{fmt_pct(agg['savings_pct'])} | "
                f"{fmt_pct(agg['cache_hit_rate_mean_after_first'] * 100)} | "
                f"{agg['latency_ms_p50']:.0f} |"
            )
    lines.append("")
    lines.append("## Per-call detail")
    lines.append("")
    for fname in FIXTURES:
        lines.append(f"### {fname}")
        lines.append("")
        for model in MODELS:
            agg = results[(fname, model)]
            lines.append(f"**{model}**")
            lines.append("")
            lines.append("| Call | creation | read | uncached_input | output | total cost | latency (ms) |")
            lines.append("|---|---|---|---|---|---|---|")
            for r in agg["rows"]:
                m = r["metrics"]
                lines.append(
                    f"| {r['call_idx']} | "
                    f"{m['creation']} | {m['read']} | {m['uncached_input']} | {m['output']} | "
                    f"{fmt_money(r['cost']['total'])} | {r['latency_ms']} |"
                )
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Pass 3 economics (the load-bearing question)")
    lines.append("")
    lines.append("Pass 3 in `MODEL_ROUTER.md` is the IC memo production pass — currently routed "
                 "to Sonnet 4.6 ($3/$15) on the rationale that the format is constrained and "
                 "Sonnet is sufficient. The open question is whether moving Pass 3 to Opus 4.7 "
                 "would justify the cost given that the static core is now fully cached.")
    lines.append("")
    lines.append("Reading from the table above, the deal_screen fixture is the closest analog "
                 "to a Pass 3 workload (structured output from a constrained input). Compare the "
                 "Sonnet vs Opus cost-per-call numbers for `deal_screen`:")
    lines.append("")
    sonnet_deal = results[("deal_screen", "claude-sonnet-4-6")]
    opus_deal   = results[("deal_screen", "claude-opus-4-7")]
    delta = opus_deal["cost_per_call_mean"] - sonnet_deal["cost_per_call_mean"]
    ratio = opus_deal["cost_per_call_mean"] / sonnet_deal["cost_per_call_mean"] if sonnet_deal["cost_per_call_mean"] > 0 else float('inf')
    lines.append(f"- Sonnet cost/call: {fmt_money(sonnet_deal['cost_per_call_mean'])}")
    lines.append(f"- Opus cost/call: {fmt_money(opus_deal['cost_per_call_mean'])}")
    lines.append(f"- Delta per call: {fmt_money(delta)} ({ratio:.2f}× Sonnet)")
    lines.append("")
    lines.append("**Interpretation:** the multiple matters more than the absolute. If Opus output "
                 "quality on Pass 3 is materially better (subjective; needs eval), the absolute "
                 "delta in dollars is small. If Pass 3 quality on Sonnet is already adequate, "
                 "the multiple is overhead with no return.")
    lines.append("")
    lines.append("**Real-traffic ground truth needed:** these are synthetic measurements. "
                 "Token counts are realistic; output quality cannot be benchmarked synthetically. "
                 "The decision-relevant data is whether IC memos produced by Opus on real pipeline "
                 "deals are visibly better than Sonnet output. That requires a side-by-side eval "
                 "across 5–10 real Pass 3 cases, which this run cannot generate.")
    lines.append("")
    _REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    fixtures = {name: _load_fixture(name) for name in FIXTURES}
    results: dict[tuple[str, str], dict[str, Any]] = {}

    print(f"Running {len(FIXTURES)} fixtures × {len(MODELS)} models × {N_CALLS} calls "
          f"= {len(FIXTURES) * len(MODELS) * N_CALLS} total calls")
    print()

    for model in MODELS:  # group by model so each model's cache stays warm between fixtures
        for fname in FIXTURES:
            print(f"[{model}] {fname}: ", end="", flush=True)
            rows = run_pair(fixtures[fname], model)
            agg = aggregate(rows)
            agg["rows"] = rows
            results[(fname, model)] = agg
            print(f"creation_first={rows[0]['metrics']['creation']}, "
                  f"read_after={agg['subsequent_read_mean']:.0f}, "
                  f"cost/call={fmt_money(agg['cost_per_call_mean'])}, "
                  f"savings={fmt_pct(agg['savings_pct'])}")

    write_report(results)
    print()
    print(f"Report: {_REPORT_FILE}")


if __name__ == "__main__":
    main()
