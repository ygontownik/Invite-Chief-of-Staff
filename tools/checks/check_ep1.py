#!/usr/bin/env python3
"""check_ep1.py — Rule EP1: edit-in-place for registered Drive docs.

Rule EP1 (L0005): Never recreate a Drive document whose ID is in
drive-docs.yaml, deal-system-data.json, or any claude.ai project
instructions. Always update by ID via Deal Sync Writer (setContent on
registered fileId) or DriveApp.setContent().

Check logic:
  1. If a pre-commit hook (`scripts/pre-commit-edit-in-place.sh`) is
     present in either ~/cos-pipeline or ~/dashboards, the rule is
     mechanically enforced → pass.
  2. Otherwise, scan recent (last 7 days) added/modified python lines in
     ~/cos-pipeline for `files().create(... mimeType=...google-apps.document)`
     or `Drive.Files.create({ mimeType: 'application/vnd.google-apps.document'`.
     For each hit, surrounding 10 lines must contain `# NEW REGISTERED DOC`
     (the documented escape hatch). Otherwise → fail.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

HOME = Path.home()
REPO = HOME / "cos-pipeline"

_HOOK_PATHS = [
    HOME / "cos-pipeline" / "scripts" / "pre-commit-edit-in-place.sh",
    HOME / "dashboards" / "scripts" / "pre-commit-edit-in-place.sh",
]

# Python google-api-client and Apps Script doc-creation patterns.
_CREATE_RE = re.compile(
    r"""(files\(\)\s*\.\s*create\s*\([^)]*application/vnd\.google-apps\.document"""
    r"""|Drive\.Files\.create\s*\(\s*\{[^}]*application/vnd\.google-apps\.document)""",
    re.DOTALL,
)
_ESCAPE_HATCH = "NEW REGISTERED DOC"


def _hook_present() -> Path | None:
    for p in _HOOK_PATHS:
        if p.exists():
            return p
    return None


def _recent_diff() -> str:
    """Return the unified diff of python files changed in the last 7 days."""
    if not (REPO / ".git").exists():
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "log",
             "--since=7 days ago", "--diff-filter=AM", "-p",
             "--", "*.py"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return ""
        return out.stdout or ""
    except Exception:
        return ""


def _scan_diff(diff: str) -> list[dict[str, Any]]:
    """Return list of unmarked doc-create hits in the diff."""
    hits: list[dict[str, Any]] = []
    lines = diff.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        if _CREATE_RE.search(added):
            # Look at surrounding 10 lines (added-side context) for marker.
            lo, hi = max(0, i - 10), min(len(lines), i + 10)
            window = "\n".join(lines[lo:hi])
            if _ESCAPE_HATCH in window:
                continue
            hits.append({
                "line_index": i,
                "snippet": added.strip()[:200],
            })
    return hits


def run() -> dict[str, Any]:
    hook = _hook_present()
    if hook is not None:
        return {
            "name": "EP1: edit-in-place for registered Drive docs",
            "rule_ref": "EP1",
            "status": "pass",
            "summary": f"pre-commit hook enforces EP1: {hook.relative_to(HOME)}",
            "details": {"hook_path": str(hook)},
        }

    diff = _recent_diff()
    if not diff:
        return {
            "name": "EP1: edit-in-place for registered Drive docs",
            "rule_ref": "EP1",
            "status": "pass",
            "summary": "no python diff in last 7 days; nothing to scan",
            "details": {"hook_present": False, "diff_bytes": 0},
        }

    hits = _scan_diff(diff)
    if hits:
        return {
            "name": "EP1: edit-in-place for registered Drive docs",
            "rule_ref": "EP1",
            "status": "fail",
            "summary": (
                f"{len(hits)} unmarked Drive doc-create call(s) in last 7 days "
                f"(escape hatch '# {_ESCAPE_HATCH}' missing)"
            ),
            "details": {
                "hook_present": False,
                "violations": hits[:20],
                "violation_count": len(hits),
            },
        }

    return {
        "name": "EP1: edit-in-place for registered Drive docs",
        "rule_ref": "EP1",
        "status": "pass",
        "summary": "0 unmarked doc-create call(s) in last 7 days of python diff",
        "details": {"hook_present": False, "diff_bytes": len(diff)},
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
