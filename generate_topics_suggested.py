#!/usr/bin/env python3
"""
generate_topics_suggested.py
────────────────────────────────────────────────────────────
Generate a 3-bullet weekly focus suggestion from deal health,
urgency signals, and recent activity — NO API call required.

Writes ~/dashboards/data/user-state/topics_suggested.json.
Called by deal-system-compile.py after every compile run.

The HQ page reads topics_suggested.json and shows an "Adopt" button
when topics.json is blank or > 7 days old. User edits to topics.json
always override the suggestion.

Output schema:
{
    "bullets": [
        {"text": "...", "deal": "<deal_id>", "reason": "..."},
        ...
    ],
    "generated_at": "YYYY-MM-DD",
    "source": "heuristic"
}

Priority logic:
  1. Deals with a critical open action AND health < 80
  2. Deals with no recent log activity (last_intel_date > 7 days or missing)
  3. Deals with the most open high-priority actions
  Fallback: firm-wide themes / LP pipeline if < 3 deal bullets available.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT           = Path.home() / "dashboards"
DEAL_SYS_PATH   = _ROOT / "data" / "compiled" / "deal-system-data.json"
TOPICS_SUGG     = _ROOT / "data" / "user-state" / "topics_suggested.json"
TOPICS_USER     = _ROOT / "data" / "user-state" / "topics.json"

STALE_DAYS = 7      # no log activity in this many days → surface deal


def _open_critical(actions: list[dict]) -> int:
    return sum(
        1 for a in actions
        if a.get("priority") in ("critical", "high")
        and a.get("status", "open") not in ("done", "closed", "complete")
    )


def _days_since(date_str: str | None, today: date) -> int | None:
    if not date_str:
        return None
    try:
        d = date.fromisoformat(str(date_str)[:10])
        return (today - d).days
    except (ValueError, TypeError):
        return None


def _deal_score(deal: dict, today: date) -> float:
    """Higher score = higher priority to surface in weekly topics."""
    score = 0.0

    # Critical/high open actions
    score += _open_critical(deal.get("actions", [])) * 20

    # Low health
    health = deal.get("health", 50) or 50
    score += max(0, 80 - health)  # up to +80 for health=0

    # Stale intel (no recent log activity)
    stale = _days_since(deal.get("last_intel_date"), today)
    if stale is None or stale >= STALE_DAYS:
        score += 30

    # Upcoming staleness flags
    score += len(deal.get("staleness_flags", [])) * 10

    return score


def _best_bullet(deal: dict, today: date) -> dict:
    """Synthesize the most useful one-line focus topic for a deal."""
    name = deal.get("name", deal.get("id", "Unknown"))
    did  = deal.get("id", "")

    # Find the top open high-priority action
    actions = deal.get("actions", [])
    open_high = [
        a for a in actions
        if a.get("priority") in ("critical", "high")
        and a.get("status", "open") not in ("done", "closed", "complete")
    ]
    open_high.sort(key=lambda a: a.get("due", "9999"))

    if open_high:
        top = open_high[0]
        text = f"{name} — {top.get('action', top.get('what', 'advance deal'))}"
        reason = f"critical/high action due {top.get('due', 'TBD')} · health {deal.get('health', '?')}"
    else:
        stale = _days_since(deal.get("last_intel_date"), today)
        if stale is not None and stale >= STALE_DAYS:
            text = f"{name} — check in / drive next step (no intel in {stale}d)"
            reason = f"last intel {deal.get('last_intel_date', 'unknown')} · {stale} days stale"
        else:
            text = f"{name} — review stage and advance"
            reason = f"health {deal.get('health', '?')} · {len(actions)} open actions"

    return {"text": text, "deal": did, "reason": reason}


def generate(today: date | None = None) -> dict:
    today = today or date.today()

    try:
        ds = json.loads(DEAL_SYS_PATH.read_text())
    except FileNotFoundError:
        return {"bullets": [], "generated_at": today.isoformat(), "source": "heuristic",
                "error": f"{DEAL_SYS_PATH} not found"}

    deals = ds.get("deals", [])
    if not deals:
        return {"bullets": [], "generated_at": today.isoformat(), "source": "heuristic",
                "error": "no deals in deal-system-data.json"}

    # Score and rank
    scored = sorted(deals, key=lambda d: _deal_score(d, today), reverse=True)

    bullets = [_best_bullet(d, today) for d in scored[:3]]

    return {
        "bullets": bullets,
        "generated_at": today.isoformat(),
        "source": "heuristic",
        "deal_count": len(deals),
    }


def should_adopt(today: date | None = None) -> bool:
    """Return True if topics.json is blank or > 7 days old."""
    today = today or date.today()
    try:
        t = json.loads(TOPICS_USER.read_text())
        content = (t.get("content") or "").strip()
        if not content:
            return True
        updated = t.get("updated_at", "")[:10]
        if not updated:
            return True
        age = (today - date.fromisoformat(updated)).days
        return age >= STALE_DAYS
    except Exception:
        return True


def main() -> int:
    today = date.today()
    result = generate(today)
    TOPICS_SUGG.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"topics_suggested.json written — {len(result.get('bullets', []))} bullets", file=sys.stderr)
    for b in result.get("bullets", []):
        print(f"  • {b['text']}", file=sys.stderr)

    # If topics.json is blank/stale, print an adopt-hint
    if should_adopt(today):
        print("\n  [HINT] topics.json is blank or stale — consider adopting suggestions above",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
