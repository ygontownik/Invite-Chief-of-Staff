#!/usr/bin/env python3
"""
sync_deals_from_drive.py
Download {deal_id}_dashboard_entry.json from each deal's Drive folder,
write a _drive_overlay.json that compile-dashboard.py merges at compile time,
and update deal.md YAML frontmatter (stage + thesis scores) so the health
formula always uses current values.

Run: python3 sync_deals_from_drive.py [--deal-id pngts]
"""

import json
import os
import pickle
import re
import subprocess
import sys
import argparse
from datetime import datetime
from pathlib import Path

import yaml
from googleapiclient.discovery import build

CREDENTIALS_PATH  = os.path.expanduser('~/credentials/gdrive_token.pickle')
DEAL_REGISTRY_PATH = os.path.expanduser('~/cos-pipeline/tools/deal-system-data.json')
OVERLAY_PATH      = Path.home() / 'dashboards' / 'data' / '_drive_overlay.json'
DEALS_DIR         = Path.home() / 'dashboards' / 'data' / 'deals'
COMPILE_SCRIPT    = Path.home() / 'dashboards' / 'routines' / 'compile' / 'compile-dashboard.py'
DASHBOARD_URL     = 'http://localhost:7777'


def get_drive_service():
    with open(CREDENTIALS_PATH, 'rb') as f:
        creds = pickle.load(f)
    return build('drive', 'v3', credentials=creds)


def download_json(service, file_id):
    req = service.files().get_media(fileId=file_id)
    content = req.execute()
    if isinstance(content, bytes):
        return json.loads(content.decode('utf-8'))
    return json.loads(content)


def find_drive_file(service, folder_id, filename):
    res = service.files().list(
        q=f"name='{filename}' and '{folder_id}' in parents and trashed=false",
        fields='files(id,name)'
    ).execute()
    files = res.get('files', [])
    return files[0]['id'] if files else None


def load_registry():
    with open(DEAL_REGISTRY_PATH) as f:
        data = json.load(f)
    return data.get('deals', [])


def _update_deal_md(deal_id: str, entry: dict) -> bool:
    """Update deal.md YAML frontmatter with stage and thesis scores from Drive entry.

    Only updates fields that the Drive entry has authoritative values for:
    - stage (string)
    - thesis[].score — matched by label; adds score to existing pillar

    Returns True if the file was modified.
    """
    deal_md_path = DEALS_DIR / deal_id / 'deal.md'
    if not deal_md_path.exists():
        return False

    text = deal_md_path.read_text()
    m = re.match(r'^---\n(.*?)\n---\n(.*)$', text, re.DOTALL)
    if not m:
        return False

    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return False

    body = m.group(2)
    changed = False

    # Sync stage
    drive_stage = entry.get('stage', '')
    if drive_stage and fm.get('stage') != drive_stage:
        fm['stage'] = drive_stage
        changed = True

    # Sync thesis pillar scores (match by label)
    drive_thesis = entry.get('thesis') or []
    local_thesis = fm.get('thesis') or []
    if drive_thesis and local_thesis:
        drive_by_label = {p['label']: p for p in drive_thesis if p.get('label')}
        for pillar in local_thesis:
            lbl = pillar.get('label')
            if lbl and lbl in drive_by_label:
                new_score = drive_by_label[lbl].get('score')
                if new_score is not None and pillar.get('score') != new_score:
                    pillar['score'] = new_score
                    changed = True

    if not changed:
        return False

    new_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True).rstrip()
    deal_md_path.write_text(f"---\n{new_yaml}\n---\n{body}")
    return True


def sync_deal(service, deal_reg: dict) -> dict | None:
    """Download a deal's Drive dashboard_entry.json and return overlay fields.

    Returns a dict of Drive-sourced fields for this deal, or None on failure.
    Does NOT write to deal-system-data.json — that's compile-dashboard.py's job.
    """
    deal_id = deal_reg['deal_id']
    folder_id = deal_reg.get('drive_folder_id')
    if not folder_id:
        print(f'  {deal_id}: no drive_folder_id — skip')
        return None

    fname = f'{deal_id}_dashboard_entry.json'
    file_id = find_drive_file(service, folder_id, fname)
    if not file_id:
        print(f'  {deal_id}: {fname} not found in Drive folder — skip')
        return None

    try:
        entry = download_json(service, file_id)
    except Exception as e:
        print(f'  {deal_id}: download failed — {e}')
        return None

    # Annotate with sync timestamp and registry-level fields
    entry['_last_synced_from_drive'] = datetime.now().strftime('%Y-%m-%d')
    for k in ('project_url', 'drive_folder_id', 'status_file_id', 'brief_file_id'):
        v = deal_reg.get(k)
        if v:
            entry[k] = v

    # Sync deal.md frontmatter (stage + thesis scores) so compile picks up fresh values
    if _update_deal_md(deal_id, entry):
        print(f'  {deal_id}: deal.md frontmatter updated (stage/thesis)')

    print(f'  {deal_id}: overlay ready')
    return entry


def warmup_dashboard():
    try:
        import requests
        r = requests.post(f'{DASHBOARD_URL}/warmup', timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deal-id', help='Sync only this deal')
    args = parser.parse_args()

    service = get_drive_service()
    full_registry = load_registry()

    registry = (
        [d for d in full_registry if d['deal_id'] == args.deal_id]
        if args.deal_id else full_registry
    )
    if args.deal_id and not registry:
        print(f'Deal {args.deal_id} not found in registry')
        sys.exit(1)

    # Load existing overlay so registry-only deals (no Drive entry) retain prior data
    try:
        prior_overlay = json.loads(OVERLAY_PATH.read_text()).get('deals', {}) if OVERLAY_PATH.exists() else {}
    except Exception:
        prior_overlay = {}

    overlay_deals = dict(prior_overlay)  # start with prior; overwrite synced deals

    # Add registry-level fields for all registered deals (so compile always has them)
    for deal_reg in full_registry:
        did = deal_reg['deal_id']
        stub = overlay_deals.setdefault(did, {})
        for k in ('project_url', 'drive_folder_id', 'status_file_id', 'brief_file_id'):
            v = deal_reg.get(k)
            if v:
                stub[k] = v

    done = 0
    for deal_reg in registry:
        entry = sync_deal(service, deal_reg)
        if entry is not None:
            overlay_deals[deal_reg['deal_id']] = entry
            done += 1

    if not done:
        print('Nothing synced from Drive.')
        return

    # Write overlay — compile-dashboard.py merges this at compile time
    OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY_PATH.write_text(json.dumps(
        {'updated_at': datetime.now().isoformat(), 'deals': overlay_deals},
        indent=2, default=str,
    ))
    print(f'\n{done} deal(s) synced → {OVERLAY_PATH.name}')

    # Trigger compile so dashboard picks up the fresh overlay immediately
    try:
        result = subprocess.run(
            [sys.executable, str(COMPILE_SCRIPT)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print('Compile OK')
        else:
            print(f'Compile WARN: {result.stderr[:200]}')
    except Exception as e:
        print(f'Compile failed (non-fatal): {e}')

    if warmup_dashboard():
        print('Dashboard warmed up ✓')
    else:
        print('Dashboard warmup failed (server may be down)')


if __name__ == '__main__':
    main()
