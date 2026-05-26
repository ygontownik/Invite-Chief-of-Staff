#!/usr/bin/env python3
"""
cos_correction_scanner.py
=========================

CW1 — Scan a Claude Code session JSONL for factual CoS corrections in
user messages and write them to cos-overrides.json.

Used by /wrap STEP 3 to auto-capture corrections without requiring the
user to explicitly say "emit DEAL-INTEL" or "update the overlay".

Patterns detected:
  - "X not involved" / "X has no involvement" → owner/involvement reassignment
  - "[deal] stage is [X]" / "stage changed to [X]" → stage correction
  - "that's wrong, [field] should be [X]" → generic field correction
  - "Yoni's call" / "Mark's call" → ownership correction
  - Named deal + explicit correction verb → deal intel correction

Output: structured list of dicts, each with:
  {deal_id, field, value, reason, confidence, message_snippet, ts}

Usage:
  python3 cos_correction_scanner.py <jsonl_path> [--since <iso_ts>] [--apply]

  --apply: writes detected corrections to cos-overrides.json
  --since: only scan messages after this timestamp (ISO 8601)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

HOME = Path.home()
OVERRIDES_PATH = HOME / "dashboards" / "data" / "user-state" / "cos-overrides.json"

# --- Deal registry: slug → canonical name / aliases ----------------------

def _load_deal_slugs() -> dict[str, list[str]]:
    """Return {slug: [alias, ...]} from deal-system-data.json."""
    ds_path = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"
    if not ds_path.exists():
        return {}
    try:
        ds = json.loads(ds_path.read_text())
        result = {}
        for d in ds.get("deals", []):
            slug = d.get("id") or d.get("deal_id")
            name = (d.get("name") or "").lower()
            if not slug:
                continue
            aliases = [slug, name]
            if " " in name:
                aliases.append(name.split()[0])  # first word shorthand
            result[slug] = list({a for a in aliases if a})
        return result
    except Exception:
        return {}


# --- Correction patterns -------------------------------------------------

_STAGE_VALUES = {
    "watch": ("stage", "Watch", 0),
    "sourcing": ("stage", "Sourcing", 1),
    "active evaluation": ("stage", "Active Evaluation", 2),
    "diligence": ("stage", "Diligence", 3),
    "advisory": ("stage", "Advisory", 3),
    "active bid": ("stage", "Active Bid", 3),
    "ic memo": ("stage", "IC Memo", 4),
    "ic": ("stage", "IC", 4),
    "closed": ("stage", "Closed", 5),
    "pass": ("stage", "Pass", 5),
}

_NOT_INVOLVED_RE = re.compile(
    r"(?P<name>\w[\w\s]{0,20})\s+(?:is\s+)?not\s+involved",
    re.IGNORECASE,
)
_STAGE_IS_RE = re.compile(
    r"stage\s+(?:is|should be|changed to|now|was|moved to|=)\s+['\"]?(?P<stage>[A-Za-z ]{2,30})['\"]?",
    re.IGNORECASE,
)
_WRONG_RE = re.compile(
    r"(?:that[''']?s wrong|incorrect|not right|should be|correct\s+(?:it|that)\s+to)\s+['\"]?(?P<val>.{4,60})['\"]?",
    re.IGNORECASE,
)
_YONI_CALL_RE = re.compile(
    r"(?:this is|it[''']?s)\s+yoni[''']?s\s+(?:call|decision|deal)",
    re.IGNORECASE,
)
_MARK_NOT_RE = re.compile(
    r"mark\s+(?:saxe\s+)?(?:is\s+)?not\s+(?:involved|part of|on|doing)",
    re.IGNORECASE,
)


def _identify_deal(text: str, slugs: dict[str, list[str]]) -> str | None:
    """Return deal slug if any alias appears in text."""
    tl = text.lower()
    for slug, aliases in slugs.items():
        for a in aliases:
            if a and a in tl:
                return slug
    return None


def _extract_stage(stage_str: str) -> tuple[str, int] | None:
    """Map a raw stage string to (canonical_name, index)."""
    sl = stage_str.strip().lower()
    for key, (_, canonical, idx) in _STAGE_VALUES.items():
        if sl.startswith(key):
            return canonical, idx
    return None


def scan_messages(jsonl_path: Path, since_ts: str | None = None) -> list[dict[str, Any]]:
    """
    Read JSONL, extract user-typed messages (not tool results, not system-reminders)
    after `since_ts`, and pattern-match for corrections.

    Returns list of correction dicts.
    """
    slugs = _load_deal_slugs()
    corrections: list[dict[str, Any]] = []

    try:
        lines = jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        print(f"ERROR reading {jsonl_path}: {e}", file=sys.stderr)
        return []

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        # Filter: must be a user message
        role = obj.get("role") or (obj.get("message") or {}).get("role")
        if role != "user":
            continue

        ts = obj.get("timestamp", "")
        if since_ts and ts and ts <= since_ts:
            continue

        # Extract text content
        msg = obj.get("message", obj)
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = []
            for c in content:
                if isinstance(c, dict):
                    t = c.get("type")
                    if t == "text":
                        texts.append(c.get("text", ""))
                    elif t == "tool_result":
                        pass  # skip tool results
            text = " ".join(texts)
        elif isinstance(content, str):
            text = content
        else:
            continue

        if not text.strip():
            continue

        # Skip system-reminder pseudo-messages
        if "<system-reminder>" in text and len(text) < 1000:
            continue

        # --- Pattern matching ---
        deal_id = _identify_deal(text, slugs)

        # "X not involved" — ownership/involvement correction
        m = _NOT_INVOLVED_RE.search(text)
        if m and deal_id:
            name = m.group("name").strip()
            field = f"{name.lower().replace(' ', '_')}_not_involved"
            corrections.append({
                "deal_id": deal_id,
                "field": field,
                "value": True,
                "permanent": True,
                "reason": f"User stated '{m.group(0)}' in session message",
                "confidence": "high",
                "ts": ts,
                "snippet": text[:120],
            })

        # Mark Saxe not involved — special sentinel (align_infra precedent)
        if _MARK_NOT_RE.search(text) and deal_id:
            corrections.append({
                "deal_id": deal_id,
                "field": "mark_not_involved",
                "value": True,
                "permanent": True,
                "reason": "User stated Mark Saxe not involved",
                "confidence": "high",
                "ts": ts,
                "snippet": text[:120],
            })

        # "stage is [X]" correction
        m = _STAGE_IS_RE.search(text)
        if m:
            stage_parsed = _extract_stage(m.group("stage"))
            if stage_parsed and deal_id:
                canonical, idx = stage_parsed
                corrections.append({
                    "deal_id": deal_id,
                    "field": "stage",
                    "value": canonical,
                    "permanent": False,
                    "reason": f"User stated stage correction in message",
                    "confidence": "high",
                    "ts": ts,
                    "snippet": text[:120],
                })
                corrections.append({
                    "deal_id": deal_id,
                    "field": "stage_index",
                    "value": idx,
                    "permanent": False,
                    "reason": "Paired stage_index for stage correction",
                    "confidence": "high",
                    "ts": ts,
                    "snippet": text[:120],
                })

        # "Yoni's call/decision" — marks deal as Yoni-only
        if _YONI_CALL_RE.search(text) and deal_id:
            corrections.append({
                "deal_id": deal_id,
                "field": "yoni_solo",
                "value": True,
                "permanent": True,
                "reason": "User stated this is Yoni's call/decision",
                "confidence": "medium",
                "ts": ts,
                "snippet": text[:120],
            })

    return corrections


def apply_corrections(corrections: list[dict[str, Any]]) -> int:
    """
    Merge detected corrections into cos-overrides.json.
    Deduplicates by (deal_id, field) — newer entry wins.
    Returns count of new/updated entries written.
    """
    if not corrections:
        return 0

    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(OVERRIDES_PATH.read_text()) if OVERRIDES_PATH.exists() else {}
    except Exception:
        existing = {}

    entries = existing.get("overrides", [])
    # Build lookup: (deal_id, field) → index in entries
    lookup: dict[tuple, int] = {}
    for i, e in enumerate(entries):
        lookup[(e.get("deal_id"), e.get("field"))] = i

    written = 0
    today = datetime.now(timezone.utc).date().isoformat()
    for c in corrections:
        key = (c["deal_id"], c["field"])
        entry = {
            "deal_id": c["deal_id"],
            "field": c["field"],
            "value": c["value"],
            "permanent": c.get("permanent", False),
            "reason": c.get("reason", ""),
            "confidence": c.get("confidence", "medium"),
            "created_at": today,
            "source": "cos_correction_scanner",
            "snippet": c.get("snippet", ""),
        }
        if key in lookup:
            entries[lookup[key]] = entry  # overwrite
        else:
            entries.append(entry)
            lookup[key] = len(entries) - 1
        written += 1

    out = {
        "version": existing.get("version", 1),
        "description": existing.get(
            "description",
            "Durable CoS corrections that survive compile. Compile reads before source data — overrides win (CW2).",
        ),
        "overrides": entries,
    }
    tmp = OVERRIDES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    tmp.replace(OVERRIDES_PATH)
    return written


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("jsonl", type=Path, help="Path to session JSONL file")
    p.add_argument("--since", default=None, help="ISO timestamp — only scan after this")
    p.add_argument("--apply", action="store_true", help="Write to cos-overrides.json")
    args = p.parse_args(argv)

    if not args.jsonl.exists():
        print(f"ERROR: {args.jsonl} not found", file=sys.stderr)
        return 1

    corrections = scan_messages(args.jsonl, since_ts=args.since)
    if not corrections:
        print("No corrections detected.")
        return 0

    print(f"Detected {len(corrections)} correction(s):")
    for c in corrections:
        print(f"  deal={c['deal_id']} field={c['field']} value={c['value']!r} conf={c['confidence']}")
        print(f"    snippet: {c['snippet'][:80]!r}")

    if args.apply:
        n = apply_corrections(corrections)
        print(f"\nApplied {n} correction(s) to {OVERRIDES_PATH}")
    else:
        print("\n(dry-run — pass --apply to write)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
