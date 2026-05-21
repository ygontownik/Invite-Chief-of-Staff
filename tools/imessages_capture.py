#!/opt/homebrew/bin/python3
"""
imessages_capture.py — Inbound iMessage / SMS capture for registered deals
====================================================================
Reads ~/Library/Messages/chat.db (read-only SQLite snapshot, requires Full
Disk Access on the parent Terminal/LaunchAgent), pulls messages received
since the last run, and appends matched ones to the right deal's log.json.

Matching is two-stage, mirroring local_file_router.py:
  1. Sender match — contact phone/email or handle vs drive-docs.yaml
     organizer_aliases + counterparties for each deal.
  2. Content match — message body vs the deal's compiled keyword/alias regex
     (same pattern as DEALS in local_file_router.py).

A message routes to a deal if EITHER stage hits. If multiple deals match,
log entries are written to each (counterparty/keyword overlap is real —
better to over-route than to drop intel).

State file: ~/credentials/imessages_state.json
  { "last_rowid": 0, "last_run": "<iso>" }

Usage:
  python3 imessages_capture.py              # one-shot, processes new since last_rowid
  python3 imessages_capture.py --dry-run    # show matches, no writes
  python3 imessages_capture.py --since-days 7  # bootstrap: scan past N days

LaunchAgent fires this every 15 min.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Sibling import: coordination.py ───────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    from coordination import lock as coord_lock
    _COORD_AVAILABLE = True
except ImportError:
    _COORD_AVAILABLE = False

# Envelope writer: pushes items into dashboard-data.json dealIntel[] so
# SMS surfaces on the dashboard's "Recent intel" tile + the /dash/mobile
# view, not just the per-deal activity log.
_ENVELOPE_PARENT = Path(__file__).resolve().parents[1]
if str(_ENVELOPE_PARENT) not in sys.path:
    sys.path.insert(0, str(_ENVELOPE_PARENT))
try:
    from _envelope_writer import append_items as envelope_append_items
    _ENVELOPE_AVAILABLE = True
except ImportError:
    _ENVELOPE_AVAILABLE = False

try:
    import yaml
except ImportError:
    print("Missing dependency: PyYAML. Run: pip install pyyaml")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME = Path.home()
STATE_PATH = HOME / "credentials" / "imessages_state.json"
LOG_PATH = HOME / "dashboards" / "logs" / "imessages_capture.log"
DRIVE_DOCS_YAML = HOME / "dashboards" / "config" / "drive-docs.yaml"
DEALS_DATA_DIR = HOME / "dashboards" / "data" / "deals"
CONTACTS_ALIAS_PATH = HOME / "cos-pipeline-config-tomac" / "known-aliases.yaml"
DEAL_SYSTEM_DATA = HOME / "dashboards" / "data" / "compiled" / "deal-system-data.json"
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"
# Scoped reader binary (Swift, ad-hoc signed). Owns the Full Disk Access
# grant so python3 never needs it. Build with:
#   swiftc -O -o imessages_reader imessages_reader.swift && codesign -s - imessages_reader
READER_BIN = _HERE / "imessages_reader"

# Apple Cocoa epoch: 2001-01-01 UTC. message.date is nanoseconds since then
# (modern macOS); older rows used seconds. We branch on magnitude.
COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


# ── Principal identity (loaded from firm_context, no hardcoded names) ─────────
def _load_principal_first_name() -> str:
    """Return the principal's first name from firm_context.yaml, or 'self'."""
    try:
        sys.path.insert(0, str(_ENVELOPE_PARENT))
        import _firm_context as _fc  # type: ignore
        ctx = _fc.load_firm_context()
        name = _fc.principal_first_name(ctx) or ""
        return name or "self"
    except Exception:
        return "self"

_PRINCIPAL_FIRST = _load_principal_first_name()


# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("imessages_capture")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


log = setup_logging()


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as e:
            log.warning(f"State file corrupt, resetting: {e}")
    return {"last_rowid": 0, "last_run": None}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


# ── Deal registry ─────────────────────────────────────────────────────────────
def load_deals() -> dict:
    """
    Returns:
      {
        "<deal_id>": {
          "name": str,
          "keyword_re": compiled regex (aliases + keywords + counterparties),
          "handles": set of normalized sender handles (phones/emails)
        }
      }
    """
    if not DRIVE_DOCS_YAML.exists():
        log.error(f"drive-docs.yaml not found at {DRIVE_DOCS_YAML}")
        return {}
    raw = yaml.safe_load(DRIVE_DOCS_YAML.read_text()) or {}
    deal_docs = raw.get("deal_docs") or {}
    contacts = _load_contacts()

    deals = {}
    for deal_id, cfg in deal_docs.items():
        aliases = cfg.get("organizer_aliases") or []
        keywords = cfg.get("keywords") or []
        counterparties = cfg.get("counterparties") or []
        # Skip ≤3-character tokens for SMS matching: they generate false
        # positives in casual text (e.g. "fit" → "right fit", "aps" → "perhaps",
        # "utl" → "shuttle"). The file router uses its own regex and is
        # unaffected. Multi-word phrases containing short tokens (e.g.
        # "Fit Ventures") still match because they're stored as one term.
        terms = sorted(
            {t.strip() for t in (aliases + keywords + counterparties)
             if t and t.strip() and len(t.strip()) > 3},
            key=len,
            reverse=True,
        )
        if not terms:
            continue

        pattern = "|".join(re.escape(t).replace(r"\.", r"[.\s_-]?") for t in terms)
        try:
            keyword_re = re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)
        except re.error as e:
            log.warning(f"Bad regex for {deal_id}: {e}; skipping")
            continue

        handles = _handles_for_counterparties(counterparties, contacts)

        deals[deal_id] = {
            "name": cfg.get("name", deal_id),
            "keyword_re": keyword_re,
            "handles": handles,
            "counterparties": counterparties,
        }
    return deals


def _load_contacts() -> dict:
    """Return the contacts directory dict, or {} if unavailable."""
    if not CONTACTS_ALIAS_PATH.exists():
        return {}
    try:
        return yaml.safe_load(CONTACTS_ALIAS_PATH.read_text()) or {}
    except Exception as e:
        log.debug(f"Could not parse contacts file: {e}")
        return {}


def _build_handle_index(contacts: dict) -> dict:
    """
    Walk known-aliases.yaml and return:
      { normalized_handle: {"name": <Display Name>, "firm": <firm or "">, "self": <bool>} }
    Used to resolve handle → human-readable identity at log-write time.
    UNLABELED__ keys are skipped (still placeholders).
    """
    index = {}
    if not isinstance(contacts, dict):
        return index
    people = contacts.get("people") or contacts
    if not isinstance(people, dict):
        return index
    for raw_name, entry in people.items():
        if not isinstance(entry, dict):
            continue
        if str(raw_name).startswith("UNLABELED__"):
            continue  # placeholder, no real name yet
        name = str(raw_name).strip()
        firm = (entry.get("firm") or "").strip()
        is_self = bool(entry.get("self"))
        for ph in (entry.get("phones") or []):
            n = _normalize_phone(ph)
            if n:
                index[n] = {"name": name, "firm": firm, "self": is_self}
        for em in (entry.get("emails") or []):
            if em:
                index[em.strip().lower()] = {"name": name, "firm": firm, "self": is_self}
    return index


def _handles_for_counterparties(counterparties: list, contacts: dict) -> set:
    """
    Walk the contacts/aliases registry for any phone/email handle whose
    associated person/firm name matches a counterparty. Returns a set of
    normalized handles (digits-only for phones, lowercase for emails).
    """
    out = set()
    if not contacts:
        return out
    cps_lower = {c.lower().strip() for c in counterparties if c}

    # Best-effort across a few common shapes — known-aliases.yaml might be
    # {person: {firm, phones, emails}} or {firms: {...}, people: {...}}.
    def _consider(name: str, entry: dict):
        if not isinstance(entry, dict):
            return
        firm = (entry.get("firm") or "").lower().strip()
        name_l = (name or "").lower().strip()
        hits = (name_l in cps_lower
                or firm in cps_lower
                or any(cp in name_l or cp in firm for cp in cps_lower))
        if not hits:
            return
        for ph in (entry.get("phones") or []):
            n = _normalize_phone(ph)
            if n:
                out.add(n)
        for em in (entry.get("emails") or []):
            if em:
                out.add(em.strip().lower())

    if isinstance(contacts, dict):
        for k, v in contacts.items():
            if isinstance(v, dict):
                _consider(k, v)
        for section in ("people", "contacts"):
            sub = contacts.get(section) if isinstance(contacts, dict) else None
            if isinstance(sub, dict):
                for k, v in sub.items():
                    _consider(k, v)
    return out


def _normalize_phone(p: str) -> str:
    if not p:
        return ""
    digits = re.sub(r"\D", "", p)
    # Strip US country code so +1-555-... matches 555-... and vice versa.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _normalize_handle(h: str) -> str:
    """imessage handle.id is usually 'tel:+15551234567' or 'mailto:foo@bar'
    or just '+15551234567' / 'foo@bar'. Normalize to phone-digits or
    lowercase email."""
    if not h:
        return ""
    h = h.strip()
    if h.startswith("tel:"):
        h = h[4:]
    if h.startswith("mailto:"):
        h = h[7:]
    if "@" in h:
        return h.lower()
    return _normalize_phone(h)


# ── Person → deals index (built from compiled dashboard data) ────────────────
_NAME_OK_RE = re.compile(r"^[A-Z][A-Za-z'\.\-]*(?:\s+[A-Z][A-Za-z'\.\-]*){0,4}$")
_REJECT_TOKENS = {"tbd", "n/a", "na", "unknown", "team", "internal"}


def _is_person_name(s: str) -> bool:
    """Reject 'TBD', 'CBRE' (all-caps), long descriptive multi-firm strings,
    parenthetical role tags, and slash-separated lists. Accept 'First Last',
    'O'Brien', 'St. John', 'Jean-Pierre'."""
    if not s:
        return False
    s = s.strip()
    if len(s) < 4 or len(s) > 50:
        return False
    if s.lower() in _REJECT_TOKENS:
        return False
    if any(ch in s for ch in (",", "/", "(", ")", "|", "—", "–", ":", ";")):
        return False
    if s.isupper():
        return False  # acronym like CBRE, NORPAC
    return bool(_NAME_OK_RE.match(s))


def _norm_name(s: str) -> str:
    """Normalize for cross-source matching: lowercase, collapse whitespace,
    strip leading/trailing punctuation."""
    return re.sub(r"\s+", " ", s.strip().strip(".,;:").lower())


def load_person_deal_index() -> tuple[dict, set]:
    """Walk deal-system-data.json + dashboard-data.json and return:

      (counterparty_index, team_set)

    counterparty_index: { normalized_name: set([deal_id, ...]) }
      — people who are external counterparties on a specific deal.
      A text from one of them auto-routes to those deal(s).

    team_set: { normalized_name }
      — same-firm teammates (the principal, partners). Listed as `contacts`
      on every deal. Their identity alone is NOT a deal signal — texts
      from them still need keyword/body match to route.
    """
    cp_idx: dict = {}
    contact_deal_count: dict = {}  # name → number of deals where they appear in contacts[]

    if DEAL_SYSTEM_DATA.exists():
        try:
            ds = json.loads(DEAL_SYSTEM_DATA.read_text())
        except Exception as e:
            log.warning(f"deal-system-data.json unreadable: {e}")
            ds = {}
        for d in ds.get("deals", []):
            deal_id = (d.get("id") or "").strip()
            if not deal_id:
                continue
            # `contacts[]` mixes TCIP team and deal-lead counterparties; we
            # disambiguate by deal-count below. For now, count appearances
            # and ALSO map each name to its deal — if it's a real team
            # member with 3+ deals it gets pruned at the end.
            for c in d.get("contacts", []) or []:
                name = c.get("name") if isinstance(c, dict) else c
                if _is_person_name(name or ""):
                    n = _norm_name(name)
                    contact_deal_count[n] = contact_deal_count.get(n, 0) + 1
                    cp_idx.setdefault(n, set()).add(deal_id)
            # Counterparties
            for c in d.get("counterparties", []) or []:
                name = c.get("name") if isinstance(c, dict) else c
                if _is_person_name(name or ""):
                    n = _norm_name(name)
                    cp_idx.setdefault(n, set()).add(deal_id)

    # TCIP team = names that appear in `contacts[]` of 3+ deals OR that
    # carry an explicit `team: true` / `self: true` flag in known-aliases.yaml.
    # The data heuristic catches Mark Saxe / Yoni (on every deal); the yaml
    # flag is the surgical override for edge cases (e.g. Nik on only 2 deals).
    team: set = {n for n, count in contact_deal_count.items() if count >= 3}
    contacts_yaml = _load_contacts()
    people_yaml = (contacts_yaml.get("people") if isinstance(contacts_yaml, dict)
                   else {}) or {}
    for raw_name, entry in people_yaml.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("team") or entry.get("self"):
            if not str(raw_name).startswith("UNLABELED__"):
                team.add(_norm_name(str(raw_name)))
        # Manual deals override: any person carrying `deals: [...]` in
        # known-aliases.yaml gets folded into cp_idx. This is the surgical
        # path for senders not yet in any deal-system-data counterparty
        # list (e.g. fresh sourcing contacts you've only met once).
        manual_deals = entry.get("deals") or []
        if isinstance(manual_deals, list) and manual_deals and not str(raw_name).startswith("UNLABELED__"):
            n = _norm_name(str(raw_name))
            for did in manual_deals:
                cp_idx.setdefault(n, set()).add(did)

    # Enrich from dashboard-data.json: awaitingExternal + followUps + dealIntel
    # carry counterparty/who fields tagged to a deal via dashboard_path.
    if DASHBOARD_DATA.exists():
        try:
            dd = json.loads(DASHBOARD_DATA.read_text())
        except Exception as e:
            log.warning(f"dashboard-data.json unreadable: {e}")
            dd = {}

        def _deal_from_path(path: str) -> str | None:
            """dashboard_path is like 'Deal Pipeline › <Deal Name> › ...' — pull <Deal Name>."""
            parts = [p.strip() for p in (path or "").split("›")]
            if len(parts) >= 2:
                return parts[1] or None
            return None

        # Build a slug→deal_id map (for path-derived names like "Cholla" → "cholla")
        slug_by_name: dict = {}
        for d in (ds.get("deals", []) if 'ds' in locals() else []):
            n = (d.get("name") or "").strip().lower()
            i = (d.get("id") or "").strip()
            if n and i:
                slug_by_name[n] = i

        def _deal_id_for(deal_name: str) -> str | None:
            if not deal_name:
                return None
            return slug_by_name.get(deal_name.lower())

        for src_key in ("awaitingExternal", "dealIntel", "originationInbox"):
            for item in dd.get(src_key) or []:
                cp = item.get("counterparty") or ""
                if not _is_person_name(cp):
                    continue
                deal = _deal_id_for(_deal_from_path(item.get("dashboard_path", "")) or "")
                if deal:
                    cp_idx.setdefault(_norm_name(cp), set()).add(deal)

        for fu in dd.get("followUps") or []:
            who = (fu.get("who") or "").strip()
            deal_name = (fu.get("deal") or "").strip()
            if _is_person_name(who) and deal_name:
                deal = _deal_id_for(deal_name)
                if deal:
                    cp_idx.setdefault(_norm_name(who), set()).add(deal)

        # emailQueue.from is a person; no deal mapping → skip for routing
        # but useful for the entity graph (not handled here).

    # Remove people who are clearly TCIP team — they should not auto-route
    cp_idx = {n: deals for n, deals in cp_idx.items() if n not in team}

    return cp_idx, team


# ── chat.db query (delegated to scoped Swift binary) ──────────────────────────
def fetch_messages(since_rowid: int, since_dt: datetime | None = None,
                   limit: int = 5000) -> list[dict]:
    """
    Invoke the imessages_reader binary (scoped FDA grant) and parse its JSONL
    stdout. since_dt is applied client-side after the rowid prefilter — for
    bootstrap (--since-days), we scan from rowid 0 and drop rows older than
    the cutoff before returning.
    """
    if not READER_BIN.exists():
        raise FileNotFoundError(
            f"imessages_reader binary missing at {READER_BIN}. "
            "Build with: swiftc -O -o imessages_reader imessages_reader.swift "
            "&& codesign -s - imessages_reader"
        )
    cmd = [str(READER_BIN), "--since-rowid", str(since_rowid),
           "--limit", str(max(1, min(limit, 50_000)))]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise TimeoutError("imessages_reader timed out after 60s")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "Full Disk Access" in stderr or "cannot open" in stderr:
            raise PermissionError(
                "imessages_reader could not read chat.db. Grant Full Disk Access "
                f"to {READER_BIN} in System Settings → Privacy & Security. "
                f"(stderr: {stderr})"
            )
        raise RuntimeError(f"imessages_reader exit {proc.returncode}: {stderr}")

    out = []
    cutoff_ns = None
    if since_dt is not None:
        cutoff_ns = int((since_dt - COCOA_EPOCH).total_seconds() * 1_000_000_000)
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cutoff_ns is not None and row.get("cocoa_date", 0) < cutoff_ns:
            continue
        handle_id = row.get("handle_id") or ""
        out.append({
            "rowid": row.get("rowid", 0),
            "cocoa_date": row.get("cocoa_date", 0),
            "date_iso": _cocoa_to_iso(row.get("cocoa_date", 0)),
            "text": row.get("text") or "",
            "service": row.get("service") or "",
            "handle_id": handle_id,
            "handle_norm": _normalize_handle(handle_id),
            "chat_id": row.get("chat_id", 0),
            "is_from_me": int(row.get("is_from_me") or 0),
            # Auto-resolved from macOS AddressBook by the Swift reader.
            # Empty string when no Contact matched.
            "sender_name": row.get("sender_name") or "",
            "sender_org":  row.get("sender_org") or "",
        })
    return out


def _cocoa_to_iso(cocoa_date: int) -> str:
    """Convert chat.db date column to ISO date (YYYY-MM-DD).
    Modern rows are nanoseconds since Cocoa epoch; older are seconds."""
    if cocoa_date is None:
        return datetime.now(timezone.utc).date().isoformat()
    # Heuristic — 10^12 separates seconds (≈10^8 typical) from nanoseconds (≈10^17).
    if abs(cocoa_date) > 10 ** 12:
        seconds = cocoa_date / 1_000_000_000
    else:
        seconds = float(cocoa_date)
    try:
        dt = COCOA_EPOCH + timedelta(seconds=seconds)
        return dt.date().isoformat()
    except (OverflowError, OSError):
        return datetime.now(timezone.utc).date().isoformat()


# ── Workstream people index (recruiting / LP / fundraising / personal) ───────
WORKSTREAM_DEAL_SLUG = {
    "job": "recruiting",
    "lp": "lp",
    "fundraising": "fundraising",
    "personal": "personal",
}

# Tiebreaker order when a contact appears in multiple workstream sources.
# Higher position = stronger claim. Rationale: fundraising > lp > job > personal
# because (a) fundraising contacts often appear in LP lists too — fundraising
# is the more specific signal; (b) job contacts who also appear in lp/fundraising
# are almost always work contacts in their fundraising role, not their hiring role.
WORKSTREAM_PRIORITY = ["fundraising", "lp", "job", "personal"]


def load_workstream_index() -> dict:
    """Return { normalized_name: workstream_slug } for non-deal contacts.

    Sources (per-source contributions collected first; then we pick the
    highest-priority workstream per name via WORKSTREAM_PRIORITY):
      - dashboard-data.recruiting.active[].contact      → workstream="job"
      - dashboard-data.lpData[].name (split on '/')     → workstream="lp"
      - dashboard-data.followUps[] where workstream != "deals" → tagged workstream
      - known-aliases.yaml entries where category == recruiting/lp/personal
    The yaml override is the strongest signal — its explicit category wins
    over anything the data heuristic infers.
    """
    candidates: dict = {}  # name → set of workstreams from data heuristics
    yaml_override: dict = {}  # name → workstream (highest authority)

    if DASHBOARD_DATA.exists():
        try:
            dd = json.loads(DASHBOARD_DATA.read_text())
        except Exception:
            dd = {}
        for r in (dd.get("recruiting") or {}).get("active", []) or []:
            ct = (r.get("contact") or "").strip()
            for nm in _split_names(ct):
                if _is_person_name(nm):
                    candidates.setdefault(_norm_name(nm), set()).add("job")
        for lp in dd.get("lpData", []) or []:
            for nm in _split_names(lp.get("name") or ""):
                if _is_person_name(nm):
                    candidates.setdefault(_norm_name(nm), set()).add("lp")
        for fu in dd.get("followUps") or []:
            ws = (fu.get("workstream") or "").strip().lower()
            if ws and ws != "deals":
                for nm in _split_names(fu.get("who") or ""):
                    if _is_person_name(nm):
                        candidates.setdefault(_norm_name(nm), set()).add(ws)

    # Yaml overrides — explicit category wins over data heuristics
    contacts_yaml = _load_contacts()
    people_yaml = (contacts_yaml.get("people") if isinstance(contacts_yaml, dict)
                   else {}) or {}
    for raw_name, entry in people_yaml.items():
        if not isinstance(entry, dict) or str(raw_name).startswith("UNLABELED__"):
            continue
        cat = (entry.get("category") or "").strip().lower()
        if cat in ("job", "lp", "fundraising", "personal", "recruiting"):
            ws = "job" if cat == "recruiting" else cat
            yaml_override[_norm_name(str(raw_name))] = ws

    # Pick winners: yaml override beats data heuristic; within data, highest
    # WORKSTREAM_PRIORITY position wins (e.g. lp contact who also shows up as
    # job in followUps stays in lp).
    priority_rank = {ws: i for i, ws in enumerate(WORKSTREAM_PRIORITY)}
    idx: dict = {}
    for name, ws_set in candidates.items():
        idx[name] = min(ws_set, key=lambda w: priority_rank.get(w, 999))
    idx.update(yaml_override)  # yaml wins
    return idx


def load_calendar_attendee_index(window_days: int = 3) -> dict:
    """Build {normalized_name: [(date_iso, workstream, event_title), ...]}
    from dashboard-data.calendar covering ±window_days. A text from someone
    you have a calendar event with that day strongly suggests routing to
    that event's workstream. The cos-dashboard-fetch.py update populates the
    `attendees` field per event; older snapshots without it return {}.
    """
    if not DASHBOARD_DATA.exists():
        return {}
    try:
        dd = json.loads(DASHBOARD_DATA.read_text())
    except Exception:
        return {}
    cal_days = dd.get("calendar") or []
    idx: dict = {}
    for day in cal_days:
        if not isinstance(day, dict):
            continue
        for ev in day.get("events", []) or []:
            ws = ev.get("workstream", "")
            title = ev.get("title", "")
            date = ev.get("date", "")
            for a in ev.get("attendees", []) or []:
                nm = a.get("name", "") if isinstance(a, dict) else ""
                em = a.get("email", "") if isinstance(a, dict) else ""
                if nm:
                    idx.setdefault(_norm_name(nm), []).append((date, ws, title))
                if em:
                    idx.setdefault(em.lower(), []).append((date, ws, title))
    return idx


def load_recent_intel_index(days: int = 30) -> dict:
    """Build {normalized_name: [(deal_id, mention_count, last_date), ...]} from
    dashboard-data.dealIntel and originationInbox entries in the last N days.
    Used to break ties when a sender maps to multiple deals via cp_idx —
    prefer the deal where they've been most recently active.
    """
    if not DASHBOARD_DATA.exists():
        return {}
    try:
        dd = json.loads(DASHBOARD_DATA.read_text())
    except Exception:
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    # Build deal_name -> deal_id lookup (intel uses display names; cp_idx uses ids)
    name_to_id: dict = {}
    if DEAL_SYSTEM_DATA.exists():
        try:
            ds = json.loads(DEAL_SYSTEM_DATA.read_text())
            for d in ds.get("deals", []):
                n = (d.get("name") or "").lower().strip()
                i = (d.get("id") or "").lower().strip()
                if n and i:
                    name_to_id[n] = i
        except Exception:
            pass

    def _to_deal_id(parent: str) -> str | None:
        if not parent:
            return None
        p = parent.lower().strip()
        return name_to_id.get(p) or (p if p in name_to_id.values() else None)

    # tally
    idx: dict = {}
    for src_key in ("dealIntel", "originationInbox"):
        for it in dd.get(src_key, []) or []:
            cp = (it.get("counterparty") or "").strip()
            if not cp:
                continue
            # Counterparty may be "Firm — Person" — split and use the last
            # token (usually the person)
            cp_norm = _norm_name(cp.split("—")[-1] if "—" in cp else cp)
            if not cp_norm:
                continue
            deal_id = _to_deal_id(it.get("parent_id") or "")
            if not deal_id:
                continue
            date = ""
            sref = it.get("source_ref") or {}
            if isinstance(sref, dict):
                date = (sref.get("date") or "")[:10]
            date = date or (it.get("addedDate") or "")[:10]
            if date and date < cutoff:
                continue
            bucket = idx.setdefault(cp_norm, {})
            slot = bucket.setdefault(deal_id, {"count": 0, "last_date": ""})
            slot["count"] += 1
            if date > slot["last_date"]:
                slot["last_date"] = date

    # Flatten to sorted list per person
    out: dict = {}
    for name, deal_map in idx.items():
        ranked = sorted(deal_map.items(),
                        key=lambda kv: (-kv[1]["count"], kv[1]["last_date"]),
                        reverse=False)
        out[name] = [(d, info["count"], info["last_date"]) for d, info in ranked]
    return out


def _split_names(s: str) -> list[str]:
    """'Quickwater / Mark' → ['Quickwater', 'Mark']  ;  'Foo Bar' → ['Foo Bar']."""
    if not s:
        return []
    parts = re.split(r"[/,]| at | with ", s)
    return [p.strip().split("(")[0].strip() for p in parts if p.strip()]


# ── Action extraction (regex-based, no LLM) ───────────────────────────────────
# Catches common commitment patterns in SMS — the highest-value extraction
# from short texts that LLM might miss for being too short to warrant the call.

_ACTION_PATTERNS = [
    # Pattern, owner, action-stem template
    (re.compile(r"\bI(?:'ll| will) (send|share|circle back|follow up|get back|pull|forward|introduce)\b[^.!?]*",
                re.IGNORECASE), _PRINCIPAL_FIRST, "I'll {verb} {obj}"),
    (re.compile(r"\b(?:can|could|would) you (send|share|provide|pull|forward|put together|draft)\b[^.!?]*",
                re.IGNORECASE), _PRINCIPAL_FIRST, "Send {obj} to {who}"),
    (re.compile(r"\b(?:please|pls) (send|share|forward|put together|draft)\b[^.!?]*",
                re.IGNORECASE), _PRINCIPAL_FIRST, "Send {obj} to {who}"),
    (re.compile(r"\b(?:send|shoot|share) me\b[^.!?]*", re.IGNORECASE),
     _PRINCIPAL_FIRST, "Send {obj} to {who}"),
    (re.compile(r"\blet me know\b[^.!?]*", re.IGNORECASE),
     "external", "Update " + _PRINCIPAL_FIRST + " on {obj}"),
]

_DUE_HINT = re.compile(
    r"\bby (tomorrow|today|EOD|EOW|next week|this week|end of (?:week|month|day)|"
    r"(?:next |this )?(?:mon|monday|tue|tues|tuesday|wed|wednesday|thu|thurs|thursday|fri|friday|sat|saturday|sun|sunday)|"
    r"\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b", re.IGNORECASE)

# Weekday string → 0..6 (Monday=0)
_WEEKDAY_NUM = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def normalize_due_hint(raw: str, reference_date: datetime | None = None) -> str:
    """Parse a relative-date hint to absolute YYYY-MM-DD.

    Handles: tomorrow, today, EOD, EOW, end of week/month/day, next week,
    this week, weekday names (mon/tue/wed/...), `next <weekday>`,
    `this <weekday>`, M/D, M/D/YY, M/D/YYYY, YYYY-MM-DD.

    Per L0008 (AB1): every stored date must be absolute YYYY-MM-DD.
    Returns "" when the hint can't be resolved unambiguously.
    """
    if not raw:
        return ""
    s = raw.strip().lower()
    if not s:
        return ""
    ref = (reference_date or datetime.now(timezone.utc)).date()

    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s

    # M/D, M/D/YY, M/D/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", s)
    if m:
        mo, da, yr = m.group(1), m.group(2), m.group(3)
        year = int(yr) if yr else ref.year
        if year < 100:
            year += 2000
        try:
            d = datetime(year, int(mo), int(da)).date()
            # If the parsed date is in the past, assume next year
            if not yr and d < ref:
                d = datetime(year + 1, int(mo), int(da)).date()
            return d.isoformat()
        except ValueError:
            return ""

    if s in ("today", "eod", "end of day"):
        return ref.isoformat()
    if s == "tomorrow":
        return (ref + timedelta(days=1)).isoformat()
    if s in ("eow", "end of week"):
        # Friday of the current week
        days_until_fri = (4 - ref.weekday()) % 7
        if days_until_fri == 0 and ref.weekday() > 4:
            days_until_fri = 7
        return (ref + timedelta(days=days_until_fri or 7 if ref.weekday() > 4 else days_until_fri)).isoformat()
    if s in ("eom", "end of month"):
        # Last day of the current month
        next_month = ref.replace(day=28) + timedelta(days=4)
        last = next_month - timedelta(days=next_month.day)
        return last.isoformat()
    if s == "next week":
        # Monday of next week
        days_until_mon = (7 - ref.weekday()) % 7 or 7
        return (ref + timedelta(days=days_until_mon)).isoformat()
    if s == "this week":
        # Friday of this week (or today if already Fri/Sat/Sun)
        days_until_fri = (4 - ref.weekday()) % 7
        return (ref + timedelta(days=days_until_fri)).isoformat()

    # Weekday handling — "monday", "next monday", "this monday"
    parts = s.split()
    is_next = is_this = False
    if parts and parts[0] in ("next", "this"):
        is_next = parts[0] == "next"
        is_this = parts[0] == "this"
        parts = parts[1:]
    if len(parts) == 1 and parts[0] in _WEEKDAY_NUM:
        target = _WEEKDAY_NUM[parts[0]]
        delta = (target - ref.weekday()) % 7
        if delta == 0:
            delta = 7   # "<weekday>" on the same weekday = a week out
        # Conversational usage: "next Monday" on a Thursday means the upcoming
        # Monday (4 days away), not the Monday after (11 days). Only force a
        # skip when delta is already 7 (same weekday today) or when user
        # said "this <weekday>" and we're past it (e.g. "this monday" on Wed
        # — interpret as the upcoming Monday a week out).
        if is_this and delta < 7 and delta < 3:
            # Past-week interpretation is rare; default to upcoming
            pass
        return (ref + timedelta(days=delta)).isoformat()

    return ""


def extract_actions(msg: dict, deal_id: str, parent_name: str,
                    who_display: str, who_firm: str) -> list[dict]:
    """Scan a single message body for commitment patterns and return envelope
    items ready for append_items(). Returns [] if nothing actionable found.

    Action ownership rules:
      Outbound msg ("I'll send X")        → owner=<principal>, my_action
      Inbound msg ("Can you send X")      → owner=<principal>, my_action (principal does it)
      Inbound msg ("I'll send X by …")    → owner=external, awaiting_external (they owe you)
      Inbound msg ("let me know …")       → owner=external, awaiting_external (they reply)
    """
    text = (msg.get("text") or "").strip()
    if not text or len(text) < 12:
        return []

    is_out = bool(msg.get("is_from_me"))
    items: list = []
    today = msg.get("date_iso") or datetime.now(timezone.utc).date().isoformat()

    # Due-date hint
    due_match = _DUE_HINT.search(text)
    due_raw = due_match.group(1) if due_match else ""
    # Don't try to resolve relative dates here; leave raw and let downstream
    # normalize. AB1 rule applies to STORED dates — these are extraction hints.

    # Dedup: a single message may match multiple patterns; we only want one
    # action item per (message, direction-of-obligation) pair.
    seen_actions: set = set()

    for pat, default_owner, template in _ACTION_PATTERNS:
        for m in pat.finditer(text):
            snippet = m.group(0).strip()[:200]
            # Direction of obligation:
            #   "I'll / I will <verb>"    — speaker commits to do <verb>
            #   "Can you / Please <verb>" — speaker asks listener to do <verb>
            #   "Send me <obj>"           — speaker asks listener to do
            #   "let me know <obj>"       — speaker asks listener to update
            is_speaker_commit = bool(
                re.search(r"\bI(?:'ll| will)\b", pat.pattern, re.IGNORECASE))
            if is_speaker_commit:
                # Speaker commits → speaker owes
                if is_out:                       # principal said "I'll …"
                    ct, owner = "my_action", _PRINCIPAL_FIRST
                else:                            # counterparty said "I'll …"
                    ct, owner = "awaiting_external", "external"
            else:
                # Speaker asks listener → listener owes
                if is_out:                       # principal asked counterparty
                    ct, owner = "awaiting_external", "external"
                else:                            # counterparty asked principal
                    ct, owner = "my_action", _PRINCIPAL_FIRST

            # Skip duplicate same-direction extractions per message
            sig = (ct, owner)
            if sig in seen_actions:
                continue
            seen_actions.add(sig)

            # Build counterparty string (only meaningful for external owner)
            cp_str = ((f"{who_firm} — {who_display}" if who_firm else who_display)
                      if owner == "external" else "")
            ctx_str = (f"iMessage {'(out)' if is_out else 'from'} "
                       f"{who_display}{(' (' + who_firm + ')') if who_firm else ''}")
            dash_path = (f"Deal Pipeline › {parent_name}" if deal_id
                         else f"Workstream › {parent_name}")
            item = {
                "content_type": ct,
                "owner": owner,
                "counterparty": cp_str,
                "parent_id": parent_name,
                "context": ctx_str,
                "dashboard_path": dash_path,
                "content": snippet,
                # Legacy-shape aliases so the existing dashboard renderer
                # (which reads who/what/deal/workstream from followUps[]
                # and awaitingExternal[]) picks these up correctly.
                # `who` = the OTHER party (the counterparty) per the existing
                # schema — NOT the action owner. The cleanup pass in
                # cos-dashboard-refresh.py drops items where who matches the
                # principal's first name (Yoni), so we must use the
                # counterparty name here even for my_action items.
                "who": who_display,
                "what": snippet,
                "deal": deal_id if deal_id else "",
                "workstream": "deals" if deal_id else "",
                "source": ctx_str,
                "priority": "medium",
                "source_ref": {
                    "source": "imessage", "date": today,
                    "rowid": msg.get("rowid"), "handle": msg.get("handle_id"),
                    "direction": "out" if is_out else "in",
                },
                "addedDate": today,
            }
            if ct in ("my_action", "awaiting_external"):
                # Resolve the relative-date hint (per L0008 — stored dates must
                # be absolute YYYY-MM-DD). Fall back to msg date + 7d only when
                # nothing parseable.
                msg_ref = None
                if msg.get("date_iso"):
                    try:
                        msg_ref = datetime.fromisoformat(msg["date_iso"])
                    except Exception:
                        msg_ref = None
                normalized = normalize_due_hint(due_raw, reference_date=msg_ref)
                if normalized:
                    item["due"] = normalized
                else:
                    fallback = (msg_ref.date() if msg_ref
                                else datetime.now(timezone.utc).date()) + timedelta(days=7)
                    item["due"] = fallback.isoformat()
            items.append(item)
    return items


# ── Awaiting-resolution detection ─────────────────────────────────────────────
# When SMS arrives from someone who has an open awaitingExternal item, check
# whether the SMS body looks like an ack/resolution of that item. Tags the
# item with `_sms_signals` so the dashboard can render a "Review — possibly
# resolved?" prompt.

_ACK_TOKENS = {
    "sent", "sending", "signed", "executed", "delivered", "shared",
    "attached", "complete", "completed", "done", "finalized", "approved",
    "confirmed", "circulated", "fwd", "forwarded", "drafted", "submitted",
    "wired", "filed", "uploaded", "received", "got it",
}
_PROGRESS_TOKENS = {
    "working", "almost", "tomorrow", "tonight", "today", "soon",
    "shortly", "circling", "drafting", "putting together",
    "wrapping", "reviewing", "in progress", "pending",
}
_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with",
    "is", "are", "was", "were", "be", "been", "being", "by", "at", "as",
    "but", "if", "this", "that", "those", "these", "from", "into", "over",
    "we", "you", "your", "yours", "i", "me", "my", "his", "her", "their",
    "it", "its", "have", "has", "had", "do", "does", "did", "will", "would",
    "should", "can", "could", "may", "might", "all", "any", "some", "no",
    "not", "only", "than", "then", "so", "such", "up", "out", "down", "off",
}

_AWAITING_RESOLUTION_PATH = Path.home() / "dashboards" / "data" / "compiled" / "dashboard-data.json"


def _tokenize(text: str) -> set:
    """Lowercase word tokens, alpha-only, length >= 3, minus stopwords."""
    if not text:
        return set()
    return {t for t in re.findall(r"[a-zA-Z][a-zA-Z']+", text.lower())
            if len(t) >= 3 and t not in _STOPWORDS}


def match_against_awaiting(msg: dict, sender_name: str, sender_firm: str,
                           awaiting_items: list) -> list[dict]:
    """For an inbound SMS from `sender_name`, scan `awaiting_items` for any
    open item this message looks like an ack/resolution of.

    Matching rules:
      A. Sender attribution — sender's name must appear in the item's
         `counterparty` field OR in the item's `content` text. Otherwise
         the SMS isn't from the party owing the action.
      B. Resolution signal — at least one of:
           - Body contains an ACK token (sent, signed, done, etc.)
           - Body contains a PROGRESS token AND ≥1 token overlap with content
           - Body has ≥30% token overlap with item content (substantive ref)

    Returns list of {item_id, item_idx, confidence, evidence, ack, overlap}.
    """
    text = (msg.get("text") or "").strip()
    if not text or len(text) < 4:
        return []
    body_lower = text.lower()
    body_tokens = _tokenize(text)
    sender_norm = _norm_name(sender_name) if sender_name else ""
    firm_norm = _norm_name(sender_firm) if sender_firm else ""

    has_ack = any(tok in body_lower for tok in _ACK_TOKENS)
    has_progress = any(tok in body_lower for tok in _PROGRESS_TOKENS)

    matches: list = []
    for idx, item in enumerate(awaiting_items):
        cp = (item.get("counterparty") or "").lower().strip()
        content = (item.get("content") or "").strip()
        if not content:
            continue
        content_lower = content.lower()
        content_tokens = _tokenize(content)
        if not content_tokens:
            continue

        # A. Sender attribution
        sender_in_cp = False
        if sender_norm:
            sender_in_cp = (
                sender_norm in cp
                or any(part in cp for part in sender_norm.split() if len(part) >= 4)
            )
        if not sender_in_cp and firm_norm:
            sender_in_cp = firm_norm in cp
        sender_in_content = bool(sender_norm and sender_norm in content_lower)
        if not (sender_in_cp or sender_in_content):
            continue

        overlap = body_tokens & content_tokens
        overlap_score = len(overlap) / max(1, len(content_tokens))

        # B. Resolution signal
        if has_ack and sender_in_cp:
            conf = "high"
        elif has_ack and overlap_score >= 0.10:
            conf = "high"
        elif overlap_score >= 0.30:
            conf = "medium"
        elif has_progress and sender_in_cp and overlap_score >= 0.10:
            conf = "low"
        else:
            continue

        matches.append({
            "item_id": item.get("id"),
            "item_idx": idx,
            "confidence": conf,
            "overlap_score": round(overlap_score, 2),
            "has_ack": has_ack,
            "has_progress": has_progress,
            "evidence": text[:200],
            "matched_tokens": sorted(overlap)[:8],
        })
    return matches


def detect_commitment_pairs(threads: list[list[dict]]) -> list[dict]:
    """Walk each thread chronologically. For every outbound message that
    contains a my_action commitment ("I'll send X", "I'll get back", etc.),
    look forward in the SAME thread for an inbound acknowledgement
    ("got it", "thanks", "perfect") within 7 days. Yield a pair record.

    Returns list of {commitment_msg, ack_msg, chat_id, days_to_ack}.
    These pairs feed into the followUp possibly-completed tagger.
    """
    # Lightweight commitment + ack patterns (the regex action extractor uses
    # the same vocabulary — keep them aligned).
    commit_re = re.compile(
        r"\bI(?:'ll| will) (send|share|circle back|follow up|get back|pull|forward|introduce|put together|draft|send over)\b",
        re.IGNORECASE)
    ack_re = re.compile(
        r"\b(got it|thanks|thank you|perfect|great|appreciated|received|that works|sounds good|will do|on it|noted)\b",
        re.IGNORECASE)

    out: list = []
    for thread in threads:
        # Pre-collect commitment + ack candidates in chronological order
        commits = []
        acks = []
        for i, m in enumerate(thread):
            text = (m.get("text") or "")
            if not text:
                continue
            if m.get("is_from_me") and commit_re.search(text):
                commits.append((i, m))
            elif not m.get("is_from_me") and ack_re.search(text):
                acks.append((i, m))
        # Pair each commitment with the NEXT ack within 7 days (by date)
        for ci, cm in commits:
            cdate = cm.get("date_iso") or ""
            for ai, am in acks:
                if ai <= ci:
                    continue  # ack must come AFTER the commitment
                adate = am.get("date_iso") or ""
                if cdate and adate:
                    try:
                        days = (datetime.fromisoformat(adate) - datetime.fromisoformat(cdate)).days
                    except Exception:
                        days = 0
                    if days < 0 or days > 7:
                        continue
                else:
                    days = 0
                out.append({
                    "chat_id": cm.get("chat_id"),
                    "commit_rowid": cm.get("rowid"),
                    "commit_date": cdate,
                    "commit_text": (cm.get("text") or "")[:200],
                    "ack_rowid": am.get("rowid"),
                    "ack_date": adate,
                    "ack_text": (am.get("text") or "")[:200],
                    "days_to_ack": days,
                })
                break  # only the first matching ack per commitment
    return out


def flag_completed_followups(pairs: list[dict], dashboard_path: Path = None) -> int:
    """For each commitment-ack pair, find the followUp `my_action` whose
    source_ref.rowid matches the commitment, and append a `_possibly_completed`
    record with the ack as evidence. Idempotent by ack_rowid.

    Returns count of followUps tagged.
    """
    path = dashboard_path or _AWAITING_RESOLUTION_PATH
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log.error(f"Cannot read dashboard data: {e}")
        return 0

    # Index followUps by source_ref.rowid (only those with imessage source)
    by_rowid: dict = {}
    for f in data.get("followUps", []):
        sref = f.get("source_ref") or {}
        if isinstance(sref, dict) and sref.get("source") == "imessage":
            rid = sref.get("rowid")
            if rid is not None:
                by_rowid.setdefault(rid, []).append(f)

    tagged = 0
    for p in pairs:
        fus = by_rowid.get(p["commit_rowid"], [])
        if not fus:
            continue
        for fu in fus:
            sigs = fu.setdefault("_possibly_completed", [])
            existing = {s.get("ack_rowid") for s in sigs if isinstance(s, dict)}
            if p["ack_rowid"] in existing:
                continue
            sigs.append({
                "ack_rowid": p["ack_rowid"],
                "ack_date": p["ack_date"],
                "ack_text": p["ack_text"],
                "days_to_ack": p["days_to_ack"],
            })
            tagged += 1

    if tagged:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(path)
    return tagged


def write_awaiting_signals(signals_by_item: dict[str, list], dashboard_path: Path = None) -> int:
    """Apply `_sms_signals` to dashboard-data.json `awaitingExternal[]`.

    signals_by_item: { item_id: [signal_dict, ...] }
    Each signal is appended to the item's `_sms_signals` array (creating it
    if missing). Returns count of items modified.
    """
    path = dashboard_path or _AWAITING_RESOLUTION_PATH
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log.error(f"Cannot read dashboard data: {e}")
        return 0

    modified = 0
    for item in data.get("awaitingExternal", []):
        iid = item.get("id")
        if iid not in signals_by_item:
            continue
        signals = item.setdefault("_sms_signals", [])
        existing_rowids = {s.get("rowid") for s in signals
                           if isinstance(s, dict) and s.get("rowid")}
        for sig in signals_by_item[iid]:
            if sig.get("rowid") in existing_rowids:
                continue
            signals.append(sig)
        modified += 1

    if modified:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(path)
    return modified


# ── LLM action extraction (subtler patterns the regex misses) ────────────────
# Uses _claude_dispatch per L0023 — never raw Anthropic SDK in pipeline code.
# Fires once per thread that has new activity since last run. Caches by
# (chat_id, last_rowid_seen) to avoid re-processing static history.

_LLM_CACHE_PATH = HOME / "credentials" / "imessages_llm_cache.json"
_LLM_MODEL = "claude-haiku-4-5-20251001"

# Per-run cap on LLM calls. Subscription dispatch spawns the Claude CLI per
# call (~5-10s each via Sonnet), so 50 calls = ~7 min. Cap protects the
# 15-min LaunchAgent cycle from over-running its own interval.
_LLM_CALLS_PER_RUN_MAX = 25
_llm_calls_this_run = 0

_LLM_SYSTEM = """You extract action items from SMS/iMessage threads for a deal pipeline tracker.

OUTPUT: a JSON object with one key `items` containing an array of action items. Empty array if nothing actionable. NEVER include other keys.

Each item must have:
- content_type: "my_action" | "awaiting_external"
- who: the OTHER party's name (the counterparty), NOT the principal
- what: a single-sentence action statement, verb-first
- due: YYYY-MM-DD if explicit, else empty string
- direction: "in" (counterparty sent) or "out" (principal sent)
- evidence: the exact source-text phrase that motivated this action (≤120 chars)

Rules:
- my_action = something the principal needs to do (for/with the counterparty)
- awaiting_external = something the counterparty owes the principal
- "I'll send X" in an OUTBOUND message → my_action (principal committed)
- "I'll send X" in an INBOUND message  → awaiting_external (they committed)
- "Can you send X" inbound  → my_action (principal does it)
- "Can you send X" outbound → awaiting_external (they do it)
- "Let me know about X" inbound → my_action (principal updates them)
- Skip social pleasantries ("nice meeting you", "thanks")
- Skip pure scheduling chatter UNLESS a concrete time + topic is set
- Be CONSERVATIVE: prefer 0 items over false positives. Output {"items":[]} if uncertain.
- Skip anything already captured by these regex patterns (we run those before you):
    * "I'll/I will <verb>"
    * "Can/Could you/Please <verb>"
    * "Send/Shoot me <obj>"
    * "Let me know <obj>"

Return ONLY the JSON object — no prose, no markdown."""


def _load_llm_cache() -> dict:
    if _LLM_CACHE_PATH.exists():
        try:
            return json.loads(_LLM_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_llm_cache(cache: dict) -> None:
    _LLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LLM_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(_LLM_CACHE_PATH)


def llm_extract_actions(thread: list[dict], parent_name: str, deal_id: str,
                        sender_lookup: dict, dry_run: bool = False) -> list[dict]:
    """Send a thread to Claude (via _claude_dispatch) and parse extracted actions.
    Returns envelope-shaped items ready for append_items()."""
    try:
        sys.path.insert(0, str(Path.home() / "cos-pipeline"))
        from _claude_dispatch import call as dispatch_call
    except ImportError:
        log.debug("_claude_dispatch unavailable, skipping LLM extraction")
        return []

    if len(thread) < 2:
        return []

    global _llm_calls_this_run
    if _llm_calls_this_run >= _LLM_CALLS_PER_RUN_MAX:
        return []  # per-run budget exhausted; next run will pick up the rest

    chat_id = thread[0].get("chat_id", 0)
    max_rowid = max(m.get("rowid", 0) for m in thread)
    cache_key = f"{chat_id}:{deal_id}"
    cache = _load_llm_cache()
    if cache.get(cache_key, 0) >= max_rowid and not dry_run:
        return []  # already processed up through this rowid

    _llm_calls_this_run += 1

    # Format thread as a compact transcript
    lines = []
    for m in thread:
        direction = "OUT" if m.get("is_from_me") else "IN"
        ov = sender_lookup.get(m.get("handle_norm",""), {})
        nm = ov.get("name") or m.get("sender_name") or m.get("handle_id","")
        date = m.get("date_iso","")
        text = (m.get("text") or "").strip().replace("\n", " ⏎ ")[:300]
        lines.append(f"[{date} {direction}] {nm}: {text}")
    transcript = "\n".join(lines)

    user_prompt = (
        f"DEAL CONTEXT: {parent_name or 'workstream'}\n\n"
        f"THREAD:\n{transcript}\n\n"
        f"Extract action items per the schema. Return ONLY the JSON object."
    )

    try:
        result = dispatch_call(
            task_type="sms_action_extract",
            model=_LLM_MODEL,
            messages=[{"role": "user", "content": user_prompt}],
            system=_LLM_SYSTEM,
            max_tokens=1024,
            cache=True,
            # Force subscription mode (per L0023) — the tenant firm_context
            # default may be api, but with an empty ANTHROPIC_API_KEY env that
            # path fails. Subscription uses the existing Claude Code OAuth.
            mode="subscription",
        )
    except Exception as e:
        log.warning(f"LLM extraction failed for thread {chat_id}: {e}")
        return []

    # Parse JSON (strip code fences if present)
    txt = (result or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        log.warning(f"LLM returned non-JSON for thread {chat_id}: {txt[:100]!r}")
        return []

    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        return []

    # Convert to envelope shape
    today = datetime.now(timezone.utc).date().isoformat()
    envelope_items = []
    for ri in raw_items:
        if not isinstance(ri, dict):
            continue
        ct = ri.get("content_type", "")
        if ct not in ("my_action", "awaiting_external"):
            continue
        what = (ri.get("what") or "").strip()
        if len(what) < 8:
            continue
        who = (ri.get("who") or "").strip()
        # Reject if who looks like the principal (per L0049)
        _pf = (_PRINCIPAL_FIRST or "").lower()
        _self_aliases = {"me", "self"}
        if _pf and _pf != "self":
            _self_aliases.add(_pf)
        if who.lower() in _self_aliases:
            continue
        due = (ri.get("due") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
            # Try the relative-date normalizer first
            normalized = normalize_due_hint(due) if due else ""
            due = normalized or (
                datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
        item = {
            "content_type": ct,
            "owner": _PRINCIPAL_FIRST if ct == "my_action" else "external",
            "counterparty": who,
            "parent_id": parent_name,
            "context": f"iMessage LLM-extracted ({ri.get('direction','in')})",
            "dashboard_path": (f"Deal Pipeline › {parent_name}" if deal_id
                               else f"Workstream › {parent_name}"),
            "content": what,
            "who": who,
            "what": what,
            "deal": deal_id if deal_id else "",
            "workstream": "deals" if deal_id else "",
            "due": due,
            "source": f"iMessage thread {chat_id}",
            "priority": "medium",
            "source_ref": {
                "source": "imessage", "date": today,
                "chat_id": chat_id, "extractor": "llm",
                "evidence": (ri.get("evidence") or "")[:200],
            },
            "addedDate": today,
        }
        envelope_items.append(item)

    # Mark cache so we don't re-process
    if not dry_run:
        cache[cache_key] = max_rowid
        _save_llm_cache(cache)

    return envelope_items


# ── Conversation grouping (chat_id + time window) ─────────────────────────────
# Bucket messages into threads — same chat_id, no gap >2h — so short replies
# without a deal keyword can inherit the deal from a nearby anchor message.
# This catches Mark Saxe / Nik replies that current per-message matching drops.

THREAD_GAP_SECONDS = 2 * 60 * 60  # 2 hours


def group_into_threads(msgs: list[dict]) -> list[list[dict]]:
    """Return list of threads. Each thread = consecutive messages sharing a
    chat_id with no time gap > THREAD_GAP_SECONDS. Messages must arrive
    pre-sorted by rowid (which is also chronological in chat.db)."""
    threads: list = []
    cur: list = []
    cur_chat: int | None = None
    last_ts: int = 0
    for m in msgs:
        cid = m.get("chat_id", 0)
        cd = m.get("cocoa_date", 0)
        # Cocoa date can be ns or s — normalize to seconds for gap math
        ts = cd / 1e9 if cd > 10 ** 12 else cd
        gap = (ts - last_ts) if last_ts else 0
        if cur and (cid != cur_chat or gap > THREAD_GAP_SECONDS):
            threads.append(cur)
            cur = []
        cur.append(m)
        cur_chat = cid
        last_ts = ts
    if cur:
        threads.append(cur)
    return threads


def attribute_thread(thread: list[dict], deals: dict,
                     person_deal_index: dict | None = None,
                     recent_intel_index: dict | None = None,
                     calendar_index: dict | None = None,
                     ) -> list[tuple[dict, list[str], list[str]]]:
    """Walk a thread chronologically. Per-message:
      - Run match_message → returns (deals, reasons) for THIS message
      - If matched: this is an "anchor". Use its deal(s).
      - If not matched: inherit the most recent preceding anchor's deal(s).
        Fallback: nearest following anchor (for messages BEFORE the first one).
        If no anchor in the thread: drop the message.
    The result is a list of (msg, deal_ids, reasons) tuples, only for messages
    that get attributed to at least one deal. Reasons may include the synthetic
    "thread_inherit" tag for non-anchor messages.
    """
    # First pass — per-message anchor classification
    anchors: list = []  # (index, deals, reasons)
    per_msg: list = [None] * len(thread)
    for i, m in enumerate(thread):
        dm, rs = match_message(m, deals, person_deal_index, recent_intel_index,
                               calendar_index=calendar_index)
        if dm:
            anchors.append((i, dm, rs))
            per_msg[i] = (list(dm), list(rs))

    if not anchors:
        return []  # no anchor in thread — every message drops

    # Second pass — inherit for non-anchors
    # Each non-anchor takes the deal(s) of the nearest anchor by index.
    anchor_idxs = [a[0] for a in anchors]
    anchor_by_idx = {a[0]: (a[1], a[2]) for a in anchors}

    def nearest_anchor(i: int) -> tuple[list, list]:
        # Find the closest anchor index. Ties broken by earlier-in-thread.
        best_idx = min(anchor_idxs, key=lambda ai: (abs(ai - i), ai))
        deals_, reasons_ = anchor_by_idx[best_idx]
        # Inherit reason tag — keep original reason for traceability + add inherit marker
        return list(deals_), ["thread_inherit" for _ in deals_]

    out: list = []
    for i, m in enumerate(thread):
        if per_msg[i] is not None:
            dm, rs = per_msg[i]
        else:
            dm, rs = nearest_anchor(i)
        out.append((m, dm, rs))
    return out


# ── Matching ──────────────────────────────────────────────────────────────────
def match_message(msg: dict, deals: dict,
                  person_deal_index: dict | None = None,
                  recent_intel_index: dict | None = None,
                  intel_boost_threshold: float = 2.0,
                  calendar_index: dict | None = None,
                  ) -> tuple[list[str], list[str]]:
    """Return (deal_ids, why) where why lists the matching pathway per deal.

    Three pathways:
      1. handle    — sender's normalized phone/email is in a deal's handles set
      2. body      — keyword regex matches text content
      3. sender    — sender's resolved name maps to a deal via person index
    """
    matches: set = set()
    reasons: dict = {}
    handle = msg.get("handle_norm", "")
    text = msg.get("text", "") or ""

    for deal_id, cfg in deals.items():
        if handle and handle in cfg["handles"]:
            matches.add(deal_id); reasons[deal_id] = "handle"
            continue
        if cfg["keyword_re"].search(text):
            matches.add(deal_id); reasons[deal_id] = "body"

    # Sender-identity routing (only when the sender resolved to a name)
    if person_deal_index:
        for nm_field in ("sender_name",):
            nm = (msg.get(nm_field) or "").strip()
            if not nm:
                continue
            sender_deals = person_deal_index.get(_norm_name(nm), set())
            if not sender_deals:
                continue

            # Recent-intel boost: when sender maps to ≥2 deals AND we have
            # recent-30d activity for them, prefer the deal(s) where they've
            # been mentioned at least `intel_boost_threshold` × more than
            # the runner-up. Avoids spraying every Mark Mitchell text to
            # both PNGTS and Unitil when 12/14 of his recent mentions are PNGTS.
            target_deals = set(sender_deals)
            if recent_intel_index and len(sender_deals) >= 2:
                ranked = recent_intel_index.get(_norm_name(nm)) or []
                ranked = [r for r in ranked if r[0] in sender_deals]
                if len(ranked) >= 2:
                    top_count = ranked[0][1]
                    second_count = ranked[1][1] if len(ranked) > 1 else 0
                    if second_count == 0 or top_count >= second_count * intel_boost_threshold:
                        target_deals = {ranked[0][0]}

            for deal_id in target_deals:
                if deal_id not in matches:
                    matches.add(deal_id)
                    reasons[deal_id] = "sender"
                # If already matched by body/handle, leave that stronger reason

    # Calendar pathway: when sender has a calendar event within ±2 days,
    # match the event's TITLE against each deal's keyword regex. If the
    # title names a deal, route. Disambiguation: when multiple deals' regexes
    # hit a single title (e.g. "Cholla / PNGTS sync"), pick the one with the
    # MOST distinct token matches in the title — strongest evidence wins.
    if calendar_index and not matches:
        sender = (msg.get("sender_name") or "").strip()
        msg_date = msg.get("date_iso", "")
        if sender and msg_date:
            events = calendar_index.get(_norm_name(sender)) or []
            try:
                msg_d = datetime.fromisoformat(msg_date).date()
            except Exception:
                msg_d = None
            if msg_d:
                for (ev_date, ws, title) in events:
                    try:
                        ev_d = datetime.fromisoformat(ev_date).date()
                        if abs((ev_d - msg_d).days) > 2:
                            continue
                    except Exception:
                        continue
                    title_l = (title or "")
                    if not title_l:
                        continue
                    # Score each deal by count of distinct keyword hits in title
                    scored: list = []
                    for deal_id, cfg in deals.items():
                        hits = cfg["keyword_re"].findall(title_l)
                        if not hits:
                            continue
                        # findall returns list of matched strings; count distinct
                        distinct = len(set(h.lower() if isinstance(h, str) else
                                           "".join(h).lower() for h in hits))
                        scored.append((distinct, deal_id))
                    if not scored:
                        continue
                    scored.sort(key=lambda t: (-t[0], t[1]))
                    top_score = scored[0][0]
                    # Route to all deals tied for the top score (rare; usually 1).
                    for sc, deal_id in scored:
                        if sc < top_score:
                            break
                        if deal_id not in matches:
                            matches.add(deal_id)
                            reasons[deal_id] = "calendar"

    return sorted(matches), [reasons[d] for d in sorted(matches)]


# ── log.json append ───────────────────────────────────────────────────────────
def append_log_entry(deal_id: str, entry: dict) -> None:
    deal_dir = DEALS_DATA_DIR / deal_id
    if not deal_dir.exists():
        log.warning(f"[{deal_id}] data dir missing at {deal_dir}; skipping log write")
        return
    log_path = deal_dir / "log.json"

    def _write():
        if log_path.exists():
            try:
                data = json.loads(log_path.read_text())
            except Exception as e:
                log.error(f"[{deal_id}] log.json corrupt ({e}); writing fresh shell")
                data = {"entries": []}
        else:
            data = {"entries": []}
        data.setdefault("entries", []).append(entry)
        tmp = log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(log_path)

    if _COORD_AVAILABLE:
        with coord_lock(f"log.json:{deal_id}", holder="imessages_capture.py",
                        ttl_seconds=30):
            _write()
    else:
        _write()


def build_envelope_item(msg: dict, deal_id: str, deal_name: str,
                        handle_index: dict | None = None,
                        is_counterparty: bool = False) -> dict:
    """Build an envelope-shaped `deal_takeaway` item for dashboard-data.json.
    Validated by _envelope_writer._validate before append. Owner=external
    only when the sender is a known counterparty (else routing rejects)."""
    text = (msg.get("text") or "").strip()
    handle_norm = msg.get("handle_norm", "")
    override = (handle_index or {}).get(handle_norm) or {}
    auto_name = (msg.get("sender_name") or "").strip()
    auto_org = (msg.get("sender_org") or "").strip()
    who = override.get("name") or auto_name or msg.get("handle_id", "")
    firm = override.get("firm") or auto_org

    item = {
        "content_type": "deal_takeaway",
        "owner": _PRINCIPAL_FIRST,
        "counterparty": f"{firm} — {who}" if (firm and who and firm != who) else who,
        "parent_id": deal_name,
        "context": f"iMessage from {who}" + (f" ({firm})" if firm else ""),
        "dashboard_path": f"Deal Pipeline › {deal_name}",
        "content": text,
        "source_ref": {
            "source": "imessage",
            "date": msg.get("date_iso"),
            "rowid": msg.get("rowid"),
            "handle": msg.get("handle_id"),
        },
        "addedDate": msg.get("date_iso") or datetime.now(timezone.utc).date().isoformat(),
    }
    return item


def build_entry(msg: dict, deal_id: str, handle_index: dict | None = None) -> dict:
    """Build a log.json entry consistent with the existing schema.

    Sender resolution priority (highest wins):
      1. known-aliases.yaml override   (handle_index) — your curated truth
      2. macOS AddressBook auto-match  (msg.sender_name from Swift reader)
      3. Raw handle (phone/email)      — fallback when neither resolves
    """
    handle = msg.get("handle_id", "")
    handle_norm = msg.get("handle_norm", "")
    text = (msg.get("text") or "").strip()
    snippet = text if len(text) <= 200 else text[:200] + "…"

    override = (handle_index or {}).get(handle_norm) or {}
    auto_name = (msg.get("sender_name") or "").strip()
    auto_org  = (msg.get("sender_org") or "").strip()

    if override.get("name"):
        who = override["name"]
        firm = override.get("firm") or auto_org
        source_layer = "alias_override"
    elif auto_name:
        who = auto_name
        firm = auto_org
        source_layer = "contacts_auto"
    else:
        who = handle
        firm = ""
        source_layer = "raw_handle"

    who_label = f"{who} ({firm})" if firm else who

    entry = {
        "id": uuid.uuid4().hex[:8],
        "date": msg["date_iso"],
        "source": "sms",
        "source_type": "imessage" if msg.get("service") == "iMessage" else "sms",
        "who": who,
        "what": text,
        "title": f"{msg.get('service','SMS')} from {who_label}: {snippet[:80]}",
        "match": deal_id,
    }
    if source_layer != "raw_handle":
        entry["from_handle"] = handle
        if firm:
            entry["from_firm"] = firm
        entry["resolved_by"] = source_layer
    return entry


# ── Main ──────────────────────────────────────────────────────────────────────
def run(dry_run: bool = False, since_days: int | None = None) -> int:
    deals = load_deals()
    if not deals:
        log.error("No deals loaded from drive-docs.yaml; aborting")
        return 1
    log.info(f"Loaded {len(deals)} deals: {', '.join(sorted(deals.keys()))}")

    contacts = _load_contacts()
    handle_index = _build_handle_index(contacts)
    self_handles = {h for h, info in handle_index.items() if info.get("self")}
    labeled_count = sum(1 for v in handle_index.values() if not v.get("self"))
    log.info(f"Contact directory: {labeled_count} labeled handles, "
             f"{len(self_handles)} self-handles (skipped)")

    person_deal_index, team_set = load_person_deal_index()
    log.info(f"Person→deals index: {len(person_deal_index)} counterparties "
             f"mapped, {len(team_set)} team names (excluded from sender routing)")

    workstream_idx = load_workstream_index()
    log.info(f"Workstream index: {len(workstream_idx)} non-deal contacts mapped")

    recent_intel_idx = load_recent_intel_index(days=30)
    log.info(f"Recent-intel index: {len(recent_intel_idx)} people with last-30d intel mentions")

    calendar_idx = load_calendar_attendee_index(window_days=3)
    log.info(f"Calendar index: {len(calendar_idx)} attendees from upcoming/recent events")

    # Load open awaitingExternal items once for resolution detection
    awaiting_items: list = []
    try:
        if DASHBOARD_DATA.exists():
            _dd = json.loads(DASHBOARD_DATA.read_text())
            awaiting_items = _dd.get("awaitingExternal", []) or []
    except Exception as e:
        log.warning(f"Could not load awaitingExternal for resolution detection: {e}")
    log.info(f"Awaiting-resolution scan against {len(awaiting_items)} open items")
    awaiting_signals: dict = {}  # item_id → [signal, ...]

    state = load_state()
    since_rowid = 0 if since_days else int(state.get("last_rowid") or 0)
    since_dt = None
    if since_days:
        since_dt = datetime.now(timezone.utc) - timedelta(days=since_days)
        log.info(f"Bootstrap scan: messages since {since_dt.isoformat()} (last {since_days}d)")
    else:
        log.info(f"Incremental scan: messages with ROWID > {since_rowid}")

    try:
        msgs = fetch_messages(since_rowid, since_dt)
    except (FileNotFoundError, PermissionError, RuntimeError, TimeoutError) as e:
        log.error(str(e))
        return 2

    # Overlay known-aliases.yaml overrides on the AddressBook-resolved fields
    # BEFORE matching. Otherwise senders like "Philip @ Montauk" who aren't in
    # macOS Contacts but ARE in our yaml never get sender-routed.
    overlaid = 0
    for m in msgs:
        ov = handle_index.get(m["handle_norm"]) or {}
        if ov.get("name") and not m.get("sender_name"):
            m["sender_name"] = ov["name"]
            if ov.get("firm"):
                m["sender_org"] = ov["firm"]
            overlaid += 1
    if overlaid:
        log.info(f"Applied known-aliases override to {overlaid} message(s) "
                 "without an AddressBook match")

    if not msgs:
        log.info("No new messages.")
        return 0

    log.info(f"Fetched {len(msgs)} new inbound messages (rowid range "
             f"{msgs[0]['rowid']}..{msgs[-1]['rowid']})")

    matched = 0
    routed = 0
    per_deal_counts: dict = {}

    self_skipped = 0
    routable_msgs = []
    for msg in msgs:
        if msg.get("handle_norm") in self_handles:
            self_skipped += 1
            continue
        routable_msgs.append(msg)

    threads = group_into_threads(routable_msgs)
    log.info(f"Grouped {len(routable_msgs)} routable msgs into {len(threads)} thread(s)")

    envelope_batch: list = []
    inherited = 0
    workstream_routed = 0
    actions_extracted = 0
    llm_actions_extracted = 0

    # Build sender_lookup once for LLM extraction context
    sender_lookup = {h: info for h, info in handle_index.items()}

    def _dedup_against_existing(new_items: list, batch: list) -> list:
        """Drop LLM items that overlap an already-extracted regex item in this run."""
        seen_keys = {((it.get("deal") or "") + ":" + (it.get("what") or "")[:50].lower())
                     for it in batch}
        out = []
        for it in new_items:
            k = (it.get("deal") or "") + ":" + (it.get("what") or "")[:50].lower()
            if k in seen_keys:
                continue
            seen_keys.add(k)
            out.append(it)
        return out

    for thread in threads:
        attributed = attribute_thread(thread, deals, person_deal_index,
                                      recent_intel_idx, calendar_idx)
        if attributed:
            for msg, deal_matches, reasons in attributed:
                # Skip writing outbound msgs to log.json (they're context, not
                # inbound intel) — but still run action extraction on them.
                is_out = bool(msg.get("is_from_me"))
                if not is_out:
                    matched += 1
                    for deal_id, why in zip(deal_matches, reasons):
                        if why == "thread_inherit":
                            inherited += 1
                        entry = build_entry(msg, deal_id, handle_index)
                        entry["match_reason"] = why
                        entry["direction"] = "in"
                        per_deal_counts[deal_id] = per_deal_counts.get(deal_id, 0) + 1
                        if dry_run:
                            log.info(f"[dry-run] {deal_id} via {why} ← {entry['title']}")
                        else:
                            append_log_entry(deal_id, entry)
                            routed += 1
                            envelope_batch.append(build_envelope_item(
                                msg, deal_id, deals[deal_id]["name"], handle_index,
                                is_counterparty=(why in ("handle", "sender")),
                            ))
                # Action extraction — runs on BOTH in and out
                for deal_id in deal_matches:
                    deal_name = deals[deal_id]["name"]
                    ov = handle_index.get(msg.get("handle_norm","")) or {}
                    nm = ov.get("name") or msg.get("sender_name") or msg.get("handle_id","")
                    fm = ov.get("firm") or msg.get("sender_org","")
                    items = extract_actions(msg, deal_id, deal_name, nm, fm)
                    if items:
                        actions_extracted += len(items)
                        if not dry_run:
                            envelope_batch.extend(items)
                        else:
                            for it in items:
                                log.info(f"[dry-run] ACTION {it['content_type']} → "
                                         f"{deal_id}: {it['content'][:80]}")
                # Awaiting-resolution detection — inbound only
                if not is_out and awaiting_items:
                    ov = handle_index.get(msg.get("handle_norm","")) or {}
                    nm = ov.get("name") or msg.get("sender_name") or ""
                    fm = ov.get("firm") or msg.get("sender_org","")
                    if nm:
                        sigs = match_against_awaiting(msg, nm, fm, awaiting_items)
                        for s in sigs:
                            iid = s["item_id"]
                            if not iid: continue
                            sig_record = {
                                "rowid": msg.get("rowid"),
                                "date": msg.get("date_iso"),
                                "from": nm,
                                "snippet": s["evidence"],
                                "confidence": s["confidence"],
                                "ack": s["has_ack"],
                                "overlap": s["overlap_score"],
                                "matched_tokens": s["matched_tokens"],
                            }
                            awaiting_signals.setdefault(iid, []).append(sig_record)
                            if dry_run:
                                log.info(f"[dry-run] AWAITING-SIGNAL conf={s['confidence']} "
                                         f"item={iid[:8]} ← {nm}: {s['evidence'][:60]!r}")
            # ── LLM action extraction for the whole thread ──
            # Fires once per thread per (chat_id, max_rowid). Catches subtler
            # commitments regex misses. Deduped against regex items by (deal, what[:50]).
            deal_ids_in_thread = {d for _, dm, _ in attributed for d in dm}
            for did in deal_ids_in_thread:
                dn = deals[did]["name"]
                llm_items = llm_extract_actions(
                    thread, dn, did, sender_lookup, dry_run=dry_run)
                if not llm_items:
                    continue
                llm_items = _dedup_against_existing(llm_items, envelope_batch)
                if not llm_items:
                    continue
                llm_actions_extracted += len(llm_items)
                if dry_run:
                    for it in llm_items:
                        log.info(f"[dry-run] LLM-ACTION {it['content_type']} → "
                                 f"{did}: {(it.get('what') or '')[:80]}")
                else:
                    envelope_batch.extend(llm_items)
            continue

        # No deal anchor — try workstream routing on the thread's senders
        ws_hits: dict = {}  # workstream_slug -> set of msg indices
        for i, msg in enumerate(thread):
            if msg.get("is_from_me"):
                continue
            ov = handle_index.get(msg.get("handle_norm","")) or {}
            nm = ov.get("name") or msg.get("sender_name") or ""
            ws = workstream_idx.get(_norm_name(nm))
            if ws:
                ws_hits.setdefault(ws, set()).add(i)
        if not ws_hits:
            continue
        # Single workstream wins the whole thread; multi-workstream uses split
        chosen_ws = max(ws_hits.keys(), key=lambda k: len(ws_hits[k]))
        ws_parent = WORKSTREAM_DEAL_SLUG.get(chosen_ws, chosen_ws)
        for msg in thread:
            is_out = bool(msg.get("is_from_me"))
            ov = handle_index.get(msg.get("handle_norm","")) or {}
            nm = ov.get("name") or msg.get("sender_name") or msg.get("handle_id","")
            fm = ov.get("firm") or msg.get("sender_org","")
            if not is_out:
                workstream_routed += 1
                # Action extraction runs for workstream messages too
            items = extract_actions(msg, "", ws_parent.title(), nm, fm)
            if items:
                # Tag with workstream
                for it in items:
                    it["workstream"] = chosen_ws
                    it["dashboard_path"] = f"Workstream › {ws_parent.title()}"
                actions_extracted += len(items)
                if not dry_run:
                    envelope_batch.extend(items)
                else:
                    for it in items:
                        log.info(f"[dry-run] WORKSTREAM-ACTION {it['content_type']} → "
                                 f"{chosen_ws}: {it['content'][:80]}")

        # LLM extraction for workstream threads as well
        llm_items = llm_extract_actions(
            thread, ws_parent.title(), "", sender_lookup, dry_run=dry_run)
        if llm_items:
            for it in llm_items:
                it["workstream"] = chosen_ws
                it["dashboard_path"] = f"Workstream › {ws_parent.title()}"
            llm_items = _dedup_against_existing(llm_items, envelope_batch)
            if llm_items:
                llm_actions_extracted += len(llm_items)
                if dry_run:
                    for it in llm_items:
                        log.info(f"[dry-run] LLM-WORKSTREAM-ACTION {it['content_type']} → "
                                 f"{chosen_ws}: {(it.get('what') or '')[:80]}")
                else:
                    envelope_batch.extend(llm_items)

    log.info(f"Of {matched} matched, {inherited} were thread-inherited; "
             f"{workstream_routed} workstream-routed; "
             f"{actions_extracted} regex actions, {llm_actions_extracted} llm actions extracted")

    # Persist awaiting-resolution signals to dashboard-data.json
    if awaiting_signals and not dry_run:
        n_items = write_awaiting_signals(awaiting_signals)
        n_signals = sum(len(v) for v in awaiting_signals.values())
        log.info(f"Awaiting signals: {n_signals} signal(s) attached to {n_items} item(s)")
    elif awaiting_signals and dry_run:
        n_signals = sum(len(v) for v in awaiting_signals.values())
        log.info(f"[dry-run] Would attach {n_signals} awaiting signal(s)")

    # Commitment-pair detection — find your "I'll do X" → their "got it" pairs
    # and flag the corresponding my_action followUp as possibly-completed.
    pairs = detect_commitment_pairs(threads)
    if pairs and not dry_run:
        n_tagged = flag_completed_followups(pairs)
        log.info(f"Commitment-pairs: detected {len(pairs)} pair(s), tagged {n_tagged} followUp(s)")
    elif pairs and dry_run:
        log.info(f"[dry-run] Detected {len(pairs)} commitment-ack pair(s)")
        for p in pairs[:5]:
            log.info(f"  pair: rowid {p['commit_rowid']} → {p['ack_rowid']} "
                     f"({p['days_to_ack']}d) {p['commit_text'][:50]!r} → {p['ack_text'][:30]!r}")

    if envelope_batch and not dry_run and _ENVELOPE_AVAILABLE:
        try:
            summary = envelope_append_items(envelope_batch)
            if summary.get("exceptions"):
                log.warning(f"envelope rejected {summary['exceptions']} item(s); "
                            f"see dashboard-data.routingExceptions")
            else:
                log.info(f"envelope: {summary.get('routed', {})}")
        except Exception as e:
            log.error(f"envelope_append_items failed: {e}", exc_info=True)

    log.info(f"Scan summary — {len(msgs)} fetched | {self_skipped} self-skipped | "
             f"{matched} matched | {routed} log entries written"
             f"{' (dry-run)' if dry_run else ''}")
    if per_deal_counts:
        log.info(f"Per-deal counts: {per_deal_counts}")

    if not dry_run and msgs:
        state["last_rowid"] = msgs[-1]["rowid"]
        state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state)
        log.info(f"State advanced to last_rowid={state['last_rowid']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="iMessage / SMS capture → deal log.json")
    p.add_argument("--dry-run", action="store_true",
                   help="Report matches without writing log.json or state")
    p.add_argument("--since-days", type=int, default=None,
                   help="Bootstrap: scan inbound messages from the last N days "
                        "(ignores last_rowid; does not advance state in dry-run)")
    args = p.parse_args()

    # Coordination lock — prevents the every-15-min LaunchAgent from colliding
    # with manual `python imessages_capture.py` runs, which previously caused
    # double-writes when both processes scanned the same rowids in parallel.
    # Skip lock on --dry-run (no writes anyway).
    if args.dry_run or not _COORD_AVAILABLE:
        return run(dry_run=args.dry_run, since_days=args.since_days)
    try:
        with coord_lock("imessages_capture", holder="imessages_capture.py",
                        ttl_seconds=900, timeout_seconds=5):
            return run(dry_run=args.dry_run, since_days=args.since_days)
    except TimeoutError:
        log.info("Another imessages_capture run is in progress; exiting cleanly. "
                 "Next 15-min tick will retry.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
