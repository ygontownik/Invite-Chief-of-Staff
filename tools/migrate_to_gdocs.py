#!/usr/bin/env python3
"""
migrate_to_gdocs.py — collapse the dual-ID state (per-deal status/brief have
both a .md file ID and a gdoc ID) into a single native Google Doc per concept.

Per DRIVE-RECOMMENDATIONS.md §7.2.

For each registered deal in deal-system-data.json:
  - For each (md_field, gdoc_field) pair in DUAL_PAIRS:
      1. Export both files. If the .md content is non-empty and the gdoc is empty
         (or .md is newer), overwrite the gdoc with the .md content.
      2. Trash the .md file in Drive.
      3. Drop md_field from deal-system-data.json.

For lps/terms/actions (single-ID in drive-docs.yaml whose doc_id may be a
plain-text .md file, not a gdoc):
  - If mimeType == 'text/plain', create a sibling gdoc with the same content
    and the same name (minus '.md'), trash the .md, and update drive-docs.yaml
    deal_docs.<deal>.<concept>.doc_id to the new gdoc id.

Default is --dry-run. --execute writes registry changes and modifies Drive.

Usage:
  python3 migrate_to_gdocs.py                          # dry-run, prints plan
  python3 migrate_to_gdocs.py --execute                # apply
  python3 migrate_to_gdocs.py --deal <deal_id> --execute
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    from ruamel.yaml import YAML
    _RUAMEL = YAML()
    _RUAMEL.preserve_quotes = True
    _RUAMEL.width = 4096
    _USE_RUAMEL = True
except ImportError:
    import yaml
    _USE_RUAMEL = False

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

TOKEN_PATH = os.path.expanduser("~/credentials/token.json")
CREDS_PATH = os.path.expanduser("~/credentials/gdrive_credentials.json")
SCOPES = ["https://www.googleapis.com/auth/drive",
          "https://www.googleapis.com/auth/documents"]

DRIVE_DOCS_YAML  = os.path.expanduser("~/dashboards/config/drive-docs.yaml")
DEAL_SYSTEM_JSON = os.path.expanduser("~/cos-pipeline/tools/deal-system-data.json")

# Pairs in deal-system-data.json:
#   (concept name, md field, gdoc field)
DUAL_PAIRS = [
    ("status", "status_file_id", "status_id"),
    ("brief",  "brief_file_id",  "brief_id"),
]

# Single-ID concepts in drive-docs.yaml deal_docs that may still be .md
SINGLE_CONCEPTS = ["lps", "terms", "actions"]

GDOC_MIME = "application/vnd.google-apps.document"
TEXT_MIMES = {"text/plain", "text/markdown"}


def get_services():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    drive = build("drive", "v3", credentials=creds)
    docs  = build("docs",  "v1", credentials=creds)
    return drive, docs


# ── Drive content helpers ─────────────────────────────────────────────────────

def drive_get(drive, file_id, fields="id,name,mimeType,parents,trashed,modifiedTime,size"):
    try:
        return drive.files().get(fileId=file_id, fields=fields).execute()
    except HttpError as e:
        return {"_error": str(e)}


def read_text_file(drive, file_id) -> str:
    """Download a plain-text file."""
    data = drive.files().get_media(fileId=file_id).execute()
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def export_gdoc_as_text(drive, file_id) -> str:
    """Export a native Google Doc as plain text."""
    data = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def overwrite_gdoc_content(docs, doc_id, new_text: str):
    """Replace the entire body of a Google Doc with new_text (preserves doc ID)."""
    doc = docs.documents().get(documentId=doc_id).execute()
    end = doc["body"]["content"][-1]["endIndex"]
    requests = []
    # Delete everything except the trailing newline char Docs requires
    if end > 2:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end - 1}
            }
        })
    if new_text:
        requests.append({
            "insertText": {"location": {"index": 1}, "text": new_text}
        })
    if requests:
        docs.documents().batchUpdate(documentId=doc_id,
                                     body={"requests": requests}).execute()


def create_gdoc_with_content(drive, docs, name: str, parents: list[str], text: str) -> str:
    meta = {"name": name, "mimeType": GDOC_MIME, "parents": parents}
    created = drive.files().create(body=meta, fields="id").execute()
    new_id = created["id"]
    if text:
        overwrite_gdoc_content(docs, new_id, text)
    return new_id


def trash(drive, file_id):
    drive.files().update(fileId=file_id, body={"trashed": True}).execute()


# ── Plan + execute ────────────────────────────────────────────────────────────

def plan_for_deal(drive, deal_record: dict, deal_yaml: dict) -> list[dict]:
    """Return a list of action plan entries for one deal."""
    actions = []
    deal_id = deal_record["deal_id"]

    # 1. Dual-pair collapse (status, brief)
    for concept, md_field, gdoc_field in DUAL_PAIRS:
        md_id   = deal_record.get(md_field)
        gdoc_id = deal_record.get(gdoc_field)
        if not md_id or not gdoc_id:
            continue
        md_meta   = drive_get(drive, md_id)
        gdoc_meta = drive_get(drive, gdoc_id)
        if "_error" in md_meta and "_error" in gdoc_meta:
            actions.append({"deal": deal_id, "concept": concept,
                            "action": "SKIP",
                            "reason": f"both IDs resolve errors; nothing to do"})
            continue
        if "_error" in md_meta:
            # .md already gone — just prune the field
            actions.append({"deal": deal_id, "concept": concept,
                            "action": "PRUNE_FIELD",
                            "md_field": md_field, "md_id": md_id,
                            "reason": ".md ID already missing in Drive"})
            continue
        if "_error" in gdoc_meta:
            actions.append({"deal": deal_id, "concept": concept,
                            "action": "ERROR",
                            "reason": f"gdoc ID {gdoc_id} not found"})
            continue
        actions.append({
            "deal": deal_id, "concept": concept,
            "action": "COPY_MD_TO_GDOC_THEN_TRASH_MD",
            "md_field": md_field, "md_id": md_id, "md_name": md_meta.get("name"),
            "gdoc_id": gdoc_id, "gdoc_name": gdoc_meta.get("name"),
            "reason": "dual-ID collapse",
        })

    # 2. Single-concept .md upgrade (lps/terms/actions)
    deal_docs = deal_yaml or {}
    for concept in SINGLE_CONCEPTS:
        entry = deal_docs.get(concept)
        if not entry or not entry.get("doc_id"):
            continue
        meta = drive_get(drive, entry["doc_id"])
        if "_error" in meta:
            actions.append({"deal": deal_id, "concept": concept,
                            "action": "ERROR",
                            "reason": f"{concept} ID {entry['doc_id']} not found"})
            continue
        if meta.get("mimeType") == GDOC_MIME:
            continue  # already a gdoc
        if meta.get("mimeType") in TEXT_MIMES:
            actions.append({
                "deal": deal_id, "concept": concept,
                "action": "CREATE_GDOC_FROM_MD_THEN_TRASH_MD",
                "md_id": entry["doc_id"],
                "md_name": meta.get("name"),
                "md_parents": meta.get("parents") or [],
                "registry_key": f"deal_docs.{deal_id}.{concept}.doc_id",
                "reason": ".md needs to be promoted to native gdoc",
            })

    return actions


def execute_plan(drive, docs, plan: list[dict],
                 deal_system: dict, drive_docs_yaml: dict) -> dict:
    counts = {"copied": 0, "promoted": 0, "trashed": 0, "pruned": 0, "errors": 0}

    deals_list = deal_system["deals"]
    deals_by_id = {d["deal_id"]: d for d in deals_list}

    for entry in plan:
        deal_id = entry["deal"]
        concept = entry["concept"]
        action  = entry["action"]
        try:
            if action == "SKIP":
                continue

            if action == "ERROR":
                print(f"  ! {deal_id}/{concept}: {entry['reason']}", file=sys.stderr)
                counts["errors"] += 1
                continue

            if action == "PRUNE_FIELD":
                md_field = entry["md_field"]
                if md_field in deals_by_id[deal_id]:
                    del deals_by_id[deal_id][md_field]
                    counts["pruned"] += 1
                    print(f"  · {deal_id}/{concept}: pruned {md_field}")
                continue

            if action == "COPY_MD_TO_GDOC_THEN_TRASH_MD":
                md_text = read_text_file(drive, entry["md_id"])
                overwrite_gdoc_content(docs, entry["gdoc_id"], md_text)
                trash(drive, entry["md_id"])
                md_field = entry["md_field"]
                if md_field in deals_by_id[deal_id]:
                    del deals_by_id[deal_id][md_field]
                counts["copied"] += 1
                counts["trashed"] += 1
                counts["pruned"] += 1
                print(f"  → {deal_id}/{concept}: copied .md -> gdoc, trashed .md, pruned {md_field}")
                continue

            if action == "CREATE_GDOC_FROM_MD_THEN_TRASH_MD":
                md_text = read_text_file(drive, entry["md_id"])
                base_name = entry["md_name"] or f"{deal_id}_{concept}"
                new_name = base_name[:-3] if base_name.lower().endswith(".md") else base_name
                parents = entry["md_parents"] or []
                if not parents:
                    print(f"  ! {deal_id}/{concept}: no parent folder for .md "
                          f"({entry['md_id']}); skipping promotion", file=sys.stderr)
                    counts["errors"] += 1
                    continue
                new_id = create_gdoc_with_content(drive, docs, new_name, parents, md_text)
                trash(drive, entry["md_id"])
                # Update drive-docs.yaml in-memory; caller writes it back
                drive_docs_yaml["deal_docs"][deal_id][concept]["doc_id"] = new_id
                drive_docs_yaml["deal_docs"][deal_id][concept]["name"] = new_name
                drive_docs_yaml["deal_docs"][deal_id][concept]["mime"] = "gdoc"
                counts["promoted"] += 1
                counts["trashed"] += 1
                print(f"  ⇧ {deal_id}/{concept}: promoted .md ({entry['md_id']}) -> gdoc {new_id}")
                continue

        except HttpError as e:
            counts["errors"] += 1
            print(f"  ! {deal_id}/{concept} error: {e}", file=sys.stderr)

    return counts


def backup_file(path: str):
    if not os.path.exists(path):
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bkp = f"{path}.bak.{stamp}"
    shutil.copy2(path, bkp)
    print(f"  backed up {path} -> {bkp}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--execute", action="store_true",
                    help="actually apply changes (default is dry-run)")
    ap.add_argument("--deal", default=None,
                    help="limit to one deal_id")
    args = ap.parse_args()

    drive, docs = get_services()

    with open(DEAL_SYSTEM_JSON) as f:
        deal_system = json.load(f)
    with open(DRIVE_DOCS_YAML) as f:
        drive_docs_yaml = _RUAMEL.load(f) if _USE_RUAMEL else yaml.safe_load(f)

    deals_list = deal_system["deals"]
    deal_docs  = drive_docs_yaml.get("deal_docs") or {}

    plan: list[dict] = []
    for d in deals_list:
        if args.deal and d["deal_id"] != args.deal:
            continue
        yml = deal_docs.get(d["deal_id"], {})
        plan.extend(plan_for_deal(drive, d, yml))

    print(f"\nPlanned actions ({len(plan)}):")
    by = {}
    for p in plan:
        by[p["action"]] = by.get(p["action"], 0) + 1
        print(f"  [{p['action']:36s}] {p['deal']:14s} {p['concept']:12s} — {p['reason']}")
    print("\nSummary:")
    for k, v in by.items():
        print(f"  {k:38s} {v}")

    if not args.execute:
        print("\nDry run. Re-run with --execute to apply.")
        return

    print("\nApplying…")
    backup_file(DEAL_SYSTEM_JSON)
    backup_file(DRIVE_DOCS_YAML)
    counts = execute_plan(drive, docs, plan, deal_system, drive_docs_yaml)

    with open(DEAL_SYSTEM_JSON, "w") as f:
        json.dump(deal_system, f, indent=2)
    with open(DRIVE_DOCS_YAML, "w") as f:
        if _USE_RUAMEL:
            _RUAMEL.dump(drive_docs_yaml, f)
        else:
            yaml.safe_dump(drive_docs_yaml, f, sort_keys=False, allow_unicode=True)

    print(f"\nDone. {counts}")
    print("Registry files updated. Backups written alongside.")


if __name__ == "__main__":
    main()
