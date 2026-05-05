#!/usr/bin/env python3
"""
_capture_query_builder.py — Derive Gmail capture queries from live dashboard state.

Reads data/compiled/dashboard-data.json and emits a JSON file containing
auto-generated Gmail search queries + parent_id hints. The Gmail
backfill pipeline (cos_email_backfill.py) unions these with the
hand-curated queries in config/email-capture.yaml before each run.

Three generators:

  1. Deal-name subject queries    — one per tracked deal
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
batch-write) so it reflects the latest dashboard state after the skill
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

def _deal_subject_queries(deals: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Return (queries, keyword→parent_id map).

    For each tracked deal we emit a single `subject:(keyword1 OR keyword2)`
    query using the deal name plus any informative contact surnames. The
    parent_id map lets the extractor anchor a matched thread to the right
    deal without a second Gemini pass.
    """
    queries: list[str] = []
    hints: dict[str, str] = {}
    # Guard against parse artifacts / placeholder rows
    _BAD_NAMES = {"unknown", "template", "update log", "tbd", "n/a", ""}
    for d in deals or []:
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

# Firm-name → primary email domain map. Built at module load from
# firm_context.yaml :: peer_firms[] (the canonical tenant-tracked firm
# list) plus an OPTIONAL `peer_firm_domains` mapping in firm_context.yaml
# for tenants who want hand-curated domains. When no override exists for
# a firm, we synthesize a heuristic domain (lowercased first token + .com),
# e.g. "Stonepeak" → "stonepeak.com". A tenant who needs a non-heuristic
# domain (e.g. "ECP" → "ecinvest.com", "I Squared" → "isquaredcapital.com")
# adds an explicit override:
#
#     # in firm_context.yaml
#     peer_firm_domains:
#       ECP: ecinvest.com
#       I Squared Capital: isquaredcapital.com
#       LS Power: lspower.com
#
# Keys in peer_firm_domains are matched case-insensitively against
# peer_firms entries. The .com heuristic is intentionally crude — the
# downstream cost of a wrong domain is just a noisy capture-query, never
# a write or a misroute. Empty firm-context (fresh tenant) yields {}.

import sys as _sys  # noqa: E402
_HERE_QB = Path(__file__).resolve().parent
if str(_HERE_QB) not in _sys.path:
    _sys.path.insert(0, str(_HERE_QB))


def _heuristic_domain(firm_name: str) -> str:
    """Fallback domain guess: first alpha token, lowercased + .com.

    Examples:  "Stonepeak"          → "stonepeak.com"
               "Brookfield Infra"   → "brookfield.com"
               "TPG Rise Climate"   → "tpg.com"
    Returns "" when no usable token is present.
    """
    tokens = re.findall(r"[A-Za-z]+", firm_name or "")
    if not tokens:
        return ""
    return f"{tokens[0].lower()}.com"


def _build_firm_domains() -> dict[str, str]:
    """Build {firm_needle_lower: domain} from firm_context.yaml.

    Sources, in order:
      1. firm_context.yaml :: peer_firm_domains  (explicit hand overrides)
      2. firm_context.yaml :: peer_firms[]       (heuristic domain per entry)
    Falls back to {} on any load error so the rest of the builder still runs.
    """
    out: dict[str, str] = {}
    try:
        import _firm_context as _fc  # noqa: PLC0415
        ctx = _fc.load_firm_context() or {}
    except Exception:
        return out

    overrides = (ctx.get("peer_firm_domains") or {})
    # Normalize override keys to lowercase needles
    overrides_lc = {str(k).strip().lower(): str(v).strip()
                    for k, v in overrides.items() if k and v}

    for firm in ctx.get("peer_firms", []) or []:
        firm_str = str(firm).strip()
        if not firm_str:
            continue
        # Use full lowercased firm name as the primary needle, plus first
        # token (mirrors prior matching style: "i squared" / "isquared",
        # "ls power" matches "ls power capital partners", etc.)
        full_lc = firm_str.lower()
        first_lc = (re.findall(r"[A-Za-z]+", firm_str) or [""])[0].lower()
        # Skip 1-2 char first tokens — they generate false-positive substring
        # matches in _lp_domain_queries (e.g. "I Squared" → "i" → matches
        # any firm with the letter i in it). Full lowercased name still
        # registers as a needle.
        if len(first_lc) < 3:
            first_lc = ""
        domain = (
            overrides_lc.get(full_lc)
            or overrides_lc.get(first_lc)
            or _heuristic_domain(firm_str)
        )
        if not domain:
            continue
        # Both the full-name needle and the first-token needle map to the
        # same domain so substring matches like "stonepeak infra" still hit.
        out[full_lc] = domain
        if first_lc and first_lc not in out:
            out[first_lc] = domain
    # Pull in explicit overrides that don't correspond to a peer_firms entry
    # (lets a tenant add a recruiter / advisor domain without bloating peers).
    for k, v in overrides_lc.items():
        out.setdefault(k, v)
    return out


_FIRM_DOMAINS: dict[str, str] = _build_firm_domains()


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
    # The dashboard-data.json shape uses tenant-specific top-level keys for
    # deal lists. Try the canonical "deals" key first, then fall back to
    # legacy tenant-prefixed keys (any list-valued top-level key whose key
    # ends in matches the deal-workstream slug).
    deals = d.get("deals")
    if not isinstance(deals, list):
        # Heuristic: pick the first top-level key whose value is a list of
        # dicts with a "name" field — that's almost certainly the deal list.
        for k, v in d.items():
            if k in ("lpData", "emailQueue"):
                continue
            if isinstance(v, list) and v and isinstance(v[0], dict) and "name" in v[0]:
                deals = v
                break
    if not isinstance(deals, list):
        deals = []
    lp_data = d.get("lpData", [])
    email_queue = d.get("emailQueue", [])

    deal_q, hints = _deal_subject_queries(deals)
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
            "deals":   len(deals),
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
