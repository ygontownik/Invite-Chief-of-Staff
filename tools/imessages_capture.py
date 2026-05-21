#!/opt/homebrew/bin/python3
"""
imessages_capture.py — Inbound iMessage / SMS capture for TCIP deals
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
    parenthetical role tags, and slash-separated lists. Accept 'Mark Saxe',
    'Mark Mitchell', 'O'Brien', 'St. John', 'Jean-Pierre'."""
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
      — TCIP-side teammates (you, Mark Saxe, Nik). Listed as `contacts`
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
            "date_iso": _cocoa_to_iso(row.get("cocoa_date", 0)),
            "text": row.get("text") or "",
            "service": row.get("service") or "",
            "handle_id": handle_id,
            "handle_norm": _normalize_handle(handle_id),
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


# ── Matching ──────────────────────────────────────────────────────────────────
def match_message(msg: dict, deals: dict,
                  person_deal_index: dict | None = None) -> tuple[list[str], list[str]]:
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
            for deal_id in person_deal_index.get(_norm_name(nm), set()):
                if deal_id not in matches:
                    matches.add(deal_id)
                    reasons[deal_id] = "sender"
                # If already matched by body/handle, leave that stronger reason

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
        "owner": "Yoni",
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
             f"mapped, {len(team_set)} TCIP-team names (excluded from sender routing)")

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

    if not msgs:
        log.info("No new messages.")
        return 0

    log.info(f"Fetched {len(msgs)} new inbound messages (rowid range "
             f"{msgs[0]['rowid']}..{msgs[-1]['rowid']})")

    matched = 0
    routed = 0
    per_deal_counts: dict = {}

    self_skipped = 0
    for msg in msgs:
        # Don't route messages YOU sent to yourself from a second device —
        # they are your own notes, not inbound counterparty signal. They
        # still feed into the entity graph + log file but here we skip.
        if msg.get("handle_norm") in self_handles:
            self_skipped += 1
            continue
        deal_matches, reasons = match_message(msg, deals, person_deal_index)
        if not deal_matches:
            continue
        matched += 1
        envelope_batch: list = []
        for deal_id, why in zip(deal_matches, reasons):
            entry = build_entry(msg, deal_id, handle_index)
            entry["match_reason"] = why  # handle | body | sender
            per_deal_counts[deal_id] = per_deal_counts.get(deal_id, 0) + 1
            if dry_run:
                log.info(f"[dry-run] {deal_id} via {why} ← {entry['title']}")
            else:
                append_log_entry(deal_id, entry)
                routed += 1
                log.info(f"[{deal_id}] via {why} ← {entry['who']} (rowid={msg['rowid']})")
                # Also emit as envelope item so the dashboard's dealIntel
                # tile + /dash/mobile picks it up (per-deal log.json alone
                # only feeds the deal-detail activity_log).
                deal_name = deals[deal_id]["name"]
                envelope_batch.append(build_envelope_item(
                    msg, deal_id, deal_name, handle_index,
                    is_counterparty=(why in ("handle", "sender")),
                ))

        if envelope_batch and not dry_run and _ENVELOPE_AVAILABLE:
            try:
                summary = envelope_append_items(envelope_batch)
                if summary.get("exceptions"):
                    log.warning(f"envelope rejected {summary['exceptions']} item(s); "
                                f"see dashboard-data.routingExceptions")
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
    return run(dry_run=args.dry_run, since_days=args.since_days)


if __name__ == "__main__":
    sys.exit(main())
