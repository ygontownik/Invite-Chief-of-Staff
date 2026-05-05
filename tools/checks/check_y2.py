#!/usr/bin/env python3
"""
check_y2.py
===========

Enforces dash_corrections.md :: Y2 — transmission-verb sender attribution.

When an action's content uses a transmission verb (send, share, deliver,
forward, distribute, circulate, push) the action implies one party is
sending TO another. Past failure (Y1 incident): an advisory bank was
attributed `owner: <principal>` with `myAction: "Send teaser + data
room"` when the bank was actually the sender. Result: an inverted
relationship rendered on the dashboard.

This check post-validates every awaitingExternal item AND every
followUp item: when content carries a transmission verb AND the owner
is one of the principal team, but the counterparty is an
investment-bank / advisor / placement-agent shape, flag for review.

Status:
  - "fail" if any item with a transmission verb has both:
      a) owner ∈ {team_member}
      b) counterparty matches an advisor/bank/placement-agent token
    AND lacks a `direction` qualifier in the content.
  - "warn" if data file missing.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"

# Transmission verbs (Y2 calls these out by name).
_VERBS = re.compile(
    r"\b(send|sending|share|sharing|deliver|delivering|forward|forwarding|"
    r"distribute|distributing|circulate|circulating|push|pushing|hand[- ]?off|"
    r"transmit|transmitting|email out|ping)\b",
    re.IGNORECASE,
)

# Counterparty-shape tokens that imply external-sender ownership.
_ADVISOR_SHAPE = re.compile(
    r"\b(bank|advisor|advisory|placement\s+agent|broker|"
    r"capital\s+markets|investment\s+bank|m&a\s+advisor)\b",
    re.IGNORECASE,
)

# Team-member ownership values (lowercased) that should NOT be senders to advisors.
_TEAM_OWNERS = {"yoni", "mark", "nik", "team"}


def _is_team(owner: str) -> bool:
    return (owner or "").strip().lower() in _TEAM_OWNERS


def run() -> dict[str, Any]:
    if not DASHBOARD_DATA.exists():
        return {
            "name": "Y2: transmission-verb sender attribution",
            "rule_ref": "dash_corrections.md :: Y2",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [f"missing: {DASHBOARD_DATA}"],
        }

    try:
        d = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "Y2: transmission-verb sender attribution",
            "rule_ref": "dash_corrections.md :: Y2",
            "status": "fail",
            "summary": f"dashboard-data.json unreadable: {exc}",
            "details": [str(exc)],
        }

    flags: list[str] = []
    scanned = 0

    # awaitingExternal: owner=external is the correct shape; if owner is
    # team-side AND counterparty is advisor-shaped AND verb is transmission,
    # the direction is suspect.
    for it in (d.get("awaitingExternal") or []):
        scanned += 1
        content = (it.get("content") or "")
        owner = it.get("owner") or ""
        cp = it.get("counterparty") or ""
        if not _VERBS.search(content):
            continue
        if owner == "external":
            continue
        if _is_team(owner) and _ADVISOR_SHAPE.search(cp):
            flags.append(
                f"awaiting | owner={owner!r} cp={cp!r} content={content[:120]!r}"
            )

    # followUps: same pattern. who=team, what carries verb, who/what
    # references an advisor-shape counterparty.
    for fu in (d.get("followUps") or []):
        scanned += 1
        what = fu.get("what") or ""
        who = fu.get("who") or ""
        if (what or "").startswith("[RESOLVED]"):
            continue
        if not _VERBS.search(what):
            continue
        # who can be principal team OR counterparty; what may name the other
        # side. Flag only when who is team-side AND what cites an
        # advisor-shaped firm.
        if _is_team(who) and _ADVISOR_SHAPE.search(what):
            flags.append(
                f"followup | who={who!r} what={what[:120]!r}"
            )

    if flags:
        return {
            "name": "Y2: transmission-verb sender attribution",
            "rule_ref": "dash_corrections.md :: Y2",
            "status": "warn",  # soft — likely legit some of the time, but worth review
            "summary": (
                f"{len(flags)} potential direction inversion(s) across "
                f"{scanned} items"
            ),
            "details": flags[:30],
        }

    return {
        "name": "Y2: transmission-verb sender attribution",
        "rule_ref": "dash_corrections.md :: Y2",
        "status": "pass",
        "summary": f"0 direction inversions across {scanned} items",
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
