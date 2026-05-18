#!/usr/bin/env python3
"""
fetch_project_instructions.py
Fetches each deal's project_instructions Google Doc from Drive,
strips non-ASCII, writes to /tmp/{deal_id}_instructions.txt.

Used by /refresh-project-instructions before the Chrome MCP paste step.

Usage:
  python3 fetch_project_instructions.py all
  python3 fetch_project_instructions.py --deal <deal_id>
"""
import argparse
import json
import pickle
import sys
from pathlib import Path

import yaml
from googleapiclient.discovery import build

HOME = Path.home()
CREDS_PATH = HOME / "credentials" / "gdrive_token.pickle"
DRIVE_DOCS = HOME / "cos-pipeline-config-tomac" / "drive-docs.yaml"


def get_creds():
    with open(CREDS_PATH, "rb") as f:
        return pickle.load(f)


def fetch_doc_text(docs_svc, file_id):
    doc = docs_svc.documents().get(documentId=file_id).execute()
    text = ""
    for element in doc.get("body", {}).get("content", []):
        if "paragraph" in element:
            for pe in element["paragraph"].get("elements", []):
                if "textRun" in pe:
                    text += pe["textRun"]["content"]
    return text


def strip_non_ascii(text):
    replacements = [
        ("═" * 32, "=" * 32),
        ("—", "--"), ("–", "-"),
        ("‘", "'"), ("’", "'"),
        ("“", '"'), ("”", '"'),
        ("→", "->"), ("×", "x"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text.encode("ascii", "replace").decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", default="all", help="all or --deal <deal_id>")
    parser.add_argument("--deal", help="Single deal_id to fetch")
    args = parser.parse_args()

    drive_docs = yaml.safe_load(DRIVE_DOCS.read_text())
    deal_docs = drive_docs.get("deal_docs", {})

    if args.deal:
        targets = [args.deal]
    else:
        targets = list(deal_docs.keys())

    creds = get_creds()
    docs_svc = build("docs", "v1", credentials=creds)

    ok = []
    failed = []

    for deal_id in targets:
        entry = deal_docs.get(deal_id, {})
        pi = entry.get("project_instructions", {})
        file_id = pi.get("doc_id") if isinstance(pi, dict) else None

        if not file_id:
            print(f"  {deal_id}: no project_instructions.doc_id in drive-docs.yaml — skipping")
            failed.append(deal_id)
            continue

        try:
            text = fetch_doc_text(docs_svc, file_id)
            text = strip_non_ascii(text)
            out = Path(f"/tmp/{deal_id}_instructions.txt")
            out.write_text(text)
            print(f"  {deal_id}: written to {out} ({len(text)} chars)")
            ok.append(deal_id)
        except Exception as e:
            print(f"  {deal_id}: ERROR — {e}")
            failed.append(deal_id)

    print(f"\nDone: {len(ok)} fetched, {len(failed)} failed")
    if failed:
        print(f"  Failed: {', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
