#!/usr/bin/env python3
"""
_envelope_writer.py — Shared router for all ingested items.

Every pipeline (transcript backfill, research processors, briefing compile)
emits items in the standard envelope shape defined in
docs/ROUTING-SPEC-2026-04-21.md §4.2, and calls append_items() here to
route them into the correct top-level array in dashboard-data.json.

Envelope:
    {
      "id":              "djb2(source|content[:60])" (computed if omitted)
      "content_type":    "my_action | awaiting_external | status_update |
                          deal_takeaway | origination_idea | lp_intel |
                          theme_note | contact"
      "owner":           "Yoni | Mark | Nick | external"
      "counterparty":    str (required when owner=external; e.g. "Stonepeak — Anthony Yammine")
      "parent_id":       str|None (deal ticker / lp slug; for types that need a parent)
      "source_ref":      { "type": "call|research|briefing|email|manual",
                           "title": str, "doc_url": str, "date": "YYYY-MM-DD" }
      "due":             "YYYY-MM-DD" | None
      "context":         str
      "dashboard_path":  str (human-readable tab path)
      "content":         str (main text)
    }

Routing:
    my_action          → followUps[]            (pipeline-authored items;
                                                  manual doc items still flow
                                                  through the Follow-ups Google Doc)
    awaiting_external  → awaitingExternal[]
    status_update      → tomac[].notes[]   (by parent_id, merges with doc parser)
    deal_takeaway      → dealIntel[]
    origination_idea   → originationInbox[]
    theme_note         → themes[]
    lp_intel           → lpData[].history[]     (plus existing LP writer)
    contact            → (handled by existing People doc writer, not this module)

    unroutable         → routingExceptions[] with `reason`
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

DASHBOARD_DATA_PATH = Path.home() / "dashboards/data/compiled/dashboard-data.json"

# djb2 hash — same scheme as client-side window.__itemId for tombstone compatibility
def _djb2(s: str) -> str:
    h = 5381
    for c in s.encode("utf-8"):
        h = ((h * 33) + c) & 0xFFFFFFFF
    return f"{h:08x}"


def compute_id(source: str, content: str) -> str:
    """Stable content-derived ID — matches client-side window.__itemId."""
    key = f"{source or ''}|{(content or '')[:60].strip()}"
    return _djb2(key)


VALID_CONTENT_TYPES = {
    "my_action",
    "awaiting_external",
    "status_update",
    "deal_takeaway",
    "origination_idea",
    "lp_intel",
    "theme_note",
    "contact",
}

VALID_OWNERS = {"Yoni", "Mark", "Nick", "external"}

# content_type → top-level array in dashboard-data.json
ARRAY_DEST = {
    "my_action":          "followUps",
    "awaiting_external":  "awaitingExternal",
    "deal_takeaway":      "dealIntel",
    "origination_idea":   "originationInbox",
    "theme_note":         "themes",
}

# content_type → (parent-collection-key, parent-id-field, parent-name-field, history-array-field)
# status_update writes to tomac[].notes[] to align with the existing doc parser
# (parse_tomac already populates notes[] from the Follow-ups doc's History blocks;
# the UI reads d.notes in buildTimeline). lp_intel writes to lpData[].history[]
# since lpData has no pre-existing timeline field.
HISTORY_DEST = {
    "status_update":  ("tomac",  "ticker", "name", "notes"),
    "lp_intel":       ("lpData", "id",     "name", "history"),
}


_lock = threading.Lock()


_DUE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")

# Per the contract in config/routing-rules.md:
# - owner=external is only meaningful with awaiting_external (a third party
#   owes Yoni something; no other content_type uses owner=external).
# - due is required for my_action and awaiting_external; must be YYYY-MM-DD.
# - counterparty is required when owner=external.
# - parent_id is required for status_update and lp_intel.
_NEEDS_DUE      = {"my_action", "awaiting_external"}
_NEEDS_PARENT   = {"status_update", "lp_intel"}
_EXTERNAL_ONLY  = {"awaiting_external"}  # only ct where owner=external makes sense

# Minimum content length — catches "TBD" / "n/a" / ellipsis slop that LLMs
# occasionally emit when they can't decide whether to emit an item at all.
_MIN_CONTENT_CHARS = 8


def _validate(item: dict) -> tuple[bool, str]:
    """Return (ok, reason). Items that fail validation go to routingExceptions.

    Enforces the contract in config/routing-rules.md:
      - content_type in VALID_CONTENT_TYPES
      - content is non-trivial prose (≥ 8 chars)
      - owner in VALID_OWNERS when set
      - owner=external only on awaiting_external
      - counterparty required when owner=external (format checked loosely)
      - due required and well-formed for my_action / awaiting_external
      - parent_id required for status_update / lp_intel
    """
    ct = item.get("content_type")
    if ct not in VALID_CONTENT_TYPES:
        return False, f"unknown content_type: {ct!r}"

    # Content sanity (contacts handled elsewhere, skip)
    if ct != "contact":
        content = (item.get("content") or "").strip()
        if not content:
            return False, f"{ct} item missing content"
        if len(content) < _MIN_CONTENT_CHARS:
            return False, f"{ct} content too short ({len(content)} chars): {content!r}"

    # Owner
    owner = (item.get("owner") or "").strip()
    if owner and owner not in VALID_OWNERS:
        return False, f"{ct} invalid owner={owner!r} (allowed: {sorted(VALID_OWNERS)})"
    if owner == "external" and ct not in _EXTERNAL_ONLY:
        return False, f"{ct} cannot use owner=external (only awaiting_external can)"

    # Counterparty required when owner=external — the chase card has no one to chase without it
    if owner == "external":
        cp = (item.get("counterparty") or "").strip()
        if not cp:
            return False, "awaiting_external with owner=external requires counterparty (Firm — Person)"

    # Due date for actions
    if ct in _NEEDS_DUE:
        due = (item.get("due") or "").strip()
        if not due:
            return False, f"{ct} requires due (YYYY-MM-DD)"
        if not _DUE_RE.match(due):
            return False, f"{ct} due must be YYYY-MM-DD, got {due!r}"

    # Parent for history-attached items
    if ct in _NEEDS_PARENT and not (item.get("parent_id") or "").strip():
        return False, f"{ct} requires parent_id (deal ticker or LP slug)"

    return True, ""


# ─── Normalization ───────────────────────────────────────────────────────────
# Minor shape fixups applied before validation. Catches LLM output quirks
# that represent the right intent but wrong format, so they pass validation
# instead of landing in exceptions. Anything genuinely wrong still falls
# through to _validate and gets rejected with a specific reason.

# content_type synonyms (value → canonical)
_CT_SYNONYMS = {
    "action":             "my_action",
    "followup":           "my_action",
    "follow_up":          "my_action",
    "my-action":          "my_action",
    "awaiting":           "awaiting_external",
    "awaiting-external":  "awaiting_external",
    "chase":              "awaiting_external",
    "status":             "status_update",
    "status-update":      "status_update",
    "update":             "status_update",
    "takeaway":           "deal_takeaway",
    "deal-takeaway":      "deal_takeaway",
    "intel":              "deal_takeaway",
    "origination":        "origination_idea",
    "origination-idea":   "origination_idea",
    "new-deal":           "origination_idea",
    "lp":                 "lp_intel",
    "lp-intel":           "lp_intel",
    "theme":              "theme_note",
    "theme-note":         "theme_note",
}

# owner synonyms — first token / first-name match to canonical
_OWNER_SYNONYMS = {
    "yoni":        "Yoni",
    "y":           "Yoni",
    "yoni gontownik": "Yoni",
    "mark":        "Mark",
    "mark saxe":   "Mark",
    "nick":        "Nick",
    "nik":         "Nick",   # transcription often shortens "Nick" to "Nik";
    "nik que":     "Nick",   # routing exceptions (4× "owner='Nik'") at 2026-04-27
    "nicholas":    "Nick",
    "external":    "external",
    "third-party": "external",
    "third party": "external",
    "counterparty": "external",
}


def _normalize_due(due: str) -> str:
    """Normalize YYYY-M-D variants to YYYY-MM-DD. Free-text like 'next Friday'
    is left intact so the validator rejects it explicitly."""
    if not due:
        return ""
    s = due.strip()
    import re
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return s


def _normalize_counterparty(cp: str) -> str:
    """Accept common counterparty formats and canonicalize to 'Firm — Person'.

    Handles:
      "Firm - Person"      → "Firm — Person" (hyphen → em-dash)
      "Firm – Person"      → "Firm — Person" (en-dash → em-dash)
      "Person (Firm)"      → "Firm — Person"
      "Person, Firm"       → "Firm — Person"
    Leaves already-canonical or unmatched values intact.
    """
    if not cp:
        return ""
    s = cp.strip()
    # Hyphen / en-dash between firm and person
    import re
    m = re.match(r"^(.+?)\s*[–\-]\s*(.+)$", s)
    if m and "—" not in s:
        return f"{m.group(1).strip()} — {m.group(2).strip()}"
    # Person (Firm) form
    m = re.match(r"^(.+?)\s*\((.+)\)\s*$", s)
    if m:
        return f"{m.group(2).strip()} — {m.group(1).strip()}"
    # Person, Firm — heuristic: firm words are capitalized; flip if first chunk
    # is clearly a person (two words, both title-cased)
    if "," in s and "—" not in s:
        left, right = [p.strip() for p in s.split(",", 1)]
        left_tokens = left.split()
        if len(left_tokens) == 2 and all(t[:1].isupper() for t in left_tokens if t):
            return f"{right} — {left}"
    return s


def _normalize(item: dict) -> dict:
    """Best-effort fix-ups applied before _validate().

    Does not invent data — only canonicalizes format. Returns a new dict.
    """
    out = dict(item)

    # Strip wrapping whitespace from all string fields
    for k, v in list(out.items()):
        if isinstance(v, str):
            out[k] = v.strip()

    # content_type canonicalization
    ct = (out.get("content_type") or "").strip().lower().replace(" ", "_")
    if ct in VALID_CONTENT_TYPES:
        out["content_type"] = ct
    elif ct in _CT_SYNONYMS:
        out["content_type"] = _CT_SYNONYMS[ct]

    # owner canonicalization
    owner_raw = (out.get("owner") or "").strip()
    if owner_raw:
        lo = owner_raw.lower()
        if lo in _OWNER_SYNONYMS:
            out["owner"] = _OWNER_SYNONYMS[lo]
        else:
            # Try first token (e.g. "Mark Saxe" → "Mark")
            first = lo.split()[0] if lo.split() else ""
            if first in _OWNER_SYNONYMS:
                out["owner"] = _OWNER_SYNONYMS[first]

    # due date
    if out.get("due"):
        out["due"] = _normalize_due(out["due"])

    # counterparty formatting
    if out.get("counterparty"):
        out["counterparty"] = _normalize_counterparty(out["counterparty"])

    return out


def _ensure_id(item: dict) -> dict:
    if not item.get("id"):
        src = (item.get("source_ref") or {}).get("title", "")
        item["id"] = compute_id(src, item.get("content", ""))
    return item


def _dedupe_append(arr: list, new_item: dict) -> bool:
    """Append if not already present by id. Returns True if appended."""
    item_id = new_item.get("id")
    if not item_id:
        arr.append(new_item)
        return True
    for existing in arr:
        if existing.get("id") == item_id:
            return False
    arr.append(new_item)
    return True


def _attach_to_parent(parent_collection: list, parent_id: str,
                      parent_id_field: str, parent_name_field: str,
                      history_item: dict,
                      history_field: str = "history") -> bool:
    """Find the parent in the collection and append to its history[].

    Matches in order:
      1. exact id/slug equality
      2. case-insensitive name equality
      3. first-token match (e.g. parent_id='GIC' matches 'GIC (Singapore SWF)')
      4. slug prefix match
    """
    target = (parent_id or "").strip()
    if not target:
        return False
    target_lo = target.lower()
    target_slug = _slugify(target)

    # Pass 1: exact id/slug
    for p in parent_collection:
        pid = p.get(parent_id_field) or _slugify(p.get(parent_name_field, ""))
        if pid and (pid == target or pid == target_slug):
            hist = p.setdefault(history_field, [])
            return _dedupe_append(hist, history_item)

    # Pass 2: name-based fuzzy
    for p in parent_collection:
        name = (p.get(parent_name_field) or "").strip()
        if not name:
            continue
        name_lo = name.lower()
        name_slug = _slugify(name)
        # case-insensitive full name
        if name_lo == target_lo:
            hist = p.setdefault(history_field, [])
            return _dedupe_append(hist, history_item)
        # first-token (e.g. 'GIC' vs 'GIC (Singapore SWF)')
        first_tok = _tokens(name)[:1]
        if first_tok and first_tok[0] == target_lo:
            hist = p.setdefault(history_field, [])
            return _dedupe_append(hist, history_item)
        # slug prefix (e.g. 'gic' prefix of 'gic-singapore-swf')
        if target_slug and (name_slug == target_slug or
                            name_slug.startswith(target_slug + "-")):
            hist = p.setdefault(history_field, [])
            return _dedupe_append(hist, history_item)
    return False


def _slugify(s: str) -> str:
    """Lowercase, hyphenated, alnum-only — for deriving stable IDs from names."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def append_items(items: list[dict], data_path: Path | None = None) -> dict:
    """
    Route a batch of envelope items to the correct destinations in
    dashboard-data.json. Atomic read-modify-write under a file lock.

    Returns a summary dict:
        { "routed": {content_type: count}, "exceptions": count, "skipped_dupes": count }
    """
    path = data_path or DASHBOARD_DATA_PATH
    summary: dict = {"routed": {}, "exceptions": 0, "skipped_dupes": 0}

    with _lock:
        data = json.loads(path.read_text())

        # Ensure target arrays exist (Phase 1 migration safety)
        for key in ("followUps", "awaitingExternal", "dealIntel",
                    "originationInbox", "themes", "routingExceptions"):
            data.setdefault(key, [])

        for raw in items:
            # Normalize first (canonicalize content_type / owner / due /
            # counterparty), then id, then validate. Normalize is a pure
            # fix-up layer — anything it can't canonicalize falls through
            # to the validator with a specific rejection reason.
            item = _ensure_id(_normalize(dict(raw)))
            ok, reason = _validate(item)
            if not ok:
                exc = {
                    "id": item.get("id"),
                    "reason": reason,
                    "item": item,
                    "flagged_at": datetime.now().isoformat(),
                }
                _dedupe_append(data["routingExceptions"], exc)
                summary["exceptions"] += 1
                continue

            ct = item["content_type"]

            # Route by content type
            if ct in ARRAY_DEST:
                arr_key = ARRAY_DEST[ct]
                if _dedupe_append(data[arr_key], item):
                    summary["routed"][ct] = summary["routed"].get(ct, 0) + 1
                else:
                    summary["skipped_dupes"] += 1
            elif ct in HISTORY_DEST:
                parent_key, id_field, name_field, hist_field = HISTORY_DEST[ct]
                parent_collection = data.get(parent_key, [])
                parent_id = item["parent_id"]
                attached = _attach_to_parent(
                    parent_collection, parent_id, id_field, name_field, item,
                    history_field=hist_field,
                )
                if attached:
                    summary["routed"][ct] = summary["routed"].get(ct, 0) + 1
                else:
                    # Parent not found → exception
                    exc = {
                        "id": item["id"],
                        "reason": f"{ct}: parent_id={parent_id!r} not found in {parent_key}",
                        "item": item,
                        "flagged_at": datetime.now().isoformat(),
                    }
                    _dedupe_append(data["routingExceptions"], exc)
                    summary["exceptions"] += 1
            elif ct == "contact":
                # Contacts are handled by the People doc writer, not this module.
                # Caller should not send them here; mark as skipped for safety.
                summary["skipped_dupes"] += 1
                continue
            else:
                # Unreachable given _validate — defensive
                exc = {
                    "id": item["id"],
                    "reason": f"no destination for content_type={ct!r}",
                    "item": item,
                    "flagged_at": datetime.now().isoformat(),
                }
                _dedupe_append(data["routingExceptions"], exc)
                summary["exceptions"] += 1

        # Atomic write
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(path)

    return summary


# ─── Parent resolver ──────────────────────────────────────────────────────────
# Given free-text context/content, try to map to a known deal ticker or LP slug.
# Soft resolution — returns None if no confident match; caller may still emit
# the item (the envelope allows parent_id=None for deal_takeaway/origination_idea).

def load_known_parents(data_path: Path | None = None) -> dict:
    """Returns { 'deals': [{id, name, aliases}], 'lps': [{id, name}] }."""
    path = data_path or DASHBOARD_DATA_PATH
    data = json.loads(path.read_text())

    deals = []
    # From deal-system-data.json (embedded as dealPortfolio.deals)
    dp = data.get("dealPortfolio", {}) or {}
    for d in dp.get("deals", []) or []:
        deals.append({
            "id":      d.get("id") or _slugify(d.get("name", "")),
            "name":    d.get("name", ""),
            "aliases": d.get("aliases", []),
        })
    # From tomac[] — human-authored deal list
    for t in data.get("tomac", []) or []:
        name = t.get("name") or t.get("title") or ""
        ticker = t.get("ticker") or _slugify(name)
        if not any(x["id"] == ticker for x in deals):
            deals.append({"id": ticker, "name": name, "aliases": []})

    lps = []
    for lp in data.get("lpData", []) or []:
        name = lp.get("name") or lp.get("firm") or ""
        lps.append({
            "id":   lp.get("id") or _slugify(name),
            "name": name,
        })

    return {"deals": deals, "lps": lps}


def resolve_parent(text: str, kind: str = "deal",
                   known: dict | None = None) -> str | None:
    """
    Best-effort resolution of free-text to a known deal/LP id.
    kind: 'deal' | 'lp'. Tries:
      1. Full candidate name as substring of text
      2. Each distinctive 2-word prefix of candidate (e.g. "Black Bayou"
         matches "Black Bayou Energy Hub")
      3. Aliases
    Returns None if no confident match.
    """
    if not text:
        return None
    known = known or load_known_parents()
    pool = known["deals"] if kind == "deal" else known["lps"]
    lo = text.lower()
    best = None
    best_score = 0

    STOPWORDS = {"the", "a", "an", "and", "or", "of", "in", "on",
                 "update", "log", "via", "from", "call", "deal"}

    for item in pool:
        name = item.get("name", "")
        if not name:
            continue
        candidates = [name] + list(item.get("aliases", []))

        # Generate 2-word prefixes from name (skip stopwords, minimum 4 chars per word)
        words = [w for w in _tokens(name) if w not in STOPWORDS and len(w) >= 4]
        if len(words) >= 2:
            candidates.append(f"{words[0]} {words[1]}")
        if len(words) == 1:
            candidates.append(words[0])

        for cand in candidates:
            cand_lo = (cand or "").lower()
            if len(cand_lo) < 5:
                continue  # avoid generic short matches
            if cand_lo in lo:
                # Score = candidate length (longer match = stronger confidence)
                if len(cand_lo) > best_score:
                    best = item["id"]
                    best_score = len(cand_lo)
    return best


def _tokens(s: str) -> list[str]:
    """Lowercase word tokens (alphanumeric only)."""
    import re
    return re.findall(r"[a-z0-9]+", (s or "").lower())


# ─── CLI: ad-hoc append / inspect ────────────────────────────────────────────

def _cli():
    import argparse, sys
    ap = argparse.ArgumentParser(description="Append envelope items to dashboard-data.json")
    ap.add_argument("--file", help="JSON file with a list of envelope items")
    ap.add_argument("--stdin", action="store_true",
                    help="Read items JSON from stdin")
    ap.add_argument("--list-parents", action="store_true",
                    help="Print known deals and LPs for parent_id resolution")
    args = ap.parse_args()

    if args.list_parents:
        known = load_known_parents()
        print("DEALS:")
        for d in known["deals"]:
            print(f"  {d['id']:30s}  {d['name']}")
        print("\nLPS:")
        for lp in known["lps"]:
            print(f"  {lp['id']:30s}  {lp['name']}")
        return 0

    items = None
    if args.file:
        items = json.loads(Path(args.file).read_text())
    elif args.stdin:
        items = json.loads(sys.stdin.read())
    if items is None:
        ap.print_help()
        return 1

    if not isinstance(items, list):
        items = [items]
    summary = append_items(items)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
