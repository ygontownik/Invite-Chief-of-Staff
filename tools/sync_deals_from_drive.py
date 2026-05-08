#!/usr/bin/env python3
"""
sync_deals_from_drive.py
Download {deal_id}_dashboard_entry.json from each deal's Drive folder,
merge into compiled/deal-system-data.json, trigger dashboard warmup.

Run: python3 sync_deals_from_drive.py [--deal-id pngts]
"""

import json
import os
import pickle
import sys
import io
import argparse
import requests
from datetime import datetime
from googleapiclient.discovery import build

CREDENTIALS_PATH = os.path.expanduser('~/credentials/gdrive_token.pickle')
DEAL_REGISTRY_PATH = os.path.expanduser('~/cos-pipeline/tools/deal-system-data.json')
COMPILED_PATH = os.path.expanduser('~/cos-pipeline/data-tomac/compiled/deal-system-data.json')
DASHBOARD_URL = 'http://localhost:7777'

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

def load_compiled():
    with open(COMPILED_PATH) as f:
        data = json.load(f)
    return data

def save_compiled(data):
    with open(COMPILED_PATH, 'w') as f:
        json.dump(data, f, indent=2)

def warmup_dashboard():
    try:
        r = requests.post(f'{DASHBOARD_URL}/warmup', timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def sync_deal(service, deal_reg, compiled_data):
    deal_id = deal_reg['deal_id']
    folder_id = deal_reg.get('drive_folder_id')
    if not folder_id:
        print(f'  {deal_id}: no drive_folder_id — skip')
        return False

    fname = f'{deal_id}_dashboard_entry.json'
    file_id = find_drive_file(service, folder_id, fname)
    if not file_id:
        print(f'  {deal_id}: {fname} not found in Drive folder — skip')
        return False

    try:
        entry = download_json(service, file_id)
    except Exception as e:
        print(f'  {deal_id}: download failed — {e}')
        return False

    entry['_last_synced_from_drive'] = datetime.now().strftime('%Y-%m-%d')

    # Merge into compiled deal-system-data.json
    compiled_deals = compiled_data.get('deals', compiled_data) if isinstance(compiled_data, dict) else compiled_data
    existing = next((d for d in compiled_deals if d.get('id') == deal_id), None)
    if existing:
        existing.update(entry)
        print(f'  {deal_id}: updated existing entry')
    else:
        compiled_deals.append(entry)
        print(f'  {deal_id}: added new entry')

    if isinstance(compiled_data, dict) and 'deals' in compiled_data:
        compiled_data['deals'] = compiled_deals
    return True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deal-id', help='Sync only this deal')
    args = parser.parse_args()

    service = get_drive_service()
    registry = load_registry()
    compiled = load_compiled()

    if args.deal_id:
        registry = [d for d in registry if d['deal_id'] == args.deal_id]
        if not registry:
            print(f'Deal {args.deal_id} not found in registry')
            sys.exit(1)

    done = 0
    for deal in registry:
        if sync_deal(service, deal, compiled):
            done += 1

    if done:
        save_compiled(compiled)
        print(f'\n{done} deal(s) synced → compiled/deal-system-data.json')
        if warmup_dashboard():
            print('Dashboard warmed up ✓')
        else:
            print('Dashboard warmup failed (server may be down)')
    else:
        print('Nothing synced.')

if __name__ == '__main__':
    main()
