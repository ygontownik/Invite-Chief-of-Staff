#!/usr/bin/env python3
"""
_capture_query_builder.py — Derive Gmail capture queries from live dashboard state.

Reads data/compiled/dashboard-data.json and emits a JSON file containing
auto-generated Gmail search queries + parent_id hints. The Gmail
backfill pipeline (cos_email_backfill.py) unions these with the
hand-curated queries in config/email-capture.yaml before each run.

Three generators:

  1. Deal-name subject queries    — one per tracked Tomac deal
  2. Counterparty-domain queries  — from LP firms + contacts field
  3. Frequent-sender queries      — any from-address that's appeared
                                    in the email queue ≥2 times recently

Output: data/user-state/capture-queries.auto.json
  {
    "generated_at": "...",
    "source": "dashboard-data.json",
    "queries": ["subject:...", "from:...", ...],
    "parent_hints_by_keyword": {
       "<deal-name lowercased>": "<parent_id>"
    }
  }

Intended cadence: daily. Wired into cos-capture-pipeline Round 3 (post
batch-write) so it reflects the latest Tomac doc state after the skill
has run.

Non-fatal on any failure — prints a warning and writes an empty/degraded
artifact so the Gmail backfill still functions with just the yaml.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DATA = ROOT / "data" / "compiled" / "dashboard-data.json"
OUT_PATH       = ROOT / "data" / "user-state" / "capture-queries.auto.json"


# Generic words in deal names / sector labels that produce too-broad
# Gmail queries if used as subject keywords.
_NAME_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "in", "on", "to", "by",
    "with", "energy", "hub", "corp", "inc", "llc", "lp", "co", "via",
    "update", "log", "system", "group", "partners", "capital", "cove",
    "deal", "call", "project", "site", "pipeline",
}


def _safe_load_dashboard() -> dict:
    try:
        return json.loads(DASHBOARD_DATA.read_text())
    except Exception as e:
        print(f"[capture-builder] WARN could not read {DASHBOARD_DATA}: {e}",
              file=sys.stderr)
        return {}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


# ─── 1. Deal subject queries ────────────────────────────────────────────────

def _deal_subject_queries(tomac: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Return (queries, keyword→parent_id map).

    For each Tomac deal we emit a single `subject:(keyword1 OR keyword2)`
    query using the deal name plus any informative contact surnames. The
    parent_id map lets the extractor anchor a matched thread to the right
    deal without a second Gemini pass.
    """
    queries: list[str] = []
    hints: dict[str, str] = {}
    # Guard against parse artifacts / placeholder rows
    _BAD_NAMES = {"unknown", "template", "update log", "tbd", "n/a", ""}
    for d in tomac or []:
        name = (d.get("name") or "").strip()
        if not name or name.lower() in _BAD_NAMES:
            continue
        if re.match(r"^(update\s+)?log\b", name, re.IGNORECASE):
            continue
        parent_id = _slug(d.get("ticker") or name)
        # Pull 1-3 meaningful tokens from the deal name
        tokens = [t for t in re.findall(r"[A-Z][A-Za-z]+", name) if t.lower() not in _NAME_STOPWORDS]
        if not tokens:
            continue
        # Quoted strings for multi-word fragments; bare tokens for single words
        def _q(tok: str) -> str:
            return f'"{tok}"' if " " in tok else tok
        subject_terms = list({_q(t) for t in tokens})[:3]
        if len(subject_terms) > 1:
            queries.append(f"subject:({' OR '.join(subject_terms)})")
        else:
            queries.append(f"subject:{subject_terms[0]}")
        for t in subject_terms:
            hints[t.strip('"').lower()] = parent_id
        # Full-doc keyword query for deal name variants (not just subject)
        if len(name) >= 6:
            queries.append(f'"{name}"')
            hints[name.lower()] = parent_id
    return queries, hints


# ─── 2. LP + counterparty domain queries ────────────────────────────────────

# Known firm-name → primary email domain map. We keep this compact and
# hand-curated rather than trying to infer a domain from a firm name
# (too error-prone). Anything not in the map just generates a subject
# keyword query on the firm name, which is still useful.
_FIRM_DOMAINS = {
    "arclight": "arclight.com",
    "stonepeak": "stonepeak.com",
    "i squared": "isquaredcapital.com",
    "isquared": "isquaredcapital.com",
    "ecp": "ecinvest.com",
    "quantum": "quantumenergy.com",
    "kkr": "kkr.com",
    "tpg": "tpg.com",
    "brookfield": "brookfield.com",
    "blackstone": "blackstone.com",
    "blackrock": "blackrock.com",
    "pennybacker": "pennybacker.com",
    "ls power": "lspower.com",
    "nuveen": "nuveen.com",
    "ridgewood": "ridgewood.com",
    "apollo": "apollo.com",
    "carlyle": "carlyle.com",
    "antin": "antin-ip.com",
    "gip": "globalinfra.com",
    "macquarie": "macquarie.com",
    "heidrick": "heidrick.com",
    "spencer stuart": "spencerstuart.com",
    "egon zehnder": "egonzehnder.com",
    "russell reynolds": "russellreynolds.com",
}


def _lp_domain_queries(lp_data: list[dict]) -> list[str]:
    domains: set[str] = set()
    keyword_queries: list[str] = []
    for lp in lp_data or []:
        name = (lp.get("name") or "").lower()
        if not name:
            continue
        hit = next((d for k, d in _FIRM_DOMAINS.items() if k in name), None)
        if hit:
            domains.add(hit)
        else:
            # Pull firm name (before "/", em-dash, etc.) as a subject keyword.
            # Guard against truncation artifacts ("Single", "Maketa" parsed
            # out of a longer string) — require multi-word OR ≥8 chars.
            firm = re.split(r"[/—\-,()]", lp.get("name") or "", maxsplit=1)[0].strip()
            if not firm or firm.lower() in _NAME_STOPWORDS:
                continue
            if " " in firm or len(firm) >= 8:
                keyword_queries.append(f'"{firm}"')
    queries: list[str] = []
    if domains:
        # Chunk to avoid overlong OR chains (Gmail caps query size ~5kB)
        dom_list = sorted(domains)
        for i in range(0, len(dom_list), 10):
            chunk = dom_list[i:i + 10]
            queries.append("from:(" + " OR ".join(f"@{d}" for d in chunk) + ")")
    queries.extend(keyword_queries)
    return queries


# ─── 3. Frequent-sender queries ─────────────────────────────────────────────

def _frequent_sender_queries(email_queue: list[dict], min_touches: int = 2) -> list[str]:
    """Any from-address appearing ≥min_touches times in the recent email queue
    gets its own `from:` query. This is the self-learning layer: counterparties
    you've actually been emailing a lot get auto-captured even if no one has
    added them to the yaml.
    """
    from collections import Counter
    senders = Counter()
    for m in email_queue or []:
        addr = (m.get("fromEmail") or "").strip().lower()
        if addr and "@" in addr and not addr.endswith("@gmail.com"):
            senders[addr] += 1
    hot = [a for a, n in senders.items() if n >= min_touches]
    if not hot:
        return []
    # One query per ~10 senders to keep it under size limits
    queries = []
    for i in range(0, len(hot), 10):
        chunk = hot[i:i + 10]
        queries.append("from:(" + " OR ".join(chunk) + ")")
    return queries


# ─── Orchestrator ───────────────────────────────────────────────────────────

def build() -> dict:
    d = _safe_load_dashboard()
    tomac = d.get("tomac", [])
    lp_data = d.get("lpData", [])
    email_queue = d.get("emailQueue", [])

    deal_q, hints = _deal_subject_queries(tomac)
    lp_q           = _lp_domain_queries(lp_data)
    sender_q       = _frequent_sender_queries(email_queue, min_touches=2)

    # Dedupe preserving order
    seen = set()
    all_queries = []
    for q in deal_q + lp_q + sender_q:
        if q and q not in seen:
            seen.add(q)
            all_queries.append(q)

    artifact = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source":       "dashboard-data.json",
        "counts": {
            "deals":   len(tomac),
            "lps":     len(lp_data),
            "senders": len({m.get("fromEmail", "") for m in (email_queue or [])
                            if m.get("fromEmail")}),
        },
        "queries": all_queries,
        "parent_hints_by_keyword": hints,
    }
    return artifact


def write(artifact: dict, path: Path = OUT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False))


def main() -> int:
    try:
        art = build()
        write(art)
        print(f"[capture-builder] wrote {len(art['queries'])} queries to "
              f"{OUT_PATH.relative_to(ROOT)} "
              f"(deals={art['counts']['deals']}, lps={art['counts']['lps']}, "
              f"senders={art['counts']['senders']})")
        return 0
    except Exception as e:
        print(f"[capture-builder] ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
