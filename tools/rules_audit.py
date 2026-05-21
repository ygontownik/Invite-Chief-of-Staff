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


# Regex to pull rule codes out of a check module's rule_ref string.
# Matches LEARNINGS-LEDGER-shaped codes (1-3 capital letters + digits like LF1,
# CC1, EP1, AA1, G2, M3) plus L0001-style ledger IDs.
_RULE_CODE_RE = re.compile(r"\b([A-Z]{1,3}\d{1,4})\b")


def _build_check_index() -> dict[str, dict[str, Any]]:
    """Run every check_*.py module ONCE upfront, extract its rule_ref via the
    standard `rule_ref` field, and return a `{code: {path, status, summary,
    details, ref}}` index keyed by every code the check claims to enforce.

    This is the matcher upgrade: a check module like check_relative_dates.py
    (filename doesn't match AB1) emits `rule_ref: "dash_corrections.md :: AB1"`,
    so the index ends up with both 'check_relative_dates.py' (filename) AND
    'AB1' (extracted code) as keys pointing to the same result.

    Cached at module-load time so we don't re-run checks per-rule. ~18 checks,
    ~3-5s total — acceptable for a daily audit.
    """
    index: dict[str, dict[str, Any]] = {}
    if not CHECKS_DIR.exists():
        return index
    for path in sorted(CHECKS_DIR.glob("check_*.py")):
        result = _run_check_module(path)
        entry = {
            "path": path,
            "filename": path.name,
            "status": result["status"],
            "summary": result["summary"],
            "details": result.get("details"),
            "rule_ref": result.get("rule_ref"),
        }
        # Index by filename stem (so check_lf1.py is reachable as "lf1")
        fname_stem = path.stem.replace("check_", "", 1)
        index.setdefault(fname_stem.lower(), entry)
        # Index by every code we can extract from rule_ref
        rule_ref = str(result.get("rule_ref") or "")
        for m in _RULE_CODE_RE.finditer(rule_ref):
            code = m.group(1)
            # First-wins so a rule_ref like "dash_corrections.md :: AA1"
            # binds AA1 → check_aa1.py and doesn't get overwritten if
            # another check happens to mention AA1 in passing.
            index.setdefault(code.lower(), entry)
    return index


def _run_check_module(path: Path) -> dict[str, Any]:
    """Load and run a check_*.py module, returning its dict result.
    Captures the rule_ref field too so the index can use it for binding."""
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
            "rule_ref": "",
        }
    run = getattr(module, "run", None)
    if not callable(run):
        return {"status": "fail", "summary": f"{path.name}: no run() callable", "rule_ref": ""}
    try:
        raw = run()
        if not isinstance(raw, dict):
            return {"status": "fail", "summary": f"{path.name}: bad return type", "rule_ref": ""}
        return {
            "status": str(raw.get("status") or "unknown").lower(),
            "summary": str(raw.get("summary") or path.stem),
            "details": raw.get("details"),
            "rule_ref": raw.get("rule_ref", ""),
        }
    except Exception as exc:
        return {
            "status": "fail",
            "summary": f"{path.name}: raised {type(exc).__name__}: {exc}",
            "rule_ref": "",
        }


def _find_check_in_index(rule_code: str | None, rule_id: str,
                         index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Look up a check entry in the pre-built index by rule_code or rule_id.
    Tries (in order):
      1. rule_code lowercased — catches both filename and rule_ref bindings
      2. rule_id lowercased   — catches check_l0001.py style
      3. rule_code without trailing digits, loose prefix match
    """
    if not index:
        return None
    if rule_code and rule_code.lower() in index:
        return index[rule_code.lower()]
    if rule_id and rule_id.lower() in index:
        return index[rule_id.lower()]
    if rule_code:
        prefix = re.sub(r"\d+$", "", rule_code.lower())
        if prefix and len(prefix) >= 2:
            # Find unique prefix match
            candidates = [k for k in index if k.startswith(prefix)]
            if len(candidates) == 1:
                return index[candidates[0]]
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


def audit() -> dict[str, Any]:
    """Run the full audit and return the report dict (does not write to disk)."""
    ledger = _load_ledger()
    learnings = ledger.get("learnings") or []
    if not isinstance(learnings, list):
        sys.exit("ERROR: LEARNINGS-LEDGER.yaml has no `learnings:` list")

    # Build the check index ONCE upfront — runs every check_*.py module a
    # single time and lets us bind rules by filename, rule_id, OR rule_ref
    # extracted code. Catches checks like check_relative_dates.py (filename
    # doesn't match AB1) whose run() emits rule_ref="dash_corrections.md :: AB1".
    check_index = _build_check_index()

    rules_out: list[dict[str, Any]] = []
    counts = {
        "enforced": 0, "violated": 0, "warned": 0,
        "paper_rule": 0, "informational": 0, "deprecated": 0,
    }
    # Track which check files were bound to a ledger rule so we can report
    # orphan checks (enforcement exists but no canonical rule yet).
    bound_check_paths: set[str] = set()

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

        check_entry = _find_check_in_index(rule_code_s, rule_id, check_index)

        if check_entry:
            bound_check_paths.add(check_entry["filename"])
            check_status = check_entry["status"]
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
                "check_module": check_entry["filename"],
                "check_status": check_status,
                "check_summary": check_entry["summary"],
                "rule_ref": check_entry.get("rule_ref"),
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

    # Compute orphan checks: check modules in tools/checks/ that emit a real
    # rule_ref but were never bound to any ledger entry above. These are
    # "promotion opportunities" — the rule is enforced in code but hasn't
    # been promoted to LEARNINGS-LEDGER.yaml as a canonical entry yet.
    # Iterate unique entries (the index has multiple keys pointing to the
    # same entry — filename stem + every extracted rule code).
    seen_filenames: set[str] = set()
    orphan_checks: list[dict[str, Any]] = []
    for entry in check_index.values():
        fname = entry["filename"]
        if fname in seen_filenames:
            continue
        seen_filenames.add(fname)
        if fname in bound_check_paths:
            continue
        orphan_checks.append({
            "filename": fname,
            "rule_ref": str(entry.get("rule_ref") or ""),
            "status": entry.get("status"),
            "summary": entry.get("summary"),
        })
    orphan_checks.sort(key=lambda x: x["filename"])
    counts["orphan_checks"] = len(orphan_checks)

    # Orphan checks are informational; they do NOT influence overall_status.
    total = sum(v for k, v in counts.items() if k != "orphan_checks")
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
        "orphan_checks": orphan_checks,
        "next_actions": _next_actions(counts, rules_out, orphan_checks),
    }


def _orphan_short_label(entry: dict[str, Any]) -> str:
    """Compact label for an orphan check: filename + extracted code if any."""
    fname = entry["filename"]
    ref = entry.get("rule_ref") or ""
    m = _RULE_CODE_RE.search(ref)
    if m:
        return f"{fname} ({m.group(1)})"
    return fname


def _next_actions(counts: dict[str, int], rules: list[dict[str, Any]],
                  orphan_checks: list[dict[str, Any]] | None = None) -> list[str]:
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
    if orphan_checks:
        out.append(
            f"INFO: {len(orphan_checks)} orphan check(s) — checks enforcing "
            "rules not yet in LEARNINGS-LEDGER: "
            + ", ".join(_orphan_short_label(o) for o in orphan_checks[:8])
        )
    if not out:
        out.append("All enforced rules passing. No paper-rule gaps.")
    return out


def _print_report(report: dict[str, Any], gaps_only: bool = False) -> None:
    c = report["counts"]
    orphans = report.get("orphan_checks") or []
    print(f"rules_audit: {report['overall_status'].upper()} · "
          f"{report['total_rules']} rules · "
          f"{c['enforced']} enforced · "
          f"{c['violated']} violated · "
          f"{c['paper_rule']} paper-rule gaps · "
          f"{c['informational']} informational · "
          f"{c['deprecated']} deprecated · "
          f"{len(orphans)} orphan checks")
    print()
    if gaps_only:
        gaps = [r for r in report["rules"] if r["classification"] == "paper_rule"]
        if not gaps and not orphans:
            print("No paper-rule gaps. All code-enforced rules have a check module.")
            return
        if gaps:
            print(f"Paper-rule gaps ({len(gaps)}):")
            for r in gaps:
                rc = r.get("rule_code") or r.get("id")
                print(f"  {rc:8s} {r.get('title')}")
                print(f"           enforced_by: {r.get('enforced_by_field')}")
        if orphans:
            if gaps:
                print()
            print(f"Orphan checks ({len(orphans)}) — code enforces a rule not in LEARNINGS-LEDGER:")
            for o in orphans:
                print(f"  {o['filename']:34s} {o.get('rule_ref') or '(no rule_ref)'}")
                print(f"           status: {o.get('status')} · {o.get('summary')}")
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
    if orphans:
        print()
        print(f"Orphan checks ({len(orphans)}) — code enforces a rule not in LEARNINGS-LEDGER:")
        for o in orphans:
            print(f"  ◇ {o['filename']:34s} {o.get('rule_ref') or '(no rule_ref)'}")
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
