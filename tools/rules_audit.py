#!/usr/bin/env python3
"""
rules_audit.py — Active audit of LEARNINGS-LEDGER rules.

Closes the back half of the dynamic-learnings loop. `/propose-learning`
captures new rules into LEARNINGS-LEDGER.yaml and sync_learnings.py
propagates them to ~/.claude/CLAUDE.md (passive ingestion — Claude has
them in context but nothing audits whether they're followed). This
script closes the gap: every rule with `enforced_by: code` should have
a matching `tools/checks/check_<rule_code>.py` module, and that module
should be passing. When a rule is added to the ledger but no
enforcement code exists yet, it's a **paper rule** — looks rigorous on
paper, but the system isn't actually enforcing it.

The "learn going forward" property: the audit reads LEARNINGS-LEDGER
at every run, not at install time. So when Yoni adds rule L0050 via
/propose-learning tomorrow, the next audit run automatically includes
it without any code change here.

Per-rule classification:
  enforced       — `enforced_by: code` AND a matching check module
                   exists AND its last run was PASS or WARN
  violated       — matching check module exists but last run was FAIL
  paper-rule     — `enforced_by` mentions code/script/lint but no
                   matching check module exists in tools/checks/
  informational  — `enforced_by: manual` / "review" / "PREFLIGHT.md" —
                   not a code-enforced rule, no audit possible
  deprecated     — `status: deprecated` or `status: archived` — skip

Output: ~/dashboards/data/compiled/rules-compliance.json
        ~/dashboards/logs/rules-audit.log (append-only one-line summary)

CLI:
  python3 rules_audit.py             # dry-run, prints report
  python3 rules_audit.py --apply     # writes report + log line
  python3 rules_audit.py --gaps-only # prints only the paper-rule gaps
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob as _glob
import importlib.util
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

HOME = Path.home()
LEDGER_PATH = HOME / "dashboards" / "docs" / "LEARNINGS-LEDGER.yaml"
CHECKS_DIR = HOME / "cos-pipeline" / "tools" / "checks"
OUTPUT_PATH = HOME / "dashboards" / "data" / "compiled" / "rules-compliance.json"
LOG_PATH = HOME / "dashboards" / "logs" / "rules-audit.log"

# Tokens that mean "this rule is enforced by code somewhere" — used to
# decide whether a rule WITHOUT a check module should be tagged
# paper-rule (yes — code mentioned but missing) vs informational (no —
# this rule is explicitly manual).
_CODE_ENFORCEMENT_TOKENS = (
    "code-review", "code", "lint", "pre-commit", "check", "audit",
    "hook", ".py", ".sh", "regex", "subprocess", "intel_capture",
    "reference_integrity", "tenant_leak", "drive organizer",
    "pre-commit-edit-in-place",
)
_MANUAL_ENFORCEMENT_TOKENS = (
    "manual", "preflight.md", "code review only", "self-enforce",
    "discipline", "visual rules", "honor",
)


def _load_ledger() -> dict[str, Any]:
    if not LEDGER_PATH.exists():
        sys.exit(f"ERROR: {LEDGER_PATH} not found")
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit("ERROR: pyyaml not installed")
    return yaml.safe_load(LEDGER_PATH.read_text()) or {}


def _find_check_module(rule_code: str | None, rule_id: str) -> Path | None:
    """Look for a check module that matches this rule. Tries (in order):
      1. check_<rule_code_lower>.py (e.g. check_lf1.py for LF1)
      2. check_<rule_id_lower>.py   (e.g. check_l0001.py)
      3. check_<rule_code_lower without trailing digits>_*.py (loose match)
    """
    if not CHECKS_DIR.exists():
        return None
    candidates: list[str] = []
    if rule_code:
        candidates.append(f"check_{rule_code.lower()}.py")
    candidates.append(f"check_{rule_id.lower()}.py")
    for name in candidates:
        p = CHECKS_DIR / name
        if p.exists():
            return p
    # Loose match — strip trailing digits, look for prefix match
    if rule_code:
        prefix = re.sub(r"\d+$", "", rule_code.lower())
        if prefix and len(prefix) >= 2:
            matches = list(CHECKS_DIR.glob(f"check_{prefix}*.py"))
            if len(matches) == 1:
                return matches[0]
    return None


def _classify_enforcement_field(enforced_by: str) -> str:
    """Return 'code', 'manual', or 'unknown' based on the free-text field."""
    if not enforced_by:
        return "unknown"
    low = enforced_by.lower()
    has_manual = any(tok in low for tok in _MANUAL_ENFORCEMENT_TOKENS)
    has_code = any(tok in low for tok in _CODE_ENFORCEMENT_TOKENS)
    if has_code and not has_manual:
        return "code"
    if has_manual and not has_code:
        return "manual"
    if has_code and has_manual:
        return "code"   # hybrid → favor code so paper-rule gaps surface
    return "unknown"


def _run_check_module(path: Path) -> dict[str, Any]:
    """Load and run a check_*.py module, returning its dict result."""
    spec = importlib.util.spec_from_file_location(
        f"_rules_audit_{path.stem}", path
    )
    if spec is None or spec.loader is None:
        return {"status": "fail", "summary": f"could not load {path.name}"}
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        return {
            "status": "fail",
            "summary": f"{path.name}: import error: {exc}",
            "traceback": traceback.format_exc(),
        }
    run = getattr(module, "run", None)
    if not callable(run):
        return {"status": "fail", "summary": f"{path.name}: no run() callable"}
    try:
        raw = run()
        if not isinstance(raw, dict):
            return {"status": "fail", "summary": f"{path.name}: bad return type"}
        return {
            "status": str(raw.get("status") or "unknown").lower(),
            "summary": str(raw.get("summary") or path.stem),
            "details": raw.get("details"),
        }
    except Exception as exc:
        return {
            "status": "fail",
            "summary": f"{path.name}: raised {type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def audit() -> dict[str, Any]:
    """Run the full audit and return the report dict (does not write to disk)."""
    ledger = _load_ledger()
    learnings = ledger.get("learnings") or []
    if not isinstance(learnings, list):
        sys.exit("ERROR: LEARNINGS-LEDGER.yaml has no `learnings:` list")

    rules_out: list[dict[str, Any]] = []
    counts = {
        "enforced": 0, "violated": 0, "warned": 0,
        "paper_rule": 0, "informational": 0, "deprecated": 0,
    }

    for entry in learnings:
        if not isinstance(entry, dict):
            continue
        status = (entry.get("status") or "").lower()
        if status in {"deprecated", "archived", "retired"}:
            counts["deprecated"] += 1
            rules_out.append({
                "id": entry.get("id"), "rule_code": entry.get("rule_code"),
                "title": entry.get("title"), "classification": "deprecated",
            })
            continue

        rule_id = str(entry.get("id") or "")
        rule_code = entry.get("rule_code")
        rule_code_s = str(rule_code) if rule_code else None
        enforced_by = str(entry.get("enforced_by") or "")
        enforcement_kind = _classify_enforcement_field(enforced_by)

        check_path = _find_check_module(rule_code_s, rule_id)

        if check_path:
            res = _run_check_module(check_path)
            check_status = res["status"]
            if check_status == "pass":
                classification = "enforced"
                counts["enforced"] += 1
            elif check_status == "warn":
                classification = "enforced"   # rule is wired, soft signal
                counts["warned"] += 1
            elif check_status == "fail":
                classification = "violated"
                counts["violated"] += 1
            else:
                classification = "enforced"   # unknown status, still wired
                counts["enforced"] += 1
            rules_out.append({
                "id": rule_id, "rule_code": rule_code_s,
                "title": entry.get("title"),
                "classification": classification,
                "check_module": check_path.name,
                "check_status": check_status,
                "check_summary": res["summary"],
                "enforced_by_field": enforced_by,
            })
        else:
            # No check module exists
            if enforcement_kind == "code":
                counts["paper_rule"] += 1
                classification = "paper_rule"
            else:
                counts["informational"] += 1
                classification = "informational"
            rules_out.append({
                "id": rule_id, "rule_code": rule_code_s,
                "title": entry.get("title"),
                "classification": classification,
                "enforced_by_field": enforced_by,
                "enforcement_kind": enforcement_kind,
            })

    total = sum(counts.values())
    health = (
        "fail" if counts["violated"] > 0 else
        "warn" if counts["paper_rule"] >= 5 or counts["warned"] > 0 else
        "pass"
    )

    return {
        "ran_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "ledger_path": str(LEDGER_PATH),
        "checks_dir": str(CHECKS_DIR),
        "total_rules": total,
        "counts": counts,
        "overall_status": health,
        "rules": rules_out,
        "next_actions": _next_actions(counts, rules_out),
    }


def _next_actions(counts: dict[str, int], rules: list[dict[str, Any]]) -> list[str]:
    """One-line action suggestions surfaced on the dashboard tile."""
    out = []
    if counts["violated"] > 0:
        violated = [r for r in rules if r.get("classification") == "violated"]
        out.append(
            f"FAIL: {counts['violated']} rule(s) violated — "
            + ", ".join(f"{r['rule_code'] or r['id']}" for r in violated[:5])
        )
    if counts["paper_rule"] > 0:
        gaps = [r for r in rules if r.get("classification") == "paper_rule"]
        out.append(
            f"GAP: {counts['paper_rule']} paper-rule(s) — add check_*.py for: "
            + ", ".join(f"{r['rule_code'] or r['id']}" for r in gaps[:5])
        )
    if not out:
        out.append("All enforced rules passing. No paper-rule gaps.")
    return out


def _print_report(report: dict[str, Any], gaps_only: bool = False) -> None:
    c = report["counts"]
    print(f"rules_audit: {report['overall_status'].upper()} · "
          f"{report['total_rules']} rules · "
          f"{c['enforced']} enforced · "
          f"{c['violated']} violated · "
          f"{c['paper_rule']} paper-rule gaps · "
          f"{c['informational']} informational · "
          f"{c['deprecated']} deprecated")
    print()
    if gaps_only:
        gaps = [r for r in report["rules"] if r["classification"] == "paper_rule"]
        if not gaps:
            print("No paper-rule gaps. All code-enforced rules have a check module.")
            return
        print(f"Paper-rule gaps ({len(gaps)}):")
        for r in gaps:
            rc = r.get("rule_code") or r.get("id")
            print(f"  {rc:8s} {r.get('title')}")
            print(f"           enforced_by: {r.get('enforced_by_field')}")
        return
    for r in report["rules"]:
        cls = r["classification"]
        if cls == "deprecated":
            continue
        marker = {
            "enforced": "✓", "violated": "✗",
            "paper_rule": "○", "informational": "·",
        }.get(cls, "?")
        rc = r.get("rule_code") or r.get("id")
        title = (r.get("title") or "")[:60]
        suffix = ""
        if cls == "enforced" and "check_status" in r:
            suffix = f"  [{r['check_module']} → {r['check_status']}]"
        elif cls == "violated":
            suffix = f"  [{r.get('check_summary')}]"
        elif cls == "paper_rule":
            suffix = "  ← needs check_*.py"
        print(f"  {marker} {rc:8s} {title}{suffix}")
    print()
    for a in report["next_actions"]:
        print(a)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--apply", action="store_true",
                   help="Write report to ~/dashboards/data/compiled/rules-compliance.json + log line")
    p.add_argument("--gaps-only", action="store_true",
                   help="Print only paper-rule gaps")
    args = p.parse_args(argv)

    report = audit()
    _print_report(report, gaps_only=args.gaps_only)

    if args.apply:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(report, indent=2))
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = report["counts"]
        line = (
            f"{report['ran_at']} · {report['overall_status']} · "
            f"{report['total_rules']} rules · "
            f"{c['enforced']} enforced · {c['violated']} violated · "
            f"{c['paper_rule']} gaps\n"
        )
        with open(LOG_PATH, "a") as f:
            f.write(line)
        print(f"\nWrote {OUTPUT_PATH}")
    return 0 if report["overall_status"] != "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main())
