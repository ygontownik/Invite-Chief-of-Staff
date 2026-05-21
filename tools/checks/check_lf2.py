#!/usr/bin/env python3
"""
check_lf2.py — Rule LF2 (L0044): Routing precedence in local_file_organizer.

Canonical order: state-check → junk → deal-alias → personal → unsorted.
The state-check happens upstream in process_folder(); classify() owns
the remaining four. Static AST inspection.

Status:
  - pass: classify() returns in {junk, deal, personal, unsorted} order
          AND process_folder() short-circuits on state["moves"]
  - warn: extra branches added (organizer extended) — surfaces sequence
  - fail: order swapped, missing branch, or classify() not found
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
ORGANIZER = HOME / "cos-pipeline" / "tools" / "local_file_organizer.py"
_CANONICAL = ("junk", "deal", "personal", "unsorted")
_STATE_GATE_RE = re.compile(r"key\s+in\s+state\[['\"]moves['\"]\]")


def _find_classify(tree: ast.AST) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "classify":
            return node
    return None


def _extract_buckets(fn: ast.FunctionDef) -> list[str]:
    """Walk in source order, collecting bucket labels from `return (label, ...)`."""
    returns: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        val = node.value
        if isinstance(val, ast.Tuple) and val.elts:
            first = val.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                returns.append((node.lineno, first.value))
    returns.sort(key=lambda r: r[0])
    return [v for _, v in returns]


def _state_gate_line(src: str) -> int | None:
    for i, line in enumerate(src.splitlines(), 1):
        if _STATE_GATE_RE.search(line):
            return i
    return None


def run() -> dict[str, Any]:
    if not ORGANIZER.exists():
        return {"name": "LF2: routing precedence", "rule_ref": "LF2",
                "status": "fail", "summary": f"missing: {ORGANIZER}",
                "details": {"path": str(ORGANIZER)}}
    src = ORGANIZER.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {"name": "LF2: routing precedence", "rule_ref": "LF2",
                "status": "fail", "summary": f"syntax error: {exc}",
                "details": {"error": str(exc)}}

    fn = _find_classify(tree)
    if fn is None:
        return {"name": "LF2: routing precedence", "rule_ref": "LF2",
                "status": "fail", "summary": "classify() not found",
                "details": {}}

    observed = _extract_buckets(fn)
    canonical_seen = [b for b in observed if b in _CANONICAL]
    extras = [b for b in observed if b not in _CANONICAL]
    state_line = _state_gate_line(src)

    if canonical_seen != list(_CANONICAL):
        return {"name": "LF2: routing precedence", "rule_ref": "LF2",
                "status": "fail",
                "summary": f"classify() order {canonical_seen!r} != {list(_CANONICAL)!r}",
                "details": {"observed": observed, "expected": list(_CANONICAL),
                            "state_gate_line": state_line}}
    if state_line is None:
        return {"name": "LF2: routing precedence", "rule_ref": "LF2",
                "status": "fail",
                "summary": "state-check gate (key in state['moves']) not found",
                "details": {"observed": observed}}

    status = "warn" if extras else "pass"
    summary = f"precedence ok: state(@{state_line})→{'→'.join(_CANONICAL)}"
    if extras:
        summary += f" · extras: {extras}"
    return {"name": "LF2: routing precedence", "rule_ref": "LF2",
            "status": status, "summary": summary,
            "details": {"observed": observed, "expected": list(_CANONICAL),
                        "extras": extras, "state_gate_line": state_line}}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
