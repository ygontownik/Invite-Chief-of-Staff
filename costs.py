#!/usr/bin/env python3
"""
costs.py — Anthropic API spend tracker.

Reads ~/dashboards/data/anthropic-usage.jsonl (written by _usage.py on every
API call) and reports daily/weekly/monthly spend with per-script and per-model
breakdowns.

Also exposed as a JSON endpoint at GET /api/costs (handled by
cos-dashboard-server.py) and rendered as a dashboard tile.

Usage:
    python3 costs.py                   # last 7 days summary (default)
    python3 costs.py --days 30         # last N days
    python3 costs.py --json            # JSON output (for the dashboard tile)
    python3 costs.py --by-script       # breakdown by site/script
    python3 costs.py --by-model        # breakdown by model
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

USAGE_LOG = Path.home() / "dashboards" / "data" / "anthropic-usage.jsonl"

# Anthropic pricing as of 2026-04 (per 1M tokens, USD).
# Cache read = 10% of input, cache write = 125% of input.
PRICING = {
    "claude-opus-4-7":            {"in": 15.00, "out": 75.00},
    "claude-opus-4-5":            {"in": 15.00, "out": 75.00},
    "claude-sonnet-4-6":          {"in":  3.00, "out": 15.00},
    "claude-sonnet-4-5":          {"in":  3.00, "out": 15.00},
    "claude-haiku-4-5":           {"in":  0.80, "out":  4.00},
    "claude-haiku-4-5-20251001":  {"in":  0.80, "out":  4.00},
    "claude-3-5-sonnet-20241022": {"in":  3.00, "out": 15.00},
    "claude-3-5-haiku-20241022":  {"in":  0.80, "out":  4.00},
}
DEFAULT_PRICING = {"in": 3.00, "out": 15.00}  # fallback to Sonnet if model unknown


def load_usage(days: int = 7) -> list[dict]:
    """Load usage entries from the last `days` days."""
    if not USAGE_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    entries = []
    with open(USAGE_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
                if ts >= cutoff:
                    e["_dt"] = ts
                    entries.append(e)
            except Exception:
                continue
    return entries


def cost_for_entry(e: dict) -> float:
    """Compute USD cost for a single usage entry."""
    model = e.get("model", "")
    p = PRICING.get(model, DEFAULT_PRICING)
    in_tokens     = e.get("in", 0) or 0
    out_tokens    = e.get("out", 0) or 0
    cache_read    = e.get("cache_read", 0) or 0
    cache_create  = e.get("cache_create", 0) or 0

    # Cache reads = 10% of input price; cache writes = 125% of input price
    in_cost     = (in_tokens     / 1_000_000) * p["in"]
    out_cost    = (out_tokens    / 1_000_000) * p["out"]
    cread_cost  = (cache_read    / 1_000_000) * p["in"] * 0.10
    cwrite_cost = (cache_create  / 1_000_000) * p["in"] * 1.25

    return in_cost + out_cost + cread_cost + cwrite_cost


def aggregate(entries: list[dict]) -> dict:
    """Aggregate by day, by script, by model. Returns summary dict."""
    by_day = defaultdict(float)
    by_script = defaultdict(float)
    by_model = defaultdict(float)
    total = 0.0
    total_calls = 0
    total_in_tokens = 0
    total_out_tokens = 0

    for e in entries:
        c = cost_for_entry(e)
        day = e["_dt"].strftime("%Y-%m-%d")
        by_day[day] += c
        by_script[e.get("site", "unknown")] += c
        by_model[e.get("model", "unknown")] += c
        total += c
        total_calls += 1
        total_in_tokens += e.get("in", 0) or 0
        total_out_tokens += e.get("out", 0) or 0

    return {
        "total_usd": round(total, 4),
        "total_calls": total_calls,
        "total_in_tokens": total_in_tokens,
        "total_out_tokens": total_out_tokens,
        "by_day":    dict(sorted(by_day.items())),
        "by_script": dict(sorted(by_script.items(), key=lambda x: -x[1])),
        "by_model":  dict(sorted(by_model.items(),  key=lambda x: -x[1])),
    }


def project_monthly(by_day: dict) -> float:
    """Project monthly cost from average daily over the last 7 days."""
    if not by_day:
        return 0.0
    recent = list(by_day.values())[-7:]
    avg = sum(recent) / len(recent)
    return round(avg * 30, 2)


def print_human(summary: dict, days: int, by_script: bool, by_model: bool):
    """Pretty-print to terminal."""
    total = summary["total_usd"]
    monthly = project_monthly(summary["by_day"])

    print()
    print("═" * 60)
    print(f"  Anthropic API spend — last {days} days")
    print("═" * 60)
    print()
    print(f"  Total:       ${total:7.2f}")
    print(f"  Calls:       {summary['total_calls']:>7}")
    print(f"  Input tokens:  {summary['total_in_tokens']:>10,}")
    print(f"  Output tokens: {summary['total_out_tokens']:>10,}")
    print(f"  Projected /mo: ${monthly:>7.2f}")
    print()

    print("  Daily breakdown:")
    if not summary["by_day"]:
        print("    (no usage in window)")
    else:
        max_day_cost = max(summary["by_day"].values())
        for day, cost in summary["by_day"].items():
            bar_len = int((cost / max_day_cost) * 30) if max_day_cost > 0 else 0
            bar = "▮" * bar_len + "▯" * (30 - bar_len)
            print(f"    {day}  ${cost:5.2f}  {bar}")

    if by_script:
        print()
        print("  By script (top 10):")
        for s, c in list(summary["by_script"].items())[:10]:
            print(f"    {s:<35} ${c:6.2f}")

    if by_model:
        print()
        print("  By model:")
        for m, c in summary["by_model"].items():
            print(f"    {m:<35} ${c:6.2f}")
    print()


_PIPELINE_ROOT = Path.home() / "cos-pipeline"


def load_subscription_dispatch(tenant: str, days: int = 7) -> list[dict]:
    """Load subscription dispatch ledger rows from the last `days` days.

    Reads ~/cos-pipeline/data-<tenant>/dispatch.jsonl, written by
    _model_router._record_subscription_call() on every subscription call.
    Subscription mode bills against the 5-hour window, not USD — these
    rows therefore have est_usd = 0.0; we surface them as a call counter
    and rate-limit-status snapshot, not a dollar figure.
    """
    path = _PIPELINE_ROOT / f"data-{tenant}" / "dispatch.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_str = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= cutoff:
            r["_dt"] = dt
            rows.append(r)
    return rows


def aggregate_subscription(rows: list[dict]) -> dict:
    """Aggregate subscription dispatch rows. Returns a counts/tokens summary."""
    if not rows:
        return {
            "total_calls": 0, "ok_calls": 0, "failed_calls": 0,
            "by_task_type": {}, "by_model": {}, "by_package": {},
            "tokens": {"input": 0, "output": 0,
                       "cache_creation": 0, "cache_read": 0},
            "latest_rate_limit_status": None,
            "latest_rate_limit_resets_at": None,
            "queue_depth": 0,
            "dead_letter_count": 0,
        }
    by_task = defaultdict(int)
    by_model = defaultdict(int)
    by_package = defaultdict(int)
    ok = failed = 0
    in_tok = out_tok = cc_tok = cr_tok = 0
    latest_rl = None
    latest_resets = None
    for r in rows:
        outcome = r.get("outcome", "")
        if outcome == "ok":
            ok += 1
        elif outcome.startswith("failure:"):
            failed += 1
        if r.get("task_type"):
            by_task[r["task_type"]] += 1
        if r.get("model"):
            by_model[r["model"]] += 1
        # Package is not stored on dispatch rows directly; can't bucket here.
        u = r.get("usage") or {}
        in_tok += int(u.get("input_tokens", 0) or 0)
        out_tok += int(u.get("output_tokens", 0) or 0)
        cc_tok += int(u.get("cache_creation_input_tokens", 0) or 0)
        cr_tok += int(u.get("cache_read_input_tokens", 0) or 0)
        rl = r.get("rate_limit_status")
        if rl and rl != "unknown":
            latest_rl = rl
            latest_resets = r.get("rate_limit_resets_at")
    return {
        "total_calls": len(rows), "ok_calls": ok, "failed_calls": failed,
        "by_task_type": dict(sorted(by_task.items(), key=lambda kv: -kv[1])),
        "by_model":     dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
        "by_package":   dict(by_package),  # always {} for now
        "tokens": {"input": in_tok, "output": out_tok,
                   "cache_creation": cc_tok, "cache_read": cr_tok},
        "latest_rate_limit_status": latest_rl,
        "latest_rate_limit_resets_at": latest_resets,
    }


def _queue_depth(tenant: str) -> int:
    p = _PIPELINE_ROOT / f"data-{tenant}" / "queue.jsonl"
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text().splitlines() if line.strip())


def _dead_letter_count(tenant: str) -> int:
    p = _PIPELINE_ROOT / f"data-{tenant}" / "queue.dead.jsonl"
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text().splitlines() if line.strip())


def print_subscription_panel(summary: dict, tenant: str) -> None:
    print()
    print("─" * 60)
    print(f"  Subscription dispatch — tenant={tenant}")
    print("─" * 60)
    print(f"  Calls:    {summary['total_calls']:>5}  "
          f"(ok={summary['ok_calls']}  failed={summary['failed_calls']})")
    t = summary["tokens"]
    print(f"  Tokens:   in={t['input']:>10,}  out={t['output']:>10,}")
    print(f"  Cache:    create={t['cache_creation']:>8,}  "
          f"read={t['cache_read']:>8,}")
    print(f"  Queue:    pending={summary['queue_depth']}  "
          f"dead={summary['dead_letter_count']}")
    print(f"  Rate-limit (latest): "
          f"status={summary['latest_rate_limit_status']}  "
          f"resets_at={summary['latest_rate_limit_resets_at']}")
    if summary["by_task_type"]:
        print()
        print("  By task_type:")
        for task, n in list(summary["by_task_type"].items())[:10]:
            print(f"    {task:<35} {n}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=7, help="Lookback window (default 7)")
    p.add_argument("--json", action="store_true", help="JSON output for dashboard")
    p.add_argument("--by-script", action="store_true", help="Show per-script breakdown")
    p.add_argument("--by-model",  action="store_true", help="Show per-model breakdown")
    p.add_argument("--tenant",    default=os.environ.get("COS_TENANT_SLUG", "default"),
                   help="Tenant slug for subscription dispatch panel "
                        "(default $COS_TENANT_SLUG or 'default')")
    p.add_argument("--no-subscription", action="store_true",
                   help="Suppress the subscription-dispatch panel")
    args = p.parse_args()

    entries = load_usage(args.days)
    summary = aggregate(entries)
    summary["projected_monthly_usd"] = project_monthly(summary["by_day"])
    summary["window_days"] = args.days

    # Subscription dispatch panel (new — no $ figure; counts + window state).
    if not args.no_subscription:
        sub_rows = load_subscription_dispatch(args.tenant, args.days)
        sub = aggregate_subscription(sub_rows)
        sub["queue_depth"] = _queue_depth(args.tenant)
        sub["dead_letter_count"] = _dead_letter_count(args.tenant)
        summary["subscription"] = {"tenant": args.tenant, **sub}

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print_human(summary, args.days, args.by_script, args.by_model)
        if not args.no_subscription:
            print_subscription_panel(summary["subscription"], args.tenant)


if __name__ == "__main__":
    main()
