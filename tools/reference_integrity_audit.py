#!/usr/bin/env python3
"""
reference_integrity_audit.py — Daily Drive reference integrity checker.

Validates two things:
  1. Every Drive ID registered in drive-docs.yaml still resolves (not deleted/trashed).
  2. Each deal's project_instructions doc (fetched from Drive) does not reference
     Drive IDs that are absent from the registry.

Writes results to:
  ~/dashboards/data/compiled/reference_integrity.json

Shared-state write is bracketed by coordination.lock().

USAGE:
  python3 reference_integrity_audit.py               # full audit
  python3 reference_integrity_audit.py --dry-run     # list checks, no API calls
  python3 reference_integrity_audit.py --section docs      # audit one YAML section
  python3 reference_integrity_audit.py --section deal_docs # audit deal docs only
"""

from __future__ import annotations  # PEP 604 compat when run under Python 3.9 launchd runners

import argparse
import json
import os
import pickle
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── Path setup (sibling import: coordination.py is in the same directory) ────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from coordination import lock as coord_lock, mark_run

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME         = Path.home()
CREDS_PATH   = HOME / "credentials" / "gdrive_token.pickle"
DRIVE_DOCS   = HOME / "dashboards" / "config" / "drive-docs.yaml"
OUTPUT_PATH  = HOME / "dashboards" / "data" / "compiled" / "reference_integrity.json"
LOG_PREFIX   = "[reference_integrity_audit]"

# Pattern for a Google Drive file/folder ID (25–44 alphanumeric + hyphen/underscore chars).
# Drive IDs are typically 25–44 chars; 20+ catches most while avoiding false positives on
# short slugs in instruction prose.
_DRIVE_ID_RE = re.compile(r'\b([0-9A-Za-z_-]{20,})\b')

# Fields whose values are Drive IDs (beyond the standard "doc_id").
_ID_FIELD_SUFFIXES = ("doc_id", "folder_id", "file_id")


# ── YAML walker ───────────────────────────────────────────────────────────────

def _collect_ids(node, path=""):
    """Recursively yield (dotted.path, drive_id) for every Drive ID field."""
    if isinstance(node, dict):
        for k, v in node.items():
            child_path = f"{path}.{k}" if path else k
            # Yield IDs stored directly under a recognised ID field name
            if isinstance(v, str) and any(k.endswith(s) for s in _ID_FIELD_SUFFIXES):
                yield child_path, v
            else:
                yield from _collect_ids(v, child_path)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _collect_ids(item, f"{path}[{i}]")
    # scalars that aren't under an ID-keyed field are skipped


def collect_all_ids(cfg: dict) -> dict:
    """Return {drive_id: [path, ...]} for every ID found in the YAML."""
    result: dict[str, list[str]] = {}
    for path, drive_id in _collect_ids(cfg):
        result.setdefault(drive_id, []).append(path)
    return result


# ── Drive / Docs auth ─────────────────────────────────────────────────────────

def _get_creds():
    with open(CREDS_PATH, "rb") as f:
        creds = pickle.load(f)
    if hasattr(creds, "expired") and creds.expired and getattr(creds, "refresh_token", None):
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(CREDS_PATH, "wb") as f:
            pickle.dump(creds, f)
    return creds


def get_drive_service():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_get_creds())


def get_docs_service():
    from googleapiclient.discovery import build
    return build("docs", "v1", credentials=_get_creds())


# ── Check 1: every registered ID resolves ─────────────────────────────────────

def audit_drive_resolution(drive_svc, all_ids: dict, section_filter: str | None) -> list[str]:
    """
    For each registered Drive ID, confirm it exists and is not trashed.
    Returns a list of violation strings.
    """
    from googleapiclient.errors import HttpError

    violations = []
    checked = 0
    for drive_id, paths in all_ids.items():
        if section_filter:
            # Only check IDs whose first-registered path is inside the requested section
            if not any(p.startswith(section_filter) for p in paths):
                continue
        try:
            meta = drive_svc.files().get(
                fileId=drive_id,
                fields="id,name,trashed",
            ).execute()
            if meta.get("trashed"):
                violations.append(
                    f"TRASHED  {drive_id}  ({', '.join(paths[:2])})  name={meta.get('name')}"
                )
            checked += 1
        except HttpError as e:
            status = e.resp.status if hasattr(e, "resp") else "?"
            violations.append(
                f"HTTP{status}  {drive_id}  ({', '.join(paths[:2])})"
            )
        except Exception as e:
            violations.append(
                f"ERROR  {drive_id}  ({', '.join(paths[:2])})  {e}"
            )
    print(f"{LOG_PREFIX} check-1: verified {checked} IDs, {len(violations)} violation(s)")
    return violations


# ── Check 2: project_instructions docs reference only registered IDs ──────────

def _doc_to_text(docs_svc, file_id: str) -> str:
    """Fetch a Google Doc and return its plain text."""
    doc = docs_svc.documents().get(documentId=file_id).execute()
    parts = []
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    parts.append(pe["textRun"]["content"])
    return "".join(parts)


def audit_project_instructions(docs_svc, deal_docs: dict, registered_ids: set, section_filter: str | None) -> list[str]:
    """
    For each deal's project_instructions doc, scan for Drive-ID-shaped strings.
    Flag any that are not in registered_ids and not a known safe external ID
    (e.g. claude.ai project UUIDs, which are not Drive IDs).
    """
    if section_filter and section_filter != "deal_docs":
        return []

    violations = []
    checked = 0

    # Claude project UUIDs look like 8-4-4-4-12 hex — exclude them to avoid false positives.
    _UUID_RE = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        re.IGNORECASE,
    )

    for deal_id, entry in deal_docs.items():
        pi = entry.get("project_instructions", {})
        pi_doc_id = pi.get("doc_id") if isinstance(pi, dict) else None
        if not pi_doc_id:
            continue

        try:
            text = _doc_to_text(docs_svc, pi_doc_id)
        except Exception as e:
            violations.append(f"{deal_id}: cannot fetch project_instructions ({pi_doc_id}): {e}")
            continue

        checked += 1
        # Strip UUID-shaped strings before scanning for raw Drive IDs
        text_no_uuid = _UUID_RE.sub("", text)

        for m in _DRIVE_ID_RE.finditer(text_no_uuid):
            cand = m.group(1)
            if cand in registered_ids:
                continue
            # Skip short common English words that happen to match the pattern length
            if len(cand) < 25:
                continue
            violations.append(
                f"{deal_id}: project_instructions references unregistered ID {cand}"
            )

    print(f"{LOG_PREFIX} check-2: scanned {checked} project_instructions docs, "
          f"{len(violations)} violation(s)")
    return violations


# ── Output ─────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_report(violations: list[str], all_ids: dict, dry_run: bool) -> None:
    report = {
        "generated_at": _now_iso(),
        "total_ids_registered": len(all_ids),
        "violations": violations,
        "violation_count": len(violations),
        "dry_run": dry_run,
    }
    if dry_run:
        print(f"{LOG_PREFIX} [dry-run] would write {len(violations)} violations to {OUTPUT_PATH}")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with coord_lock("reference-integrity-report", holder="reference_integrity_audit.py", ttl_seconds=60):
        tmp = OUTPUT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2))
        tmp.replace(OUTPUT_PATH)

    status = "CLEAN" if not violations else f"{len(violations)} VIOLATION(S)"
    print(f"{LOG_PREFIX} report written → {OUTPUT_PATH} [{status}]")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate every Drive ID in drive-docs.yaml still resolves."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be checked without hitting APIs or writing output.",
    )
    parser.add_argument(
        "--section",
        help="Limit audit to one top-level YAML section (e.g. docs, deal_docs, folders).",
    )
    args = parser.parse_args()

    if not DRIVE_DOCS.exists():
        print(f"{LOG_PREFIX} ERROR: drive-docs.yaml not found at {DRIVE_DOCS}", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(DRIVE_DOCS.read_text())
    if not cfg:
        print(f"{LOG_PREFIX} ERROR: drive-docs.yaml is empty or invalid", file=sys.stderr)
        return 1

    # Apply section filter
    if args.section:
        if args.section not in cfg:
            print(f"{LOG_PREFIX} ERROR: section '{args.section}' not in drive-docs.yaml. "
                  f"Available: {list(cfg.keys())}", file=sys.stderr)
            return 1

    all_ids = collect_all_ids(cfg)
    registered_ids: set[str] = set(all_ids.keys())

    print(f"{LOG_PREFIX} drive-docs.yaml: {len(cfg)} sections, {len(all_ids)} registered IDs")
    if args.section:
        print(f"{LOG_PREFIX} section filter: {args.section}")

    deal_docs = cfg.get("deal_docs", {})
    pi_count = sum(
        1 for d in deal_docs.values()
        if isinstance(d.get("project_instructions"), dict) and d["project_instructions"].get("doc_id")
    )

    if args.dry_run:
        print(f"\n{LOG_PREFIX} [dry-run] would check:")
        print(f"  {len(all_ids)} Drive IDs via files().get()")
        print(f"  {pi_count} project_instructions docs for foreign IDs")
        print(f"\n{LOG_PREFIX} [dry-run] sample IDs to check:")
        for i, (drive_id, paths) in enumerate(list(all_ids.items())[:8]):
            print(f"  {drive_id[:28]}...  ← {paths[0]}")
        if len(all_ids) > 8:
            print(f"  ... and {len(all_ids) - 8} more")
        print(f"\n{LOG_PREFIX} [dry-run] output would go to: {OUTPUT_PATH}")
        return 0

    # Full audit — requires Drive/Docs credentials
    try:
        drive_svc = get_drive_service()
        docs_svc  = get_docs_service()
    except Exception as e:
        print(f"{LOG_PREFIX} ERROR: cannot authenticate to Google APIs: {e}", file=sys.stderr)
        return 1

    violations: list[str] = []

    violations += audit_drive_resolution(drive_svc, all_ids, args.section)
    violations += audit_project_instructions(docs_svc, deal_docs, registered_ids, args.section)

    write_report(violations, all_ids, dry_run=False)

    # Record successful completion in coordination state so /wrap STEP 8c
    # cadence-staleness check can see when this script last ran. Fixed
    # 2026-05-21 after /wrap pt 4 surfaced "never recorded" warning.
    mark_run("reference_integrity_audit.py")

    if violations:
        print(f"\n{LOG_PREFIX} VIOLATIONS ({len(violations)}):")
        for v in violations:
            print(f"  • {v}")
        return 1

    print(f"{LOG_PREFIX} All registered IDs resolve. No foreign references found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
