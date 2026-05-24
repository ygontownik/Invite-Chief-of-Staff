"""check_dashboard_coherence.py — Dashboard rendering sanity check.

Auto-discovered by system_health.py. Runs on every health check invocation.

Catches three classes of problems:

1. SURFACE BLEED — prose written for the wrong audience ends up on a page.
   e.g. deal names (Astris, Cholla) appearing in the Personal prose;
   personal-only recruiting firms appearing in HQ prose when there's no deal
   connection. Both are signs the LLM was given the wrong surface data.

2. STALE PROSE — Tier 2 AI prose hasn't refreshed in too long. Fires warn
   at >6h, fail at >24h (weekdays). Means the synthesis LaunchAgent is down
   or the LLM call is erroring silently.

3. RENDERING GARBAGE — "undefined", "[object Object]", "null" appearing in
   prose/worthNoticing fields. Means the JS or Python pipeline wrote bad data
   into dashboard-data.json.

Returns:
  pass  — all surfaces coherent, fresh, no garbage
  warn  — one surface missing prose, or prose stale 6-24h, or soft bleed
  fail  — hard surface bleed confirmed, prose stale >24h, or rendering garbage
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_DASH_DATA    = Path.home() / "dashboards/data/compiled/dashboard-data.json"
_DEAL_DATA    = Path.home() / "dashboards/data/deals"
_SYSTEM_DATA  = Path.home() / "cos-pipeline-config-tomac/deal-system-data.json"

# Terms that signal the prose is about the DEAL pipeline (not personal/job)
_DEAL_SIGNAL_TERMS = [
    r"\bastris\b", r"\bcholla\b", r"\bthunderhead\b", r"\bgridfree\b",
    r"\bpngts\b", r"\bunitil\b", r"\bbbeh\b", r"\bfit\b(?! for)",
    r"\bmercuria\b", r"\balign.?infra\b", r"\balign.?capital\b",
    r"\bblack bayou\b", r"\binterconnect", r"\bthermal\b", r"\bgenco\b",
    r"\bmiso\b", r"\bercot\b", r"\bpjm\b", r"\blng\b", r"\bmidstream\b",
]

# Terms that signal the prose is about job search / personal (not deal pipeline)
_PERSONAL_SIGNAL_TERMS = [
    r"\bomerta\b", r"\bmaven partnership\b", r"\bgcm grosvenor\b",
    r"\bheadhunter\b", r"\brecruiter\b",
    r"sent.{0,20}resume", r"job search", r"\binterview\b",
]

# Rendering garbage patterns — these should NEVER appear in prose
_GARBAGE_PATTERNS = [
    r"\bundefined\b", r"\[object Object\]", r"^null$",
    r"\bNaN\b", r"\btoFixed is not a function\b",
]

STALE_WARN_HRS = 6
STALE_FAIL_HRS = 24


def _load() -> tuple[dict, dict]:
    if not _DASH_DATA.exists():
        return {}, {}
    d = json.loads(_DASH_DATA.read_text())
    ps = d.get("prioritySynthesis") or {}
    return d, ps


def _contains(text: str, patterns: list[str]) -> list[str]:
    """Return which patterns match in text (case-insensitive)."""
    hits = []
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            hits.append(p)
    return hits


def _prose_age_hours(ps: dict) -> float | None:
    ts = ps.get("tier2GeneratedAt")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def run() -> dict:
    _, ps = _load()

    issues: list[str] = []
    warns:  list[str] = []

    # ── 1. Surface bleed ──────────────────────────────────────────────────────
    prose_personal = ps.get("prose_personal", "")
    prose_hq       = ps.get("prose_hq", ps.get("prose", ""))

    if prose_personal:
        bleed = _contains(prose_personal, _DEAL_SIGNAL_TERMS)
        if bleed:
            issues.append(
                f"Surface bleed: Personal prose contains deal terms: {bleed[:3]}"
            )

    if prose_hq:
        # HQ prose mentioning recruiting firms without deal context is suspicious
        p_bleed = _contains(prose_hq, _PERSONAL_SIGNAL_TERMS)
        if len(p_bleed) >= 2:
            warns.append(
                f"Possible surface bleed: HQ prose contains {len(p_bleed)} personal-only terms"
            )

    # ── 2. Missing surface prose ──────────────────────────────────────────────
    t1 = ps.get("tier1") or {}
    hq_has_items = bool(t1.get("hq") or t1.get("drifting", {}).get("hq") or t1.get("blocked", {}).get("hq"))
    p_has_items  = bool(t1.get("personal") or t1.get("drifting", {}).get("personal") or t1.get("blocked", {}).get("personal"))

    if hq_has_items and not prose_hq:
        warns.append("HQ has Tier 1 items but prose_hq is empty")
    if p_has_items and not prose_personal:
        warns.append("Personal has Tier 1 items but prose_personal is empty (first run after 2026-05-24 upgrade)")

    # ── 3. Stale prose ────────────────────────────────────────────────────────
    age_h = _prose_age_hours(ps)
    if age_h is None:
        warns.append("No tier2GeneratedAt timestamp — synthesis may never have run")
    elif age_h > STALE_FAIL_HRS:
        issues.append(f"Synthesis prose stale: {age_h:.1f}h since last refresh (>{STALE_FAIL_HRS}h fail threshold)")
    elif age_h > STALE_WARN_HRS:
        warns.append(f"Synthesis prose stale: {age_h:.1f}h since last refresh (>{STALE_WARN_HRS}h warn threshold)")

    # ── 4. Rendering garbage ──────────────────────────────────────────────────
    for field, text in [("prose_hq", prose_hq), ("prose_personal", prose_personal),
                         ("worthNoticing_hq", ps.get("worthNoticing_hq", "")),
                         ("worthNoticing_personal", ps.get("worthNoticing_personal", ""))]:
        if not text:
            continue
        garbage = _contains(text, _GARBAGE_PATTERNS)
        if garbage:
            issues.append(f"Rendering garbage in {field}: {garbage}")

    # ── 5. Tier 1 surface cross-contamination ─────────────────────────────────
    # HQ items should not carry workstream=personal; Personal items should not
    # carry workstream=tomac (deal pipeline workstream key)
    for item in t1.get("hq", []):
        ws = (item.get("workstream") or "").lower()
        if ws == "personal":
            warns.append(f"HQ Tier 1 contains personal-workstream item: {item.get('title','?')[:60]}")
            break
    for item in t1.get("personal", []):
        ws = (item.get("workstream") or "").lower()
        if ws in ("tomac", "tc"):  # noqa: tenant-leak (backward-compat workstream key)
            warns.append(f"Personal Tier 1 contains deal-workstream item: {item.get('title','?')[:60]}")
            break

    # ── Result ────────────────────────────────────────────────────────────────
    age_str = f"{age_h:.1f}h ago" if age_h is not None else "never"
    hq_chars  = len(prose_hq)
    p_chars   = len(prose_personal)

    if issues:
        status = "fail"
        summary = f"dashboard coherence: {len(issues)} fail(s) — {issues[0]}"
    elif warns:
        status = "warn"
        summary = f"dashboard coherence: {len(warns)} warn(s) — {warns[0]}"
    else:
        status = "pass"
        summary = (
            f"dashboard coherence: clean · prose_hq={hq_chars}c "
            f"prose_personal={p_chars}c · refreshed {age_str}"
        )

    return {
        "name": "dashboard coherence",
        "status": status,
        "summary": summary,
        "details": {
            "prose_hq_chars":       hq_chars,
            "prose_personal_chars": p_chars,
            "age_hours":            round(age_h, 2) if age_h is not None else None,
            "issues":               issues,
            "warns":                warns,
        },
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run())
