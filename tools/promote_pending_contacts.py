#!/usr/bin/env python3
"""promote_pending_contacts.py — move verified entries from pending → canonical

Workflow (per L0050 — never write confabulated fields to canonical):
  1. Claude (or any auto-extractor) writes a NEW contact entry with one or
     more inferred fields to known-aliases-pending.yaml. The `_inferred`
     field lists which keys still need user confirmation.
  2. The principal reviews the entry, edits the inferred fields to verified values,
     and removes the `_inferred` array entry-by-entry.
  3. This script promotes entries that have NO remaining `_inferred` items
     from pending → canonical known-aliases.yaml.

Usage:
    python3 promote_pending_contacts.py --list            # show pending entries + status
    python3 promote_pending_contacts.py "Philip Krim"     # promote one entry by name
    python3 promote_pending_contacts.py --all-verified    # promote all entries with empty _inferred
    python3 promote_pending_contacts.py --reject NAME     # delete a pending entry (e.g. spam/wrong)
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing PyYAML. Install: pip install pyyaml")
    sys.exit(1)

CONFIG_DIR = Path.home() / "cos-pipeline-config-tomac"
PENDING = CONFIG_DIR / "known-aliases-pending.yaml"
CANONICAL = CONFIG_DIR / "known-aliases.yaml"


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        print(f"error: failed to parse {path}: {e}", file=sys.stderr)
        sys.exit(2)


def save_yaml(path: Path, data: dict) -> None:
    """Write YAML preserving the file's existing top-matter (comments + header)."""
    tmp = path.with_suffix(".tmp")
    # Best-effort: keep the file's existing header comments by reading and
    # splicing. If the file has no header marker, we just write the data fresh.
    header = ""
    if path.exists():
        raw = path.read_text()
        # Header = lines up to (but not including) the first non-comment non-blank line.
        out_lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line)
                continue
            break
        if out_lines:
            header = "\n".join(out_lines) + "\n\n"
    tmp.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                                            default_flow_style=False))
    tmp.replace(path)


def list_pending() -> None:
    pending = load_yaml(PENDING).get("pending") or {}
    if not pending:
        print("No pending contacts.")
        return
    print(f"{len(pending)} pending contact(s):\n")
    print(f"  {'NAME':<30} {'STATUS':<14} INFERRED FIELDS")
    print("  " + "-" * 78)
    for name, entry in pending.items():
        if not isinstance(entry, dict):
            print(f"  {name:<30} malformed     -")
            continue
        inferred = entry.get("_inferred") or []
        status = "ready" if not inferred else f"needs {len(inferred)}"
        infs = ", ".join(inferred) if inferred else "—"
        print(f"  {name:<30} {status:<14} {infs}")


def promote(target_name: str | None, all_verified: bool) -> int:
    pending_doc = load_yaml(PENDING)
    pending = pending_doc.get("pending") or {}
    canon = load_yaml(CANONICAL)
    canon_people = canon.setdefault("people", {})

    to_promote = []
    if all_verified:
        for name, entry in pending.items():
            if isinstance(entry, dict) and not (entry.get("_inferred") or []):
                to_promote.append(name)
    elif target_name:
        if target_name not in pending:
            print(f"error: {target_name!r} not in pending", file=sys.stderr)
            return 1
        entry = pending[target_name]
        if not isinstance(entry, dict):
            print(f"error: {target_name!r} is malformed", file=sys.stderr)
            return 1
        if entry.get("_inferred"):
            print(f"error: {target_name!r} still has inferred fields: "
                  f"{entry['_inferred']}. Confirm them in {PENDING} first.",
                  file=sys.stderr)
            return 1
        to_promote.append(target_name)

    if not to_promote:
        print("Nothing to promote.")
        return 0

    for name in to_promote:
        entry = dict(pending[name])
        entry.pop("_inferred", None)
        # Keep _source as audit trail — useful when reviewing later why a
        # value was set the way it was.
        if name in canon_people:
            print(f"warn: {name!r} already in canonical; merging (canonical wins)")
            existing = canon_people[name]
            for k, v in entry.items():
                if k not in existing:
                    existing[k] = v
        else:
            canon_people[name] = entry
        print(f"  promoted: {name}")
        del pending[name]

    pending_doc["pending"] = pending
    save_yaml(PENDING, pending_doc)
    save_yaml(CANONICAL, canon)
    print(f"\n{len(to_promote)} promoted → {CANONICAL}")
    return 0


def reject(target_name: str) -> int:
    pending_doc = load_yaml(PENDING)
    pending = pending_doc.get("pending") or {}
    if target_name not in pending:
        print(f"error: {target_name!r} not in pending", file=sys.stderr)
        return 1
    del pending[target_name]
    pending_doc["pending"] = pending
    save_yaml(PENDING, pending_doc)
    print(f"Rejected (removed): {target_name}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", nargs="?", help="Promote this single contact by name")
    p.add_argument("--list", action="store_true", help="Show pending entries + status")
    p.add_argument("--all-verified", action="store_true",
                   help="Promote every entry whose _inferred list is empty")
    p.add_argument("--reject", metavar="NAME",
                   help="Delete a pending entry (spam / wrong contact)")
    args = p.parse_args()

    if args.list:
        list_pending(); return 0
    if args.reject:
        return reject(args.reject)
    if args.all_verified or args.name:
        return promote(args.name, args.all_verified)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
