#!/usr/bin/env python3
"""check_jane_substrate.py — north_star + jane_brief staleness check.

Architectural note (2026-05-25): renamed from check_decision_state.py.
_check_decision_state() has been removed — the per-deal decision_state_jane
Drive Docs are retired. master_brief.md per deal (auto-maintained by /deal-sync)
is now the authoritative per-deal strategic context for Jane critics.
Only north_star.md (persona-level) and jane_brief.md (per-deal, written by
/deal-sync) are checked here.

Checks:
- north_star.md: missing → warn, >60d unchanged → warn
- jane_brief.md (per active deal): missing → warn, >4h unchanged → warn
  (mtime-based: /deal-sync is the writer, so mtime is the right signal)

Auto-discovered by ~/cos-pipeline/tools/system_health.py (check_*.py convention).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

HOME = Path.home()
DEALS_DIR = HOME / "dashboards" / "data" / "deals"
DEAL_SYSTEM_DATA = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"
NORTH_STAR = HOME / "dashboards" / "data" / "jane" / "north_star.md"

# Active stages — substring match (actual stage values may carry extra context
# e.g. "Diligence / Platform positioning")
ACTIVE_STAGE_SUBSTRINGS = {
    "Sourcing", "Evaluating", "Diligence", "Structuring",
    "Memo", "IC", "Active Bid", "Watch", "Advisory",
    # Extended forms seen in practice:
    "Active Evaluation",
    "Pre-FID",  # e.g. "Pre-FID Bridge Sizing / Platform Formation"
    "Bridge Sizing",
}

NORTH_STAR_STALE_DAYS = 60
JANE_BRIEF_STALE_HOURS = 4

# Matches "Last updated: YYYY-MM-DD" in various markdown formats
_LAST_UPDATED_RE = re.compile(
    r"^\s*-?\s*[*_]{0,2}Last\s+updated:?[*_]{0,2}\s*[:_]?\s*[*_]{0,2}(\d{4}-\d{2}-\d{2})[*_]{0,2}\s*$",
    re.MULTILINE,
)


def _is_active_stage(stage: str) -> bool:
    """Return True if stage value contains any active-stage keyword."""
    if not stage:
        return False
    for kw in ACTIVE_STAGE_SUBSTRINGS:
        if kw.lower() in stage.lower():
            return True
    return False


def _active_slugs() -> set[str]:
    """Return set of deal slugs whose stage is active."""
    if not DEAL_SYSTEM_DATA.exists():
        return set()
    try:
        doc = json.loads(DEAL_SYSTEM_DATA.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {
        d["id"]
        for d in doc.get("deals", [])
        if d.get("id") and _is_active_stage(d.get("stage", ""))
    }


def _parse_last_updated(text: str) -> date | None:
    """Extract and parse the Last updated date from file content. Returns None
    if not found or if the value is a placeholder (YYYY-MM-DD literal)."""
    m = _LAST_UPDATED_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    # Reject placeholder values like "YYYY-MM-DD"
    if not raw[0].isdigit() or raw == "YYYY-MM-DD":
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _check_north_star(today: date) -> dict[str, Any]:
    """north_star.md freshness check."""
    if not NORTH_STAR.exists():
        return {
            "status": "warn",
            "summary": "north_star.md missing",
            "last_updated": None,
            "age_days": None,
        }
    try:
        text = NORTH_STAR.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "status": "warn",
            "summary": f"north_star.md unreadable: {exc}",
            "last_updated": None,
            "age_days": None,
        }
    ts = _parse_last_updated(text)
    if ts is None:
        return {
            "status": "warn",
            "summary": "north_star.md missing or placeholder Last updated date",
            "last_updated": None,
            "age_days": None,
        }
    age = (today - ts).days
    if age > NORTH_STAR_STALE_DAYS:
        return {
            "status": "warn",
            "summary": f"north_star.md unchanged {age} days (threshold {NORTH_STAR_STALE_DAYS})",
            "last_updated": str(ts),
            "age_days": age,
        }
    return {
        "status": "pass",
        "summary": f"north_star fresh ({age}d old)",
        "last_updated": str(ts),
        "age_days": age,
    }


def _check_jane_briefs(now_ts: float) -> dict[str, Any]:
    """Per-deal jane_brief.md freshness. /deal-sync writes these every ~2h;
    missing or mtime >4h on an active deal signals /deal-sync is skipping it."""
    active = _active_slugs()
    if not active:
        return {
            "status": "pass",
            "summary": "No active deals to check",
            "missing": [],
            "stale": [],
        }

    missing: list[str] = []
    stale: list[dict] = []

    for slug in sorted(active):
        brief = DEALS_DIR / slug / "jane_brief.md"
        if not brief.exists():
            missing.append(slug)
            continue
        try:
            age_h = (now_ts - brief.stat().st_mtime) / 3600
        except OSError:
            missing.append(slug)
            continue
        if age_h > JANE_BRIEF_STALE_HOURS:
            stale.append({"deal": slug, "age_hours": round(age_h, 1)})

    if missing:
        status = "warn"
        summary = f"{len(missing)} active deal(s) missing jane_brief.md: {', '.join(missing)}"
    elif stale:
        status = "warn"
        summary = (
            f"{len(stale)} active deal(s) have stale jane_brief.md "
            f"(>{JANE_BRIEF_STALE_HOURS}h): {', '.join(s['deal'] for s in stale)}"
        )
    else:
        n = len(active)
        status = "pass"
        summary = f"All {n} active deal(s) have fresh jane_brief.md"

    return {
        "status": status,
        "summary": summary,
        "missing": missing,
        "stale": stale,
    }


def run() -> dict[str, Any]:
    today = date.today()
    now_ts = datetime.now().timestamp()

    ns = _check_north_star(today)
    briefs = _check_jane_briefs(now_ts)

    # Roll-up: worst-of-all wins (fail > warn > pass)
    all_statuses = [ns["status"], briefs["status"]]
    if "fail" in all_statuses:
        overall = "fail"
    elif "warn" in all_statuses:
        overall = "warn"
    else:
        overall = "pass"

    # Compose human-readable summary
    issue_parts: list[str] = []
    if ns["status"] != "pass":
        issue_parts.append(f"north_star: {ns['summary']}")
    if briefs["status"] != "pass":
        issue_parts.append(f"jane_briefs: {briefs['summary']}")

    if issue_parts:
        summary = "; ".join(issue_parts)
    else:
        n_active = len(_active_slugs())
        summary = f"north_star fresh; all {n_active} active deal(s) have fresh jane_brief.md"

    return {
        "name": "check_jane_substrate: north_star + jane_brief staleness",
        "status": overall,
        "summary": summary,
        "details": {
            "north_star": {
                "status": ns["status"],
                "summary": ns["summary"],
                "last_updated": ns["last_updated"],
                "age_days": ns["age_days"],
            },
            "jane_briefs": {
                "status": briefs["status"],
                "summary": briefs["summary"],
                "missing": briefs["missing"],
                "stale": briefs["stale"],
            },
        },
    }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(run(), indent=2))
