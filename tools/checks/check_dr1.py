#!/usr/bin/env python3
"""check_dr1.py — Rule DR1: overwrite-not-skip on same-deal_id registry collision.

tcip_new_deal.py must OVERWRITE an existing deal_id entry in
compile_drive_writeback.py rather than skip — a same-id collision
usually signals an orphan Drive file from a prior failed /new-deal run.
Skipping leaves the mapping pointing at the orphan while THIS run
creates an unregistered new file.

Check inspects update_compile_writeback() in tcip_new_deal.py for:
  - presence of an overwrite path (.sub + .write_text)
  - absence of a skip-on-collision early return that bypasses the write
    (an identity-check early return — old_id == file_id — is OK)

Status: pass = overwrite present, no skip; warn = function shape
unrecognized; fail = skip-on-collision detected.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
TARGET = HOME / "cos-pipeline" / "tools" / "tcip_new_deal.py"
FUNC_NAME = "update_compile_writeback"

_NAME = "DR1: overwrite-not-skip on registry collision"
_REF = "DR1"


def _result(status: str, summary: str, **details: Any) -> dict[str, Any]:
    return {"name": _NAME, "rule_ref": _REF, "status": status,
            "summary": summary, "details": details}


def run() -> dict[str, Any]:
    if not TARGET.exists():
        return _result("warn", f"target file not present: {TARGET}",
                       path=str(TARGET))

    try:
        src = TARGET.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception as exc:
        return _result("fail", f"could not parse {TARGET.name}: {exc}",
                       error=str(exc))

    func_src = None
    func_lineno = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == FUNC_NAME:
            func_lineno = node.lineno
            func_src = ast.get_source_segment(src, node) or ""
            break

    if not func_src:
        return _result("warn", f"{FUNC_NAME}() not found in {TARGET.name}",
                       path=str(TARGET))

    has_substitution = bool(re.search(r"existing_re\.sub\(", func_src))
    has_write = bool(re.search(r"\.write_text\(", func_src))

    # Scan for skip-on-collision: a bare `return` inside the existing-
    # match branch BEFORE any write_text. Allowlist identity-check
    # returns (old_id == file_id / "already") which are correct no-ops.
    lines = func_src.splitlines()
    suspicious_skip: list[str] = []
    in_match_block = False
    saw_write = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if "existing_re.search" in s or s.startswith("m = existing_re"):
            in_match_block = True
            saw_write = False
            continue
        if not in_match_block:
            continue
        if ".write_text(" in s:
            saw_write = True
        if s == "return" or s.startswith("return"):
            window = "\n".join(lines[max(0, i - 4):i]).lower()
            if "old_id == file_id" in window or "already" in window:
                continue
            if not saw_write:
                suspicious_skip.append(f"line {i + 1}: {s[:120]}")

    if suspicious_skip:
        return _result(
            "fail",
            f"DR1: {len(suspicious_skip)} suspected skip-on-collision "
            f"return(s) in {FUNC_NAME}()",
            func=FUNC_NAME, func_lineno=func_lineno,
            suspicious_returns=suspicious_skip[:10],
        )

    if has_substitution and has_write:
        return _result(
            "pass",
            f"DR1: {FUNC_NAME}() at line {func_lineno} has overwrite path "
            "(.sub + .write_text) and no skip-on-collision",
            func=FUNC_NAME, func_lineno=func_lineno,
            has_substitution=has_substitution, has_write=has_write,
        )

    return _result(
        "warn",
        f"DR1: {FUNC_NAME}() shape changed — could not confirm overwrite path",
        func=FUNC_NAME, func_lineno=func_lineno,
        has_substitution=has_substitution, has_write=has_write,
    )


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
