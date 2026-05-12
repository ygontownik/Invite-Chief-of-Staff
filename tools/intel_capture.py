#!/usr/bin/env python3
"""
intel_capture.py — scan Claude Code transcripts (and later claude.ai chats)
for `---DEAL-INTEL---` blocks and route them into the corresponding deal's
`~/dashboards/data/deals/<deal>/log.json` feed.

This is the bridge from "Claude said something useful about a deal in a
session" to "that intel reaches the deal's status doc on the next /deal-sync
cycle." Single source of truth: log.json. Single regenerator: /deal-sync.

Sub-commands:
  scan-claude-code      grep ~/.claude/projects/*/*.jsonl for new DEAL-INTEL
                        blocks since last scan; route each to log.json
  parse-stdin           read text from stdin, extract any DEAL-INTEL blocks,
                        route them. Used for ad-hoc piping and testing.
  route-transcript      read a call/meeting transcript (local file or Drive
                        file ID), identify all registered deals mentioned,
                        extract per-deal intel, write to each deal's log.json.
                        Uses Claude API — model claude-sonnet-4-6.
  scan-claude-ai        TODO: Chrome MCP scrape of claude.ai project chats.
                        Requires running inside a Claude Code session that
                        has Chrome MCP loaded — invoked via /deal-sync child.

State:
  ~/dashboards/data/intel_capture_state.json
    { "<surface>": { "<file_or_chat_id>": { "scanned_at": ISO,
                     "last_block_offset": int, "captured_block_ids": [...] }}}

Block format (canonical, see ~/.claude/CLAUDE.md Rule DI1):
  ---DEAL-INTEL---
  deal: <deal_id>
  date: YYYY-MM-DD
  title: <one-line>
  summary: <1-2 sentences>
  facts:
    - <fact 1>
  counterparties:
    - <name (firm)> — <info>
  actions:
    - <date>: <action> [@owner]
  ---END-DEAL-INTEL---

Tolerant parser: trailing/leading whitespace OK, missing optional sections OK,
case-insensitive section names. Required: `deal:` line. If `deal:` doesn't
match a registered deal_id, the block is logged to errors and skipped.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import yaml
from datetime import datetime
from pathlib import Path

HOME = Path.home()
TRANSCRIPTS_DIR = HOME / ".claude" / "projects"
STATE_PATH = HOME / "dashboards" / "data" / "intel_capture_state.json"
ERROR_LOG = HOME / "dashboards" / "logs" / "intel_capture_errors.log"
DRIVE_DOCS_YAML = HOME / "dashboards" / "config" / "drive-docs.yaml"
DEAL_REGISTRY = HOME / "cos-pipeline" / "tools" / "deal-system-data.json"
HELPER_BIN = HOME / "cos-pipeline" / "tools" / "deal_extract_helpers.py"

BLOCK_RE = re.compile(
    r"---DEAL-INTEL---\s*\n(.*?)\n\s*---END-DEAL-INTEL---",
    re.DOTALL | re.IGNORECASE,
)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def djb2(s):
    h = 5381
    for c in s:
        h = ((h << 5) + h) + ord(c)
        h &= 0xFFFFFFFF
    return f"{h:08x}"


def log_error(source, ident, err):
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat()
    with open(ERROR_LOG, "a") as f:
        f.write(f"{ts} source={source} id={ident} error={err}\n")


# ── Registry ──────────────────────────────────────────────────────────────────

def load_registered_deal_ids():
    try:
        data = json.loads(DEAL_REGISTRY.read_text())
        return {d["deal_id"] for d in data.get("deals", [])}
    except Exception:
        return set()


# ── Block parsing ─────────────────────────────────────────────────────────────

def parse_block(body):
    """Parse the body of a DEAL-INTEL block (the text between the markers)
    into a dict. Tolerant of YAML-ish formatting."""
    # Try YAML first — the format is essentially YAML.
    try:
        d = yaml.safe_load(body)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    # Fall back to manual key:value parsing for malformed YAML
    out = {}
    cur_key = None
    cur_list = None
    for line in body.split("\n"):
        if not line.strip():
            continue
        if line.startswith("  - ") or line.startswith("    - "):
            if cur_list is not None:
                cur_list.append(line.split("- ", 1)[1].strip())
            continue
        m = re.match(r"^([a-zA-Z_][a-zA-Z_]*)\s*:\s*(.*)$", line)
        if m:
            cur_key = m.group(1).lower()
            val = m.group(2).strip()
            if val:
                out[cur_key] = val
                cur_list = None
            else:
                # Begin a list-valued field
                out[cur_key] = []
                cur_list = out[cur_key]
    return out


def _s(v, default=""):
    """Coerce any YAML-parsed value to a stripped string."""
    if v is None:
        return default
    return str(v).strip() or default


def block_to_log_entry(block_data, surface_label):
    """Convert a parsed DEAL-INTEL block into a log.json entry."""
    deal_id = _s(block_data.get("deal")).lower()
    title = _s(block_data.get("title"), "deal-intel block")
    date = _s(block_data.get("date"), datetime.now().strftime("%Y-%m-%d"))
    summary = _s(block_data.get("summary"))
    facts = block_data.get("facts", []) or []
    counterparties = block_data.get("counterparties", []) or []
    actions = block_data.get("actions", []) or []

    # what — the human-readable rollup of the block content
    what_lines = []
    if summary:
        what_lines.append(summary)
    if facts:
        what_lines.append("Facts: " + "; ".join(str(f) for f in facts))
    if counterparties:
        what_lines.append("Counterparties: " + "; ".join(str(c) for c in counterparties))
    if actions:
        what_lines.append("Actions: " + "; ".join(str(a) for a in actions))
    what = " | ".join(what_lines) or title

    # Stable id from content hash so re-scanning is idempotent.
    content_for_id = f"{deal_id}|{date}|{title}|{summary}|{surface_label}"
    return deal_id, {
        "id": djb2(content_for_id),
        "date": date,
        "source": "intel",
        "source_type": surface_label,  # "claude-code" | "claude-ai" | "stdin"
        "who": "Claude session",
        "what": what,
        "title": title,
        "match": deal_id,
    }


def route_block(block_data, surface_label):
    """Validate + route one parsed block to the correct deal's log.json.
    Returns (deal_id, entry_id, status) where status is 'ok' or 'error'."""
    registered = load_registered_deal_ids()
    deal_id, entry = block_to_log_entry(block_data, surface_label)
    if not deal_id:
        log_error(surface_label, "<no deal>", "block missing 'deal:' field")
        return None, None, "error"
    if deal_id not in registered:
        log_error(surface_label, deal_id, f"deal '{deal_id}' not in registry")
        return deal_id, None, "error"
    # Append via helper (idempotent on id collision)
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", str(HELPER_BIN), "append-log-entry", deal_id],
            input=json.dumps(entry),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            log_error(surface_label, entry["id"], f"helper exit {result.returncode}: {result.stderr}")
            return deal_id, entry["id"], "error"
        return deal_id, entry["id"], "ok"
    except Exception as e:
        log_error(surface_label, entry["id"], str(e))
        return deal_id, entry["id"], "error"


# ── Sub-commands ──────────────────────────────────────────────────────────────

def cmd_scan_claude_code(args):
    """Walk ~/.claude/projects/*/[uuid].jsonl files, find DEAL-INTEL blocks
    not yet captured (per state file), route each."""
    state = load_state()
    cc_state = state.setdefault("claude-code", {})
    routed = 0
    skipped = 0
    errors = 0
    if not TRANSCRIPTS_DIR.exists():
        print(f"No transcripts dir at {TRANSCRIPTS_DIR}")
        return
    for proj_dir in sorted(TRANSCRIPTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.rglob("*.jsonl"):
            file_key = str(jsonl.relative_to(TRANSCRIPTS_DIR))
            file_state = cc_state.setdefault(file_key, {"captured": []})
            captured = set(file_state.get("captured", []))
            try:
                # JSONL — each line is a message object. Concatenate text content.
                text_buf = []
                for line in jsonl.read_text(errors="replace").split("\n"):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    # Various shapes: {message:{content:[{text:...}]}}, {content:...}
                    msg = obj.get("message") or obj
                    content = msg.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                text_buf.append(c.get("text", ""))
                    elif isinstance(content, str):
                        text_buf.append(content)
                full = "\n".join(text_buf)
            except Exception as e:
                log_error("claude-code", file_key, f"read failed: {e}")
                continue

            for m in BLOCK_RE.finditer(full):
                body = m.group(1)
                block_hash = djb2(body)
                if block_hash in captured:
                    skipped += 1
                    continue
                try:
                    parsed = parse_block(body)
                except Exception as e:
                    log_error("claude-code", file_key, f"parse failed: {e}")
                    errors += 1
                    continue
                deal_id, entry_id, status = route_block(parsed, "claude-code")
                if status == "ok":
                    routed += 1
                    captured.add(block_hash)
                else:
                    errors += 1
            file_state["captured"] = sorted(captured)
            file_state["last_scan"] = datetime.now().isoformat()
    save_state(state)
    print(f"scan-claude-code: routed={routed}, skipped={skipped}, errors={errors}")


def cmd_parse_stdin(args):
    """Read text from stdin, route any DEAL-INTEL blocks. For testing
    and ad-hoc piping."""
    full = sys.stdin.read()
    routed = 0
    errors = 0
    for m in BLOCK_RE.finditer(full):
        body = m.group(1)
        try:
            parsed = parse_block(body)
        except Exception as e:
            log_error("stdin", "<inline>", f"parse failed: {e}")
            errors += 1
            continue
        deal_id, entry_id, status = route_block(parsed, "stdin")
        if status == "ok":
            routed += 1
            print(f"  ok: {deal_id} <- {entry_id}")
        else:
            errors += 1
            print(f"  error: {deal_id} ({entry_id})", file=sys.stderr)
    print(f"parse-stdin: routed={routed}, errors={errors}")


def _load_deal_registry_full():
    """Return list of {deal_id, name, keywords} for the extraction prompt."""
    try:
        data = json.loads(DEAL_REGISTRY.read_text())
        return [
            {"deal_id": d["deal_id"], "name": d.get("name", d["deal_id"]),
             "keywords": d.get("keywords", [])}
            for d in data.get("deals", [])
        ]
    except Exception:
        return []


def _read_local_transcript(path):
    """Read a local transcript file."""
    return Path(path).read_text(errors="replace")


def _read_drive_transcript(file_id):
    """Read a Drive file via deal_extract_helpers.py read-file."""
    result = subprocess.run(
        ["/opt/homebrew/bin/python3", str(HELPER_BIN), "read-file", file_id],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"read-file failed: {result.stderr.strip()}")
    return result.stdout


def _extract_intel_from_transcript(transcript_text, deals, explicit_date=None):
    """Call Claude Sonnet to extract per-deal intel AND workstream envelope items.

    Returns {
      call_date,
      deals: [{deal_id, title, summary, facts, counterparties, actions}],
      envelope_items: [{content_type, content, ...}]  # LP intel, new deals, actions, themes
    }
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed — pip3 install anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    deal_list = "\n".join(
        f"  - {d['deal_id']}: {d['name']}"
        + (f" (keywords: {', '.join(d['keywords'])})" if d.get('keywords') else "")
        for d in deals
    )
    date_hint = f"\nThe call date is known to be {explicit_date}." if explicit_date else \
        "\nInfer the call date from the transcript (look for timestamps, date mentions, recording headers). If not found, use today's date."

    MAX_CHARS = 120_000
    if len(transcript_text) > MAX_CHARS:
        transcript_text = transcript_text[:MAX_CHARS] + "\n\n[transcript truncated]"

    prompt = f"""You are processing a call/meeting transcript for an infrastructure investment firm (Tomac Cove Infrastructure Partners).

Extract ALL intelligence from this transcript across two layers:

LAYER 1 — Registered deal intel (for deal narrative synthesis):
REGISTERED DEALS:
{deal_list}

LAYER 2 — Workstream items (for operational dashboard):
Content types:
- my_action: concrete follow-up action Yoni or the team must take
- awaiting_external: commitment or deliverable owed by a third party
- lp_intel: intelligence about an LP, capital partner, or fundraising counterparty
- origination_idea: a new deal/asset/company not in the registered deal list above
- deal_takeaway: intel about a registered deal that doesn't need full narrative synthesis
- theme_note: a market theme or thesis observation not tied to a specific deal

{date_hint}

TRANSCRIPT:
{transcript_text}

Return a JSON object with EXACTLY this structure:
{{
  "call_date": "YYYY-MM-DD",
  "deals": [
    {{
      "deal_id": "<must match a registered deal_id exactly>",
      "title": "<one-line>",
      "summary": "<1-2 sentences of most important new info>",
      "facts": ["<specific fact with numbers/names>"],
      "counterparties": ["<name (firm)> — <new info>"],
      "actions": ["<YYYY-MM-DD>: <verb-first action> [@owner]"]
    }}
  ],
  "envelope_items": [
    {{
      "content_type": "<one of the types above>",
      "content": "<the intel, action, or idea — specific, verb-first for actions>",
      "counterparty": "<Firm — Person if relevant>",
      "parent_id": "<deal_id or lp-slug if applicable, else omit>",
      "due": "<YYYY-MM-DD if action has deadline, else omit>",
      "owner": "<Yoni|Mark|external — omit if unclear>"
    }}
  ]
}}

Rules:
- deals[]: only registered deals with MEANINGFUL new intel; omit brief mentions
- envelope_items[]: capture everything else — LP mentions, new deal ideas, action items, themes
- For actions from the call: include as my_action (owner=Yoni/Mark) or awaiting_external (owner=external)
- For new companies/assets discussed as potential deals: use origination_idea
- For LP or investor mentions with new intel: use lp_intel
- If neither layer has anything meaningful, return {{"call_date": "...", "deals": [], "envelope_items": []}}
- Return ONLY the JSON object, no other text"""

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def cmd_route_transcript(args):
    """Read a transcript, identify deals mentioned, write per-deal log.json entries."""
    # Read the transcript
    if args.drive_file_id:
        print(f"Reading Drive file {args.drive_file_id}...")
        try:
            transcript_text = _read_drive_transcript(args.drive_file_id)
            source_label = f"drive:{args.drive_file_id}"
        except Exception as e:
            print(f"ERROR reading Drive file: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.file:
        print(f"Reading {args.file}...")
        try:
            transcript_text = _read_local_transcript(args.file)
            source_label = f"file:{Path(args.file).name}"
        except Exception as e:
            print(f"ERROR reading file: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Reading transcript from stdin...")
        transcript_text = sys.stdin.read()
        source_label = "stdin"

    if not transcript_text.strip():
        print("ERROR: empty transcript", file=sys.stderr)
        sys.exit(1)

    # Dedup: skip if already processed (keyed by SHA256 of content)
    content_hash = hashlib.sha256(transcript_text.encode()).hexdigest()[:16]
    state = load_state()
    rt_state = state.setdefault("route-transcript", {})
    if content_hash in rt_state and not getattr(args, 'force', False):
        print(f"SKIP: transcript already processed (hash {content_hash}). Use --force to reprocess.")
        return

    # Load deal registry
    deals = _load_deal_registry_full()
    if not deals:
        print("ERROR: could not load deal registry", file=sys.stderr)
        sys.exit(1)

    # Extract intel via Claude
    print(f"Extracting intel for {len(deals)} registered deals...")
    try:
        result = _extract_intel_from_transcript(transcript_text, deals, args.date)
    except Exception as e:
        print(f"ERROR during extraction: {e}", file=sys.stderr)
        sys.exit(1)

    call_date = result.get("call_date", datetime.now().strftime("%Y-%m-%d"))
    deal_hits = result.get("deals", [])
    envelope_hits = result.get("envelope_items", [])
    print(f"Call date: {call_date} | Deals with intel: {len(deal_hits)} | Envelope items: {len(envelope_hits)}")

    registered = load_registered_deal_ids()
    deal_routed = 0
    deal_errors = 0
    envelope_routed = 0

    # Phase 1: route to per-deal log.json for registered deals
    for hit in deal_hits:
        deal_id = hit.get("deal_id", "").lower().strip()
        if not deal_id or deal_id not in registered:
            # Unknown deal → emit as origination_idea so it surfaces in CoS inbox
            envelope_hits.append({
                "content_type": "origination_idea",
                "content": f"[{hit.get('deal_id', 'unknown')}] {hit.get('summary') or hit.get('title', '')}",
                "counterparty": ", ".join(str(c) for c in hit.get("counterparties", []))[:120] or None,
            })
            print(f"  unregistered deal {hit.get('deal_id')!r} → origination_idea")
            continue

        block_data = {
            "deal": deal_id,
            "date": call_date,
            "title": hit.get("title", "Transcript intel"),
            "summary": hit.get("summary", ""),
            "facts": hit.get("facts", []),
            "counterparties": hit.get("counterparties", []),
            "actions": hit.get("actions", []),
        }
        _, entry_id, status = route_block(block_data, f"transcript:{source_label}")
        if status == "ok":
            print(f"  log.json ok: {deal_id} <- {entry_id[:8]} ({hit.get('title', '')[:50]})")
            deal_routed += 1
        else:
            print(f"  log.json error: {deal_id}", file=sys.stderr)
            deal_errors += 1

    # Phase 2: route envelope items (LP intel, actions, new deals, themes) → dashboard-data.json
    if envelope_hits:
        try:
            _pipeline = Path(__file__).resolve().parent.parent
            if str(_pipeline) not in sys.path:
                sys.path.insert(0, str(_pipeline))
            from _envelope_writer import append_items  # noqa: PLC0415

            stamped = []
            for item in envelope_hits:
                e = {k: v for k, v in item.items() if v is not None}
                e.setdefault("source_ref", {
                    "type": "call",
                    "title": source_label,
                    "date": call_date,
                })
                stamped.append(e)

            summary = append_items(stamped)
            envelope_routed = sum(summary.get("routed", {}).values())
            exceptions = summary.get("exceptions", 0)
            print(f"  envelope ok: {envelope_routed} routed, {exceptions} exceptions")
        except Exception as e:
            print(f"  envelope error: {e}", file=sys.stderr)

    if not deal_hits and not envelope_hits:
        print("No intel found in transcript.")

    # Mark processed
    rt_state[content_hash] = {
        "processed_at": datetime.now().isoformat(),
        "source": source_label,
        "call_date": call_date,
        "deals": [h.get("deal_id") for h in deal_hits],
        "envelope_items": envelope_routed,
    }
    save_state(state)
    print(f"\nroute-transcript: deals={deal_routed} log entries, envelope={envelope_routed} items, errors={deal_errors}, call_date={call_date}")


def cmd_scan_claude_ai(args):
    """Stub: Chrome MCP scrape of claude.ai project chats. Requires running
    inside a Claude Code session that has Chrome MCP loaded — typically
    invoked by a slash command, not the helper directly."""
    print("scan-claude-ai is meant to be run inside a Claude Code session "
          "via the /capture-deal-chats slash command (TODO). It scrapes "
          "DEAL-INTEL blocks only — never full transcripts.")
    sys.exit(2)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan-claude-code")
    sub.add_parser("parse-stdin")
    sub.add_parser("scan-claude-ai")
    rt = sub.add_parser("route-transcript",
        help="Extract per-deal intel from a call transcript and route to log.json")
    src = rt.add_mutually_exclusive_group()
    src.add_argument("file", nargs="?", help="Local transcript file path")
    src.add_argument("--drive-file-id", help="Drive file ID to read via deal_extract_helpers")
    rt.add_argument("--date", help="Override call date (YYYY-MM-DD); inferred from content if omitted")
    rt.add_argument("--force", action="store_true", help="Reprocess even if already in dedup state")
    args = p.parse_args()
    handlers = {
        "scan-claude-code": cmd_scan_claude_code,
        "parse-stdin": cmd_parse_stdin,
        "scan-claude-ai": cmd_scan_claude_ai,
        "route-transcript": cmd_route_transcript,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
