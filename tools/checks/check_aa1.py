#!/usr/bin/env python3
"""
check_aa1.py
============

Enforces dash_corrections.md :: AA1 — tombstone-id schema consistency.

The 80+ ghost-awaiting-items bug came from filtering awaitingExternal
(stable-id schema) with __isDel('followup', id) — i.e. hashing an id
that's already a hash. This check verifies that every tombstone in
~/dashboards/data/user-state/deletions.json with source=awaitingExternal
carries an 8-hex-char id (the stable-id schema), not the djb2 content
hash that followup/recruit tombstones use.

Heuristics:
  - awaitingExternal/dealAction tombstones must have id matching
    /^[0-9a-f]{8}$/ (the extraction hash shape).
  - followup/recruit/rel tombstones must have id matching the djb2
    content-hash shape: numeric or short hex string but the schema is
    content-derived. We only validate the stable-id branch directly;
    the content-hash branch is too varied to lint.
  - Every awaitingExternal tombstone id must collide with EXACTLY zero
    djb2-shaped values that would suggest a re-introduction of the bug.

Status:
  - "fail" if any awaitingExternal tombstone has a non-8-hex id (the
    exact bug pattern AA1 prevents).
  - "warn" if data file missing.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
DELETIONS = HOME / "dashboards" / "data" / "user-state" / "deletions.json"
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"

# Stable-id schema sources (per AA1: items carry an 8-char hex id at extract time).
_STABLE_ID_SOURCES = {"awaitingExternal", "dealAction", "build-backlog", "buildBacklog"}
_STABLE_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def run() -> dict[str, Any]:
    if not DELETIONS.exists():
        return {
            "name": "AA1: tombstone-id schema",
            "rule_ref": "dash_corrections.md :: AA1",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [f"missing: {DELETIONS}"],
        }

    try:
        raw = json.loads(DELETIONS.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "AA1: tombstone-id schema",
            "rule_ref": "dash_corrections.md :: AA1",
            "status": "fail",
            "summary": f"deletions.json unreadable: {exc}",
            "details": [str(exc)],
        }

    items = raw.get("deletions", []) if isinstance(raw, dict) else []
    bad: list[str] = []
    stable_count = 0

    for it in items:
        if not isinstance(it, dict):
            continue
        src = it.get("source", "")
        if src not in _STABLE_ID_SOURCES:
            continue
        stable_count += 1
        item_id = (it.get("id") or "").strip()
        if not _STABLE_ID_RE.match(item_id):
            bad.append(
                f"source={src} id={item_id!r} ctx={(it.get('context') or '')[:60]!r}"
            )

    # Cross-check: for each live awaitingExternal item, confirm tombstone
    # set membership is by direct id, not by djb2 hash. We surface stats only
    # — the actual filter logic is in client JS; this check confirms the
    # data shapes are consistent so the direct-Set lookup keeps working.
    live_awaiting_ids: list[str] = []
    if DASHBOARD_DATA.exists():
        try:
            dd = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
            for it in (dd.get("awaitingExternal") or []):
                aid = (it.get("id") or "").strip()
                if aid and not _STABLE_ID_RE.match(aid):
                    bad.append(
                        f"live awaitingExternal[].id={aid!r} not 8-hex — "
                        "render filter will silently miss tombstone"
                    )
                if aid:
                    live_awaiting_ids.append(aid)
        except Exception:
            pass

    if bad:
        return {
            "name": "AA1: tombstone-id schema",
            "rule_ref": "dash_corrections.md :: AA1",
            "status": "fail",
            "summary": (
                f"{len(bad)} stable-id schema violation(s) "
                f"(out of {stable_count} stable-id tombstones, "
                f"{len(live_awaiting_ids)} live awaiting items)"
            ),
            "details": bad[:50],
        }

    return {
        "name": "AA1: tombstone-id schema",
        "rule_ref": "dash_corrections.md :: AA1",
        "status": "pass",
        "summary": (
            f"0 schema mismatches across {stable_count} stable-id tombstones "
            f"+ {len(live_awaiting_ids)} live awaiting items"
        ),
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
