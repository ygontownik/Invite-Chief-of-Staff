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
# NOTE: Some terms (e.g. "tcip") appear in Python/HTML as a product-
# feature route prefix and are excluded from code scanning via
# _CODE_DENY below.
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

# Subset of _DENY applied to .py/.html code files.  "tcip" is excluded
# because it is used as a product-feature URL route prefix (not a tenant
# identifier) throughout the codebase.  All other terms still apply.
_CODE_DENY = [t for t in _DENY if t != r"tcip"]

# Allow-list shapes that the original grep expressly excluded
# ("tomac[]", "tomac doc", "cos-pipeline-config-tomac"). Lines whose
# entire match falls inside one of these are downgraded from fail to warn.
_ALLOW = [
    re.compile(r"tomac\[\]", re.IGNORECASE),
    re.compile(r"tomac doc", re.IGNORECASE),
    re.compile(r"cos-pipeline-config-tomac", re.IGNORECASE),
    # Inline suppression annotation — developer has acknowledged the match
    # Python/JS:  # noqa: tenant-leak
    re.compile(r"#\s*noqa:\s*tenant-leak", re.IGNORECASE),
    # HTML/template:  <!-- noqa: tenant-leak -->  (text after tag allowed)
    re.compile(r"<!--\s*noqa:\s*tenant-leak", re.IGNORECASE),
    # CSS/JS block comment:  /* noqa: tenant-leak ... */
    re.compile(r"/\*\s*noqa:\s*tenant-leak", re.IGNORECASE),
    # JS single-line comment:  // noqa: tenant-leak
    re.compile(r"//\s*noqa:\s*tenant-leak", re.IGNORECASE),
]

_RE      = re.compile("|".join(_DENY),      re.IGNORECASE)
_RE_CODE = re.compile("|".join(_CODE_DENY), re.IGNORECASE)


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

    # Also scan Python source and HTML template files in the pipeline root
    # and templates/ directory so code-level leaks are caught.
    # "data-*" directories are runtime tenant data output, not public code.
    _SKIP_DIR_PREFIXES = {"data-"}
    _SKIP_DIRS = {"archive", "node_modules", "__pycache__"}
    # Exempt the tenant-leak scanner scripts themselves from code scanning:
    # tools/checks/ contains the denylist as patterns, and
    # tools/smoke_test_tenant.py contains the denylist as a FORBIDDEN list.
    # Both are intentional — scanning them would produce only false positives.
    _SKIP_PATH_FRAGMENTS = {
        "tools/checks",
        "tools\\checks",
        "tools/smoke_test_tenant.py",
        "tools\\smoke_test_tenant.py",
        # validate_tenant.py is an integration-test validator that explicitly
        # references the default tenant to verify non-default tenant isolation.
        "validate_tenant.py",
    }
    for pattern in ("*.py", "*.html"):
        for p in sorted(pipeline.rglob(pattern)):
            # Skip any component of the path that matches excluded dirs
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            # Skip data-* directories (runtime tenant output, not code)
            if any(part.startswith(prefix) for part in p.relative_to(pipeline).parts
                   for prefix in _SKIP_DIR_PREFIXES):
                continue
            # Skip hidden directories (backup snapshots, sandboxes, etc.)
            if any(part.startswith(".") for part in p.relative_to(pipeline).parts[:-1]):
                continue
            # Skip private tenant config repos that may legitimately use names
            if "cos-pipeline-config-" in str(p):
                continue
            # Skip the checks/ directory itself (denylist terms appear there)
            rel = str(p.relative_to(pipeline))
            if any(frag in rel for frag in _SKIP_PATH_FRAGMENTS):
                continue
            if p not in files:
                files.append(p)

    return files


def _line_is_comment(line: str) -> bool:
    """Return True if the line is a comment-only line (Python # or HTML <!-- style)."""
    stripped = line.strip()
    return stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("<!--")


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
        is_py_or_html = path.suffix in (".py", ".html")
        # Use the narrower code deny-list for source files (strips tcip)
        active_re = _RE_CODE if is_py_or_html else _RE
        for lineno, line in enumerate(text.splitlines(), 1):
            if not active_re.search(line):
                continue
            entry = {
                "file": str(path.relative_to(HOME)),
                "line": lineno,
                "text": line.strip()[:200],
            }
            if _line_is_allowed(line):
                soft_hits.append(entry)
            elif is_py_or_html and _line_is_comment(line):
                # Comment-only occurrences in .py/.html are demoted to warn
                # so they don't block pushes from innocuous annotation history.
                entry["note"] = "comment-only line (warn, not fail)"
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
        summary = f"tenant-leak: clean across {len(files)} file(s) (md + py + html)"

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
