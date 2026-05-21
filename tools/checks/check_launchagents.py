#!/usr/bin/env python3
"""
check_launchagents.py — LaunchAgent health check (system_health.py discovers).

Walks ~/Library/LaunchAgents/com.{yoni,tcip}.*.plist (excluding webinar
one-shots), validates each is loaded with launchctl, parses its log file
for last-fire timestamp, and computes a freshness verdict against the
plist's schedule.

Also writes a dashboard-renderable JSON to
~/dashboards/data/compiled/launchagent-health.json so the dashboard can
surface a LaunchAgent tile group without re-running this logic.

Status semantics:
  pass — all expected agents are loaded; none stale beyond their cadence.
  warn — agents loaded but at least one stale (> 2x its cadence with no fire).
  fail — at least one expected agent is not loaded.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any

HOME = Path.home()
LA_DIR = HOME / "Library" / "LaunchAgents"
LOG_DIR_CANDIDATES = [HOME / "Library" / "Logs", HOME / "dashboards" / "logs"]
OUT_PATH = HOME / "dashboards" / "data" / "compiled" / "launchagent-health.json"

# Ignore one-shot webinar plists (Zoom record/stop transient). Anything else
# matching the com.yoni|tcip prefix is in scope.
IGNORE_RE = re.compile(r"\.webinar\.[0-9a-f-]+(\.stop)?\.plist$")

# LaunchAgents that are INTENTIONALLY installed-but-not-loaded — they're
# discoverable on disk so the user can flip them on, but their default state
# is dormant. Health check treats these as "info" not "fail" when unloaded.
# Added 2026-05-21 after /wrap pt 4 — github-watch is the documented example
# per github_watch.py docstring "installed-but-disabled until the user flips
# RunAtLoad → true or runs launchctl load".
INTENTIONALLY_DORMANT_LABELS = {
    "com.yoni.github-watch",
}

# Default freshness window per cadence type. Tightened for high-cadence agents.
DEFAULT_STALE_MULTIPLIER = 2.0


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _list_loaded() -> set[str]:
    """Set of LaunchAgent labels currently registered with launchctl."""
    try:
        proc = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    loaded: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            label = parts[2]
            if label.startswith("com.yoni.") or label.startswith("com.tcip."):
                loaded.add(label)
    return loaded


def _parse_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return plistlib.load(f)


def _schedule_label(spec: dict) -> str:
    if "StartInterval" in spec:
        n = int(spec["StartInterval"])
        if n < 60:
            return f"every {n}s"
        if n < 3600:
            return f"every {n // 60}m"
        return f"every {n // 3600}h"
    if "StartCalendarInterval" in spec:
        sci = spec["StartCalendarInterval"]
        if isinstance(sci, list):
            return f"{len(sci)} calendar entries"
        h = sci.get("Hour")
        m = sci.get("Minute", 0)
        wd = sci.get("Weekday")
        if wd is not None:
            return f"weekly Wd={wd} {h:02d}:{m:02d}"
        if h is not None:
            return f"daily {h:02d}:{m:02d}"
        return f"calendar {sci}"
    if spec.get("KeepAlive"):
        return "keep-alive"
    if spec.get("WatchPaths"):
        return f"watch {spec['WatchPaths']}"
    if spec.get("RunAtLoad"):
        return "on-load only"
    return "manual / unknown"


def _cadence_seconds(spec: dict) -> int | None:
    """Approximate cadence in seconds. None = no fixed cadence."""
    if "StartInterval" in spec:
        return int(spec["StartInterval"])
    if "StartCalendarInterval" in spec:
        sci = spec["StartCalendarInterval"]
        if isinstance(sci, dict):
            if "Weekday" in sci:
                return 7 * 86400
            if "Hour" in sci or "Minute" in sci:
                return 86400
        return 86400
    return None


def _last_fire(spec: dict, label: str) -> _dt.datetime | None:
    """Return the most-recent log mtime that plausibly indicates a fire."""
    candidates: list[Path] = []
    for key in ("StandardOutPath", "StandardErrorPath"):
        val = spec.get(key)
        if val:
            candidates.append(Path(val))
    short = label.replace("com.yoni.", "").replace("com.tcip.", "").replace("claude-task.", "")
    for d in LOG_DIR_CANDIDATES:
        if d.is_dir():
            for ext in (".log", ".out", ".err"):
                candidates.extend(d.glob(f"{short}*{ext}"))
    best: _dt.datetime | None = None
    for p in candidates:
        try:
            if p.is_file() and p.stat().st_size > 0:
                t = _dt.datetime.fromtimestamp(p.stat().st_mtime, _dt.timezone.utc)
                if best is None or t > best:
                    best = t
        except OSError:
            continue
    return best


def _verdict(loaded: bool, cadence: int | None, last: _dt.datetime | None,
             now: _dt.datetime, label: str = "") -> tuple[str, str]:
    if not loaded:
        if label in INTENTIONALLY_DORMANT_LABELS:
            return "pass", "intentionally dormant (allowlisted)"
        return "fail", "not loaded"
    if cadence is None:
        if last is None:
            return "warn", "no log evidence; cadence unknown"
        return "pass", f"manual/event-driven; last activity {last.isoformat(timespec='seconds')}"
    if last is None:
        return "warn", "loaded; no log evidence yet"
    age = (now - last).total_seconds()
    if age > cadence * DEFAULT_STALE_MULTIPLIER:
        days = age / 86400
        return "warn", f"stale ({days:.1f}d since last fire vs cadence ~{cadence}s)"
    return "pass", f"fresh (last fire {last.isoformat(timespec='seconds')})"


def _walk_plists() -> list[Path]:
    out: list[Path] = []
    if not LA_DIR.is_dir():
        return out
    for p in sorted(LA_DIR.iterdir()):
        if not p.name.endswith(".plist"):
            continue
        if not (p.name.startswith("com.yoni.") or p.name.startswith("com.tcip.")):
            continue
        if IGNORE_RE.search(p.name):
            continue
        out.append(p)
    return out


def _audit() -> dict[str, Any]:
    now = _now_utc()
    loaded = _list_loaded()
    agents: list[dict[str, Any]] = []
    counts = {"pass": 0, "warn": 0, "fail": 0}

    for path in _walk_plists():
        try:
            spec = _parse_plist(path)
        except Exception as exc:
            agents.append({
                "label": path.stem,
                "plist": str(path),
                "status": "fail",
                "summary": f"plist parse error: {exc}",
            })
            counts["fail"] += 1
            continue
        label = spec.get("Label") or path.stem
        is_loaded = label in loaded
        cadence = _cadence_seconds(spec)
        last = _last_fire(spec, label)
        status, summary = _verdict(is_loaded, cadence, last, now, label=label)
        agents.append({
            "label": label,
            "plist": str(path),
            "status": status,
            "summary": summary,
            "loaded": is_loaded,
            "schedule": _schedule_label(spec),
            "cadence_seconds": cadence,
            "last_fire": last.isoformat(timespec="seconds") if last else None,
            "program": spec.get("ProgramArguments", []),
        })
        counts[status] += 1

    overall = "fail" if counts["fail"] else ("warn" if counts["warn"] else "pass")
    return {
        "ts": now.isoformat(timespec="seconds"),
        "overall": overall,
        "counts": counts,
        "total": len(agents),
        "agents": agents,
    }


def _write_dashboard_json(report: dict[str, Any]) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")


def run() -> dict[str, Any]:
    report = _audit()
    _write_dashboard_json(report)
    c = report["counts"]
    return {
        "name": "launchagents",
        "rule_ref": "LA1",
        "status": report["overall"],
        "summary": (
            f"launchagents: {report['total']} total · "
            f"{c['pass']} healthy · {c['warn']} stale · {c['fail']} down"
        ),
        "details": {
            "report_path": str(OUT_PATH),
            "down": [a["label"] for a in report["agents"] if a["status"] == "fail"],
            "stale": [a["label"] for a in report["agents"] if a["status"] == "warn"],
        },
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] != "fail" else 1)
