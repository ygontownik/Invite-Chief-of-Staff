#!/usr/bin/env python3
"""
check_u2.py
===========

Enforces dash_corrections.md :: U2 — market intel ↔ deal readthrough.

Each active deal in deal-system-data.json should carry a
`recent_readthroughs[]` array (or surface a recent_log[] of market
matches) so the briefing handler can render "Deal Readthrough"
sections. Past failure: market briefings sat in the briefing tab for
weeks while a directly-relevant active deal had no readthrough surface
linking the two.

Heuristic: the dashboard has marketCommentary[]. Each active deal
should have either:
  - A non-empty `recent_readthroughs[]` field, OR
  - A `recent_log[]` containing at least one entry with `source` in
    {market, intel, podcast, brief} or `match: explicit`.

If marketCommentary has any items but the deal-system has zero
readthroughs across all deals, that strongly suggests the
_compute_deal_readthroughs() pass didn't run — fail.

Status:
  - "fail" when marketCommentary has items but zero deals show
    readthrough connections.
  - "warn" when individual deals have stale or missing readthroughs.
  - "pass" otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"
DEAL_SYSTEM = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"
DEALS_DATA_DIR = HOME / "dashboards" / "data" / "deals"

_INTEL_SOURCES = {"market", "intel", "podcast", "brief", "marketcommentary"}


def _has_intel_in_full_log(deal_id: str) -> bool:
    """Fallback: check full log.json when recent_log only has followup entries.
    Deals often have intel entries in log.json that are displaced from recent_log
    by newer followup entries — recent_log only holds top-5 by date.
    """
    log_path = DEALS_DATA_DIR / deal_id / "log.json"
    if not log_path.exists():
        return False
    try:
        raw = json.loads(log_path.read_text(encoding="utf-8"))
        entries = raw.get("entries", raw) if isinstance(raw, dict) else raw
        for e in (entries or []):
            src = (e.get("source") or "").lower()
            if src in _INTEL_SOURCES or (e.get("match") or "").lower() == "explicit":
                return True
    except Exception:
        pass
    return False


def _market_item_count(dd: dict[str, Any]) -> int:
    mc = dd.get("marketCommentary") or {}
    if isinstance(mc, list):
        return len(mc)
    if isinstance(mc, dict):
        n = 0
        for sec in (mc.get("sections") or []):
            n += len(sec.get("items") or [])
        return n
    return 0


def run() -> dict[str, Any]:
    if not DASHBOARD_DATA.exists() or not DEAL_SYSTEM.exists():
        return {
            "name": "U2: market-intel deal readthroughs present",
            "rule_ref": "dash_corrections.md :: U2",
            "status": "warn",
            "summary": "data file not present, skipped",
            "details": [
                f"missing: {p}" for p in [DASHBOARD_DATA, DEAL_SYSTEM] if not p.exists()
            ],
        }

    try:
        dd = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
        ds = json.loads(DEAL_SYSTEM.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "U2: market-intel deal readthroughs present",
            "rule_ref": "dash_corrections.md :: U2",
            "status": "fail",
            "summary": f"data unreadable: {exc}",
            "details": [str(exc)],
        }

    market_n = _market_item_count(dd)
    deals = ds.get("deals") or []
    deals_with_readthroughs = 0
    per_deal: list[str] = []

    for d in deals:
        rts = d.get("recent_readthroughs") or []
        log = d.get("recent_log") or []
        explicit_intel = [
            e
            for e in log
            if (e.get("source") or "").lower() in _INTEL_SOURCES
            or (e.get("match") or "").lower() == "explicit"
        ]
        # Fallback: recent_log only holds top-5 by date; intel entries may be
        # displaced by newer followup entries. Check full log.json before warning.
        if not rts and not explicit_intel:
            deal_id = d.get("id") or ""
            if deal_id and _has_intel_in_full_log(deal_id):
                deals_with_readthroughs += 1
                continue
        if rts or explicit_intel:
            deals_with_readthroughs += 1
        else:
            per_deal.append(
                f"deal={d.get('name')!r} has 0 recent_readthroughs and "
                f"0 intel-source log entries"
            )

    if market_n > 0 and deals_with_readthroughs == 0 and deals:
        return {
            "name": "U2: market-intel deal readthroughs present",
            "rule_ref": "dash_corrections.md :: U2",
            "status": "fail",
            "summary": (
                f"{market_n} marketCommentary items but 0/{len(deals)} deals "
                "carry readthrough connections — _compute_deal_readthroughs() "
                "likely never ran"
            ),
            "details": per_deal[:20],
        }

    if per_deal:
        return {
            "name": "U2: market-intel deal readthroughs present",
            "rule_ref": "dash_corrections.md :: U2",
            "status": "warn",
            "summary": (
                f"{deals_with_readthroughs}/{len(deals)} deals have readthroughs "
                f"({market_n} marketCommentary items available)"
            ),
            "details": per_deal[:20],
        }

    return {
        "name": "U2: market-intel deal readthroughs present",
        "rule_ref": "dash_corrections.md :: U2",
        "status": "pass",
        "summary": (
            f"{deals_with_readthroughs}/{len(deals)} deals carry readthroughs "
            f"({market_n} marketCommentary items available)"
        ),
        "details": [],
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
