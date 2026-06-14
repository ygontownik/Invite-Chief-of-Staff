#!/usr/bin/env python3
"""
check_pi1.py
============

Enforces PI1 — project-instructions coverage for all online deal projects.

A deal is "online" when it has a project_url in sync-state.json (meaning
/capture-deal-chats actively scrapes it). If that deal has no
project_instructions.doc_id in drive-docs.yaml, /refresh-project-instructions
silently skips it — the project never receives the SESSION START PROTOCOL
and its chats operate without firm context, status docs, or DEAL-INTEL
emission instructions.

This check surfaces that mismatch before it causes a silent miss.

Status:
  - "fail" if any online deal (has project_url) lacks project_instructions.doc_id
  - "warn" if drive-docs.yaml or sync-state.json is missing/unreadable
  - "pass" if every online deal has a wired doc_id (or has no project_url)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

HOME = Path.home()
SYNC_STATE = HOME / "cos-pipeline" / "tools" / "sync-state.json"
DRIVE_DOCS = HOME / "cos-pipeline-config-tomac" / "drive-docs.yaml"


def run() -> dict[str, Any]:
    if not SYNC_STATE.exists():
        return {
            "name": "PI1: project-instructions coverage",
            "rule_ref": "PI1",
            "status": "warn",
            "summary": "sync-state.json not found — skipping",
            "details": [str(SYNC_STATE)],
        }
    if not DRIVE_DOCS.exists():
        return {
            "name": "PI1: project-instructions coverage",
            "rule_ref": "PI1",
            "status": "warn",
            "summary": "drive-docs.yaml not found — skipping",
            "details": [str(DRIVE_DOCS)],
        }

    try:
        sync_state = json.loads(SYNC_STATE.read_text())
    except Exception as e:
        return {
            "name": "PI1: project-instructions coverage",
            "rule_ref": "PI1",
            "status": "warn",
            "summary": f"sync-state.json unreadable: {e}",
            "details": [],
        }

    try:
        drive_docs = yaml.safe_load(DRIVE_DOCS.read_text())
    except Exception as e:
        return {
            "name": "PI1: project-instructions coverage",
            "rule_ref": "PI1",
            "status": "warn",
            "summary": f"drive-docs.yaml unreadable: {e}",
            "details": [],
        }

    deal_docs = drive_docs.get("deal_docs", {})

    missing: list[str] = []
    online_count = 0

    for slug, state in sync_state.items():
        if not state.get("project_url"):
            continue
        online_count += 1
        entry = deal_docs.get(slug, {})
        pi = entry.get("project_instructions", {})
        doc_id = pi.get("doc_id") if isinstance(pi, dict) else None
        if not doc_id:
            missing.append(
                f"{slug}: has project_url but no project_instructions.doc_id "
                f"in drive-docs.yaml — /refresh-project-instructions will silently skip"
            )

    if missing:
        return {
            "name": "PI1: project-instructions coverage",
            "rule_ref": "PI1",
            "status": "fail",
            "summary": (
                f"{len(missing)} of {online_count} online deal(s) missing "
                f"project_instructions.doc_id"
            ),
            "details": missing,
        }

    return {
        "name": "PI1: project-instructions coverage",
        "rule_ref": "PI1",
        "status": "pass",
        "summary": (
            f"all {online_count} online deal(s) have project_instructions.doc_id wired"
        ),
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
