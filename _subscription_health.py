#!/usr/bin/env python3
"""
_subscription_health.py — canary observability for subscription dispatch.

Reads ``data-<tenant>/dispatch.jsonl`` (and ``queue.jsonl`` /
``queue.dead.jsonl``) and prints a summary suited to "did the canary
soak clean?" review. Designed to be dashboard-data-style (also dumps
JSON via ``--json`` for the dashboard server to ingest).

USAGE

    # Quick text summary for the past 24h:
    python3 _subscription_health.py

    # Specific tenant + window:
    python3 _subscription_health.py --tenant=re-dev --hours=168

    # JSON output (dashboard ingestion):
    python3 _subscription_health.py --tenant=<slug> --json

WHAT IT REPORTS

  - Total subscription calls in the window.
  - Success / failure breakdown (outcomes from the ledger).
  - Latest seen rate-limit status + resets_at timestamp.
  - Per-package call distribution.
  - Per-task-type call distribution.
  - Queue depth (pending re-fires) and dead-letter count.
  - Project-id usage (how many calls landed on a configured project
    vs inlined the preamble).

Stdlib only. Cron-callable. Exits non-zero only on argument errors;
empty data is "0 calls" not failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import os as _os

_PIPELINE_ROOT = Path.home() / "cos-pipeline"
_DEFAULT_TENANT = _os.environ.get("COS_TENANT_SLUG", "default")
_DEFAULT_HOURS = 24


# ─────────────────────────────────────────────────────────────────────
# File paths.
# ─────────────────────────────────────────────────────────────────────

def _data_dir(tenant: str) -> Path:
    return _PIPELINE_ROOT / f"data-{tenant}"


def _dispatch_path(tenant: str) -> Path:
    return _data_dir(tenant) / "dispatch.jsonl"


def _queue_path(tenant: str) -> Path:
    return _data_dir(tenant) / "queue.jsonl"


def _dead_path(tenant: str) -> Path:
    return _data_dir(tenant) / "queue.dead.jsonl"


# ─────────────────────────────────────────────────────────────────────
# JSONL readers.
# ─────────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _parse_iso_ts(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────
# Aggregation.
# ─────────────────────────────────────────────────────────────────────

def summarize(tenant: str, *, hours: int = _DEFAULT_HOURS) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    dispatch = _read_jsonl(_dispatch_path(tenant))
    in_window = [r for r in dispatch
                 if (_parse_iso_ts(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]

    outcomes = Counter(r.get("outcome", "unknown") for r in in_window)
    by_task = Counter(r.get("task_type") for r in in_window if r.get("task_type"))
    by_model = Counter(r.get("model") for r in in_window if r.get("model"))

    # Latest rate-limit signal from any row (window or not).
    latest_rl = None
    latest_resets_at = None
    if dispatch:
        for r in reversed(dispatch):
            status = r.get("rate_limit_status")
            if status and status != "unknown":
                latest_rl = status
                latest_resets_at = r.get("rate_limit_resets_at")
                break

    # Project-id coverage — how many calls had a project_id assigned.
    with_project = sum(1 for r in in_window if r.get("project_id"))
    without_project = len(in_window) - with_project

    queue_depth = len(_read_jsonl(_queue_path(tenant)))
    dead_count = len(_read_jsonl(_dead_path(tenant)))

    # Aggregate token usage (subscription mode = telemetry only, no $).
    total_input = total_output = total_cache_creation = total_cache_read = 0
    for r in in_window:
        u = r.get("usage") or {}
        total_input        += int(u.get("input_tokens", 0) or 0)
        total_output       += int(u.get("output_tokens", 0) or 0)
        total_cache_creation += int(u.get("cache_creation_input_tokens", 0) or 0)
        total_cache_read   += int(u.get("cache_read_input_tokens", 0) or 0)

    return {
        "tenant": tenant,
        "hours_window": hours,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "calls": {
            "total": len(in_window),
            "by_outcome": dict(outcomes),
            "by_task_type": dict(by_task.most_common()),
            "by_model": dict(by_model.most_common()),
        },
        "rate_limit": {
            "latest_status": latest_rl,
            "latest_resets_at": latest_resets_at,
        },
        "projects": {
            "with_project_id": with_project,
            "preamble_inlined": without_project,
        },
        "queue": {
            "pending": queue_depth,
            "dead_letter": dead_count,
        },
        "tokens": {
            "input": total_input,
            "output": total_output,
            "cache_creation": total_cache_creation,
            "cache_read": total_cache_read,
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Pretty printer.
# ─────────────────────────────────────────────────────────────────────

def _format_text(s: dict) -> str:
    lines = []
    lines.append(f"subscription health — tenant={s['tenant']} (last {s['hours_window']}h)")
    lines.append(f"  as_of: {s['as_of']}")
    lines.append("")
    lines.append(f"  calls: {s['calls']['total']}")
    for outcome, n in sorted(s['calls']['by_outcome'].items()):
        lines.append(f"    {outcome:<30} {n}")
    lines.append("")
    if s['calls']['by_task_type']:
        lines.append("  by task_type:")
        for task, n in s['calls']['by_task_type'].items():
            lines.append(f"    {task:<30} {n}")
        lines.append("")
    if s['calls']['by_model']:
        lines.append("  by model:")
        for model, n in s['calls']['by_model'].items():
            lines.append(f"    {model:<30} {n}")
        lines.append("")
    rl = s['rate_limit']
    lines.append(f"  rate-limit (latest seen): status={rl['latest_status']} "
                 f"resets_at={rl['latest_resets_at']}")
    p = s['projects']
    lines.append(f"  project usage: with_id={p['with_project_id']} "
                 f"inlined_preamble={p['preamble_inlined']}")
    q = s['queue']
    lines.append(f"  queue: pending={q['pending']} dead={q['dead_letter']}")
    t = s['tokens']
    lines.append(f"  tokens: in={t['input']} out={t['output']} "
                 f"cache_create={t['cache_creation']} cache_read={t['cache_read']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI.
# ─────────────────────────────────────────────────────────────────────

def _main() -> int:
    ap = argparse.ArgumentParser(
        description="Summarize subscription dispatch health from data-<tenant>/dispatch.jsonl"
    )
    ap.add_argument("--tenant", default=_DEFAULT_TENANT)
    ap.add_argument("--hours", type=int, default=_DEFAULT_HOURS,
                    help=f"window in hours (default {_DEFAULT_HOURS})")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of human-readable text")
    args = ap.parse_args()

    s = summarize(args.tenant, hours=args.hours)
    if args.json:
        print(json.dumps(s, indent=2, default=str))
    else:
        print(_format_text(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
