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
- warn  : 1-30 leaks (residual TCIP project-name uses + a small tail
          of legitimate code-identifier hits that the scanner can't
          easily distinguish from real residue)
- fail  : 31+ leaks (real regression: new docstring/comment residue
          OR a runtime hardcode appearing in code paths)

Threshold history:
  v1 (initial)  : warn 1-50,  fail 51+   — first calibration, ~55 baseline
  v2 (2026-05-21): warn 1-100, fail 101+ — temporary headroom during sweep
  v3 (2026-05-21, post-sweep): warn 1-30, fail 31+
       Set by commit ${this} after cos-pipeline@5535d8f swept docstrings
       to placeholders (<principal>/<firm>/<partner_a>/<partner_b>) and
       dropped the baseline from 55 → 17. The 30-hit fail boundary gives
       ~13 hits of headroom above the cleaned baseline for natural
       fluctuation while catching real regressions.

Tighten further once the remaining ~17 hits are addressed individually
(most are TCIP product-name references in PPT watermarks / file names
that have legitimate runtime uses).
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
    elif leak_count <= 30:
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
