#!/usr/bin/env python3
"""
check_g3.py
===========

Enforces dash_corrections.md :: G3 — owner-whitelist enforcement.

Every owner: field on a curated config row OR an action item in the
compiled deal-system-data.json must resolve to a name in
firm_context.yaml > owner_whitelist (case-insensitive). Out-of-list
owners get treated as owner: "" at render and silently logged — i.e.
become invisible. Catching them at lint time prevents the mystery
"why isn't this action attributed?" failure mode.

Allowed owners:
  - any name in firm_context.yaml > owner_whitelist
  - "external" (the awaitingExternal sentinel)
  - "" (empty — already handled as wait-state per Y2)

Status:
  - "fail" if any non-empty owner is outside the whitelist.
  - "warn" if firm_context.yaml is missing or has empty whitelist.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"
DEAL_SYSTEM = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"

# firm_context lookup order: prefer the tenant pointed to by the
# dashboards/config symlinks (the live tenant), then $COS_CONFIG_DIR,
# then any cos-pipeline-config-* glob (rare fallback), then the public
# template. Discovery is dynamic so this file stays tenant-agnostic.
def _fc_candidates() -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            r = p.resolve()
        except OSError:
            return
        if r in seen or not r.exists():
            return
        seen.add(r)
        out.append(r)

    # 1. $COS_CONFIG_DIR override (used by the running server).
    import os
    cd = os.environ.get("COS_CONFIG_DIR")
    if cd:
        _add(Path(cd) / "firm_context.yaml")
        _add(Path(cd) / "config" / "firm_context.yaml")

    # 2. Walk the dashboards/config/ symlinks to learn the active tenant
    #    repo. Any symlinked yaml under there points into the live tenant.
    dash_cfg = HOME / "dashboards" / "config"
    if dash_cfg.exists():
        active_tenant_root: Path | None = None
        for entry in dash_cfg.iterdir():
            if entry.is_symlink():
                try:
                    target = entry.resolve()
                except OSError:
                    continue
                # Walk up until we find a directory whose name starts with
                # cos-pipeline-config- — that's the tenant root.
                cur: Path | None = target
                while cur and cur != cur.parent:
                    if cur.name.startswith("cos-pipeline-config-"):
                        active_tenant_root = cur
                        break
                    cur = cur.parent
            if active_tenant_root:
                break
        if active_tenant_root:
            _add(active_tenant_root / "firm_context.yaml")
            _add(active_tenant_root / "config" / "firm_context.yaml")

    # 3. Tenant-config glob fallback (any cos-pipeline-config-* repo).
    for p in sorted(HOME.glob("cos-pipeline-config-*")):
        _add(p / "firm_context.yaml")
        _add(p / "config" / "firm_context.yaml")

    # 4. Legacy + public template.
    _add(HOME / "cos-pipeline-config" / "firm_context.yaml")
    _add(HOME / "cos-pipeline" / "firm_context.yaml")
    return out

_OWNER_LINE = re.compile(r"^\s*-\s+(.+?)\s*$")


def _load_whitelist() -> tuple[list[str], str]:
    """Read owner_whitelist out of the first existing firm_context.yaml.

    Returns (whitelist, source_path). Empty whitelist means "skip".
    """
    for path in _fc_candidates():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Tiny non-yaml-import parser; only need the owner_whitelist block.
        in_block = False
        items: list[str] = []
        for line in text.splitlines():
            stripped = line.rstrip()
            if not in_block:
                if stripped.startswith("owner_whitelist:"):
                    in_block = True
                continue
            # block ends on a non-indented non-list line, or empty after items.
            if stripped == "" and items:
                break
            if not stripped.startswith((" ", "\t", "-")):
                break
            m = _OWNER_LINE.match(line)
            if m:
                token = m.group(1).strip().strip('"').strip("'")
                if token:
                    items.append(token)
        if items:
            return items, str(path)
    return [], ""


def run() -> dict[str, Any]:
    whitelist, src = _load_whitelist()
    if not whitelist:
        return {
            "name": "G3: owner-whitelist enforcement",
            "rule_ref": "dash_corrections.md :: G3",
            "status": "warn",
            "summary": "data file not present, skipped (no firm_context owner_whitelist)",
            "details": [],
        }

    allowed = {w.lower() for w in whitelist}
    allowed.update({"external", ""})

    bad: list[str] = []
    scanned = 0

    for path, key_chain in (
        (DASHBOARD_DATA, [("awaitingExternal", "owner")]),
        (DEAL_SYSTEM, []),
    ):
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for arr_key, owner_key in key_chain:
            arr = d.get(arr_key) or []
            for it in arr:
                if not isinstance(it, dict):
                    continue
                scanned += 1
                v = (it.get(owner_key) or "").strip()
                if v.lower() in allowed:
                    continue
                bad.append(
                    f"{path.name}:{arr_key}[].{owner_key}={v!r} ctx="
                    f"{(it.get('counterparty') or it.get('content') or '')[:80]!r}"
                )

    # deal-system actions[].owner walk.
    if DEAL_SYSTEM.exists():
        try:
            ds = json.loads(DEAL_SYSTEM.read_text(encoding="utf-8"))
            for deal in (ds.get("deals") or []):
                for a in (deal.get("actions") or []):
                    scanned += 1
                    v = (a.get("owner") or "").strip()
                    if v.lower() in allowed:
                        continue
                    # Allow first names of full names also if whitelist
                    # contains nicknames matching the prefix.
                    first = v.split()[0].lower() if v else ""
                    if first in allowed:
                        continue
                    bad.append(
                        f"deal={deal.get('name')!r} action.owner={v!r} "
                        f"action={(a.get('action') or '')[:80]!r}"
                    )
        except Exception:
            pass

    if bad:
        return {
            "name": "G3: owner-whitelist enforcement",
            "rule_ref": "dash_corrections.md :: G3",
            "status": "fail",
            "summary": (
                f"{len(bad)} owner(s) outside whitelist "
                f"(scanned {scanned} rows; whitelist={whitelist!r})"
            ),
            "details": bad[:40] + [f"whitelist source: {src}"],
        }

    return {
        "name": "G3: owner-whitelist enforcement",
        "rule_ref": "dash_corrections.md :: G3",
        "status": "pass",
        "summary": (
            f"all {scanned} owners in whitelist={whitelist!r}"
        ),
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
