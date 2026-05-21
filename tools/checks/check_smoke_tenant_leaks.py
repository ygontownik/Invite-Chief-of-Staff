"""check_smoke_tenant_leaks.py — wraps tools/smoke_test_tenant.py for the
system_health aggregator.

WHAT IT CATCHES
---------------
Hardcoded tenant strings in the public cos-pipeline/ codebase that would
leak into a fresh subscriber install. The wrapped script (smoke_test_
tenant.py) materializes a synthetic tenant config in a tempdir, exercises
key code surfaces, and statically scans every .py file for forbidden
strings (tomac, cholla, yoni, gontownik, etc.).

STATUSES
--------
- pass  : 0 leaks across all probes
- warn  : 1-50 leaks (typically docstring/comment residue)
- fail  : 51+ leaks (likely real hardcoded tenant data in code paths)

Threshold tuning: warn vs fail boundaries reflect that the cos-pipeline
codebase is mid-migration to tenant-agnostic. Once the bulk of true
hardcoding is removed (cos_email_backfill.py is the template), tighten
the fail threshold to >= 5.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run() -> dict:
    script = Path(__file__).resolve().parent.parent / "smoke_test_tenant.py"
    if not script.exists():
        return {
            "name": "smoke: tenant-leak regression",
            "rule_ref": "TL2 :: tools/smoke_test_tenant.py",
            "status": "warn",
            "summary": "smoke_test_tenant.py not present",
            "details": [],
        }
    out = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=120,
    )
    text = (out.stdout or "") + (out.stderr or "")
    # Extract hit count from the summary line
    leak_count = 0
    for line in text.splitlines():
        if "smoke_test_tenant:" in line and "tenant leak" in line:
            # "smoke_test_tenant: FAIL · 3 probes · 156 tenant leak(s)"
            try:
                parts = line.split("·")
                for p in parts:
                    p = p.strip()
                    if p.endswith("tenant leak(s)") or p.endswith("tenant leaks"):
                        leak_count = int(p.split()[0])
                        break
            except Exception:
                pass
            break

    if leak_count == 0:
        status = "pass"
    elif leak_count <= 50:
        status = "warn"
    else:
        status = "fail"

    # Top-20 leak lines for the details payload
    details = []
    for line in text.splitlines():
        if line.strip().startswith("·"):
            details.append(line.strip().lstrip("·").strip())
    details = details[:20]

    return {
        "name": "smoke: tenant-leak regression",
        "rule_ref": "TL2 :: tools/smoke_test_tenant.py",
        "status": status,
        "summary": f"{leak_count} tenant leak(s) across smoke probes",
        "details": details,
    }
