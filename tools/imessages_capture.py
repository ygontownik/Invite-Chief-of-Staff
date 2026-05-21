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

    # Optional contact directory: maps person -> {phones, emails, firms}
    contacts = {}
    if CONTACTS_ALIAS_PATH.exists():
        try:
            contacts = yaml.safe_load(CONTACTS_ALIAS_PATH.read_text()) or {}
        except Exception as e:
            log.debug(f"Could not parse contacts file: {e}")

    deals = {}
    for deal_id, cfg in deal_docs.items():
        aliases = cfg.get("organizer_aliases") or []
        keywords = cfg.get("keywords") or []
        counterparties = cfg.get("counterparties") or []
        terms = sorted(
            {t.strip() for t in (aliases + keywords + counterparties) if t and t.strip()},
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
def match_message(msg: dict, deals: dict) -> list[str]:
    """Return list of deal_ids this message matches."""
    matches = set()
    handle = msg.get("handle_norm", "")
    text = msg.get("text", "") or ""
    for deal_id, cfg in deals.items():
        if handle and handle in cfg["handles"]:
            matches.add(deal_id)
            continue
        if cfg["keyword_re"].search(text):
            matches.add(deal_id)
    return sorted(matches)


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


def build_entry(msg: dict, deal_id: str) -> dict:
    """Build a log.json entry consistent with the existing schema."""
    handle = msg.get("handle_id", "")
    text = (msg.get("text") or "").strip()
    snippet = text if len(text) <= 200 else text[:200] + "…"
    return {
        "id": uuid.uuid4().hex[:8],
        "date": msg["date_iso"],
        "source": "sms",
        "source_type": "imessage" if msg.get("service") == "iMessage" else "sms",
        "who": handle,
        "what": text,
        "title": f"{msg.get('service','SMS')} from {handle}: {snippet[:80]}",
        "match": deal_id,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def run(dry_run: bool = False, since_days: int | None = None) -> int:
    deals = load_deals()
    if not deals:
        log.error("No deals loaded from drive-docs.yaml; aborting")
        return 1
    log.info(f"Loaded {len(deals)} deals: {', '.join(sorted(deals.keys()))}")

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

    for msg in msgs:
        deal_matches = match_message(msg, deals)
        if not deal_matches:
            continue
        matched += 1
        for deal_id in deal_matches:
            entry = build_entry(msg, deal_id)
            per_deal_counts[deal_id] = per_deal_counts.get(deal_id, 0) + 1
            if dry_run:
                log.info(f"[dry-run] {deal_id} ← {entry['title']}")
            else:
                append_log_entry(deal_id, entry)
                routed += 1
                log.info(f"[{deal_id}] appended log entry from {msg['handle_id']} "
                         f"(rowid={msg['rowid']})")

    log.info(f"Scan summary — {len(msgs)} fetched | {matched} matched | "
             f"{routed} log entries written{' (dry-run)' if dry_run else ''}")
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
