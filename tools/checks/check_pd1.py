#!/usr/bin/env python3
"""
check_pd1.py
============

Binds Rule **PD1 — Public-first architecture** to its existing
enforcement. Tenant slug leak detection (no hardcoded tenant identifiers
in ~/cos-pipeline/) already lives in `check_tenant_leak.py`. This module
is a thin wrapper that delegates to that check and re-labels the result
with `rule_ref="PD1"` so `rules_audit.py` can bind it to the ledger
entry via the `check_<rule_code>.py` naming convention.

Status semantics:
  - "fail" — any hard-hit tenant leak in public repo
  - "warn" — only allow-listed / soft matches (or upstream warn)
  - "pass" — clean
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()


def run() -> dict[str, Any]:
    try:
        # Delegate to the canonical tenant-leak scanner.
        from check_tenant_leak import run as _tenant_run  # type: ignore
    except Exception:
        # Fallback: import from absolute path so the module is loadable
        # both as a package member and as a standalone script.
        import importlib.util
        path = Path(__file__).resolve().parent / "check_tenant_leak.py"
        if not path.exists():
            return {
                "name": "PD1: public-first / tenant-leak",
                "rule_ref": "PD1",
                "status": "warn",
                "summary": "check_tenant_leak.py not present; PD1 unbound",
                "details": {"missing": str(path)},
            }
        spec = importlib.util.spec_from_file_location("_pd1_tenant", path)
        if spec is None or spec.loader is None:
            return {
                "name": "PD1: public-first / tenant-leak",
                "rule_ref": "PD1",
                "status": "fail",
                "summary": f"could not load {path.name}",
                "details": {},
            }
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            return {
                "name": "PD1: public-first / tenant-leak",
                "rule_ref": "PD1",
                "status": "fail",
                "summary": f"{path.name} import error: {exc}",
                "details": {"error": str(exc)},
            }
        _tenant_run = getattr(module, "run", None)
        if not callable(_tenant_run):
            return {
                "name": "PD1: public-first / tenant-leak",
                "rule_ref": "PD1",
                "status": "fail",
                "summary": f"{path.name}: no run() callable",
                "details": {},
            }

    try:
        upstream = _tenant_run()
    except Exception as exc:
        return {
            "name": "PD1: public-first / tenant-leak",
            "rule_ref": "PD1",
            "status": "fail",
            "summary": f"tenant-leak check raised: {exc}",
            "details": {"error": str(exc)},
        }

    status = str(upstream.get("status") or "warn").lower()
    if status not in {"pass", "warn", "fail"}:
        status = "warn"

    return {
        "name": "PD1: public-first / tenant-leak",
        "rule_ref": "PD1",
        "status": status,
        "summary": f"PD1 via tenant_leak: {upstream.get('summary', '')}",
        "details": upstream.get("details") or {},
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
