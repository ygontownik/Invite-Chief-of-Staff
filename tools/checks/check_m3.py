#!/usr/bin/env python3
"""
check_m3.py
===========

Enforces dash_corrections.md :: M3 — followUps doc ranks above briefing prose.

The fact-reconciliation hierarchy: when the briefing prose claims a
deal is in state X but the followUps doc says state Y, followUps wins.
Past failure: an analyst pass updated `next_milestone` to "<counterparty>
proposal received" based on briefing prose; followUps actually showed
the team was DRAFTING the term sheet itself.

Detection: for every active deal (compiled deal-system-data.json),
compare the deal's curated next_milestone / stage prose against the
last 14 days of followUps that mention the deal. If the briefing
synopsis (briefingSynopsis.lastBriefingSnippet or any string-valued
field on captureSummary) contains a phrase that contradicts the
followUps signal — specifically "received", "complete", "closed"
present in briefing while followUps show open same-deal items with
verbs implying still-active work — flag.

This is a soft warning: divergence is fine, contradiction is not.

Status:
  - "warn" when divergence detected (analyst pass needed).
  - "warn" when data missing.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"
DEAL_SYSTEM = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"

# "Past-tense / closed" phrases in briefing prose suggesting completion.
_CLOSED_RE = re.compile(
    r"\b(received|completed|closed|signed|executed|finalized|delivered|"
    r"submitted|wrapped|done)\b",
    re.IGNORECASE,
)
# "Active / in-flight" verbs from followUps that contradict closure.
_ACTIVE_RE = re.compile(
    r"\b(draft|drafting|preparing|reviewing|finalizing|aligning|negotiating|"
    r"awaiting|pending|tbd|owed|expecting)\b",
    re.IGNORECASE,
)


def _within_days(dt_str: str, days: int) -> bool:
    if not dt_str:
        return False
    try:
        d = datetime.fromisoformat(dt_str[:10])
    except Exception:
        return False
    return (datetime.now() - d) <= timedelta(days=days)


def _deal_tokens(deal: dict[str, Any]) -> list[str]:
    toks: list[str] = []
    for f in ("name", "ticker", "id"):
        v = (deal.get(f) or "").strip()
        if v and len(v) >= 3:
            toks.append(v.lower())
    # Geography / sector adds high-recall tokens.
    for f in ("geography",):
        v = (deal.get(f) or "").strip()
        if v and len(v) >= 4:
            toks.append(v.lower())
    return toks


def run() -> dict[str, Any]:
    if not DASHBOARD_DATA.exists() or not DEAL_SYSTEM.exists():
        return {
            "name": "M3: briefing-vs-followups divergence",
            "rule_ref": "dash_corrections.md :: M3",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [
                f"missing: {DASHBOARD_DATA}" if not DASHBOARD_DATA.exists() else "",
                f"missing: {DEAL_SYSTEM}" if not DEAL_SYSTEM.exists() else "",
            ],
        }

    try:
        dd = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
        ds = json.loads(DEAL_SYSTEM.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "M3: briefing-vs-followups divergence",
            "rule_ref": "dash_corrections.md :: M3",
            "status": "fail",
            "summary": f"data unreadable: {exc}",
            "details": [str(exc)],
        }

    bs = dd.get("briefingSynopsis") or {}
    cs = bs.get("captureSummary") or {}
    # Concatenate all string-valued fields on captureSummary (the workstream
    # category keys are tenant-defined; we read them generically).
    cs_strings = [v for v in cs.values() if isinstance(v, str)]
    briefing_text = " ".join(
        [bs.get("lastBriefingSnippet") or ""] + cs_strings
    )

    followups = dd.get("followUps") or []
    flags: list[str] = []

    for deal in ds.get("deals") or []:
        tokens = _deal_tokens(deal)
        if not tokens:
            continue
        # Briefing claims closure on this deal?
        deal_in_briefing = any(t in briefing_text.lower() for t in tokens)
        if not deal_in_briefing or not _CLOSED_RE.search(briefing_text):
            continue
        # Active followups in last 14d that mention the deal?
        active_recent = []
        for fu in followups:
            what = fu.get("what") or ""
            who = fu.get("who") or ""
            if what.startswith("[RESOLVED]"):
                continue
            blob = (who + " " + what).lower()
            if not any(t in blob for t in tokens):
                continue
            if not _within_days(fu.get("addedDate") or "", 14):
                continue
            if _ACTIVE_RE.search(what):
                active_recent.append(what[:120])
        if active_recent:
            flags.append(
                f"deal={deal.get('name')!r} briefing implies closure but "
                f"{len(active_recent)} active followup(s) in last 14d "
                f"(sample: {active_recent[0]!r})"
            )

    if flags:
        return {
            "name": "M3: briefing-vs-followups divergence",
            "rule_ref": "dash_corrections.md :: M3",
            "status": "warn",
            "summary": f"{len(flags)} deal(s) with briefing-vs-followups divergence",
            "details": flags[:20],
        }

    return {
        "name": "M3: briefing-vs-followups divergence",
        "rule_ref": "dash_corrections.md :: M3",
        "status": "pass",
        "summary": f"0 divergences across {len(ds.get('deals') or [])} deals",
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
