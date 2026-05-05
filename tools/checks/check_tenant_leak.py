#!/usr/bin/env python3
"""
check_tenant_leak.py
====================

Scan public-config and docs Markdown files under ~/cos-pipeline/ for
tenant-identifying tokens that should never appear in code intended to
ship to subscribers. The denylist is kept verbatim from the working-norms
section of dashboards/docs/HANDOFF_NEXT_SESSION.md so a single edit there
and here stays in sync if it ever changes.

Status semantics:
  - "pass" : zero matches
  - "warn" : matches found ONLY in known false-positive shapes
  - "fail" : real matches surface at least one tenant identifier in a
             public file

Run as part of system_health.py; also invokable standalone for spot
checks:
  python3 ~/cos-pipeline/tools/checks/check_tenant_leak.py
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

HOME = Path.home()

# Verbatim denylist (mirrors HANDOFF_NEXT_SESSION.md lines 171-175).
# Words/phrases that must not appear in shipped public configs/docs.
_DENY = [
    r"tomac\b",
    r"cholla",
    r"thunderhead",
    r"black bayou",
    r"mercuria",
    r"harbert",
    r"gideon",
    r"wafra",
    r"piper",
    r"berkman",
    r"astris",
    r"fit ventures",
    r"us towers",
    r"pngts",
    r"tcip",
    r"reinova",
    r"onesearch",
    r"korn ferry",
    r"hudson bay",
    r"quantum",
    r"citadel",
    r"castleton",
    r"grosvenor",
    r"ridgewood",
    r"barton",
    r"maven",
    r"omerta",
]

# Allow-list shapes that the original grep expressly excluded
# ("tomac[]", "tomac doc", "cos-pipeline-config-tomac"). Lines whose
# entire match falls inside one of these are downgraded from fail to warn.
_ALLOW = [
    re.compile(r"tomac\[\]", re.IGNORECASE),
    re.compile(r"tomac doc", re.IGNORECASE),
    re.compile(r"cos-pipeline-config-tomac", re.IGNORECASE),
]

_RE = re.compile("|".join(_DENY), re.IGNORECASE)


def _scan_dirs() -> list[Path]:
    pipeline = HOME / "cos-pipeline"
    roots = [
        pipeline / "config",
        pipeline / "docs",
    ]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(sorted(root.rglob("*.md")))
    # Plus the explicit dash_corrections.md target from the audit shape,
    # in case it is ever moved out of config/.
    explicit = pipeline / "config" / "dash_corrections.md"
    if explicit.exists() and explicit not in files:
        files.append(explicit)
    return files


def _line_is_allowed(line: str) -> bool:
    return any(p.search(line) for p in _ALLOW)


def run() -> dict[str, Any]:
    files = _scan_dirs()
    hard_hits: list[dict[str, Any]] = []
    soft_hits: list[dict[str, Any]] = []

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            soft_hits.append(
                {
                    "file": str(path.relative_to(HOME)),
                    "error": f"read failed: {exc}",
                }
            )
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if not _RE.search(line):
                continue
            entry = {
                "file": str(path.relative_to(HOME)),
                "line": lineno,
                "text": line.strip()[:200],
            }
            if _line_is_allowed(line):
                soft_hits.append(entry)
            else:
                hard_hits.append(entry)

    if hard_hits:
        status = "fail"
        summary = (
            f"tenant-leak: {len(hard_hits)} disallowed match(es) "
            f"across {len({h['file'] for h in hard_hits})} file(s)"
        )
    elif soft_hits:
        status = "warn"
        summary = (
            f"tenant-leak: {len(soft_hits)} allow-listed match(es) "
            f"(likely fine; review)"
        )
    else:
        status = "pass"
        summary = f"tenant-leak: clean across {len(files)} markdown file(s)"

    return {
        "name": "tenant_leak",
        "status": status,
        "summary": summary,
        "details": {
            "files_scanned": len(files),
            "hard_hits": hard_hits[:50],
            "soft_hits": soft_hits[:50],
            "hard_hit_count": len(hard_hits),
            "soft_hit_count": len(soft_hits),
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
