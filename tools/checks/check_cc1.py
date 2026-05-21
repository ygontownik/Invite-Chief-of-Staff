#!/usr/bin/env python3
"""check_cc1.py — Rule CC1: Claude Code over API.

Rule CC1 (L0001): Always prefer Claude Code (CLI/SDK) over raw Anthropic
API calls. Hierarchy:
  1. Claude Code native capability (subagent, Skill, scheduled task, hook)
  2. Claude Code SDK / Agent SDK wrapper
  3. Raw anthropic SDK (only when Claude Code has no equivalent)

Hard enforcement of "no raw anthropic SDK" lives in check_l0023.py. This
module binds CC1 to its companion artifact: the `_claude_dispatch.py`
wrapper. We confirm the wrapper exists and measure adoption.

Status:
  - "fail" if `_claude_dispatch.py` is missing (rule cannot be honored).
  - "warn" if usage ratio (files importing _claude_dispatch /
    (files importing _claude_dispatch + files importing anthropic raw))
    is < 50%.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
DISPATCH = HOME / "cos-pipeline" / "_claude_dispatch.py"
TOOLS = HOME / "cos-pipeline" / "tools"

_DISPATCH_IMPORT_RE = re.compile(
    r"(from\s+_claude_dispatch\b|import\s+_claude_dispatch\b|_claude_dispatch\.call\b)"
)
_RAW_IMPORT_RE = re.compile(
    r"^\s*(from\s+anthropic\b|import\s+anthropic\b)", re.MULTILINE
)
_SKIP_DIRS = {".git", "__pycache__", "node_modules", "archive", ".venv", "venv"}


def _iter_py(root: Path) -> list[Path]:
    out: list[Path] = []
    if not root.exists():
        return out
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def run() -> dict[str, Any]:
    if not DISPATCH.exists():
        return {
            "name": "CC1: Claude Code over API",
            "rule_ref": "CC1",
            "status": "fail",
            "summary": f"_claude_dispatch.py missing at {DISPATCH}",
            "details": {"dispatch_path": str(DISPATCH)},
        }

    files = _iter_py(TOOLS)
    dispatch_users: list[str] = []
    raw_users: list[str] = []
    for p in files:
        if p.name == "_claude_dispatch.py":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = str(p.relative_to(HOME))
        except ValueError:
            rel = str(p)
        if _DISPATCH_IMPORT_RE.search(text):
            dispatch_users.append(rel)
        if _RAW_IMPORT_RE.search(text):
            raw_users.append(rel)

    total_anthropic_callers = len(set(dispatch_users) | set(raw_users))
    if total_anthropic_callers == 0:
        # Wrapper present, nothing in tools/ calls Anthropic at all — fine.
        return {
            "name": "CC1: Claude Code over API",
            "rule_ref": "CC1",
            "status": "pass",
            "summary": (
                f"_claude_dispatch.py present; 0 anthropic callers in "
                f"{len(files)} tools/ files"
            ),
            "details": {
                "dispatch_path": str(DISPATCH),
                "tools_scanned": len(files),
                "dispatch_users": 0,
                "raw_users": 0,
            },
        }

    ratio = len(dispatch_users) / total_anthropic_callers
    if ratio < 0.5:
        status = "warn"
        summary = (
            f"_claude_dispatch adoption {ratio:.0%} "
            f"({len(dispatch_users)} dispatch / {len(raw_users)} raw)"
        )
    else:
        status = "pass"
        summary = (
            f"_claude_dispatch adoption {ratio:.0%} "
            f"({len(dispatch_users)} dispatch / {len(raw_users)} raw)"
        )

    return {
        "name": "CC1: Claude Code over API",
        "rule_ref": "CC1",
        "status": status,
        "summary": summary,
        "details": {
            "dispatch_path": str(DISPATCH),
            "tools_scanned": len(files),
            "dispatch_users": len(dispatch_users),
            "raw_users": len(raw_users),
            "adoption_ratio": round(ratio, 3),
            "raw_user_sample": raw_users[:10],
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
