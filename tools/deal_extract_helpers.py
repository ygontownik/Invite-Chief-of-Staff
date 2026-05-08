#!/usr/bin/env python3
"""
deal_extract_helpers.py — file I/O primitives invoked by the /deal-sync
slash command.

The slash command is the workflow orchestrator and runs inside a Claude
Code session (subscription-backed). All AI work — reading new source
files, regenerating status/brief docs, regenerating the firm-context
pipeline section — happens in the LLM context window. This module does
NO API calls. It only handles Drive reads/writes, dedup state, and
filesystem moves.

Sub-commands (each prints structured output the slash command can parse):

    list-new-files <deal_id>
        Print JSON list of files in the deal's Drive folder modified
        since last_run, filtered to processable types, excluding
        _Ready/ contents and per-deal state files (status, brief,
        dashboard_entry).

    read-file <file_id>
        Print raw file contents (Drive media get + decoded text).

    read-deal-doc <deal_id> {status|brief}
        Print current status or brief doc contents.

    write-deal-doc <deal_id> {status|brief}
        Read new content from stdin, overwrite the corresponding doc
        in Drive (text/plain media update).

    move-to-ready <file_id> <deal_folder_id>
        Move file into the deal's _Ready/ subfolder (creates folder
        if absent), mirroring the cos_transcript_hook _Ready/ pattern.

    mark-processed <deal_id> <file_id> {success|failed}
        Append/update the dedup record in deal_extract_state.json.

    update-last-run <deal_id>
        Set last_run for the deal to now (called after a successful
        cycle, even if no files were processed).

    list-deals [--filter cholla,pngts]
        Print JSON list of {deal_id, status_id, brief_id, folder_id}
        for each deal in deal_docs registry.

    regenerate-pipeline-section
        Read replacement section content from stdin, replace the body
        between AUTO-GENERATED-PIPELINE-START/END markers in the
        TCIP Firm Context doc.

Usage examples:
    python3 deal_extract_helpers.py list-deals
    python3 deal_extract_helpers.py list-new-files pngts
    python3 deal_extract_helpers.py read-file 1abc...
    cat new_status.md | python3 deal_extract_helpers.py write-deal-doc pngts status
"""
import argparse
import io
import json
import os
import pickle
import sys
from datetime import datetime
from pathlib import Path

import yaml
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

# ── Paths / config ────────────────────────────────────────────────────────────
CREDS_PATH = os.path.expanduser("~/credentials/gdrive_token.pickle")
DRIVE_DOCS_YAML = os.path.expanduser("~/dashboards/config/drive-docs.yaml")
EXTRACT_STATE_PATH = os.path.expanduser("~/dashboards/data/deal_extract_state.json")
EXTRACT_LOG_DIR = os.path.expanduser("~/dashboards/logs")
ERROR_LOG = os.path.join(EXTRACT_LOG_DIR, "extract_errors.log")

# Files in a deal folder we never feed back as inputs (they ARE the deal state)
EXCLUDED_NAME_SUFFIXES = (
    "_status.md",
    "_master_brief.md",
    "_dashboard_entry.json",
    " -- Status.md",
    " -- Master Brief.md",
)

# Mime types we accept as processable
PROCESSABLE_MIMES = {
    "text/plain",
    "text/markdown",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.document",
}

PIPELINE_MARKER_START = "<!-- AUTO-GENERATED-PIPELINE-START -->"
PIPELINE_MARKER_END = "<!-- AUTO-GENERATED-PIPELINE-END -->"

# ── Services ──────────────────────────────────────────────────────────────────

_SERVICES = {}


def get_drive():
    if "drive" in _SERVICES:
        return _SERVICES["drive"]
    with open(CREDS_PATH, "rb") as f:
        creds = pickle.load(f)
    if hasattr(creds, "expired") and creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(CREDS_PATH, "wb") as f:
            pickle.dump(creds, f)
    svc = build("drive", "v3", credentials=creds)
    _SERVICES["drive"] = svc
    _SERVICES["_creds"] = creds
    return svc


def get_docs():
    if "docs" in _SERVICES:
        return _SERVICES["docs"]
    if "_creds" not in _SERVICES:
        get_drive()
    svc = build("docs", "v1", credentials=_SERVICES["_creds"])
    _SERVICES["docs"] = svc
    return svc


# ── Config loading ────────────────────────────────────────────────────────────

def load_drive_docs():
    with open(DRIVE_DOCS_YAML) as f:
        return yaml.safe_load(f)


def get_deal_entry(deal_id):
    cfg = load_drive_docs()
    deal_docs = cfg.get("deal_docs", {})
    if deal_id not in deal_docs:
        die(f"deal '{deal_id}' not found in deal_docs registry")
    return deal_docs[deal_id]


# ── State (extract dedup) ─────────────────────────────────────────────────────

def djb2(s):
    h = 5381
    for c in s:
        h = ((h << 5) + h) + ord(c)
        h &= 0xFFFFFFFF
    return f"{h:08x}"


def load_state():
    if not os.path.exists(EXTRACT_STATE_PATH):
        return {}
    with open(EXTRACT_STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(EXTRACT_STATE_PATH), exist_ok=True)
    with open(EXTRACT_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def deal_state(state, deal_id):
    if deal_id not in state:
        state[deal_id] = {"last_run": None, "processed_files": {}}
    return state[deal_id]


# ── Logging ───────────────────────────────────────────────────────────────────

def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def log_error(deal_id, file_id, err):
    os.makedirs(EXTRACT_LOG_DIR, exist_ok=True)
    ts = datetime.now().isoformat()
    with open(ERROR_LOG, "a") as f:
        f.write(f"{ts} deal={deal_id} file={file_id} error={err}\n")


# ── Drive helpers ─────────────────────────────────────────────────────────────

def drive_list_folder(folder_id):
    svc = get_drive()
    res = svc.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType,modifiedTime,parents)",
        pageSize=200,
    ).execute()
    return res.get("files", [])


def drive_get_or_create_subfolder(parent_id, name):
    svc = get_drive()
    res = svc.files().list(
        q=f"'{parent_id}' in parents and name='{name}' and "
          f"mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)",
    ).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    f = svc.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
    ).execute()
    return f["id"]


def drive_read_file_text(file_id):
    """Return file text. Handles native Docs (export to text), text/plain,
    PDF (export not supported — return marker), .docx (export to text)."""
    svc = get_drive()
    meta = svc.files().get(fileId=file_id, fields="id,name,mimeType").execute()
    mime = meta["mimeType"]

    if mime == "application/vnd.google-apps.document":
        data = svc.files().export(fileId=file_id, mimeType="text/plain").execute()
        return data.decode("utf-8") if isinstance(data, bytes) else data

    if mime == "application/pdf":
        # PDF text extraction is out of scope here; return a marker so the
        # session can decide whether to skip or use a separate tool.
        return f"[BINARY PDF — file_id={file_id}, name={meta['name']}, size unknown via media get]"

    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        # Drive can't export non-Google docs to text/plain; the session
        # should download it separately if it really wants the body.
        return f"[BINARY DOCX — file_id={file_id}, name={meta['name']}]"

    # text/plain, text/markdown, anything else readable
    request = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8", errors="replace")


def drive_overwrite_text_file(file_id, text):
    svc = get_drive()
    media = MediaInMemoryUpload(text.encode("utf-8"), mimetype="text/plain")
    svc.files().update(fileId=file_id, media_body=media).execute()


def drive_move(file_id, new_parent_id):
    svc = get_drive()
    f = svc.files().get(fileId=file_id, fields="parents").execute()
    prev = ",".join(f.get("parents", []))
    svc.files().update(
        fileId=file_id, addParents=new_parent_id, removeParents=prev, fields="id,parents"
    ).execute()


def docs_overwrite_native(doc_id, text):
    svc = get_docs()
    doc = svc.documents().get(documentId=doc_id).execute()
    end_index = doc["body"]["content"][-1]["endIndex"] - 1
    requests = []
    if end_index > 1:
        requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index}}})
    requests.append({"insertText": {"location": {"index": 1}, "text": text}})
    svc.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()


def docs_read_native(doc_id):
    svc = get_docs()
    doc = svc.documents().get(documentId=doc_id).execute()
    out = []
    for elem in doc["body"]["content"]:
        if "paragraph" in elem:
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    out.append(pe["textRun"]["content"])
    return "".join(out)


# ── Sub-command handlers ──────────────────────────────────────────────────────

def cmd_list_deals(args):
    cfg = load_drive_docs()
    deal_docs = cfg.get("deal_docs", {})
    out = []
    filt = set(args.filter.split(",")) if args.filter else None
    state = load_state()
    for deal_id, entry in deal_docs.items():
        if filt and deal_id not in filt:
            continue
        out.append({
            "deal_id": deal_id,
            "drive_folder_id": entry["drive_folder_id"],
            "status_id": entry["status"]["doc_id"],
            "brief_id": entry["master_brief"]["doc_id"],
            "last_run": deal_state(state, deal_id)["last_run"],
        })
    print(json.dumps(out, indent=2))


def cmd_list_new_files(args):
    deal_id = args.deal_id
    entry = get_deal_entry(deal_id)
    folder_id = entry["drive_folder_id"]
    excluded_ids = {entry["status"]["doc_id"], entry["master_brief"]["doc_id"]}

    state = load_state()
    ds = deal_state(state, deal_id)
    last_run = ds.get("last_run")
    processed = ds.get("processed_files", {})

    files = drive_list_folder(folder_id)
    out = []
    for f in files:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            continue  # _Ready/ subfolder etc.
        if f["id"] in excluded_ids:
            continue
        if any(f["name"].endswith(s) for s in EXCLUDED_NAME_SUFFIXES):
            continue
        if f["mimeType"] not in PROCESSABLE_MIMES:
            continue
        # Already processed?
        h = djb2(f["id"])
        if h in processed and processed[h].get("outcome") == "success":
            continue
        # If last_run set, only files modified since
        if last_run and f["modifiedTime"] <= last_run:
            continue
        out.append({
            "file_id": f["id"],
            "name": f["name"],
            "mime": f["mimeType"],
            "modified": f["modifiedTime"],
        })
    print(json.dumps(out, indent=2))


def cmd_read_file(args):
    print(drive_read_file_text(args.file_id))


def cmd_read_deal_doc(args):
    entry = get_deal_entry(args.deal_id)
    field = "status" if args.kind == "status" else "master_brief"
    doc_id = entry[field]["doc_id"]
    # Native Doc vs text/plain — try text/plain media get first
    svc = get_drive()
    meta = svc.files().get(fileId=doc_id, fields="mimeType").execute()
    if meta["mimeType"] == "application/vnd.google-apps.document":
        print(docs_read_native(doc_id))
    else:
        print(drive_read_file_text(doc_id))


def cmd_write_deal_doc(args):
    entry = get_deal_entry(args.deal_id)
    field = "status" if args.kind == "status" else "master_brief"
    doc_id = entry[field]["doc_id"]
    text = sys.stdin.read()
    if not text.strip():
        die("empty stdin — refusing to write empty doc")
    svc = get_drive()
    meta = svc.files().get(fileId=doc_id, fields="mimeType").execute()
    if args.dry_run:
        print(f"[DRY-RUN] would overwrite {field} doc ({doc_id}) for {args.deal_id} with {len(text)} chars")
        return
    if meta["mimeType"] == "application/vnd.google-apps.document":
        docs_overwrite_native(doc_id, text)
    else:
        drive_overwrite_text_file(doc_id, text)
    print(f"OK wrote {field} for {args.deal_id} ({len(text)} chars)")


def cmd_move_to_ready(args):
    if args.dry_run:
        print(f"[DRY-RUN] would move {args.file_id} into _Ready/ of {args.deal_folder_id}")
        return
    ready_id = drive_get_or_create_subfolder(args.deal_folder_id, "_Ready")
    drive_move(args.file_id, ready_id)
    print(f"OK moved {args.file_id} -> _Ready/ ({ready_id})")


def cmd_mark_processed(args):
    state = load_state()
    ds = deal_state(state, args.deal_id)
    h = djb2(args.file_id)
    ds["processed_files"][h] = {
        "file_id": args.file_id,
        "processed_at": datetime.now().isoformat(),
        "outcome": args.outcome,
    }
    save_state(state)
    print(f"OK marked {args.file_id} {args.outcome}")


def cmd_update_last_run(args):
    state = load_state()
    ds = deal_state(state, args.deal_id)
    ds["last_run"] = datetime.now().isoformat() + "Z"
    save_state(state)
    print(f"OK last_run for {args.deal_id} = {ds['last_run']}")


def cmd_regenerate_pipeline_section(args):
    cfg = load_drive_docs()
    firm_id = cfg["reference_docs"]["firm_context"]["doc_id"]
    new_body = sys.stdin.read()
    if not new_body.strip():
        die("empty stdin — refusing to wipe pipeline section")
    if args.dry_run:
        print(f"[DRY-RUN] would replace pipeline section in firm_context ({firm_id}) with {len(new_body)} chars")
        return
    current = docs_read_native(firm_id)
    start = current.find(PIPELINE_MARKER_START)
    end = current.find(PIPELINE_MARKER_END)
    if start == -1 or end == -1 or end <= start:
        die("pipeline markers not found in firm_context doc")
    end += len(PIPELINE_MARKER_END)
    replaced = (
        current[:start]
        + PIPELINE_MARKER_START
        + "\n"
        + new_body.strip()
        + "\n"
        + PIPELINE_MARKER_END
        + current[end:]
    )
    docs_overwrite_native(firm_id, replaced)
    print(f"OK replaced pipeline section ({len(new_body)} chars)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--dry-run", action="store_true", help="No Drive writes; print intended action")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list-deals"); s.add_argument("--filter")
    s = sub.add_parser("list-new-files"); s.add_argument("deal_id")
    s = sub.add_parser("read-file"); s.add_argument("file_id")
    s = sub.add_parser("read-deal-doc"); s.add_argument("deal_id"); s.add_argument("kind", choices=["status", "brief"])
    s = sub.add_parser("write-deal-doc"); s.add_argument("deal_id"); s.add_argument("kind", choices=["status", "brief"])
    s = sub.add_parser("move-to-ready"); s.add_argument("file_id"); s.add_argument("deal_folder_id")
    s = sub.add_parser("mark-processed"); s.add_argument("deal_id"); s.add_argument("file_id"); s.add_argument("outcome", choices=["success", "failed"])
    s = sub.add_parser("update-last-run"); s.add_argument("deal_id")
    s = sub.add_parser("regenerate-pipeline-section")
    return p


HANDLERS = {
    "list-deals": cmd_list_deals,
    "list-new-files": cmd_list_new_files,
    "read-file": cmd_read_file,
    "read-deal-doc": cmd_read_deal_doc,
    "write-deal-doc": cmd_write_deal_doc,
    "move-to-ready": cmd_move_to_ready,
    "mark-processed": cmd_mark_processed,
    "update-last-run": cmd_update_last_run,
    "regenerate-pipeline-section": cmd_regenerate_pipeline_section,
}


def main():
    args = build_parser().parse_args()
    HANDLERS[args.cmd](args)


if __name__ == "__main__":
    main()
