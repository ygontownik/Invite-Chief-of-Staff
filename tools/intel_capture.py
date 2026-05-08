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
    args = p.parse_args()
    handlers = {
        "scan-claude-code": cmd_scan_claude_code,
        "parse-stdin": cmd_parse_stdin,
        "scan-claude-ai": cmd_scan_claude_ai,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
