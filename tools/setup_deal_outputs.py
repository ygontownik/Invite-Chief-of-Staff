#!/usr/bin/env python3
"""
setup_deal_outputs.py — Retroactive _Outputs/ setup for registered deal folders.

For each deal in drive-docs.yaml that lacks outputs_folder_id:
  1. Create _Outputs/ subfolder in the deal's drive_folder_id (idempotent)
  2. Create session_log.md inside _Outputs/ (idempotent)
  3. Patch dashboard_entry.json in claude_context_folder_id with claude_outputs + claude_outputs_folder_id
  4. Write outputs_folder_id + session_log_file_id back to drive-docs.yaml

Usage:
  python3 setup_deal_outputs.py           # run all deals
  python3 setup_deal_outputs.py --deal <deal_id> <deal_id>   # specific deals only
  python3 setup_deal_outputs.py --dry-run                     # print plan, no writes
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).parent
SCOPES = ["https://www.googleapis.com/auth/drive"]
TODAY = date.today().isoformat()


# ── auth ──────────────────────────────────────────────────────────────────────

def _get_drive_service():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("google-api-python-client not installed. Run: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")

    token_path = SCRIPT_DIR / "token.json"
    creds_path = SCRIPT_DIR / "credentials.json"
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


# ── drive helpers ─────────────────────────────────────────────────────────────

def _list_children(svc, parent_id, name=None, mime=None):
    q_parts = [f"'{parent_id}' in parents", "trashed = false"]
    if name:
        q_parts.append(f"name = '{name}'")
    if mime:
        q_parts.append(f"mimeType = '{mime}'")
    q = " and ".join(q_parts)
    res = svc.files().list(q=q, fields="files(id,name,mimeType)").execute()
    return res.get("files", [])


def _get_or_create_folder(svc, name, parent_id, dry_run=False):
    existing = _list_children(svc, parent_id, name=name, mime="application/vnd.google-apps.folder")
    if existing:
        return existing[0]["id"], False  # (id, created)
    if dry_run:
        return f"DRY-RUN-FOLDER-{name}", True
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    f = svc.files().create(body=meta, fields="id").execute()
    return f["id"], True


def _get_or_create_text_file(svc, name, content, parent_id, dry_run=False):
    if dry_run or (parent_id and parent_id.startswith("DRY-RUN")):
        return f"DRY-RUN-FILE-{name}", True
    existing = _list_children(svc, parent_id, name=name)
    if existing:
        return existing[0]["id"], False  # (id, created)
    from googleapiclient.http import MediaInMemoryUpload
    meta = {"name": name, "parents": [parent_id]}
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain", resumable=False)
    f = svc.files().create(body=meta, media_body=media, fields="id").execute()
    return f["id"], True


def _read_json_file(svc, file_id):
    """Download a JSON file from Drive and return parsed dict."""
    data = svc.files().get_media(fileId=file_id).execute()
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)


def _write_json_file(svc, file_id, obj, dry_run=False):
    """Overwrite a Drive file's content with a JSON-serialised dict."""
    if dry_run:
        return
    from googleapiclient.http import MediaInMemoryUpload
    content = json.dumps(obj, indent=2).encode("utf-8")
    media = MediaInMemoryUpload(content, mimetype="application/json", resumable=False)
    svc.files().update(fileId=file_id, media_body=media).execute()


# ── config ────────────────────────────────────────────────────────────────────

def _find_drive_docs_yaml():
    """Find drive-docs.yaml via COS_CONFIG_DIR, glob, or fallback."""
    env = os.environ.get("COS_CONFIG_DIR")
    if env:
        p = Path(env) / "drive-docs.yaml"
        if p.exists():
            return p
    # Glob for cos-pipeline-config-* repos
    for candidate in sorted(Path.home().glob("cos-pipeline-config-*")):
        p = candidate / "drive-docs.yaml"
        if p.exists():
            return p
    # dashboards/config fallback
    p = Path.home() / "dashboards" / "config" / "drive-docs.yaml"
    if p.exists():
        return p
    sys.exit("drive-docs.yaml not found. Set COS_CONFIG_DIR or create ~/cos-pipeline-config-<slug>/drive-docs.yaml")


def _load_deal_docs(yaml_path):
    with open(yaml_path) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("deal_docs", {})


def _update_yaml_field(yaml_path, deal_id, fields: dict):
    """Patch specific fields into deal_docs.<deal_id> in the YAML file in-place."""
    text = yaml_path.read_text()
    cfg = yaml.safe_load(text)
    deal_node = cfg.setdefault("deal_docs", {}).setdefault(deal_id, {})
    for k, v in fields.items():
        deal_node[k] = v
    yaml_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False))


# ── per-deal logic ────────────────────────────────────────────────────────────

def _find_dashboard_entry(svc, deal_id, claude_context_folder_id):
    """Search for {deal_id}_dashboard_entry.json in claude_context_folder_id."""
    hits = _list_children(svc, claude_context_folder_id, name=f"{deal_id}_dashboard_entry.json")
    return hits[0]["id"] if hits else None


def process_deal(svc, deal_id, deal_cfg, yaml_path, dry_run=False):
    drive_folder_id = deal_cfg.get("drive_folder_id")
    ctx_folder_id   = deal_cfg.get("claude_context_folder_id")

    if not drive_folder_id:
        print(f"  ⚠  {deal_id}: no drive_folder_id — skip")
        return
    if not ctx_folder_id:
        print(f"  ⚠  {deal_id}: no claude_context_folder_id — skip")
        return

    # -- 1. _Outputs/ folder --
    outputs_folder_id, created = _get_or_create_folder(svc, "_Outputs", drive_folder_id, dry_run)
    tag = "created" if created else "exists"
    print(f"  _Outputs/  [{tag}]  {outputs_folder_id}")

    # -- 2. session_log.md --
    deal_name = deal_cfg.get("status", {}).get("name", deal_id).replace("_status", "").replace("_", " ").title()
    log_header = (
        f"# {deal_name} — Session Output Log\n\n"
        f"| Date | Type | Title | Description | File |\n"
        f"|------|------|-------|-------------|------|\n"
    )
    log_id, created = _get_or_create_text_file(svc, "session_log.md", log_header, outputs_folder_id, dry_run)
    tag = "created" if created else "exists"
    print(f"  session_log.md  [{tag}]  {log_id}")

    # -- 3. Patch dashboard_entry.json --
    entry_id = None if dry_run else _find_dashboard_entry(svc, deal_id, ctx_folder_id)
    if entry_id:
        if not dry_run:
            try:
                entry = _read_json_file(svc, entry_id)
                changed = False
                if "claude_outputs" not in entry:
                    entry["claude_outputs"] = []
                    changed = True
                if "claude_outputs_folder_id" not in entry:
                    entry["claude_outputs_folder_id"] = outputs_folder_id
                    changed = True
                if changed:
                    _write_json_file(svc, entry_id, entry)
                    print(f"  dashboard_entry.json  [patched]  {entry_id}")
                else:
                    print(f"  dashboard_entry.json  [already has outputs fields]")
            except Exception as e:
                print(f"  ⚠  dashboard_entry.json patch failed: {e}")
        else:
            print(f"  dashboard_entry.json  [DRY-RUN would patch]  {entry_id}")
    else:
        print(f"  dashboard_entry.json  [not found in ctx folder — skip]")

    # -- 4. Write back to drive-docs.yaml --
    if not dry_run:
        existing_outputs_id = deal_cfg.get("outputs_folder_id")
        existing_log_id = deal_cfg.get("session_log_file_id")
        if not existing_outputs_id or not existing_log_id:
            _update_yaml_field(yaml_path, deal_id, {
                "outputs_folder_id": outputs_folder_id,
                "session_log_file_id": log_id,
            })
            print(f"  drive-docs.yaml  [updated]")
        else:
            print(f"  drive-docs.yaml  [already has outputs fields — skip]")
    else:
        print(f"  drive-docs.yaml  [DRY-RUN would write outputs_folder_id + session_log_file_id]")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Retroactive _Outputs/ setup for registered deals")
    parser.add_argument("--deal", nargs="+", metavar="DEAL_ID", help="Limit to specific deal IDs")
    parser.add_argument("--dry-run", action="store_true", help="Print plan, no Drive writes")
    args = parser.parse_args()

    yaml_path  = _find_drive_docs_yaml()
    deal_docs  = _load_deal_docs(yaml_path)
    target_ids = args.deal or list(deal_docs.keys())

    print(f"drive-docs.yaml: {yaml_path}")
    print(f"Deals to process: {', '.join(target_ids)}")
    if args.dry_run:
        print("DRY-RUN mode — no writes.\n")

    svc = _get_drive_service()

    ok = fail = skip = 0
    for deal_id in target_ids:
        if deal_id not in deal_docs:
            print(f"\n✗ {deal_id} not found in drive-docs.yaml")
            fail += 1
            continue
        print(f"\n── {deal_id} ──")
        try:
            process_deal(svc, deal_id, deal_docs[deal_id], yaml_path, dry_run=args.dry_run)
            ok += 1
        except Exception as e:
            print(f"  ✗ ERROR: {e}")
            fail += 1

    print(f"\n{'=' * 40}")
    print(f"Done: {ok} ok | {fail} failed | {skip} skipped")


if __name__ == "__main__":
    main()
