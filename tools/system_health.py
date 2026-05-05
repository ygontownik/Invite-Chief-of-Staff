#!/usr/bin/env python3
"""
system_health.py
================

Aggregate every COS pipeline audit/check into a single scheduled run and
write the consolidated result to ~/dashboards/data/system-health/.

Discovery model
---------------
Each Python module under ~/cos-pipeline/tools/checks/ matching
`check_*.py` is dynamically imported and expected to expose:

    def run() -> dict:
        return {
            "name":    "<short slug>",
            "status":  "pass" | "warn" | "fail",
            "summary": "<one-line human readable>",
            "details": <any json-serializable structure>,
        }

New checks drop into that directory and are picked up automatically.

In addition, the existing standalone audit
~/cos-pipeline/tools/audit_config_drift.py is invoked as a subprocess
and its exit code is mapped to status (0 -> pass, otherwise fail). Its
stdout is captured into details.

Outputs
-------
  ~/dashboards/data/system-health/<YYYY-MM-DD>.json
  ~/dashboards/data/system-health/latest.json

Both files contain the same shape:

    {
      "ts": "<ISO 8601 UTC>",
      "date": "YYYY-MM-DD",
      "overall": "pass" | "warn" | "fail",
      "counts": {"pass": N, "warn": N, "fail": N, "total": N},
      "checks": [ <check result>, ... ]
    }

Stdout summary line:
    system_health: PASS|WARN|FAIL · X checks · Y warns · Z fails

Exit codes (cron-friendly):
    0  no failures (warnings still 0)
    1  at least one fail

Test command (one-liner):
    python3 ~/cos-pipeline/tools/system_health.py && \
      cat ~/dashboards/data/system-health/latest.json | python3 -m json.tool | head -20
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

HOME = Path.home()
TOOLS_DIR = HOME / "cos-pipeline" / "tools"
CHECKS_DIR = TOOLS_DIR / "checks"
AUDIT_CONFIG_DRIFT = TOOLS_DIR / "audit_config_drift.py"
OUT_DIR = HOME / "dashboards" / "data" / "system-health"

_VALID_STATUSES = {"pass", "warn", "fail"}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _load_check_module(path: Path):
    spec = importlib.util.spec_from_file_location(
        f"_health_check_{path.stem}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_result(name_hint: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "name": name_hint,
            "status": "fail",
            "summary": f"{name_hint}: check did not return a dict",
            "details": {"raw_type": type(raw).__name__},
        }
    name = str(raw.get("name") or name_hint)
    status = str(raw.get("status") or "").lower()
    if status not in _VALID_STATUSES:
        return {
            "name": name,
            "status": "fail",
            "summary": f"{name}: invalid status {raw.get('status')!r}",
            "details": raw,
        }
    return {
        "name": name,
        "status": status,
        "summary": str(raw.get("summary") or name),
        "details": raw.get("details"),
    }


def _discover_check_results() -> list[dict[str, Any]]:
    if not CHECKS_DIR.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(CHECKS_DIR.glob("check_*.py")):
        name_hint = path.stem
        try:
            module = _load_check_module(path)
            run = getattr(module, "run", None)
            if not callable(run):
                results.append(
                    {
                        "name": name_hint,
                        "status": "fail",
                        "summary": f"{name_hint}: module has no run() callable",
                        "details": None,
                    }
                )
                continue
            raw = run()
            results.append(_normalize_result(name_hint, raw))
        except Exception as exc:  # noqa: BLE001 — per-check isolation
            results.append(
                {
                    "name": name_hint,
                    "status": "fail",
                    "summary": f"{name_hint}: raised {type(exc).__name__}: {exc}",
                    "details": {"traceback": traceback.format_exc()},
                }
            )
    return results


def _run_audit_config_drift() -> dict[str, Any]:
    name = "config_drift"
    if not AUDIT_CONFIG_DRIFT.exists():
        return {
            "name": name,
            "status": "fail",
            "summary": f"{name}: script not found at ~/{AUDIT_CONFIG_DRIFT.relative_to(HOME)}",
            "details": None,
        }
    try:
        proc = subprocess.run(
            [sys.executable, str(AUDIT_CONFIG_DRIFT)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "name": name,
            "status": "fail",
            "summary": f"{name}: timed out after {exc.timeout}s",
            "details": {"stdout": exc.stdout or "", "stderr": exc.stderr or ""},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": name,
            "status": "fail",
            "summary": f"{name}: subprocess error: {exc}",
            "details": None,
        }

    status = "pass" if proc.returncode == 0 else "fail"
    # Parse the audit's [ BROKEN ]/[ DRIFT ]/[ STALE ] counts to make a
    # tighter summary than just "exit N".
    out = proc.stdout or ""
    summary_bits: list[str] = []
    for marker in ("BROKEN", "DRIFT", "STALE"):
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"[ {marker} ]"):
                # e.g. "[ BROKEN ]  0"
                token = stripped.split("]", 1)[1].strip().split()
                if token and token[0].isdigit():
                    n = int(token[0])
                    if n:
                        summary_bits.append(f"{marker.lower()}={n}")
                break
    if status == "pass":
        summary = f"{name}: clean"
    else:
        summary = f"{name}: " + (", ".join(summary_bits) or "drift detected")

    return {
        "name": name,
        "status": status,
        "summary": summary,
        "details": {
            "exit_code": proc.returncode,
            "stdout_tail": "\n".join(out.splitlines()[-40:]),
            "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-20:]),
        },
    }


def _aggregate(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"pass": 0, "warn": 0, "fail": 0, "total": len(checks)}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    if counts["fail"]:
        overall = "fail"
    elif counts["warn"]:
        overall = "warn"
    else:
        overall = "pass"
    now = _now()
    return {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "overall": overall,
        "counts": counts,
        "checks": checks,
    }


def _write_outputs(report: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dated = OUT_DIR / f"{report['date']}.json"
    latest = OUT_DIR / "latest.json"
    payload = json.dumps(report, indent=2, default=str)
    dated.write_text(payload, encoding="utf-8")
    latest.write_text(payload, encoding="utf-8")


def main() -> int:
    checks: list[dict[str, Any]] = []
    checks.append(_run_audit_config_drift())
    checks.extend(_discover_check_results())

    report = _aggregate(checks)
    _write_outputs(report)

    overall = report["overall"].upper()
    counts = report["counts"]
    print(
        f"system_health: {overall} · {counts['total']} checks · "
        f"{counts['warn']} warns · {counts['fail']} fails"
    )
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
