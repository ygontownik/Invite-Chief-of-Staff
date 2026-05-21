#!/usr/bin/env python3
"""
sync_setup_to_drive.py — Mirror Claude Code setup files to Drive for visibility.

Maintains a Drive folder (_System/_Claude Code Setup/) containing read-only
mirrors of:
  - ~/.claude/commands/*.md            → skills/        (one gdoc per skill)
  - ~/Library/LaunchAgents/com.<principal>.*.plist → launchagents/
  - ~/.claude/CLAUDE.md                → global/CLAUDE.md
  - ~/dashboards/scripts/wrap_auto.sh  → global/wrap_auto.sh
  - ~/dashboards/scripts/load-secrets.sh → global/load-secrets.sh

Source-of-truth is always the local file. Drive copies are READ-ONLY mirrors
for easy visibility from any browser. Edit-in-place per I11 (never recreate
once registered) — state file maps local_path → drive_doc_id.

Run modes:
  python3 sync_setup_to_drive.py            # dry-run: report what would change
  python3 sync_setup_to_drive.py --apply    # actually upload/update Drive
"""
from __future__ import annotations
import argparse, json, os, pickle, sys
from datetime import datetime
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HOME = Path.home()
REGISTRY_PATH = HOME / "cos-pipeline-config-tomac/claude_code_setup_drive_ids.json"
STATE_PATH = HOME / "credentials/setup_drive_sync_state.json"


def _load_launchagent_prefixes() -> list[str]:
    """Return tenant-specific LaunchAgent name prefixes from firm_context.yaml.

    Falls back to a generic ['principal'] if firm_context isn't available
    — this script is run against an already-installed tenant; absent config
    just means nothing to mirror.
    """
    try:
        import yaml as _yaml  # type: ignore
        for _candidate in (
            HOME / "cos-pipeline-config-tomac" / "firm_context.yaml",
            HOME / "dashboards" / "config" / "firm_context.yaml",
        ):
            if _candidate.exists():
                _data = _yaml.safe_load(_candidate.read_text()) or {}
                _prefixes = _data.get("launchagent_prefixes")
                if isinstance(_prefixes, list) and _prefixes:
                    return [str(p).strip() for p in _prefixes if p]
    except Exception:
        pass
    return ["principal"]


_LAUNCHAGENT_PREFIXES = _load_launchagent_prefixes()


# Source -> target-subfolder-key mapping
SOURCES = [
    # Personal skills
    {"glob": HOME / ".claude/commands/*.md",
     "subfolder": "skills",
     "name_pattern": "claude-code skill: {stem}"},
    # LaunchAgents — name prefixes loaded from firm_context.yaml so this
    # public-repo script carries no hardcoded tenant identifiers.
    *[
        {"glob": HOME / f"Library/LaunchAgents/com.{_prefix}.*.plist",
         "subfolder": "launchagents",
         "name_pattern": "launchagent: {stem}"}
        for _prefix in _LAUNCHAGENT_PREFIXES
    ],
    # Globals
    {"glob": HOME / ".claude/CLAUDE.md",
     "subfolder": "global",
     "name_pattern": "claude-code global: CLAUDE.md"},
    {"glob": HOME / "dashboards/scripts/wrap_auto.sh",
     "subfolder": "global",
     "name_pattern": "claude-code global: wrap_auto.sh"},
    {"glob": HOME / "dashboards/scripts/load-secrets.sh",
     "subfolder": "global",
     "name_pattern": "claude-code global: load-secrets.sh"},
]


def _expand_sources():
    """Expand glob patterns into concrete file paths."""
    # Exclude ephemeral LaunchAgents that get auto-created per calendar event
    # or per webinar — these come and go, no value in mirroring each one.
    EXCLUDE_NAME_PATTERNS = [
        "recorder.start.gcal.",
        "recorder.stop.gcal.",
        ".webinar.",
    ]
    out = []
    for src in SOURCES:
        p = src["glob"]
        if "*" in str(p):
            parent = p.parent
            pattern = p.name
            if parent.exists():
                for f in sorted(parent.glob(pattern)):
                    if not f.is_file():
                        continue
                    if any(pat in f.name for pat in EXCLUDE_NAME_PATTERNS):
                        continue
                    out.append({**src, "path": f})
        else:
            if p.exists():
                out.append({**src, "path": p})
    return out


def _get_drive_and_docs():
    with open(HOME / "credentials/gdrive_token.pickle", "rb") as f:
        creds = pickle.load(f)
    return build("drive", "v3", credentials=creds), build("docs", "v1", credentials=creds)


def _load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _create_gdoc(drive, docs, parent_id, name, content):
    """Create a new Google Doc with the given content."""
    # Step 1: create empty doc
    doc = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.document",
              "parents": [parent_id]},
        fields="id"
    ).execute()
    doc_id = doc["id"]
    # Step 2: insert content via Docs API
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{
            "insertText": {"location": {"index": 1}, "text": content}
        }]}
    ).execute()
    return doc_id


def _update_gdoc(docs, doc_id, content):
    """Overwrite content of an existing Google Doc (edit-in-place per I11)."""
    # Get current length
    doc = docs.documents().get(documentId=doc_id, fields="body").execute()
    body = doc.get("body", {}).get("content", [])
    end_index = 1
    for el in body:
        if el.get("endIndex"):
            end_index = max(end_index, el["endIndex"])
    requests = []
    # Delete all existing content (skip the section break at index 0; delete [1, end-1))
    if end_index > 2:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end_index - 1}
            }
        })
    # Insert new content
    requests.append({
        "insertText": {"location": {"index": 1}, "text": content}
    })
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
    return doc_id


def sync(apply=False):
    if not REGISTRY_PATH.exists():
        sys.exit(f"ERROR: {REGISTRY_PATH} not found. Run folder-creation step first.")
    folders = json.loads(REGISTRY_PATH.read_text())
    state = _load_state()
    drive, docs = _get_drive_and_docs()

    sources = _expand_sources()
    print(f"Found {len(sources)} source files to mirror")
    print(f"Target folders:")
    for k, v in folders.items():
        if k != "setup_root":
            print(f"  {k}: {v}")
    print()

    new_count = upd_count = skip_count = err_count = 0
    for src in sources:
        path: Path = src["path"]
        subfolder_key = src["subfolder"]
        parent_id = folders[subfolder_key]
        name = src["name_pattern"].format(stem=path.stem if path.suffix != ".sh" else path.name)
        # Read content as text (skill .md / plist XML / shell scripts — all text)
        try:
            content = path.read_text(errors="replace")
        except Exception as e:
            print(f"  ERROR read {path}: {e}")
            err_count += 1
            continue

        # State key = full local path
        key = str(path)
        existing = state.get(key, {})
        existing_doc_id = existing.get("doc_id")
        existing_mtime = existing.get("mtime")
        cur_mtime = path.stat().st_mtime

        if existing_doc_id:
            # Verify the gdoc still exists
            try:
                drive.files().get(fileId=existing_doc_id, fields="id,trashed").execute()
                if existing_mtime and cur_mtime <= existing_mtime + 1:
                    skip_count += 1
                    continue
                # Update content
                if apply:
                    _update_gdoc(docs, existing_doc_id, content)
                    state[key] = {"doc_id": existing_doc_id, "name": name,
                                  "mtime": cur_mtime, "synced_at": datetime.now().isoformat()}
                upd_count += 1
                print(f"  UPDATE: {path.name} -> {existing_doc_id}")
                continue
            except HttpError as e:
                # Existing gdoc gone (trashed?) — recreate
                existing_doc_id = None

        # Create new gdoc
        if apply:
            try:
                doc_id = _create_gdoc(drive, docs, parent_id, name, content)
                state[key] = {"doc_id": doc_id, "name": name,
                              "mtime": cur_mtime, "synced_at": datetime.now().isoformat()}
                new_count += 1
                print(f"  CREATE: {name} -> {doc_id}")
            except Exception as e:
                print(f"  ERROR create {path}: {e}")
                err_count += 1
        else:
            new_count += 1
            print(f"  CREATE (dry): {name}")

    if apply:
        _save_state(state)

    print(f"\nSummary: {new_count} created, {upd_count} updated, {skip_count} skipped, {err_count} errors")
    if not apply:
        print("(Dry run — re-run with --apply to push to Drive)")

    # Also write a TOC file in the setup_root for navigation
    if apply:
        toc_lines = [
            f"# Claude Code Setup — Drive Mirror\n",
            f"_Last synced: {datetime.now().isoformat()}_\n",
            f"\nAll files in this folder are READ-ONLY mirrors of local files. Source of truth is the local path. Edit locally, then re-run sync_setup_to_drive.py.\n",
            f"\n## Source paths\n",
        ]
        for key in sorted(state.keys()):
            v = state[key]
            toc_lines.append(f"- **{v['name']}** ← `{key}` (synced {v['synced_at'][:10]})\n")
        toc_text = "".join(toc_lines)
        # Upload TOC as a gdoc in setup_root
        toc_state = state.get("_toc", {})
        toc_doc_id = toc_state.get("doc_id")
        if toc_doc_id:
            try:
                _update_gdoc(docs, toc_doc_id, toc_text)
            except HttpError:
                toc_doc_id = None
        if not toc_doc_id:
            toc_doc_id = _create_gdoc(drive, docs, folders["setup_root"], "_README — Claude Code Setup", toc_text)
        state["_toc"] = {"doc_id": toc_doc_id, "name": "_README — Claude Code Setup",
                         "mtime": 0, "synced_at": datetime.now().isoformat()}
        _save_state(state)
        print(f"TOC updated: {toc_doc_id}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Actually push to Drive (default: dry-run)")
    args = p.parse_args()
    sync(apply=args.apply)
