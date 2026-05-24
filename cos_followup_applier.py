#!/usr/bin/env python3
"""cos_followup_applier.py — Apply staged follow-up proposals to Drive Follow-ups doc.

Reads ~/dashboards/data/staging/proposed-followups.jsonl. For each row whose
confidence ≥ AUTOAPPLY_THRESHOLD (default 0.95; tunable), appends a markdown
table row to the Drive Follow-ups doc via the Docs API. Idempotent — checks
the doc for an existing row matching (who, what[:40]) before writing.

Rows below threshold remain staged for human review (via Phase I gap section
once shipped, or `--list` here).

Usage:
  python3 cos_followup_applier.py                  # apply all eligible
  python3 cos_followup_applier.py --threshold 0.85 # lower bar
  python3 cos_followup_applier.py --list           # show eligible, no writes
  python3 cos_followup_applier.py --dry-run        # build payload, no Drive write

Phase J.B — see ~/dashboards/docs/DESIGN-phase-J-artifact-ingest.md
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import _firm_context as _fc  # noqa: E402

_STAGING = Path.home() / "dashboards" / "data" / "staging" / "proposed-followups.jsonl"
_APPLIED = Path.home() / "dashboards" / "data" / "staging" / "applied-followups.jsonl"
_GDRIVE_PICKLE = Path.home() / "credentials" / "gdrive_token.pickle"

_DEFAULT_THRESHOLD = 0.95
_ANCHOR_ROW_DEFAULT = 88  # last row in the canonical "Open Follow-ups" table
                          # (insertion point ≡ end-of-row-88, matching the
                          # manual Dror catch-up from 2026-05-21)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _load_followups_doc_id() -> str:
    docs = _fc.load_drive_docs()
    return docs.get("followups", "10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY")


def _gdrive_docs_client():
    from googleapiclient.discovery import build  # noqa: PLC0415
    cred = pickle.load(open(_GDRIVE_PICKLE, "rb"))
    return build("docs", "v1", credentials=cred)


def _read_doc_body(docs_client, doc_id: str) -> tuple[str, dict]:
    """Return (body_text, raw_doc) for the Follow-ups doc."""
    doc = docs_client.documents().get(documentId=doc_id).execute()
    chunks = []
    for el in doc.get("body", {}).get("content", []):
        p = el.get("paragraph") or {}
        for r in p.get("elements", []) or []:
            tr = r.get("textRun") or {}
            if tr.get("content"):
                chunks.append(tr["content"])
    return "".join(chunks), doc


def _highest_row_number(body: str) -> int:
    nums = [int(m.group(1)) for m in re.finditer(r"^\| (\d+) \| ", body, re.M)]
    return max(nums) if nums else _ANCHOR_ROW_DEFAULT


def _find_insertion_index(doc: dict, anchor_row: int) -> int | None:
    """Return the Docs API endIndex of the paragraph containing `| {anchor_row} |`."""
    pat = re.compile(rf"^\|\s*{anchor_row}\s*\|")
    for el in doc.get("body", {}).get("content", []):
        p = el.get("paragraph") or {}
        text = "".join((r.get("textRun") or {}).get("content", "") for r in p.get("elements", []))
        if pat.match(text):
            return el["endIndex"]
    return None


def _existing_row_match(body: str, who: str, what: str) -> bool:
    """Idempotency check: does a row with matching who + what[:40] already exist?"""
    who_token = (who or "").strip().split("/")[0].strip()[:30]
    what_prefix = (what or "").strip()[:40]
    if not who_token or not what_prefix:
        return False
    pat = (re.escape(who_token), re.escape(what_prefix))
    rx = re.compile(rf"^\|\s*\d+\s*\|\s*[^|]*{pat[0]}[^|]*\|\s*{pat[1]}", re.M)
    return bool(rx.search(body))


def _format_row(row_num: int, fu: dict) -> str:
    """Build the markdown table line."""
    def cell(v): return (v or "").replace("|", "/").replace("\n", " ").strip()
    who = cell(fu.get("who"))
    what = cell(fu.get("what"))
    due = cell(fu.get("due") or "TBD")
    ws = cell(fu.get("workstream"))
    src = cell(fu.get("source"))
    linked = cell(fu.get("linked_to"))
    return f"| {row_num} | {who} | {what} | {due} | {ws} | {src} | {linked} |"


def _read_staging() -> list[dict]:
    if not _STAGING.exists():
        return []
    out = []
    for line in _STAGING.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _record_applied(rows: list[dict]) -> None:
    if not rows:
        return
    _APPLIED.parent.mkdir(parents=True, exist_ok=True)
    with _APPLIED.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _truncate_staging(keep: list[dict]) -> None:
    """Rewrite the staging file with `keep` rows only (drops the applied ones)."""
    _STAGING.parent.mkdir(parents=True, exist_ok=True)
    with _STAGING.open("w") as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=_DEFAULT_THRESHOLD,
                    help=f"Auto-apply confidence threshold (default {_DEFAULT_THRESHOLD})")
    ap.add_argument("--list", action="store_true",
                    help="Show eligible rows; no writes")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build payload + print, no Drive write")
    args = ap.parse_args()

    print(f"=== cos_followup_applier ({datetime.now(timezone.utc).isoformat(timespec='seconds')}) ===")
    print(f"  threshold        : {args.threshold}")
    print(f"  staging file     : {_STAGING}")

    proposed = _read_staging()
    print(f"  staged proposals : {len(proposed)}")
    if not proposed:
        return 0

    eligible = [p for p in proposed if float(p.get("confidence", 0.0)) >= args.threshold]
    holdback = [p for p in proposed if float(p.get("confidence", 0.0)) < args.threshold]
    print(f"  eligible (>={args.threshold}) : {len(eligible)}")
    print(f"  held for review  : {len(holdback)}")

    if args.list:
        for p in eligible:
            print(f"\n  [{p.get('confidence', 0):.2f}] {p.get('who','')} :: {p.get('what','')[:80]}")
            print(f"        due={p.get('due')} workstream={p.get('workstream')}")
            print(f"        source={p.get('source')}")
        return 0

    # Open Drive
    doc_id = _load_followups_doc_id()
    docs = _gdrive_docs_client()
    body, doc = _read_doc_body(docs, doc_id)
    next_row = _highest_row_number(body)
    anchor = _ANCHOR_ROW_DEFAULT  # insertion point stable at end of row 88
    target_end = _find_insertion_index(doc, anchor)
    if target_end is None:
        print(f"ERROR: anchor row {anchor} not found in Follow-ups doc", file=sys.stderr)
        return 2

    applied, dropped_dup, kept_for_review = [], [], list(holdback)
    rows_to_insert = []
    for p in eligible:
        if _existing_row_match(body, p.get("who", ""), p.get("what", "")):
            dropped_dup.append(p)
            continue
        next_row += 1
        rows_to_insert.append(_format_row(next_row, p))
        p["applied_row"] = next_row
        p["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        applied.append(p)

    if not rows_to_insert:
        print(f"\n  nothing to insert (eligible:{len(eligible)} dup:{len(dropped_dup)})")
        if dropped_dup:
            # Still drop dups from staging so they don't loop
            _truncate_staging(kept_for_review)
        return 0

    insert_block = "\n".join(rows_to_insert) + "\n"
    print(f"\n  inserting {len(rows_to_insert)} row(s) at endIndex {target_end}")
    for line in rows_to_insert:
        print(f"    {line[:140]}")

    if args.dry_run:
        print("  [dry-run — no Drive write]")
        return 0

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {
            "location": {"index": target_end}, "text": insert_block,
        }}]},
    ).execute()

    _record_applied(applied)
    _truncate_staging(kept_for_review)

    # Trigger dashboard warmup
    try:
        req = urllib.request.Request("http://localhost:7777/warmup", method="POST", data=b"")
        urllib.request.urlopen(req, timeout=5)
        print("  dashboard warmup : triggered")
    except Exception:
        pass

    print(f"\n  applied          : {len(applied)}")
    print(f"  dup-skipped      : {len(dropped_dup)}")
    print(f"  held for review  : {len(kept_for_review)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
