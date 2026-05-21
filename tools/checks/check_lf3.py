#!/usr/bin/env python3
"""
check_lf3.py — Rule LF3 (L0045): Use st_atime, not st_mtime, for staleness.

The "is this file stale enough to route?" gate in process_folder() must
use st_atime (last access). st_mtime is allowed for the separate
"don't touch in-flight downloads" gate (MIN_QUIET_SEC) — different
question, legitimately needs mtime.

Status:
  - pass: staleness gate (the STALE_AFTER_SEC if-block) references st_atime
  - fail: staleness gate references st_mtime only (LF3 regression)
  - warn: gate not locatable (refactor without sentinel)
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

HOME = Path.home()
ORGANIZER = HOME / "cos-pipeline" / "tools" / "local_file_organizer.py"
_STALE_CONST = "STALE_AFTER_SEC"


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def run() -> dict[str, Any]:
    if not ORGANIZER.exists():
        return {"name": "LF3: atime not mtime", "rule_ref": "LF3",
                "status": "fail", "summary": f"missing: {ORGANIZER}",
                "details": {"path": str(ORGANIZER)}}
    try:
        tree = ast.parse(ORGANIZER.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return {"name": "LF3: atime not mtime", "rule_ref": "LF3",
                "status": "fail", "summary": f"syntax error: {exc}",
                "details": {"error": str(exc)}}

    fn = _find_function(tree, "process_folder")
    if fn is None:
        return {"name": "LF3: atime not mtime", "rule_ref": "LF3",
                "status": "warn", "summary": "process_folder() not found",
                "details": {}}

    # Tally st_atime / st_mtime refs across process_folder() for the report.
    atime_lines, mtime_lines = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute):
            if node.attr == "st_atime":
                atime_lines.append(node.lineno)
            elif node.attr == "st_mtime":
                mtime_lines.append(node.lineno)

    # Find the staleness gate: the if-block whose test references STALE_AFTER_SEC.
    stale_node: ast.If | None = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Name) and sub.id == _STALE_CONST:
                stale_node = node
                break
        if stale_node:
            break

    if stale_node is None:
        return {"name": "LF3: atime not mtime", "rule_ref": "LF3",
                "status": "warn",
                "summary": f"{_STALE_CONST} gate not located in process_folder()",
                "details": {"st_atime_refs": atime_lines,
                            "st_mtime_refs": mtime_lines}}

    # Inspect attributes referenced inside the gate's test subtree.
    gate_attrs: set[str] = set()
    for sub in ast.walk(stale_node.test):
        if isinstance(sub, ast.Attribute) and sub.attr.startswith("st_"):
            gate_attrs.add(sub.attr)

    line = stale_node.lineno
    if "st_atime" in gate_attrs:
        status = "pass"
        summary = (f"staleness gate @ line {line} uses st_atime "
                   f"(atime={len(atime_lines)}, mtime={len(mtime_lines)})")
    elif gate_attrs == {"st_mtime"}:
        status = "fail"
        summary = f"LF3 REGRESSION: gate @ line {line} uses st_mtime, not st_atime"
    else:
        status = "warn"
        summary = f"gate @ line {line} uses unexpected attrs: {sorted(gate_attrs)}"

    return {"name": "LF3: atime not mtime", "rule_ref": "LF3",
            "status": status, "summary": summary,
            "details": {"stale_gate_line": line,
                        "gate_attrs": sorted(gate_attrs),
                        "st_atime_ref_count": len(atime_lines),
                        "st_mtime_ref_count": len(mtime_lines),
                        "st_atime_lines": atime_lines,
                        "st_mtime_lines": mtime_lines}}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
