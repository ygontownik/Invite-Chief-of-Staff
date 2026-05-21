#!/usr/bin/env python3
"""check_l0023.py — Rule L0023: never raw Anthropic SDK in pipeline code.

Every Claude API call from ~/cos-pipeline/, ~/dashboards/{routines,app}/,
and ~/cos-pipeline-config-*/ must go through `_claude_dispatch.call()`.

Allow-listed: `_claude_dispatch.py` itself, comment-only lines, and
files with a top-of-file `# noqa: claude-dispatch-exempt` marker.

Status: fail = raw import in a non-exempt file; warn = leftover anthropic
import in a file that also uses _claude_dispatch; pass = clean.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()

_SCAN_ROOTS = [
    HOME / "cos-pipeline",
    HOME / "dashboards" / "routines",
    HOME / "dashboards" / "app",
]

_IMPORT_RE = re.compile(
    r"^\s*(from\s+anthropic\b|import\s+anthropic\b|anthropic\.Anthropic\(|anthropic\.Client\()"
)
_DISPATCH_REF_RE = re.compile(r"_claude_dispatch")
_EXEMPT_MARKER = "noqa: claude-dispatch-exempt"
_SKIP_DIRS = {".git", "__pycache__", "node_modules", "archive", ".venv", "venv"}


def _is_canonical_dispatch(path: Path) -> bool:
    return path.name == "_claude_dispatch.py"


def _file_is_exempt(text: str) -> bool:
    # Allow the marker anywhere in the first ~20 lines (top-of-file).
    head = "\n".join(text.splitlines()[:20])
    return _EXEMPT_MARKER in head


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            if any(part.startswith(".") for part in p.relative_to(root).parts[:-1]):
                continue
            out.append(p)
    return out


def run() -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    soft: list[dict[str, Any]] = []
    files_scanned = 0

    for path in _iter_py_files():
        files_scanned += 1
        if _is_canonical_dispatch(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _file_is_exempt(text):
            continue

        raw_hits: list[tuple[int, str]] = []
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if _IMPORT_RE.match(line):
                raw_hits.append((lineno, line.strip()[:200]))
        if not raw_hits:
            continue
        has_dispatch_ref = bool(_DISPATCH_REF_RE.search(text))
        try:
            rel = str(path.relative_to(HOME))
        except ValueError:
            rel = str(path)
        for lineno, snippet in raw_hits:
            entry = {"file": rel, "line": lineno, "snippet": snippet}
            if has_dispatch_ref:
                entry["note"] = "file also uses _claude_dispatch (likely leftover)"
                soft.append(entry)
            else:
                violations.append(entry)

    if violations:
        status = "fail"
        summary = (
            f"L0023: {len(violations)} raw anthropic import(s) across "
            f"{len({v['file'] for v in violations})} file(s) "
            f"(scanned {files_scanned})"
        )
    elif soft:
        status = "warn"
        summary = (
            f"L0023: {len(soft)} leftover anthropic import(s) in "
            f"files that already use _claude_dispatch"
        )
    else:
        status = "pass"
        summary = f"L0023: clean across {files_scanned} python file(s)"

    return {
        "name": "L0023: _claude_dispatch enforcement",
        "rule_ref": "L0023",
        "status": status,
        "summary": summary,
        "details": {
            "files_scanned": files_scanned,
            "violations": violations[:50],
            "soft_hits": soft[:25],
            "violation_count": len(violations),
            "soft_hit_count": len(soft),
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
