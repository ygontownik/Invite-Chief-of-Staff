#!/usr/bin/env python3
"""
cleanup_my_drive_root.py — manifest-driven cleanup of loose files at My Drive root.

Discovers every non-folder file at My Drive root, classifies each by filename
pattern against the rules in ~/dashboards/docs/DRIVE-ARCHITECTURE.md §8
("Mistakes-as-rules"), and either writes a JSON manifest for review (--dry-run,
default) or executes the moves (--execute).

Classification rules (in order of precedence):
  1. Registered Drive ID match (drive-docs.yaml) -> SKIP (file is registered, leave alone)
  2. <deal>_status.md gdoc                       -> TRASH (historical orphans;
                                                    real status lives at the
                                                    registered status doc_id)
  3. *.gscript                                   -> 00 Tomac Cove/_Context/
  4. Filename matches a deal's organizer_aliases -> that deal's _Outputs/
       a. If filename contains a version tag    -> _Outputs/Drafts/ (after
                                                    folder ensured)
  5. "Presentation Standards" / "Practice Patterns" -> 00 Tomac Cove/_Context/
  6. Otherwise                                   -> UNROUTED (surface for review)

Usage:
  python3 cleanup_my_drive_root.py                  # dry-run, write manifest
  python3 cleanup_my_drive_root.py --execute        # apply moves
  python3 cleanup_my_drive_root.py --manifest path  # use a hand-edited manifest

Manifest format (JSON):
  [
    {"file_id": "...", "name": "...", "action": "MOVE"|"TRASH"|"SKIP",
     "dest_folder_id": "..." or null, "reason": "..."},
    ...
  ]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Auth ──────────────────────────────────────────────────────────────────────
TOKEN_PATH = os.path.expanduser("~/credentials/token.json")
CREDS_PATH = os.path.expanduser("~/credentials/gdrive_credentials.json")
SCOPES = ["https://www.googleapis.com/auth/drive"]

DRIVE_DOCS_YAML = os.path.expanduser("~/dashboards/config/drive-docs.yaml")
MANIFEST_DIR    = os.path.expanduser("~/dashboards/data/drive-cleanup/")

# Destination folders that are constants (loaded from drive-docs.yaml when possible)
TC_ROOT_ID            = "1JWzfdAKq9OmiCq8OKwei0DXye4cal4bA"   # 00 Tomac Cove/
TC_CONTEXT_FOLDER_NAME = "_Context"                            # under TC_ROOT


def get_service():
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
    return build("drive", "v3", credentials=creds)


# ── Registry helpers ──────────────────────────────────────────────────────────

def load_registry() -> dict:
    with open(DRIVE_DOCS_YAML) as f:
        return yaml.safe_load(f)


def collect_registered_ids(registry: dict) -> set[str]:
    """Every Drive ID known to drive-docs.yaml. These MUST NOT be moved or trashed."""
    ids: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("doc_id", "folder_id", "file_id") and isinstance(v, str):
                    ids.add(v)
                # also handle id fields nested as 'session_log_file_id' etc.
                if isinstance(k, str) and k.endswith("_id") and isinstance(v, str) and len(v) > 20:
                    ids.add(v)
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(registry)
    return ids


def build_deal_alias_index(registry: dict) -> list[tuple[str, re.Pattern, str]]:
    """Returns [(deal_id, compiled_regex, outputs_folder_id), ...] for filename match."""
    out = []
    for deal_id, deal in (registry.get("deal_docs") or {}).items():
        aliases = deal.get("organizer_aliases") or []
        if not aliases:
            aliases = [deal_id]
        # also add the deal's keywords (lowered, simple ones) as soft matches
        kws = [k for k in (deal.get("keywords") or [])
               if len(k) >= 4 and " " not in k and k.isalpha()]
        all_terms = sorted(set(aliases + kws))
        pattern = r"\b(" + "|".join(re.escape(t) for t in all_terms) + r")\b"
        out.append((deal_id, re.compile(pattern, re.I), deal.get("outputs_folder_id")))
    return out


# ── Classification ────────────────────────────────────────────────────────────

VERSION_TAG = re.compile(r"\bv\d+\b", re.I)
STATUS_MD_AT_ROOT = re.compile(r"^[a-z_]+(_status|_brief|_master_brief|_lps|_terms|_actions)(\.md)?$", re.I)
PRESENTATION_STANDARDS = re.compile(r"(presentation\s*standards|practice\s*patterns|firm\s*context)", re.I)
GSCRIPT = re.compile(r"\.gscript$", re.I)


def classify(file: dict, registered_ids: set[str], deal_index: list) -> dict:
    """Return a manifest entry for this file."""
    fid = file["id"]
    name = file["name"]
    mime = file.get("mimeType", "")

    # 1. Registered? Leave alone.
    if fid in registered_ids:
        return {"file_id": fid, "name": name, "action": "SKIP",
                "dest_folder_id": None, "reason": "registered in drive-docs.yaml"}

    # 2. Loose <deal>_status.md / brief / etc. gdoc at root -> TRASH
    if STATUS_MD_AT_ROOT.match(name):
        return {"file_id": fid, "name": name, "action": "TRASH",
                "dest_folder_id": None,
                "reason": "historical orphan; canonical doc lives at registered ID"}

    # 3. *.gscript -> 00 Tomac Cove/_Context/
    if GSCRIPT.search(name) or mime == "application/vnd.google-apps.script":
        return {"file_id": fid, "name": name, "action": "MOVE",
                "dest_folder_id": _tc_context_folder_id(),
                "reason": "gscript -> 00 Tomac Cove/_Context/"}

    # 4. Filename matches a deal alias -> that deal's _Outputs/ (or Drafts/)
    for deal_id, pattern, outputs_folder_id in deal_index:
        if outputs_folder_id and pattern.search(name):
            if VERSION_TAG.search(name):
                return {"file_id": fid, "name": name, "action": "MOVE_TO_DRAFTS",
                        "dest_folder_id": outputs_folder_id,
                        "deal_id": deal_id,
                        "reason": f"versioned draft -> {deal_id}/_Outputs/Drafts/"}
            return {"file_id": fid, "name": name, "action": "MOVE",
                    "dest_folder_id": outputs_folder_id,
                    "deal_id": deal_id,
                    "reason": f"alias match -> {deal_id}/_Outputs/"}

    # 5. Firm context docs
    if PRESENTATION_STANDARDS.search(name):
        return {"file_id": fid, "name": name, "action": "MOVE",
                "dest_folder_id": _tc_context_folder_id(),
                "reason": "firm context -> 00 Tomac Cove/_Context/"}

    # 6. Unrouted
    return {"file_id": fid, "name": name, "action": "UNROUTED",
            "dest_folder_id": None,
            "reason": "no rule matched — manual review"}


_TC_CONTEXT_CACHE = {"id": None}

def _tc_context_folder_id() -> str:
    """Return the _Context folder id under 00 Tomac Cove/, lazily resolved."""
    if _TC_CONTEXT_CACHE["id"]:
        return _TC_CONTEXT_CACHE["id"]
    svc = get_service()
    q = (f"'{TC_ROOT_ID}' in parents and name = '{TC_CONTEXT_FOLDER_NAME}' "
         f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    resp = svc.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
    files = resp.get("files", [])
    if files:
        _TC_CONTEXT_CACHE["id"] = files[0]["id"]
    else:
        # Create it
        meta = {"name": TC_CONTEXT_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [TC_ROOT_ID]}
        created = svc.files().create(body=meta, fields="id").execute()
        _TC_CONTEXT_CACHE["id"] = created["id"]
    return _TC_CONTEXT_CACHE["id"]


# ── Discovery ─────────────────────────────────────────────────────────────────

def list_my_drive_root_files(svc) -> list[dict]:
    """Every non-folder file with My Drive root as a parent and not trashed."""
    out: list[dict] = []
    page_token = None
    q = ("'root' in parents and trashed = false "
         "and mimeType != 'application/vnd.google-apps.folder'")
    while True:
        resp = svc.files().list(
            q=q,
            fields="nextPageToken, files(id,name,mimeType,parents,modifiedTime,size,owners(emailAddress))",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


# ── Execution ─────────────────────────────────────────────────────────────────

def execute_manifest(svc, manifest: list[dict]) -> dict:
    counts = {"moved": 0, "moved_to_drafts": 0, "trashed": 0, "skipped": 0, "errors": 0}
    drafts_cache: dict[str, str] = {}  # outputs_folder_id -> drafts subfolder id

    for entry in manifest:
        action = entry["action"]
        fid = entry["file_id"]
        try:
            if action == "SKIP" or action == "UNROUTED":
                counts["skipped"] += 1
                continue
            if action == "TRASH":
                svc.files().update(fileId=fid, body={"trashed": True}).execute()
                counts["trashed"] += 1
                print(f"  ✗ trashed: {entry['name']}")
                continue
            if action == "MOVE":
                dest = entry["dest_folder_id"]
                current = svc.files().get(fileId=fid, fields="parents").execute()
                prev_parents = ",".join(current.get("parents", []))
                svc.files().update(fileId=fid, addParents=dest,
                                   removeParents=prev_parents, fields="id,parents").execute()
                counts["moved"] += 1
                print(f"  → moved: {entry['name']} -> {dest}")
                continue
            if action == "MOVE_TO_DRAFTS":
                outputs = entry["dest_folder_id"]
                drafts_id = drafts_cache.get(outputs)
                if not drafts_id:
                    drafts_id = _ensure_drafts_subfolder(svc, outputs)
                    drafts_cache[outputs] = drafts_id
                current = svc.files().get(fileId=fid, fields="parents").execute()
                prev_parents = ",".join(current.get("parents", []))
                svc.files().update(fileId=fid, addParents=drafts_id,
                                   removeParents=prev_parents, fields="id,parents").execute()
                counts["moved_to_drafts"] += 1
                print(f"  → drafted: {entry['name']} -> {drafts_id}")
                continue
        except HttpError as e:
            counts["errors"] += 1
            print(f"  ! error on {entry['name']}: {e}", file=sys.stderr)

    return counts


def _ensure_drafts_subfolder(svc, outputs_folder_id: str) -> str:
    q = (f"'{outputs_folder_id}' in parents and name = 'Drafts' "
         f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    resp = svc.files().list(q=q, fields="files(id)", pageSize=5).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": "Drafts",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [outputs_folder_id]}
    return svc.files().create(body=meta, fields="id").execute()["id"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--execute", action="store_true",
                    help="apply moves (default is dry-run -> writes manifest only)")
    ap.add_argument("--manifest", default=None,
                    help="path to a hand-edited manifest JSON to execute "
                         "(implies --execute)")
    args = ap.parse_args()

    svc = get_service()

    if args.manifest:
        with open(args.manifest) as f:
            manifest = json.load(f)
        print(f"Executing manifest from {args.manifest} ({len(manifest)} entries)…")
        counts = execute_manifest(svc, manifest)
        print(f"\nDone. {counts}")
        return

    print("Loading registry…")
    registry = load_registry()
    registered_ids = collect_registered_ids(registry)
    deal_index = build_deal_alias_index(registry)
    print(f"  {len(registered_ids)} registered IDs, {len(deal_index)} deals")

    print("Scanning My Drive root…")
    files = list_my_drive_root_files(svc)
    print(f"  {len(files)} non-folder files at root")

    manifest = [classify(f, registered_ids, deal_index) for f in files]

    by_action: dict[str, int] = {}
    for entry in manifest:
        by_action[entry["action"]] = by_action.get(entry["action"], 0) + 1
    print("\nClassification summary:")
    for action, n in sorted(by_action.items(), key=lambda x: -x[1]):
        print(f"  {action:18s} {n}")

    os.makedirs(MANIFEST_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(MANIFEST_DIR, f"my-drive-root-manifest-{stamp}.json")
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest written to {out_path}")

    if args.execute:
        print("\n--execute set; applying…")
        counts = execute_manifest(svc, manifest)
        print(f"\nDone. {counts}")
    else:
        print("\nDry run. Review the manifest, then:")
        print(f"  python3 {Path(__file__).name} --manifest {out_path}")


if __name__ == "__main__":
    main()
