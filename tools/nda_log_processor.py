#!/usr/bin/env python3
"""
nda_log_processor.py — merge ---NDA-LESSONS--- blocks into the NDA Reviewer doc.

Reads NDA-LESSONS blocks from stdin or a transcript file, uses Claude to
intelligently merge them into the current NDA Reviewer doc in Google Drive
(revising §3/§9 where lessons conflict, appending to §7 deal log), then
writes back via Deal Sync Writer (edit-in-place, invariant EP1).

Sub-commands:
  parse-stdin       read NDA-LESSONS blocks from stdin, merge + write back
  scan-transcript   scan a .jsonl Claude Code transcript for NDA-LESSONS blocks

Usage:
  echo "<NDA-LESSONS block>" | python3 nda_log_processor.py parse-stdin
  python3 nda_log_processor.py scan-transcript <path-to-transcript.jsonl>
  python3 nda_log_processor.py parse-stdin --dry-run   # show merge plan, no write

State:
  ~/dashboards/data/nda_lessons_state.json
    { "processed_block_hashes": [...], "last_run": ISO }
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import requests
import yaml
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
COS_PIPELINE = HOME / "cos-pipeline"
STATE_PATH = HOME / "dashboards" / "data" / "nda_lessons_state.json"
NDA_REVIEWER_ID = "1Z_ohniOGLK3avordlS7tlVzmBzoorx6W6F3JDzjmlw0"

LESSONS_RE = re.compile(
    r"---NDA-LESSONS---\s*\n(.*?)\n\s*---END-NDA-LESSONS---",
    re.DOTALL | re.IGNORECASE,
)

# ── State helpers ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"processed_block_hashes": [], "last_run": None}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _block_hash(block_text: str) -> str:
    return hashlib.sha256(block_text.strip().encode()).hexdigest()[:16]


# ── Drive read ─────────────────────────────────────────────────────────────────

def _read_nda_reviewer() -> str:
    """Fetch the current NDA Reviewer doc content via Google Drive export."""
    token_path = HOME / "credentials" / "token.json"
    if not token_path.exists():
        raise FileNotFoundError(f"Drive token not found: {token_path}")

    token_data = json.loads(token_path.read_text())
    access_token = token_data.get("access_token") or token_data.get("token")

    # Try plain text export first (preserves markdown-ish structure)
    url = f"https://docs.googleapis.com/v1/documents/{NDA_REVIEWER_ID}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if r.status_code == 401:
        # Refresh token
        access_token = _refresh_drive_token(token_data, token_path)
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    r.raise_for_status()

    # Extract plain text from Docs API response
    doc = r.json()
    return _doc_to_text(doc)


def _doc_to_text(doc: dict) -> str:
    """Convert Google Docs API document object to plain text."""
    parts = []
    for elem in doc.get("body", {}).get("content", []):
        for item in elem.get("paragraph", {}).get("elements", []):
            text_run = item.get("textRun", {})
            if text_run.get("content"):
                parts.append(text_run["content"])
    return "".join(parts)


def _refresh_drive_token(token_data: dict, token_path: Path) -> str:
    """Attempt a token refresh; update token.json; return new access_token."""
    import subprocess
    result = subprocess.run(
        ["python3", str(COS_PIPELINE / "tools" / "refresh_drive_token.py")],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        refreshed = json.loads(token_path.read_text())
        return refreshed.get("access_token") or refreshed.get("token", "")
    raise RuntimeError(f"Token refresh failed: {result.stderr[:200]}")


# ── Drive write ────────────────────────────────────────────────────────────────

def _write_nda_reviewer(content: str, label: str = "NDA Reviewer") -> bool:
    """Write back to the NDA Reviewer doc via Deal Sync Writer (setContent)."""
    config_yaml = Path.home() / "cos-pipeline-config-tomac" / "config" / "deal_sync.yaml"
    if not config_yaml.exists():
        print(f"  Drive {label}: SKIP — deal_sync.yaml not found", file=sys.stderr)
        return False
    cfg = yaml.safe_load(config_yaml.read_text())
    url = cfg.get("url")
    secret = cfg.get("secret")
    if not url or not secret:
        print(f"  Drive {label}: SKIP — missing url/secret", file=sys.stderr)
        return False
    try:
        r = requests.post(
            url,
            json={"secret": secret, "fileId": NDA_REVIEWER_ID, "content": content},
            timeout=30,
            allow_redirects=True,
        )
        result = (
            r.json()
            if r.headers.get("content-type", "").startswith("application/json")
            else {"status": "unknown", "raw": r.text[:200]}
        )
        if result.get("status") == "ok":
            print(f"  Drive {label}: ✓ ({result.get('bytes')} bytes)")
            return True
        print(f"  Drive {label}: FAIL — {result.get('message', result)}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  Drive {label}: ERROR {e}", file=sys.stderr)
        return False


# ── Claude merge ───────────────────────────────────────────────────────────────

MERGE_SYSTEM = """You are updating a living NDA playbook document for PJA Properties LLC.

You will receive:
1. CURRENT_DOC — the full current text of the NDA Reviewer document
2. NDA_LESSONS — a structured block of lessons from a completed NDA review session

Your job is to produce the COMPLETE revised document text with these changes applied:

DEAL-LOG-ROW (§7): Always add a new row to the Deal Log table. Append after the last
filled row, before any blank rows. Format: | Date | Counterparty | NDA Type | Term | Fees | Key Outcomes |

FRAMEWORK-UPDATE (§3), Action=REVISE: Find the existing row whose "Issue Type" matches
the Revises field. Update only the fields that changed (What to Look For, PJA Position).
Add "(Updated YYYY-MM-DD)" to the Issue Type cell. Do not touch other rows.

FRAMEWORK-UPDATE (§3), Action=ADD: Add a new row in the correct priority tier
(RED rows before YELLOW before GREEN). Do not duplicate an existing Issue Type.

LANGUAGE-UPDATE (§9), Action=REVISE: Replace the full language block for that section.
Preserve the bold section header. Add "(Updated YYYY-MM-DD)" after the header.

LANGUAGE-UPDATE (§9), Action=ADD: Add a new section after the last existing §9 entry.
Bold header + italicized language block.

CRITICAL RULES:
- Return ONLY the complete revised document text. No preamble, no commentary, no markdown fences.
- Preserve all existing content, formatting, and sections exactly — only make targeted changes.
- If a FRAMEWORK-UPDATE or LANGUAGE-UPDATE would contradict a non-negotiable in §2, skip it.
- If a FRAMEWORK-UPDATE issue type already exists without meaningful difference, skip it.
- Preserve the document's table structure for §3, §6, §7."""


def _claude_merge(current_doc: str, lessons_block: str) -> str:
    """Use Claude to produce the merged document."""
    sys.path.insert(0, str(COS_PIPELINE))
    try:
        from _claude_dispatch import call as _claude_call
    except ImportError as e:
        raise RuntimeError(f"_claude_dispatch not importable: {e}")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"CURRENT_DOC:\n\n{current_doc}",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"NDA_LESSONS:\n\n{lessons_block}\n\nProduce the complete revised document.",
                },
            ],
        }
    ]

    result = _claude_call(
        task_type="nda-log-merge",
        model="claude-sonnet-4-6",
        system=MERGE_SYSTEM,
        messages=messages,
        max_tokens=8192,
    )

    if isinstance(result, dict):
        content = result.get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block["text"].strip()
        return str(result)
    return str(result).strip()


# ── Block extraction ───────────────────────────────────────────────────────────

def extract_blocks_from_text(text: str) -> list[str]:
    """Return list of NDA-LESSONS block bodies (text between markers)."""
    return [m.group(1).strip() for m in LESSONS_RE.finditer(text)]


def extract_blocks_from_jsonl(path: Path) -> list[str]:
    """Scan a Claude Code .jsonl transcript for NDA-LESSONS blocks."""
    blocks = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Scan all string values in the entry
                text = json.dumps(entry)
                found = extract_blocks_from_text(text)
                blocks.extend(found)
    except Exception as e:
        print(f"  scan {path.name}: error — {e}", file=sys.stderr)
    return blocks


# ── Core processing ────────────────────────────────────────────────────────────

def process_blocks(blocks: list[str], dry_run: bool = False) -> int:
    """Merge a list of NDA-LESSONS blocks into the NDA Reviewer doc. Returns count processed."""
    if not blocks:
        print("  nda_log_processor: no NDA-LESSONS blocks found")
        return 0

    state = _load_state()
    seen = set(state.get("processed_block_hashes", []))
    new_blocks = [b for b in blocks if _block_hash(b) not in seen]

    if not new_blocks:
        print(f"  nda_log_processor: {len(blocks)} block(s) already processed — skip")
        return 0

    print(f"  nda_log_processor: {len(new_blocks)} new NDA-LESSONS block(s) to merge")

    # Read current doc
    try:
        current_doc = _read_nda_reviewer()
    except Exception as e:
        print(f"  nda_log_processor: cannot read NDA Reviewer doc — {e}", file=sys.stderr)
        return 0

    # Merge all new blocks (combine into one merge pass if multiple)
    combined_lessons = "\n\n---\n\n".join(new_blocks)

    if dry_run:
        print("  [DRY RUN] Would merge the following block(s):")
        print(combined_lessons[:500] + ("..." if len(combined_lessons) > 500 else ""))
        return len(new_blocks)

    try:
        revised_doc = _claude_merge(current_doc, combined_lessons)
    except Exception as e:
        print(f"  nda_log_processor: Claude merge failed — {e}", file=sys.stderr)
        return 0

    ok = _write_nda_reviewer(revised_doc)
    if ok:
        state["processed_block_hashes"].extend([_block_hash(b) for b in new_blocks])
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        print(f"  nda_log_processor: merged {len(new_blocks)} block(s) → NDA Reviewer ✓")
        return len(new_blocks)
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def cmd_parse_stdin(dry_run: bool = False) -> None:
    text = sys.stdin.read()
    blocks = extract_blocks_from_text(text)
    if not blocks:
        print("  nda_log_processor: no ---NDA-LESSONS--- block found in stdin")
        sys.exit(0)
    processed = process_blocks(blocks, dry_run=dry_run)
    sys.exit(0 if processed >= 0 else 1)


def cmd_scan_transcript(transcript_path: str, dry_run: bool = False) -> None:
    path = Path(transcript_path).expanduser()
    if not path.exists():
        print(f"  nda_log_processor: transcript not found: {path}", file=sys.stderr)
        sys.exit(1)
    blocks = extract_blocks_from_jsonl(path)
    process_blocks(blocks, dry_run=dry_run)
    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge NDA-LESSONS blocks into NDA Reviewer doc")
    parser.add_argument("command", choices=["parse-stdin", "scan-transcript"])
    parser.add_argument("transcript", nargs="?", help="Path to .jsonl transcript (scan-transcript only)")
    parser.add_argument("--dry-run", action="store_true", help="Show merge plan without writing")
    args = parser.parse_args()

    if args.command == "parse-stdin":
        cmd_parse_stdin(dry_run=args.dry_run)
    elif args.command == "scan-transcript":
        if not args.transcript:
            print("scan-transcript requires a transcript path", file=sys.stderr)
            sys.exit(1)
        cmd_scan_transcript(args.transcript, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
