#!/usr/bin/env python3
"""
cos_prefetch_drive.py — parallel data collection for the COS pipeline.

Fire this alongside gmail_search_messages + gcal_list_events at the very
start of every pipeline run. Returns all Drive/Docs data needed in ~1–2s
so Claude never needs a separate Docs read mid-run.

Collected concurrently (ThreadPoolExecutor, max_workers=11):
  1. Google OAuth token refresh
  2. Follow-ups doc — all paragraphs with startIndex/endIndex/text
  3. Recruiting doc  — row text only (for reconciliation)
  4-12. All 9 Drive priority folders — files modified since yesterday 7am

Usage:
    python3 ~/tomac-cove-pipeline/cos_prefetch_drive.py
    python3 ~/tomac-cove-pipeline/cos_prefetch_drive.py --since 2026-04-14T07:00:00

Outputs JSON to stdout. On error, outputs {"error": "..."}.
"""
import argparse
import concurrent.futures
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

TOKEN_PATH     = Path.home() / "credentials/token.json"
FOLLOWUPS_DOC  = "10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY"
RECRUITING_DOC = "1ZnTCVoA0ID7XTDFy27yDnrEVhBqx75kaTg_QXFq4eXA"

FOLDERS = {
    "Otter/Tomac":   "1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S",
    "Otter/Recruit": "1tMEGofeqzfF93YhPCyGe0dgJj8tzdRlF",
    "Otter/Other":   "1dt-s-D1SWaTrpIEsi0GiBAu1BCQCoPGq",
    "CallRec":       "1jYntgSVBsW5-5rdx18TeZhHRsI9xT74p",
    "RecruitDrive":  "1x0HQzC_Qq4_4xLeGFS384-4UnYWD2W4W",
    "ATT_FTTH":      "1qMhb6QUe9n3OT0zDGT2gyck1NDT7QEDm",
    "BlackBayou":    "1dY0sJM2BbknEAMv9aTolImhJWJM3Roqu",
    "PacificFleet":  "1rD1oggo5PIzV7_Jy_s-TJ6iujvhyjECx",
    "TomacRoot":     "1JWzfdAKq9OmiCq8OKwei0DXye4cal4bA",
}

MIME_FILTER = (
    "mimeType='application/vnd.google-apps.document' "
    "or mimeType='text/plain' "
    "or mimeType='application/vnd.openxmlformats-officedocument.wordprocessingml.document'"
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def refresh_token():
    with open(TOKEN_PATH) as f:
        creds = json.load(f)
    data = urllib.parse.urlencode({
        "client_id":     creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request(creds["token_uri"], data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        new_token = json.loads(r.read())["access_token"]
    creds["token"] = new_token
    with open(TOKEN_PATH, "w") as f:
        json.dump(creds, f)
    return new_token


# ── Drive search ──────────────────────────────────────────────────────────────

def scan_folder(token, folder_name, folder_id, since):
    q = f"'{folder_id}' in parents and modifiedTime > '{since}' and ({MIME_FILTER})"
    params = urllib.parse.urlencode({
        "q":      q,
        "fields": "files(id,name,modifiedTime,mimeType)",
        "pageSize": 20,
    })
    url = f"https://www.googleapis.com/drive/v3/files?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return folder_name, json.loads(r.read()).get("files", [])
    except Exception as e:
        return folder_name, []


# ── Docs read ─────────────────────────────────────────────────────────────────

def read_doc_paragraphs(token, doc_id, text_limit=200):
    """Return list of {si, ei, text} for every paragraph in doc."""
    url = f"https://docs.googleapis.com/v1/documents/{doc_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            doc = json.loads(r.read())
    except Exception as e:
        return [], 0

    paras = []
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            text = "".join(
                pe["textRun"].get("content", "")
                for pe in elem["paragraph"].get("elements", [])
                if "textRun" in pe
            )
            paras.append({
                "si":   elem.get("startIndex", 0),
                "ei":   elem.get("endIndex", 0),
                "text": text[:text_limit],
            })

    end_index = doc["body"]["content"][-1].get("endIndex", 0) if doc.get("body", {}).get("content") else 0
    return paras, end_index


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None,
                        help="ISO datetime floor for Drive scan (default: yesterday 7am)")
    args = parser.parse_args()

    since = args.since or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT07:00:00")

    # Step 1: refresh token (needed before concurrent tasks)
    try:
        token = refresh_token()
    except Exception as e:
        json.dump({"error": f"Token refresh failed: {e}"}, sys.stdout)
        return

    # Step 2: all Drive scans + both doc reads, concurrently
    tasks = []
    for name, fid in FOLDERS.items():
        tasks.append(("folder", name, fid))
    tasks.append(("doc", "followups", FOLLOWUPS_DOC))
    tasks.append(("doc", "recruiting", RECRUITING_DOC))

    folder_results  = {}
    doc_results     = {}

    def run_task(task):
        kind = task[0]
        if kind == "folder":
            _, name, fid = task
            return ("folder", *scan_folder(token, name, fid, since))
        else:
            _, name, doc_id = task
            paras, end_idx = read_doc_paragraphs(token, doc_id)
            return ("doc", name, paras, end_idx)

    with concurrent.futures.ThreadPoolExecutor(max_workers=11) as ex:
        for result in ex.map(run_task, tasks):
            if result[0] == "folder":
                _, name, files = result
                folder_results[name] = files
            else:
                _, name, paras, end_idx = result
                doc_results[name] = {"paragraphs": paras, "end_index": end_idx}

    # Compute helpers from followups doc
    followups_paras = doc_results.get("followups", {}).get("paragraphs", [])
    followups_end   = doc_results.get("followups", {}).get("end_index", 0)

    # Find last row number and last table row's endIndex
    last_row_num   = 0
    last_table_end = 0
    for p in followups_paras:
        m = re.match(r"^\|\s*(\d+)\s*\|", p["text"])
        if m:
            last_row_num   = max(last_row_num, int(m.group(1)))
            last_table_end = p["ei"]

    new_drive_docs = {k: v for k, v in folder_results.items() if v}

    output = {
        "since":        since,
        "token_ok":     True,
        "followups": {
            "paragraphs":      followups_paras,
            "end_index":       followups_end,
            "last_row_num":    last_row_num,
            "last_table_end":  last_table_end,
        },
        "recruiting": {
            "paragraphs": doc_results.get("recruiting", {}).get("paragraphs", []),
            "end_index":  doc_results.get("recruiting", {}).get("end_index", 0),
        },
        "drive": folder_results,
        "drive_new_count": sum(len(v) for v in new_drive_docs.values()),
        "drive_new_folders": list(new_drive_docs.keys()),
    }

    json.dump(output, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
