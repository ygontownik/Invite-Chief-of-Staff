#!/usr/bin/env python3
"""check_l0021.py — Rule L0021: UI must be functional, not placeholder.

Rule L0021: Every form/button must have a working backend route. Never
ship UI that 404s or no-ops on submit.

Check logic:
  - Extract every `fetch('/path'` and `fetch("/path"` call-site from
    ~/cos-pipeline/templates/cos-dashboard.template.html.
  - Extract every server route handler from cos-dashboard-server.py
    (both `self.path == '/x'` exact matches and
    `self.path.startswith('/x'` prefix matches).
  - A frontend route is considered handled if it matches exactly OR
    starts with any registered prefix.
  - Fail if any fetched route has no handler.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
TEMPLATE = HOME / "cos-pipeline" / "templates" / "cos-dashboard.template.html"
SERVER = HOME / "cos-pipeline" / "cos-dashboard-server.py"

_FETCH_RE = re.compile(r"""fetch\(\s*['"`](/[^'"`?\s]+)""")
_EXACT_RE = re.compile(r"""self\.path\s*==\s*['"](/[^'"]+|/)['"]""")
_PREFIX_RE = re.compile(r"""self\.path\.startswith\(\s*['"](/[^'"]+)['"]""")


def run() -> dict[str, Any]:
    if not TEMPLATE.exists():
        return {
            "name": "L0021: UI functional / no placeholder routes",
            "rule_ref": "L0021",
            "status": "fail",
            "summary": f"template missing: {TEMPLATE}",
            "details": {"template": str(TEMPLATE)},
        }
    if not SERVER.exists():
        return {
            "name": "L0021: UI functional / no placeholder routes",
            "rule_ref": "L0021",
            "status": "fail",
            "summary": f"server missing: {SERVER}",
            "details": {"server": str(SERVER)},
        }

    try:
        tpl = TEMPLATE.read_text(encoding="utf-8", errors="replace")
        srv = SERVER.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "name": "L0021: UI functional / no placeholder routes",
            "rule_ref": "L0021",
            "status": "fail",
            "summary": f"read error: {exc}",
            "details": {"error": str(exc)},
        }

    fetched: set[str] = set(_FETCH_RE.findall(tpl))
    # Strip query strings just in case.
    fetched = {f.split("?", 1)[0].rstrip("/") or "/" for f in fetched}

    exact_routes: set[str] = {r.rstrip("/") or "/" for r in _EXACT_RE.findall(srv)}
    prefix_routes: set[str] = {r.rstrip("/") for r in _PREFIX_RE.findall(srv)}

    unhandled: list[str] = []
    for route in sorted(fetched):
        if route in exact_routes:
            continue
        if any(route == p or route.startswith(p + "/") or route.startswith(p)
               for p in prefix_routes):
            continue
        unhandled.append(route)

    if unhandled:
        return {
            "name": "L0021: UI functional / no placeholder routes",
            "rule_ref": "L0021",
            "status": "fail",
            "summary": (
                f"{len(unhandled)} frontend fetch route(s) have no server handler "
                f"(of {len(fetched)} total)"
            ),
            "details": {
                "unhandled": unhandled[:30],
                "fetched_count": len(fetched),
                "exact_handler_count": len(exact_routes),
                "prefix_handler_count": len(prefix_routes),
            },
        }

    return {
        "name": "L0021: UI functional / no placeholder routes",
        "rule_ref": "L0021",
        "status": "pass",
        "summary": (
            f"all {len(fetched)} frontend fetch route(s) wired to a server handler"
        ),
        "details": {
            "fetched_count": len(fetched),
            "exact_handler_count": len(exact_routes),
            "prefix_handler_count": len(prefix_routes),
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
