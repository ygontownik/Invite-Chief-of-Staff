"""
Track G — costs/quota aggregator for the COS dashboard tile.

Reads per-call JSONL cost rows written by ~/cos-pipeline/_model_router.py
into ~/cos-pipeline/data-<tenant>/costs/YYYY-MM-DD.jsonl, rolls them up
for the dashboard "Costs" tile.

Schema (per MODEL_ROUTER.md and _model_router.py::_record_cost):
    {
      "ts": "<ISO-8601 UTC>",
      "task_type": "pass2_pipeline_analyst",       # PRIMARY pass identifier
      "model": "claude-opus-4-7",
      "input_tokens": int,
      "output_tokens": int,
      "cached_input_tokens": int,
      "mode": "api" | "subscription" | ...,
      "est_usd": float                              # 0.0 when mode=='subscription'
    }

The brief asks for `pass_name` and `routine_name` aggregations. The current
schema only carries `task_type`. We treat:
  - `pass_name`    := row.get("pass_name") or row.get("task_type")
  - `routine_name` := row.get("routine_name") or row.get("task_type")
so the aggregator is forward-compatible if future router versions emit those
explicit fields, and degrades cleanly today.

Pure-Python, stdlib only. Safe to call with no costs/ dir present.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Any

_PIPELINE_ROOT = Path(os.path.expanduser("~/cos-pipeline"))


def _costs_dir(tenant: str) -> Path:
    return _PIPELINE_ROOT / f"data-{tenant}" / "costs"


def _row_date(row: dict) -> str | None:
    """YYYY-MM-DD from the row's ts; None if unparseable."""
    ts = row.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        # fromisoformat handles +00:00; strip trailing Z if present
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return ts[:10] if len(ts) >= 10 else None


def _empty_result(lookback_days: int) -> dict[str, Any]:
    return {
        "total_usd": 0.0,
        "by_model": {},
        "by_pass": {},
        "by_routine": {},
        "daily": [],
        "jsonl_files_seen": 0,
        "lines_read": 0,
        "lookback_days": lookback_days,
    }


def aggregate_costs(tenant: str, lookback_days: int = 30) -> dict[str, Any]:
    """Glob ~/cos-pipeline/data-<tenant>/costs/*.jsonl and roll up.

    Returns the empty-result shape if the costs dir does not exist.
    Per-line errors are skipped silently (forward-compat with schema drift).
    """
    cdir = _costs_dir(tenant)
    if not cdir.exists():
        return _empty_result(lookback_days)

    today = date.today()
    cutoff = today - timedelta(days=lookback_days - 1)  # inclusive window

    files = sorted(glob(str(cdir / "*.jsonl")))
    by_model: dict[str, float] = defaultdict(float)
    by_pass: dict[str, float] = defaultdict(float)
    by_routine: dict[str, float] = defaultdict(float)
    daily: dict[str, float] = defaultdict(float)
    total = 0.0
    files_seen = 0
    lines_read = 0

    for path in files:
        # Filename is YYYY-MM-DD.jsonl — cheap pre-filter on date.
        stem = Path(path).stem
        try:
            file_day = date.fromisoformat(stem)
            if file_day < cutoff or file_day > today:
                continue
        except ValueError:
            # Non-date filename in costs/ — read anyway, filter per-row.
            file_day = None

        files_seen += 1
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    lines_read += 1

                    # Per-row date filter (defense in depth).
                    rd = _row_date(row) or (file_day.isoformat() if file_day else None)
                    if rd:
                        try:
                            rdate = date.fromisoformat(rd)
                            if rdate < cutoff or rdate > today:
                                continue
                        except ValueError:
                            pass

                    usd = float(row.get("est_usd", 0.0) or 0.0)
                    model = str(row.get("model") or "unknown")
                    pass_name = str(row.get("pass_name") or row.get("task_type") or "unknown")
                    routine_name = str(row.get("routine_name") or row.get("task_type") or "unknown")

                    total += usd
                    by_model[model] += usd
                    by_pass[pass_name] += usd
                    by_routine[routine_name] += usd
                    if rd:
                        daily[rd] += usd
        except OSError:
            continue

    daily_sorted = [{"date": d, "usd": round(daily[d], 6)} for d in sorted(daily)]

    return {
        "total_usd": round(total, 6),
        "by_model": {k: round(v, 6) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
        "by_pass": {k: round(v, 6) for k, v in sorted(by_pass.items(), key=lambda x: -x[1])},
        "by_routine": {k: round(v, 6) for k, v in sorted(by_routine.items(), key=lambda x: -x[1])},
        "daily": daily_sorted,
        "jsonl_files_seen": files_seen,
        "lines_read": lines_read,
        "lookback_days": lookback_days,
    }


def format_for_tile(agg: dict[str, Any], top_n: int = 5) -> dict[str, Any]:
    """Coerce the raw aggregate into a dashboard-friendly shape."""
    total = float(agg.get("total_usd", 0.0) or 0.0)
    lookback = int(agg.get("lookback_days", 30))

    def _top(d: dict[str, float]) -> list[list]:
        # list[list] (not tuples) so it survives JSON round-trip cleanly.
        return [[k, round(v, 4)] for k, v in list(d.items())[:top_n]]

    return {
        "summary": f"${total:,.2f} last {lookback}d",
        "totalUsd": round(total, 4),
        "lookbackDays": lookback,
        "topModels": _top(agg.get("by_model", {})),
        "topPasses": _top(agg.get("by_pass", {})),
        "topRoutines": _top(agg.get("by_routine", {})),
        "dailyChart": agg.get("daily", []),
        "filesSeen": int(agg.get("jsonl_files_seen", 0)),
        "linesRead": int(agg.get("lines_read", 0)),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    agg = aggregate_costs("tomac", lookback_days=30)
    tile = format_for_tile(agg)
    print("── raw aggregate ──")
    print(json.dumps(agg, indent=2))
    print()
    print("── tile shape ──")
    print(json.dumps(tile, indent=2))
