#!/usr/bin/env python3
"""
cos_batch_write.py — single-pass writer for the COS pipeline.

Accepts a JSON spec from stdin and applies ALL document changes in the
minimum number of API calls. Claude produces the spec; this script handles
all index arithmetic, offset tracking, and API calls.

Replaces 4–6 sequential Bash API calls with one invocation (~2s total).

SPEC FORMAT (Claude writes this JSON):
{
  "followups": {
    "last_table_end": N,       // from cos_prefetch_drive.py output
    "end_index": N,            // from cos_prefetch_drive.py output
    "delete_rows": [           // rows to fully remove (resolved/invalid)
      {"si": N, "ei": N, "reason": "optional note"}
    ],
    "update_rows": [           // rows to replace with new content
      {"si": N, "ei": N, "new_text": "| 9 | Who | What | Due | WS | Src | Link |\n"}
    ],
    "append_rows": [           // new rows to insert after last table row
      "| 27 | Who | What | Due | WS | Src | Link |\n"
    ],
    "append_log": "text"       // processing log to append at doc end
  },
  "briefing_log": {
    "end_index": N,            // from a prior read, or omit to auto-fetch
    "append": "## Capture Summary — YYYY-MM-DD\n..."
  },
  "run_state": {
    "followupsAdded": N,
    "followupsResolved": N,
    "emailDrafts": N,
    "transcripts": N,
    "durationSec": N,
    "newDrafts": [],           // list of draft dicts for emailQueue
    "transcriptsProcessed": [] // list of {title, date, category, followupsAdded, driveFileId}
  }
}

Usage:
    echo '{...spec...}' | python3 ~/tomac-cove-pipeline/cos_batch_write.py
    python3 ~/tomac-cove-pipeline/cos_batch_write.py --spec-file /tmp/cos_spec.json
"""
import argparse
import datetime
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

TOKEN_PATH      = Path.home() / "credentials/token.json"
FOLLOWUPS_DOC   = "10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY"
BRIEFING_DOC    = "14wE3L6ZRsjhhx2psRKbaHS5i0kgEoteWYZusqETiAZ0"
RUN_STATE_PATH  = Path.home() / "docs/cos-run-state.json"
DASHBOARD_URL   = "http://localhost:7777/warmup"


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token():
    with open(TOKEN_PATH) as f:
        return json.load(f)["token"]


# ── Docs API ──────────────────────────────────────────────────────────────────

def docs_batch_update(token, doc_id, requests):
    if not requests:
        return
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate"
    data = json.dumps({"requests": requests}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"[write] Docs API error {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        raise


def docs_get_end_index(token, doc_id):
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        doc = json.loads(r.read())
    content = doc.get("body", {}).get("content", [])
    return content[-1].get("endIndex", 1) if content else 1


# ── Core write logic ──────────────────────────────────────────────────────────

def apply_followups(token, spec):
    """
    Single batchUpdate for all follow-up doc changes:
      1. delete_rows  — remove resolved/invalid rows entirely
      2. update_rows  — delete row + insert replacement text
      3. append_rows  — insert new rows after last table row
      4. append_log   — append processing log at doc end

    All index arithmetic is handled here. Claude just provides
    original si/ei values from the prefetch output.
    """
    fu = spec.get("followups", {})
    delete_rows  = fu.get("delete_rows",  [])
    update_rows  = fu.get("update_rows",  [])
    append_rows  = fu.get("append_rows",  [])
    append_log   = fu.get("append_log",   "")
    last_table_end_orig = fu.get("last_table_end", 0)
    end_index_orig      = fu.get("end_index", 0)

    # Build unified ops list: (original_si, original_ei, replacement_or_None)
    # Sorted highest→lowest so lower-index positions are not shifted by earlier ops.
    ops = []
    for d in delete_rows:
        ops.append((d["si"], d["ei"], None))
    for u in update_rows:
        ops.append((u["si"], u["ei"], u["new_text"]))
    ops.sort(key=lambda x: x[0], reverse=True)

    # Build request list — each op uses its ORIGINAL indices because we
    # process highest-first (no prior op has shifted a lower-index position).
    requests = []
    for si, ei, replacement in ops:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": si, "endIndex": ei, "segmentId": ""}
            }
        })
        if replacement:
            requests.append({
                "insertText": {
                    "location": {"index": si, "segmentId": ""},
                    "text": replacement,
                }
            })

    # Compute net offset from all ops to find correct append positions.
    # Any op with si < last_table_end shifts last_table_end by (len(replacement) - (ei-si)).
    net_for_table = 0
    net_for_end   = 0
    for si, ei, replacement in ops:
        delta = len(replacement or "") - (ei - si)
        net_for_end += delta
        if si < last_table_end_orig:
            net_for_table += delta

    new_last_table_end = last_table_end_orig + net_for_table
    new_end_index      = end_index_orig      + net_for_end

    # Append new rows immediately after the (now-shifted) last table row.
    if append_rows:
        combined = "".join(append_rows)
        requests.append({
            "insertText": {
                "location": {"index": new_last_table_end, "segmentId": ""},
                "text": combined,
            }
        })
        net_for_end   += len(combined)
        new_end_index += len(combined)

    # Append processing log at doc end.
    if append_log:
        requests.append({
            "insertText": {
                "location": {"index": new_end_index - 1, "segmentId": ""},
                "text": append_log,
            }
        })

    if requests:
        docs_batch_update(token, FOLLOWUPS_DOC, requests)
        n_del = len(delete_rows)
        n_upd = len(update_rows)
        n_app = len(append_rows)
        print(f"[write] Follow-ups: {n_del} deleted, {n_upd} updated, {n_app} appended "
              f"({len(requests)} API requests in 1 batchUpdate)")
    else:
        print("[write] Follow-ups: no changes.")


def apply_briefing_log(token, spec):
    bl = spec.get("briefing_log", {})
    text = bl.get("append", "")
    if not text:
        return

    end_index = bl.get("end_index")
    if not end_index:
        end_index = docs_get_end_index(token, BRIEFING_DOC)

    docs_batch_update(token, BRIEFING_DOC, [{
        "insertText": {
            "location": {"index": end_index - 1, "segmentId": ""},
            "text": text,
        }
    }])
    print("[write] Briefing log: appended.")


def apply_run_state(spec):
    rs = spec.get("run_state", {})
    if not rs:
        return

    try:
        state = json.loads(RUN_STATE_PATH.read_text())
    except Exception:
        state = {"emailQueue": [], "processedTranscripts": [], "runHistory": []}

    entry = {
        "at":               datetime.datetime.now().isoformat(),
        "type":             "full",
        "transcripts":      rs.get("transcripts", 0),
        "followupsAdded":   rs.get("followupsAdded", 0),
        "followupsResolved": rs.get("followupsResolved", 0),
        "emailDrafts":      rs.get("emailDrafts", 0),
        "durationSec":      rs.get("durationSec", 0),
    }
    state["runHistory"]   = (state.get("runHistory", []) + [entry])[-14:]
    state["lastFullRunAt"] = entry["at"]

    # Merge new drafts into emailQueue (skip duplicates by subject)
    existing_subjects = {e["subject"] for e in state.get("emailQueue", []) if e.get("status") == "DRAFT_READY"}
    for draft in rs.get("newDrafts", []):
        if draft.get("subject") not in existing_subjects:
            state.setdefault("emailQueue", []).append(draft)

    state["processedTranscripts"] = (
        state.get("processedTranscripts", []) + rs.get("transcriptsProcessed", [])
    )[-30:]

    RUN_STATE_PATH.parent.mkdir(exist_ok=True)
    RUN_STATE_PATH.write_text(json.dumps(state, indent=2))
    print("[write] Run state: updated.")


def warmup():
    try:
        urllib.request.urlopen(
            urllib.request.Request(DASHBOARD_URL, method="POST"), timeout=3
        )
        print("[write] Dashboard warmup: triggered.")
    except Exception:
        print("[write] Dashboard warmup: server not running (skipped).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-file", help="Path to JSON spec file (default: read from stdin)")
    parser.add_argument("--no-warmup", action="store_true", help="Skip dashboard warmup")
    args = parser.parse_args()

    if args.spec_file:
        spec = json.loads(Path(args.spec_file).read_text())
    else:
        spec = json.load(sys.stdin)

    token = get_token()

    apply_followups(token, spec)
    apply_briefing_log(token, spec)
    apply_run_state(spec)

    if not args.no_warmup:
        warmup()

    print("[write] Done.")


if __name__ == "__main__":
    main()
