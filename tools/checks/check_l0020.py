#!/usr/bin/env python3
"""check_l0020.py — Rule L0020: pre-push confidence check.

Always run system_health.py before `git push`. This module checks whether
the discipline is being followed across the three managed repos
(cos-pipeline, dashboards, cos-pipeline-config-tomac):
  1. If a repo has `.git/hooks/pre-push` invoking system_health.py, the
     discipline is mechanically enforced (pass for that repo).
  2. Otherwise compare ~/dashboards/data/system-health/latest.json mtime
     against each repo's latest commit timestamp. A commit landing
     >60 min AFTER the last health check flags discipline skip.

Status: pass = all enforced-by-hook OR no lag; warn = a repo lags;
fail = never (discipline rule).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

HOME = Path.home()

REPOS = [
    HOME / "cos-pipeline",
    HOME / "dashboards",
    HOME / "cos-pipeline-config-tomac",
]
HEALTH_LATEST = HOME / "dashboards" / "data" / "system-health" / "latest.json"
WARN_LAG_SEC = 60 * 60  # 60 min


def _hook_enforces(repo: Path) -> bool:
    hook = repo / ".git" / "hooks" / "pre-push"
    if not hook.exists():
        return False
    try:
        text = hook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "system_health.py" in text or "system_health" in text


def _last_commit_ts(repo: Path) -> int | None:
    if not (repo / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        s = out.stdout.strip()
        return int(s) if s else None
    except Exception:
        return None


def run() -> dict[str, Any]:
    health_mtime: float | None = None
    if HEALTH_LATEST.exists():
        try:
            health_mtime = HEALTH_LATEST.stat().st_mtime
        except OSError:
            health_mtime = None

    per_repo: list[dict[str, Any]] = []
    any_lag = False
    all_enforced_by_hook = True

    for repo in REPOS:
        rel = (str(repo.relative_to(HOME)) if str(repo).startswith(str(HOME))
               else str(repo))
        info: dict[str, Any] = {"repo": rel, "exists": repo.exists()}
        if not repo.exists():
            info["status"] = "missing"
            all_enforced_by_hook = False
        elif _hook_enforces(repo):
            info["pre_push_hook"] = True
            info["status"] = "enforced-by-hook"
        else:
            all_enforced_by_hook = False
            info["pre_push_hook"] = False
            commit_ts = _last_commit_ts(repo)
            info["last_commit_ts"] = commit_ts
            info["health_mtime"] = health_mtime
            if commit_ts is None or health_mtime is None:
                info["status"] = "unknown"
            else:
                lag = commit_ts - health_mtime
                info["commit_minus_health_sec"] = int(lag)
                if lag > WARN_LAG_SEC:
                    info["status"] = "lagging"
                    any_lag = True
                else:
                    info["status"] = "ok"
        per_repo.append(info)

    if all_enforced_by_hook:
        status = "pass"
        summary = "L0020: all 3 repos have pre-push hook invoking system_health.py"
    elif any_lag:
        status = "warn"
        lagging = [r["repo"] for r in per_repo if r.get("status") == "lagging"]
        summary = (
            f"L0020: {len(lagging)} repo(s) committed >60 min after last "
            f"health-check run: {', '.join(lagging)}"
        )
    else:
        status = "pass"
        summary = (
            "L0020: discipline holding — every repo's last commit predates "
            "the last health-check run"
        )

    return {
        "name": "L0020: pre-push confidence check",
        "rule_ref": "L0020",
        "status": status,
        "summary": summary,
        "details": {
            "health_latest_path": str(HEALTH_LATEST),
            "health_mtime_iso": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(health_mtime))
                if health_mtime else None
            ),
            "warn_lag_sec": WARN_LAG_SEC,
            "repos": per_repo,
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
