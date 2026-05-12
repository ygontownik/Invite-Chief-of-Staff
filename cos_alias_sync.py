#!/usr/bin/env python3
"""
cos_alias_sync.py — Auto-discover counterparty aliases from originationInbox.

After each dashboard fetch, new companies that landed in originationInbox but
have no alias in firm_context.yaml are written to a sidecar file:
  <config_dir>/counterparty_aliases_auto.json

_firm_context.load_firm_context() merges the sidecar at load time, so the
envelope writer, fast resolver, and capture pipeline all see the new aliases
on their next run — no restarts, no manual edits required.

Only adds aliases — never removes or modifies existing ones.
Idempotent: running twice produces the same result.

Usage:
  python3 cos_alias_sync.py            # normal run
  python3 cos_alias_sync.py --dry-run  # print what would be added, write nothing
  python3 cos_alias_sync.py --verbose  # print all decisions
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import _firm_context as _fc

# Tokens that carry no identification value as standalone needles.
# The full counterparty name is still added as a needle even when all tokens are skipped.
_SKIP_TOKENS: frozenset[str] = frozenset([
    # Corporate suffixes
    "llc", "limited", "inc", "corp", "corporation",
    "company", "group", "partners", "capital",
    "holdings", "management", "ventures", "fund",
    "services", "solutions", "systems", "global",
    "international", "national", "american",
    # Too-generic industry/sector words
    "energy", "power", "infrastructure", "digital",
    "technology", "financial", "finance", "market",
    "data", "media", "content", "information",
    "center", "network", "platform", "analytics",
    # Function words and meta-terms
    "newsletter", "substack", "anonymous", "executive",
    "search", "recruiting", "talent", "advisory",
    "research", "consulting", "communications",
])
_MIN_TOKEN_LEN = 6   # individual split tokens shorter than this are skipped

DASHBOARD_DATA_PATH = Path.home() / "dashboards/data/compiled/dashboard-data.json"


# ── Needle generation ─────────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _generate_needles(raw_counterparty: str) -> list[str]:
    """Return a deduplicated list of match needles for a counterparty string.

    Strategy: split on high-level separators (—, /, |, parens) to get
    company-part and person-part as separate phrases. Add each phrase as a
    needle. Also add the first meaningful word of multi-word company names
    when it's specific (>= 8 chars, not in skip list).

    This avoids adding ambiguous single tokens like "mountain" or "bennett"
    while still matching on "black mountain" or "rhett bennett".

    Handles formats like:
      "NextDecade"
      "Rio Grande LNG"
      "Black Mountain — Rhett Bennett"   → "black mountain", "rhett bennett"
      "John Smith / Acme Energy"         → "john smith", "acme energy"
      "Riverside Power (Jane Doe)"       → "riverside power", "jane doe"
    """
    # Split on em-dash, en-dash, slash, parens, pipe — these separate phrases
    parts = re.split(r"\s*[—–/|()\[\]]\s*", raw_counterparty)
    needles: list[str] = []
    seen: set[str] = set()

    for part in parts:
        part = _normalise(part)
        if not part or len(part) < 3:
            continue
        # Add the whole phrase as a needle
        if part not in seen:
            needles.append(part)
            seen.add(part)

        # For multi-word phrases: add the first word if it's specific enough
        # to serve as a standalone needle (unique company shorthand, e.g. "pennybacker")
        words = [w for w in part.split() if w not in _SKIP_TOKENS]
        if len(words) >= 2:
            first = words[0]
            if len(first) >= 8 and first not in seen:
                needles.append(first)
                seen.add(first)

    return needles


# ── Alias dedup check ─────────────────────────────────────────────────────────

def _build_needle_set(alias_list: list[dict]) -> set[str]:
    """Flat set of all needles across all alias entries (lowercased)."""
    out: set[str] = set()
    for entry in alias_list:
        out.add(_normalise(entry.get("canonical", "")))
        for n in entry.get("needles", []):
            out.add(_normalise(n))
    return out


def _already_covered(canonical: str, new_needles: list[str],
                     existing_needle_set: set[str]) -> bool:
    """True if the canonical or any of its needles overlap an existing alias.

    Also catches "Company — Person" variants: if any existing needle of 6+
    chars is a substring of the new canonical, it's already covered
    (e.g. "garden investments" is a substring of "garden investments — brian jacoby").
    """
    norm_canonical = _normalise(canonical)
    candidates = {norm_canonical} | {_normalise(n) for n in new_needles}
    if candidates & existing_needle_set:
        return True
    # Substring check: existing needle contained in new canonical string
    for existing in existing_needle_set:
        if existing and len(existing) >= 6 and existing in norm_canonical:
            return True
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, verbose: bool = False) -> dict:
    stats = {"scanned": 0, "added": 0, "skipped_existing": 0, "skipped_empty": 0}

    # Load dashboard data
    try:
        dash = json.loads(DASHBOARD_DATA_PATH.read_text())
    except Exception as e:
        print(f"[alias-sync] dashboard-data.json error: {e}", file=sys.stderr)
        return stats

    origination_inbox = dash.get("originationInbox") or []
    if not origination_inbox:
        print("[alias-sync] originationInbox empty — nothing to do")
        return stats

    # Load config dir and existing aliases (manual + auto already merged by _fc)
    config_dir = _fc._find_config_dir()
    sidecar_path = config_dir / "counterparty_aliases_auto.json"

    ctx = _fc.load_firm_context()
    manual_aliases = ctx.get("counterparty_aliases") or []

    try:
        auto_aliases: list[dict] = json.loads(sidecar_path.read_text()) if sidecar_path.exists() else []
    except Exception:
        auto_aliases = []

    all_aliases = manual_aliases + auto_aliases
    needle_set = _build_needle_set(all_aliases)
    auto_canonicals = {_normalise(e.get("canonical", "")) for e in auto_aliases}

    new_entries: list[dict] = []

    # Only derive aliases from email/call-sourced origination items.
    # Research docs (Jefferies, RBN, podcast transcripts) also route to
    # originationInbox but their "counterparties" are authors/analysts —
    # not email contacts we'd want to alias-match against incoming mail.
    SKIP_SOURCE_TYPES = {"research", "podcast", "briefing", "newsletter"}

    for item in origination_inbox:
        src_type = (item.get("source_ref") or {}).get("type", "")
        if src_type in SKIP_SOURCE_TYPES:
            stats["scanned"] += 1
            if verbose:
                print(f"[alias-sync] skip (source={src_type!r}): {item.get('counterparty','')!r}")
            continue

        counterparty = (item.get("counterparty") or "").strip()
        stats["scanned"] += 1

        if not counterparty or len(counterparty) < 3:
            stats["skipped_empty"] += 1
            continue

        needles = _generate_needles(counterparty)
        if not needles:
            stats["skipped_empty"] += 1
            continue

        if _already_covered(counterparty, needles, needle_set):
            stats["skipped_existing"] += 1
            if verbose:
                print(f"[alias-sync] already covered: {counterparty!r}")
            continue

        # Use the raw counterparty as canonical (preserve original casing)
        canonical = counterparty.strip()
        entry = {
            "canonical":  canonical,
            "needles":    needles,
            "added_date": str(date.today()),
            "source":     "originationInbox_auto",
        }

        if verbose:
            print(f"[alias-sync] NEW: {canonical!r} → needles={needles}")

        # Add to working needle set so subsequent items in this run don't re-add
        needle_set |= {_normalise(canonical)} | {_normalise(n) for n in needles}
        auto_canonicals.add(_normalise(canonical))
        new_entries.append(entry)
        stats["added"] += 1

    if new_entries and not dry_run:
        auto_aliases.extend(new_entries)
        sidecar_path.write_text(json.dumps(auto_aliases, indent=2, ensure_ascii=False))
        print(f"[alias-sync] added {len(new_entries)} new alias(es) → {sidecar_path}")
        for e in new_entries:
            print(f"  + {e['canonical']!r}: {e['needles']}")
    elif new_entries:
        print(f"[alias-sync] dry-run — would add {len(new_entries)} alias(es):")
        for e in new_entries:
            print(f"  + {e['canonical']!r}: {e['needles']}")
    else:
        print(f"[alias-sync] no new aliases "
              f"(scanned={stats['scanned']}, existing={stats['skipped_existing']})")

    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Auto-sync counterparty aliases from originationInbox")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run(dry_run=args.dry_run, verbose=args.verbose)
    sys.exit(0)
