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
      "owner":           "<owner-from-firm-context> | external"
      "counterparty":    str (required when owner=external; e.g. "Firm — Person")
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
    status_update      → <deal-pipeline-array>[].notes[]   (by parent_id, merges with doc parser)
    deal_takeaway      → dealIntel[]
    origination_idea   → originationInbox[]
    theme_note         → themes[]
    lp_intel           → lpData[].history[]     (plus existing LP writer)
    contact            → (handled by existing People doc writer, not this module)

    unroutable         → routingExceptions[] with `reason`
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

DASHBOARD_DATA_PATH = Path.home() / "dashboards/data/compiled/dashboard-data.json"


# ── Firm-context loader ────────────────────────────────────────────────────
# Mirrors the parameterization pattern in cos_email_backfill.py (commit
# 7b6ed62). Every tenant-specific value (owner whitelist, principal/team
# names, counterparty aliases used as the person-name veto list) is derived
# from firm_context.yaml at module load. All literal principal/team/firm/
# deal-name references have been replaced with config-derived values so
# this file stays universal across tenants.

_CTX: dict[str, Any] = {}
try:
    import _firm_context as _fc  # type: ignore
    _CTX = _fc.load_firm_context() or {}
except Exception:
    _CTX = {}


def _principal_first() -> str:
    """First name of the principal (config-driven; falls back to 'Principal')."""
    name = ((_CTX.get("principal") or {}).get("name") or "").strip()
    return name.split()[0] if name else "Principal"


def _build_owner_whitelist() -> set[str]:
    """Canonical owner names for validation. Sourced from
    firm_context.yaml :: owner_whitelist (with principal/team filled in
    if owner_whitelist is absent), plus the literal 'external'."""
    owners = list(_CTX.get("owner_whitelist") or [])
    if not owners:
        principal = (_CTX.get("principal") or {}).get("name") or ""
        owners.append(principal.split()[0] if principal else "Principal")
        for m in _CTX.get("team") or []:
            n = (m.get("name") or "").strip()
            if n:
                owners.append(n.split()[0])
    out = {o for o in owners if o}
    out.add("external")
    return out


def _build_owner_synonyms() -> dict[str, str]:
    """Build the owner-canonicalization map at module load from principal
    + team + owner_whitelist. Replaces the prior hardcoded full-name →
    canonical map. Synonyms include: each canonical name lowercased, the
    first-token of any full name, and a couple of universal aliases
    ('external', 'third-party', 'counterparty')."""
    syn: dict[str, str] = {}

    def _add(full: str, canonical: str) -> None:
        if not (full and canonical):
            return
        syn.setdefault(full.strip().lower(), canonical)
        first = full.strip().split()[0] if full.strip() else ""
        if first:
            syn.setdefault(first.lower(), canonical)

    # Principal
    p_name = ((_CTX.get("principal") or {}).get("name") or "").strip()
    if p_name:
        canonical = p_name.split()[0]
        _add(p_name, canonical)
        # owner_whitelist may use a nickname (e.g. owner_whitelist entry
        # is the principal's first name; principal.name is the full name —
        # first-token derivation already matches).

    # Team members
    for m in _CTX.get("team") or []:
        n = (m.get("name") or "").strip()
        if n:
            _add(n, n.split()[0])

    # owner_whitelist canonicals (covers nicknames not appearing in principal/team)
    for o in _CTX.get("owner_whitelist") or []:
        if o:
            syn.setdefault(o.lower(), o)

    # Universal external synonyms (tenant-agnostic)
    syn.setdefault("external",     "external")
    syn.setdefault("third-party",  "external")
    syn.setdefault("third party",  "external")
    syn.setdefault("counterparty", "external")
    return syn


def _build_person_name_blocklist() -> set[str]:
    """Derive the false-positive name-candidate blocklist from
    firm_context.yaml :: counterparty_aliases[].canonical (so deal/firm
    canonicals get vetoed) plus a small set of universal English bigrams
    that look like 'First Last' but never are. Keeps
    _infer_person_from_text() from emitting deal nicknames as person
    names."""
    out: set[str] = set()
    for entry in _CTX.get("counterparty_aliases") or []:
        canon = (entry.get("canonical") or "").strip()
        if canon and len(canon.split()) >= 2:
            out.add(canon)
        # Multi-word needles that happen to look like Title Case bigrams
        for n in entry.get("needles") or []:
            ns = (n or "").strip()
            if ns and len(ns.split()) == 2 and all(
                t[:1].isalpha() for t in ns.split()
            ):
                out.add(" ".join(t.capitalize() for t in ns.split()))
    # Firm name itself
    firm_name = (_CTX.get("firm") or {}).get("name") or ""
    if firm_name and len(firm_name.split()) >= 2:
        # Add first-two-token form (e.g. "Acme Holdings Infrastructure
        # Partners" → "Acme Holdings") so the bigram search catches it.
        out.add(" ".join(firm_name.split()[:2]))
    # Universal generic bigrams that recur across all tenants
    out.update({
        "United States", "Investment Decision", "Investment Committee",
        "Final Investment", "Senior Advisor", "Senior Individual",
        "Term Sheet", "Initial Public", "Internal Rate", "Real Estate",
        "Operating Officer", "Chief Executive", "Office Of",
        "Energy Hub", "Energy Solutions", "Energy Group",
    })
    return out


def _build_common_first_names() -> set[str]:
    """First-name set used to break the firm-token veto when a person's
    first name happens to appear in a tracked firm name. The static list
    covers common English / PE first names; we additionally fold in the
    first-tokens of every owner_whitelist member + principal + team so
    a tenant whose principal is named e.g. 'Aria' isn't filtered out."""
    base = {
        "adam", "alex", "andrew", "anna", "ben", "bill", "bob", "brad",
        "brian", "bruce", "carl", "chris", "dan", "dave", "david", "doug",
        "ed", "eric", "frank", "gary", "george", "greg", "henry", "jack",
        "james", "jane", "jeff", "jen", "jenny", "jim", "joe", "john",
        "jon", "josh", "kate", "ken", "kevin", "kyle", "larry", "laura",
        "lee", "linda", "lisa", "mark", "mary", "matt", "max", "michael",
        "mike", "neil", "nick", "nikola", "pat", "paul", "peter", "phil",
        "rachel", "rick", "rob", "robert", "ron", "ross", "ryan", "sam",
        "scott", "sean", "steve", "tim", "todd", "tom", "tony", "will",
    }
    p = (_CTX.get("principal") or {}).get("name") or ""
    if p:
        base.add(p.split()[0].lower())
    for m in _CTX.get("team") or []:
        n = (m.get("name") or "").strip()
        if n:
            base.add(n.split()[0].lower())
    for o in _CTX.get("owner_whitelist") or []:
        if o:
            base.add(o.split()[0].lower())
    return base


# ── Aliases / people directory loader ──────────────────────────────────────
# Loaded lazily from <COS_CONFIG_DIR>/known-aliases.yaml. Used to:
#   1. Rewrite known transcription errors before validation
#      (e.g. Otter mishears "Oncor" as "Encore")
#   2. Resolve a person name to (firm, role) so counterparty inference
#      can produce "Firm — Person" instead of just "TBD — Person"

def _config_dir() -> Path:
    """Locate the active firm's config directory. Delegates to
    _firm_context._find_config_dir() so we share the canonical search
    order (env var → slug-suffixed dir → legacy dir → pipeline dir).
    Falls back to a local search if _firm_context isn't importable
    (keeps this module usable as a thin fallback during installs)."""
    env = os.environ.get("COS_CONFIG_DIR")
    if env and Path(env).exists():
        return Path(env)
    try:
        import _firm_context as _fc  # type: ignore
        return _fc._find_config_dir()
    except Exception:
        # Generic search — any cos-pipeline-config* sibling in $HOME
        home = Path.home()
        for c in sorted(home.glob("cos-pipeline-config*")):
            if c.is_dir():
                return c
        return home / "cos-pipeline-config"


_aliases_cache: dict[str, Any] | None = None

def _load_aliases() -> dict[str, Any]:
    """Load known-aliases.yaml + auto-merge People/CRM doc data.

    Returns merged structure with firm_aliases / person_aliases /
    people_directory. The static yaml takes precedence over the
    auto-merged People doc data (so manual entries stick). Cached."""
    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache

    merged: dict[str, Any] = {
        "firm_aliases": {},
        "person_aliases": {},
        "people_directory": {},
    }

    # Layer 1: static yaml (manual / authoritative)
    path = _config_dir() / "config" / "known-aliases.yaml"
    if path.exists():
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(path.read_text()) or {}
            for k in merged:
                if data.get(k):
                    merged[k].update(data[k])
        except Exception:
            pass

    # Layer 2: People/CRM doc parsed cache (auto-populated)
    people_cache = _people_doc_cache_path()
    if people_cache.exists():
        try:
            doc_people = json.loads(people_cache.read_text())
            # Static yaml overrides doc-derived (so a curated yaml entry
            # beats whatever the doc says). Only fill in missing entries.
            for name, info in (doc_people or {}).items():
                if name not in merged["people_directory"]:
                    merged["people_directory"][name] = info
        except Exception:
            pass

    _aliases_cache = merged
    return _aliases_cache


def _people_doc_cache_path() -> Path:
    return Path.home() / "credentials" / "people_doc_directory.json"


def refresh_people_directory_from_doc(doc_id: str | None = None) -> int:
    """Parse the People/CRM Google Doc into a people_directory cache file.
    Idempotent — safe to run on every pipeline tick. Returns count parsed.

    Doc format expected (per the user's curated file):
        ## <Name>
        - **Role:** <role>
        - **Firm:** <firm>
        - **Workstream:** ...
        - **Notes:** ...
    """
    import re
    if doc_id is None:
        # Look up via firm_context's drive-docs config
        try:
            cfg_path = _config_dir() / "drive-docs.yaml"
            if cfg_path.exists():
                import yaml  # type: ignore
                cfg = yaml.safe_load(cfg_path.read_text()) or {}
                doc_id = (cfg.get("docs", {}) or {}).get("people")
        except Exception:
            doc_id = None
    if not doc_id:
        return 0

    try:
        import pickle
        from googleapiclient.discovery import build  # type: ignore
        token_path = Path.home() / "credentials" / "gdrive_token.pickle"
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
        docs = build("docs", "v1", credentials=creds)
        doc = docs.documents().get(documentId=doc_id).execute()
    except Exception:
        return 0

    # Concatenate all text
    full_text = ""
    for el in doc.get("body", {}).get("content", []):
        if "paragraph" in el:
            for run in el["paragraph"].get("elements", []):
                full_text += run.get("textRun", {}).get("content", "")

    # Split into per-person sections at "## "
    sections = re.split(r"\n## ", "\n" + full_text)
    directory: dict[str, dict] = {}
    field_re = re.compile(r"^\-\s+\*\*(\w[\w\s]*?):\*\*\s*(.*?)$", re.MULTILINE)
    for sec in sections[1:]:
        lines = sec.split("\n", 1)
        name = lines[0].strip()
        if not name or name.lower() in ("template", "people", "claude maintains"):
            continue
        body = lines[1] if len(lines) > 1 else ""
        fields = {}
        for m in field_re.finditer(body):
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if val.lower() in ("unknown", "n/a", "tbd", ""):
                continue
            fields[key] = val
        # Only keep if we got at least firm OR role (otherwise useless)
        if fields.get("firm") or fields.get("role"):
            directory[name] = {
                "firm": fields.get("firm", ""),
                "role": fields.get("role", ""),
                "notes": fields.get("notes", "")[:200],
                "source": "people_doc",
            }

    cache = _people_doc_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(directory, indent=2, ensure_ascii=False))
    # Bust the in-process alias cache so the next call re-reads
    global _aliases_cache
    _aliases_cache = None
    return len(directory)


def _apply_firm_aliases(text: str) -> str:
    """Rewrite known transcription errors in a string. Word-boundary safe."""
    if not text:
        return text
    aliases = (_load_aliases().get("firm_aliases") or {})
    if not aliases:
        return text
    import re
    out = text
    for wrong, right in aliases.items():
        if not wrong or not right:
            continue
        out = re.sub(rf"\b{re.escape(wrong)}\b", str(right), out)
    return out


def _lookup_person(name: str) -> dict | None:
    """Return {firm, role, notes} dict for a known person, or None."""
    if not name:
        return None
    directory = (_load_aliases().get("people_directory") or {})
    # Exact match (case-sensitive — names are stored Title Case)
    if name in directory:
        return directory[name] if isinstance(directory[name], dict) else None
    # Case-insensitive fallback
    lo = name.lower()
    for k, v in directory.items():
        if k.lower() == lo and isinstance(v, dict):
            return v
    return None

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

VALID_OWNERS = _build_owner_whitelist()

# content_type → top-level array in dashboard-data.json
ARRAY_DEST = {
    "my_action":          "followUps",
    "awaiting_external":  "awaitingExternal",
    "deal_takeaway":      "dealIntel",
    "origination_idea":   "originationInbox",
    "theme_note":         "themes",
}

# content_type → (parent-collection-key, parent-id-field, parent-name-field, history-array-field)
# status_update writes to the deal-pipeline array .notes[] to align with
# the existing doc parser (parse_deal_pipeline already populates notes[]
# from the Follow-ups doc's History blocks; the UI reads d.notes in
# buildTimeline). lp_intel writes to lpData[].history[] since lpData has
# no pre-existing timeline field.
#
# NOTE: _DEAL_ARR resolves to the top-level dashboard-data.json schema
# key for the deal-pipeline array. This key is fixed across tenants and
# is consumed by cos-dashboard-server / deal-system-compile / dashboard
# UI; renaming requires a coordinated migration across those modules.
# Resolution order: env var COS_DEAL_ARRAY_KEY → firm_config.json value →
# firm_context.yaml :: tenant_slug → live dashboard-data.json probe (any
# top-level list whose first item has 'name' + 'stage') → 'deals' fallback.
def _resolve_deal_array_key() -> str:
    env = os.environ.get("COS_DEAL_ARRAY_KEY")
    if env:
        return env
    try:
        import _firm_context as _fc  # type: ignore
        fc = _fc.load_firm_config() or {}
        v = fc.get("deal_array_key")
        if v:
            return str(v)
    except Exception:
        pass
    # Last resort: derive from tenant_slug. The legacy maintainer install
    # named the dashboard-data.json deal-array key after the tenant slug
    # (i.e. the slug from firm_context.yaml maps 1:1 to the array key).
    # For new subscriber installs the operator should set firm_config.json
    # :: deal_array_key or COS_DEAL_ARRAY_KEY explicitly — but the
    # slug-based default keeps the legacy install working without any
    # config touch.
    try:
        ctx = _CTX or {}
        slug = (ctx.get("tenant_slug") or "").strip().lower()
        if slug:
            return slug
    except Exception:
        pass
    # Hard fallback — let the live data file tell us which top-level array
    # holds deal-shaped entries (avoids hardcoding any particular key).
    try:
        live = json.loads(DASHBOARD_DATA_PATH.read_text())
        for key, val in live.items():
            if (isinstance(val, list) and val
                    and isinstance(val[0], dict)
                    and "name" in val[0] and "stage" in val[0]):
                return key
    except Exception:
        pass
    return "deals"


_DEAL_ARR = _resolve_deal_array_key()
HISTORY_DEST = {
    "status_update":  (_DEAL_ARR,  "ticker", "name", "notes"),
    "lp_intel":       ("lpData",   "id",     "name", "history"),
}


_lock = threading.Lock()


_DUE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")

# Per the contract in config/routing-rules.md:
# - owner=external is only meaningful with awaiting_external (a third party
#   owes the principal something; no other content_type uses owner=external).
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

# owner synonyms — first token / full-name match to canonical. Built from
# firm_context.yaml :: principal + team + owner_whitelist at module load
# (replaces the prior literal map). See _build_owner_synonyms() above.
_OWNER_SYNONYMS = _build_owner_synonyms()


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

    # Apply known transcription-error aliases to free-text fields. The
    # call-transcript pipeline regularly mishears proper nouns ("Encore"
    # for "Oncor", etc.); known-aliases.yaml maps wrong → right.
    for field in ("content", "context", "counterparty"):
        if out.get(field):
            rewritten = _apply_firm_aliases(out[field])
            if rewritten != out[field]:
                out[field] = rewritten
                out["_aliases_applied"] = True

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
            # Try first token (e.g. "Full Name" → "First")
            first = lo.split()[0] if lo.split() else ""
            if first in _OWNER_SYNONYMS:
                out["owner"] = _OWNER_SYNONYMS[first]

    # due date
    if out.get("due"):
        out["due"] = _normalize_due(out["due"])

    # counterparty formatting
    if out.get("counterparty"):
        out["counterparty"] = _normalize_counterparty(out["counterparty"])

    # ── Self-healing pass ───────────────────────────────────────────────────
    # The extractor (Sonnet/Haiku) emits items that are mostly right but
    # occasionally violate the schema. Rather than rejecting these to
    # routingExceptions, infer the missing piece from neighboring fields
    # or demote to the closest valid bucket. Every transformation leaves
    # an `_inferred_*` / `_demoted_from` breadcrumb for audit.
    ct = out.get("content_type")

    # 1. Soft parent resolution for status_update / lp_intel without parent_id.
    if ct in _NEEDS_PARENT and not (out.get("parent_id") or "").strip():
        haystack = " ".join([
            out.get("context") or "",
            out.get("content") or "",
            (out.get("source_ref") or {}).get("title", ""),
        ])
        try:
            kind = "lp" if ct == "lp_intel" else "deal"
            resolved = resolve_parent(haystack, kind=kind)
        except Exception:
            resolved = None
        if resolved:
            out["parent_id"] = resolved
            out["_inferred_parent_id"] = True
        elif ct == "status_update":
            # No tracked deal — demote to deal_takeaway (dealIntel[]).
            out["content_type"] = "deal_takeaway"
            out["_demoted_from"] = "status_update"
            ct = "deal_takeaway"
        elif ct == "lp_intel":
            # Unknown LP = new origination opportunity. Demote to
            # origination_idea (originationInbox[], no parent_id required).
            out["content_type"] = "origination_idea"
            out["_demoted_from"] = "lp_intel"
            ct = "origination_idea"

    # 2. Fix invalid (content_type, owner=external) combinations.
    #    Only awaiting_external is allowed to use owner=external. For other
    #    types, owner=external is meaningless — strip to the principal (the
    #    default actor in this single-principal pipeline).
    if (out.get("owner") or "").strip().lower() == "external" and ct not in _EXTERNAL_ONLY:
        principal_first = _principal_first()
        out["owner"] = principal_first
        out["_inferred_owner"] = f"external→{principal_first} (invalid combo)"

    # 3. Counterparty inference for awaiting_external. Without a Firm — Person
    #    pair the validator rejects, but we can usually derive Firm from
    #    parent_id (deal name) and Person from name patterns in content.
    if ct == "awaiting_external" and not (out.get("counterparty") or "").strip():
        firm = _infer_firm_from_parent(out.get("parent_id"))
        if not firm:
            firm = _infer_firm_from_text(" ".join([
                out.get("context") or "",
                out.get("content") or "",
                (out.get("source_ref") or {}).get("title", ""),
            ]))
        person = _infer_person_from_text(
            (out.get("content") or "") + " " + (out.get("context") or "")
        )
        # If person matches a known directory entry, use the directory's
        # firm — even if the parent_id-based firm differs (the directory
        # is the most authoritative source for person→firm association).
        person_lookup = _lookup_person(person) if person else None
        if person_lookup and person_lookup.get("firm"):
            firm = person_lookup["firm"]
            inference_source = "directory: person→firm lookup"
        elif firm and person:
            inference_source = "firm+person from parent+content"
        elif firm:
            inference_source = "firm-only from parent"
        elif person:
            inference_source = "person-only from content"
        else:
            inference_source = None

        if firm and person:
            out["counterparty"] = f"{firm} — {person}"
            out["_inferred_counterparty"] = inference_source
        elif firm:
            out["counterparty"] = f"{firm} — TBD"
            out["_inferred_counterparty"] = inference_source
        elif person:
            out["counterparty"] = f"TBD — {person}"
            out["_inferred_counterparty"] = inference_source
        else:
            # No way to identify the third party — demote to my_action so
            # the follow-up still surfaces, just on the principal's side.
            out["content_type"] = "my_action"
            out["owner"] = _principal_first()
            out["counterparty"] = ""
            out["_demoted_from"] = "awaiting_external"
            ct = "my_action"

    return out


# ── Inference helpers (used by self-healing pass) ──────────────────────────

def _infer_firm_from_parent(parent_id: str | None) -> str:
    """Look up parent_id against the deal-pipeline array / dealPortfolio /
    deal-pipeline-data and return the human-readable name. Checks id,
    slug-of-name, name, and aliases. Empty string if not found."""
    if not parent_id:
        return ""
    try:
        known = load_known_parents()
    except Exception:
        return ""
    pid = parent_id.strip().lower()
    pid_slug = _slugify(parent_id)
    for d in known.get("deals", []) + known.get("lps", []):
        d_id = d.get("id", "").lower()
        if d_id == pid or d_id == pid_slug:
            return d.get("name", "")
        if d.get("name", "").lower() == pid:
            return d.get("name", "")
        # Aliases
        for alias in d.get("aliases", []) or []:
            if alias and alias.lower() == pid:
                return d.get("name", "")
        # Bidirectional slug-prefix match (mirrors _attach_to_parent logic):
        # parent_id 'foo' should match deal slug 'foo-long-name-suffix'.
        if d_id and pid_slug and (
            d_id.startswith(pid_slug + "-") or pid_slug.startswith(d_id + "-")
        ):
            return d.get("name", "")
    return ""


def _infer_firm_from_text(text: str) -> str:
    """Try resolve_parent against free text to find a known firm name.
    Returns the deal/LP name if matched, else empty string."""
    if not text:
        return ""
    try:
        rid = resolve_parent(text, kind="deal")
        if rid:
            return _infer_firm_from_parent(rid)
        rid = resolve_parent(text, kind="lp")
        if rid:
            return _infer_firm_from_parent(rid)
    except Exception:
        pass
    return ""


def _known_firm_words() -> set[str]:
    """Lowercase token set covering all known firm/deal names — used to
    veto person-name candidates that are actually firm fragments. Excludes
    common English first names so person inference still works for names
    like 'Mark Mitchell' even when 'Mark' shows up as part of an LP firm
    name (e.g. 'Mark Lane Family Office')."""
    try:
        known = load_known_parents()
    except Exception:
        return set()
    out: set[str] = set()
    for d in known.get("deals", []) + known.get("lps", []):
        for tok in _tokens(d.get("name", "")):
            if len(tok) >= 4 and tok not in _COMMON_FIRST_NAMES:
                out.add(tok)
    return out


# Common English first names — never veto a person candidate just because
# one of these appears in a firm name. Static base + principal + team +
# owner_whitelist first-tokens (so a tenant whose principal is named e.g.
# "Aria" still gets person inference). Built from firm_context at load.
_COMMON_FIRST_NAMES = _build_common_first_names()


# Common false-positive name candidates we should skip when inferring Person.
# Built from firm_context :: counterparty_aliases[].canonical (so deal/firm
# brand names get vetoed) plus a small set of universal English bigrams.
_PERSON_NAME_BLOCKLIST = _build_person_name_blocklist()


def _infer_person_from_text(text: str) -> str:
    """Find a 'First Last' two-word capitalized name in the text. Returns
    the most plausible candidate or empty string. Avoids known firm-name
    bigrams (firm + counterparty canonicals from firm_context) and any
    token that's part of a known tracked firm."""
    if not text:
        return ""
    import re
    firm_words = _known_firm_words()
    # Two-word Title Case sequences. Use lookahead so consecutive
    # matches don't consume their first token — otherwise "Engage Mark
    # Mitchell" would only check "Engage Mark" and skip "Mark Mitchell".
    pattern = re.compile(r"(?=\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b)")
    GENERIC = {"The", "This", "That", "Phase", "Section", "Update",
               "Note", "Project", "Big", "New", "Old", "First",
               "Second", "Third", "Next", "Last", "Final"}
    # Imperative verbs that get capitalized at the start of action items.
    # Sonnet often emits content like "Confirm X" or "Ping Y" — the regex
    # would otherwise capture "Confirm Encore" or "Ping Mark" as a person.
    IMPERATIVE_VERBS = {
        "Confirm", "Ping", "Send", "Schedule", "Review", "Follow",
        "Get", "Set", "Call", "Email", "Text", "Reach", "Engage",
        "Pressure", "Verify", "Check", "Update", "Push", "Forward",
        "Reply", "Draft", "Sign", "Approve", "Decline", "Cancel",
        "Book", "Setup", "Setup", "Make", "Tell", "Ask", "Find",
        "Look", "Pull", "Share", "Discuss", "Debrief", "Submit",
        "Coordinate", "Arrange", "Loop", "Intro", "Introduce",
        "Investigate", "Research", "Confirm", "Establish",
    }
    for m in pattern.finditer(text):
        candidate = f"{m.group(1)} {m.group(2)}"
        if candidate in _PERSON_NAME_BLOCKLIST:
            continue
        if m.group(1) in GENERIC or m.group(2) in GENERIC:
            continue
        if m.group(1) in IMPERATIVE_VERBS:
            continue
        # Veto if either token is part of a known firm name (caught via
        # firm_context counterparty_aliases canonicals + deal-array / lpData[] tokens).
        if (m.group(1).lower() in firm_words or
                m.group(2).lower() in firm_words):
            continue
        return candidate
    return ""


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
        # slug prefix — match either direction:
        #   target='gic'  ↔ name='gic-singapore-swf' (target is prefix of name)
        #   target='foo-bar-baz' ↔ name='foo' (name is prefix of target)
        if target_slug and (name_slug == target_slug or
                            name_slug.startswith(target_slug + "-") or
                            target_slug.startswith(name_slug + "-")):
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
                    # Parent not found in destination collection. Common
                    # cause: deal was added to the source Google Doc but
                    # the deal-pipeline array hasn't been refreshed via
                    # cos-dashboard-fetch yet. Self-heal by demoting to
                    # deal_takeaway (dealIntel[])
                    # rather than failing — the item is preserved, and once
                    # fetch runs the parent will exist for future items.
                    if ct == "status_update":
                        item_demoted = dict(item)
                        item_demoted["content_type"] = "deal_takeaway"
                        item_demoted["_demoted_from"] = (
                            f"status_update (parent_id={parent_id!r} not yet in {parent_key})"
                        )
                        if _dedupe_append(data["dealIntel"], item_demoted):
                            summary["routed"]["deal_takeaway"] = (
                                summary["routed"].get("deal_takeaway", 0) + 1
                            )
                        else:
                            summary["skipped_dupes"] += 1
                    else:
                        # lp_intel parent miss → originationInbox (new LP idea)
                        item_demoted = dict(item)
                        item_demoted["content_type"] = "origination_idea"
                        item_demoted["_demoted_from"] = (
                            f"lp_intel (parent_id={parent_id!r} not yet in {parent_key})"
                        )
                        if _dedupe_append(data["originationInbox"], item_demoted):
                            summary["routed"]["origination_idea"] = (
                                summary["routed"].get("origination_idea", 0) + 1
                            )
                        else:
                            summary["skipped_dupes"] += 1
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
    # From deal-system-data.json (embedded as dealPortfolio.deals).
    # Use slug-of-name (not the short id like "pfs") so resolution returns
    # a value that matches what _attach_to_parent looks up in the
    # deal-pipeline array (which keys by slug-of-name, not by short id).
    dp = data.get("dealPortfolio", {}) or {}
    for d in dp.get("deals", []) or []:
        nm = d.get("name", "")
        deals.append({
            "id":      _slugify(nm) or d.get("id") or "",
            "name":    nm,
            "aliases": list(d.get("aliases", []) or []) + (
                [d["id"]] if d.get("id") and d["id"] != _slugify(nm) else []
            ),
        })

    # Build a name allowlist from the strategic deal pipeline
    # (deal-pipeline-data.json themes[].targets[]) — used to validate auto-
    # sourced deal-array entries. Only entries whose name matches a strategic
    # target's first-token will be promoted into the resolver pool.
    strategic_first_tokens: set[str] = set()
    try:
        sibling = (data_path or DASHBOARD_DATA_PATH).parent / "deal-pipeline-data.json"
        if sibling.exists():
            dp_data = json.loads(sibling.read_text())
            for theme in dp_data.get("themes", []) or []:
                for t in theme.get("targets", []) or []:
                    toks = _tokens(t.get("name", ""))
                    if toks and len(toks[0]) >= 4:
                        strategic_first_tokens.add(toks[0])
    except Exception:
        pass

    # From the deal-pipeline array — human-authored deal list. Auto-sourced
    # entries (stage starts "Sourcing / Auto") are mostly noise — vendors,
    # email senders, the firm itself, generic terms. We include them only
    # if their name matches a strategic target (i.e. an auto-tagged entry
    # that also appears in deal-pipeline-data.json themes).
    for t in data.get(_DEAL_ARR, []) or []:
        name = t.get("name") or t.get("title") or ""
        if not name:
            continue
        stage = (t.get("stage") or "")
        if stage.startswith("Sourcing / Auto"):
            first = _tokens(name)[:1]
            if not (first and first[0] in strategic_first_tokens):
                continue
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
      2. Each distinctive 2-word prefix of candidate (e.g. "Acme Power"
         matches "Acme Power Energy Hub")
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
        # Also test the first significant token alone — handles names like
        # "Foo / Person Bar" where the 2-word prefix "foo person" won't
        # appear in free text but "foo" will. Require 6+ chars to avoid
        # common-word collisions (e.g. "black" matching "Black Mountain"
        # against a "Black Forest" deal).
        if words and len(words[0]) >= 6:
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
