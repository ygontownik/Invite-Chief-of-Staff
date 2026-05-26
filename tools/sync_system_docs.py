#!/usr/bin/env python3
"""
sync_system_docs.py — Mirror local narrative system docs to Drive gdocs.

Pushes content of local Markdown files to their corresponding Drive gdocs
registered in drive-docs.yaml reference_docs section. Uses Deal Sync Writer
(setContent edit-in-place per invariant I11) so the gdoc IDs stay stable —
existing project-instructions and shared links continue to work.

The 4 mirrored docs (drive-docs.yaml reference_docs):
  - readme              ← ~/dashboards/docs/README.md
  - system_reference    ← ~/dashboards/docs/SYSTEM-REFERENCE.md
  - user_manual         ← ~/dashboards/docs/USER-MANUAL.md
  - skills_catalog      ← ~/dashboards/docs/SKILLS-CATALOG.md

Usage:
  python3 sync_system_docs.py                    # dry-run (default)
  python3 sync_system_docs.py --apply            # push changes only for files
                                                   newer than tracker
  python3 sync_system_docs.py --apply --force    # push all unconditionally

Multi-tenant safe: reads drive-docs.yaml via env var / glob discovery.
"""

from __future__ import annotations
import argparse
import glob as _glob
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from coordination import lock, mark_run  # noqa: E402

HOLDER = "sync_system_docs.py"
STATE_PATH = Path.home() / "credentials" / "sync_system_docs_state.json"

# Which reference_docs entries this script mirrors (others are not Markdown).
MIRRORED_KEYS = {"readme", "system_reference", "user_manual", "skills_catalog", "my_skills"}

def _strip_backslash_corruption(content: str, label: str) -> str:
    """Detect and strip backslash corruption.

    Symptom: Markdown files where every `#`, `-`, `*`, `[`, `]` char
    has been prepended with one or more backslashes (backslash-hash, double-backslash-hash, ...).
    This happens when content passes through a JSON-escaping step one or
    more times and the escaped string is written raw to disk rather than
    being unescaped first.  Compounding on each /wrap doubles the count.

    Detection heuristic: if >8% of non-whitespace chars are backslashes,
    assume corruption and strip ALL backslashes.  Real Markdown prose has
    virtually zero backslash chars.
    """
    non_ws = [c for c in content if not c.isspace()]
    if not non_ws:
        return content
    bs_ratio = content.count("\\") / len(non_ws)
    if bs_ratio > 0.08:
        cleaned = content.replace("\\", "")
        print(f"  [corruption-guard] {label}: stripped {content.count(chr(92))} "
              f"backslashes ({bs_ratio:.1%} density) before push")
        return cleaned
    return content




def find_drive_docs() -> Path:
    env = os.environ.get("COS_CONFIG_DIR")
    if env:
        p = Path(env) / "drive-docs.yaml"
        if p.exists():
            return p
    cands = sorted(_glob.glob(str(Path.home() / "cos-pipeline-config-*/drive-docs.yaml")))
    if not cands:
        sys.exit("ERROR: drive-docs.yaml not found.")
    return Path(cands[0])


def find_deal_sync_config() -> Path | None:
    """Locate deal_sync.yaml for the tenant. Returns None if unconfigured."""
    env = os.environ.get("COS_CONFIG_DIR")
    if env:
        p = Path(env) / "config" / "deal_sync.yaml"
        if p.exists():
            return p
    cands = sorted(_glob.glob(str(Path.home() / "cos-pipeline-config-*/config/deal_sync.yaml")))
    return Path(cands[0]) if cands else None


def push_to_drive(file_id: str, content: str, label: str) -> bool:
    """Use Deal Sync Writer (setContent) to overwrite gdoc in place. I11 compliant."""
    import requests
    cfg_path = find_deal_sync_config()
    if not cfg_path:
        print(f"  {label}: SKIP — deal_sync.yaml not found")
        return False
    cfg = yaml.safe_load(cfg_path.read_text())
    url, secret = cfg.get("url"), cfg.get("secret")
    if not url or not secret:
        print(f"  {label}: SKIP — deal_sync.yaml missing url/secret")
        return False
    try:
        r = requests.post(
            url,
            json={"secret": secret, "fileId": file_id, "content": content},
            timeout=30, allow_redirects=True,
        )
        if r.headers.get("content-type", "").startswith("application/json"):
            result = r.json()
        else:
            result = {"status": "unknown", "raw": r.text[:200]}
        if result.get("status") == "ok":
            print(f"  {label}: ✓ ({result.get('bytes')} bytes)")
            return True
        print(f"  {label}: FAIL — {result.get('message', result)}")
        return False
    except Exception as e:
        print(f"  {label}: ERROR {e}")
        return False


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--apply", action="store_true",
                   help="Push to Drive (default: dry-run)")
    p.add_argument("--force", action="store_true",
                   help="With --apply: push every doc regardless of mtime")
    args = p.parse_args(argv)

    docs = yaml.safe_load(find_drive_docs().read_text())
    refs = docs.get("reference_docs") or {}
    state = load_state()

    targets = []
    for key, entry in refs.items():
        if key not in MIRRORED_KEYS:
            continue
        doc_id = entry.get("doc_id")
        mirror_path = entry.get("mirror_path", "").replace("~", str(Path.home()))
        if not (doc_id and mirror_path):
            print(f"  {key}: SKIP — missing doc_id or mirror_path")
            continue
        path = Path(mirror_path)
        if not path.exists():
            print(f"  {key}: SKIP — local file not found at {mirror_path}")
            continue
        targets.append((key, doc_id, path, entry.get("name", key)))

    if not targets:
        print("No targets found.")
        return 0

    dry = not args.apply
    pushed = 0
    print(f"sync_system_docs.py — {len(targets)} targets, mode={'DRY-RUN' if dry else 'APPLY'}\n")
    with lock("learnings-ledger", HOLDER, ttl_seconds=120, timeout_seconds=60):
        for key, doc_id, path, label in targets:
            mtime = path.stat().st_mtime
            last_mtime = state.get(key, {}).get("mtime", 0)
            if not args.force and mtime <= last_mtime:
                print(f"  {label}: skip (unchanged since last push)")
                continue
            content = _strip_backslash_corruption(path.read_text(), label)
            if dry:
                print(f"  {label}: WOULD PUSH ({len(content)} bytes)")
            else:
                if push_to_drive(doc_id, content, label):
                    state[key] = {"mtime": mtime, "bytes": len(content)}
                    pushed += 1

    if args.apply:
        save_state(state)
        if pushed:
            mark_run(HOLDER)
        print(f"\nDone. Pushed {pushed}/{len(targets)} doc(s).")
    else:
        print("\nDry-run complete. Re-run with --apply to push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
