#!/usr/bin/env python3
"""
dash-state-hook.py — Claude Code Stop hook (tenant-agnostic).

Fires at the end of every Claude Code turn. Orchestrates auto-pipelines:
  - Patches Dashboard State doc in Drive (every 30m, when commits roll)
  - Pulls per-deal dashboard_entry.json from Drive (every 2h)
  - Spawns headless `claude -p /deal-sync` (every 2h)
  - Mirrors 4 reference docs Drive -> tenant config repo (every 2h)
  - Spawns headless `claude -p /capture-deal-chats all` (every 4h)
  - Spawns headless `claude -p /refresh-project-instructions all`
    (every 24h, gated by ref-docs-changed)
  - Scans Claude Code transcripts for ---DEAL-INTEL--- blocks (every fire)
  - Regenerates SYSTEM-MAP.md (every fire, config-driven)

Tenant-agnostic. Reads paths from environment + filesystem discovery:
  - COS_DATA_DIR (default: ~/dashboards) — where data + logs + state live
  - COS_CONFIG_DIR or glob ~/cos-pipeline-config-* — tenant config repo
  - CLAUDE_BIN (default: ~/.local/bin/claude)
"""

import os
import sys
import subprocess
import pickle
import re
import glob
from datetime import datetime
from pathlib import Path

# ── Tenant-agnostic path discovery ────────────────────────────────────────────
COS_DATA_DIR = Path(os.environ.get("COS_DATA_DIR", os.path.expanduser("~/dashboards")))


def _find_config_dir():
    """Discover the active tenant config dir.
    Priority:
      1. $COS_CONFIG_DIR env var
      2. Glob ~/cos-pipeline-config-* — prefer the candidate that has
         drive-docs.yaml (the hook's canonical config artifact). Avoids
         picking up template/stub config dirs.
      3. Legacy ~/cos-pipeline-config/
    """
    if os.environ.get("COS_CONFIG_DIR"):
        return Path(os.environ["COS_CONFIG_DIR"])
    matches = sorted(glob.glob(os.path.expanduser("~/cos-pipeline-config-*")))
    populated = [m for m in matches if (Path(m) / "drive-docs.yaml").exists()]
    if populated:
        return Path(populated[0])
    if matches:
        return Path(matches[0])
    return Path(os.path.expanduser("~/cos-pipeline-config"))


COS_CONFIG_DIR = _find_config_dir()
COS_PIPELINE_DIR = Path(os.path.expanduser("~/cos-pipeline"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))

# ── Config ────────────────────────────────────────────────────────────────────
DOC_ID = "1TWhl8GcFO2l3mD7jCpaEQk8fGRurW1YD-2v529DgQ3Q"
CREDS_PATH = os.path.expanduser("~/credentials/gdrive_token.pickle")
LOCK_FILE = Path("/tmp/dash-state-hook.last")
COMMIT_LOCK_FILE = Path("/tmp/dash-state-hook.commits")

DEAL_ENTRY_SYNC_LOCK = Path("/tmp/dash-state-hook-deals.last")
DEAL_ENTRY_SYNC_INTERVAL = 7200   # 2 hours
DEAL_ENTRY_SYNC_SCRIPT = str(COS_PIPELINE_DIR / "tools" / "sync_deals_from_drive.py")

# /deal-sync extraction (Claude Code session — subscription-backed, never raw API)
DEAL_EXTRACT_SYNC_LOCK = Path("/tmp/dash-state-hook-extract.last")
DEAL_EXTRACT_SYNC_INTERVAL = 600   # 10 min minimum gap between spawns
DEAL_EXTRACT_TRIGGER_FLAG = Path("/tmp/dash-state-hook-extract.trigger")
DEAL_EXTRACT_LOG = str(COS_DATA_DIR / "logs" / "deal_sync.log")

# Reference-doc mirror (Drive -> tenant config repo, git-tracked)
REF_DOC_SYNC_LOCK = Path("/tmp/dash-state-hook-refdocs.last")
REF_DOC_SYNC_INTERVAL = 7200  # 2 hours
REF_DOC_STATE_PATH = COS_DATA_DIR / "data" / "reference_docs_state.json"
REF_DOC_REPO = str(COS_CONFIG_DIR)
DRIVE_DOCS_YAML = str(COS_DATA_DIR / "config" / "drive-docs.yaml")

# Project-instructions auto-refresh (claude.ai project Instructions paste via Chrome MCP)
# Min cadence 24h; only fires when ref docs changed since last sync.
PROJECT_INST_SYNC_LOCK = Path("/tmp/dash-state-hook-project-inst.last")
PROJECT_INST_SYNC_INTERVAL = 86400  # 24 hours minimum between fires
PROJECT_INST_LOG = str(COS_DATA_DIR / "logs" / "project_inst_sync.log")

# SYSTEM-MAP.md regen (config-driven; cheap). Public location preferred;
# falls back to legacy private location for unmigrated tenants.
_smg_public = COS_PIPELINE_DIR / "tools" / "generate-system-map.py"
_smg_legacy = COS_DATA_DIR / "scripts" / "generate-system-map.py"
SYSTEM_MAP_GENERATOR = str(_smg_public if _smg_public.exists() else _smg_legacy)
SYSTEM_MAP_OUTPUT = COS_DATA_DIR / "docs" / "SYSTEM-MAP.md"
SYSTEM_MAP_INPUTS = [
    COS_DATA_DIR / "config" / "drive-docs.yaml",
    COS_DATA_DIR / "config" / "schedule.yaml",
    COS_DATA_DIR / "config" / "dashboard-tiles.yaml",
]

# DEAL-INTEL block scan from Claude Code transcripts (cheap; runs every fire)
INTEL_CAPTURE_SCRIPT = str(COS_PIPELINE_DIR / "tools" / "intel_capture.py")

# actions.md local→Drive mirror state tracker
ACTIONS_MIRROR_STATE_PATH = COS_DATA_DIR / "data" / "actions_mirror_state.json"

# claude.ai project chat scrape — runs /capture-deal-chats headless every 4h
CHAT_CAPTURE_LOCK = Path("/tmp/dash-state-hook-chat-capture.last")
CHAT_CAPTURE_INTERVAL = 14400  # 4 hours
CHAT_CAPTURE_LOG = str(COS_DATA_DIR / "logs" / "chat_capture.log")

RATE_LIMIT_SECONDS = 1800   # 30 min — max write frequency
HEARTBEAT_SECONDS  = 3600   # 60 min — write even with no commits (heartbeat)

# Repos to scan for recent commits. Hook skips any that don't exist on disk.
REPOS = [
    (str(COS_PIPELINE_DIR), "cos-pipeline"),
    (str(COS_CONFIG_DIR),   "config"),
    (str(COS_DATA_DIR),     "data"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def seconds_since_last_run():
    if not LOCK_FILE.exists():
        return float("inf")
    try:
        last = float(LOCK_FILE.read_text().strip())
        return datetime.now().timestamp() - last
    except Exception:
        return float("inf")


def get_recent_commits(since_hours=2):
    """Return list of (label, commit_line) tuples from last N hours."""
    results = []
    for path, label in REPOS:
        if not os.path.isdir(path):
            continue
        try:
            out = subprocess.run(
                ["git", "log", f"--since={since_hours} hours ago",
                 "--format=%h %as %s", "--no-merges"],
                capture_output=True, text=True, cwd=path, timeout=5
            ).stdout.strip()
            commits = [l for l in out.split("\n") if l.strip()]
            if commits:
                results.append((label, commits[:12]))
        except Exception:
            pass
    return results


def commits_changed(new_commits):
    """Return True if the commit set differs from last write."""
    key = repr(new_commits)
    if not COMMIT_LOCK_FILE.exists():
        return True
    try:
        return COMMIT_LOCK_FILE.read_text().strip() != key
    except Exception:
        return True


def get_docs_service():
    with open(CREDS_PATH, "rb") as f:
        creds = pickle.load(f)
    if hasattr(creds, "expired") and creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(CREDS_PATH, "wb") as f:
            pickle.dump(creds, f)
    from googleapiclient.discovery import build
    return build("docs", "v1", credentials=creds)


def read_doc_text(service):
    doc = service.documents().get(documentId=DOC_ID).execute()
    text = ""
    for elem in doc["body"]["content"]:
        if "paragraph" in elem:
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    text += pe["textRun"]["content"]
    return text


def patch_doc_text(current_text, commit_groups, now_str):
    """
    Patch the Dashboard State doc text:
      - Update 'Last Updated:' line
      - Update 'Updated By:' line
      - Replace everything between the RECENT CHANGES header line
        and the next '====' separator with fresh commit data.
    Everything else (CURRENT STATE, DEFERRED ITEMS, NEXT PRIORITIES,
    QUICK COMMANDS) is preserved verbatim.
    """
    lines = current_text.split("\n")
    out = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Patch header lines
        if line.startswith("Last Updated:"):
            out.append(f"Last Updated: {now_str}")
            i += 1
            continue
        if line.startswith("Updated By:"):
            out.append("Updated By: Claude Code Stop hook (auto)")
            i += 1
            continue

        # Replace RECENT CHANGES section content
        if "RECENT CHANGES" in line:
            out.append(line)
            i += 1
            # Skip existing recent-changes content until next === separator
            while i < len(lines) and not lines[i].startswith("==="):
                i += 1
            # Write fresh commits
            if commit_groups:
                for label, commits in commit_groups:
                    out.append(f"\n{label}")
                    for c in commits:
                        out.append(f"  {c}")
            else:
                out.append("\n(no new commits in last 2 hours)")
            continue

        out.append(line)
        i += 1

    return "\n".join(out)


def write_doc_text(service, new_text):
    doc = service.documents().get(documentId=DOC_ID).execute()
    end_index = doc["body"]["content"][-1]["endIndex"] - 1
    requests = []
    if end_index > 1:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end_index}
            }
        })
    requests.append({
        "insertText": {
            "location": {"index": 1},
            "text": new_text
        }
    })
    service.documents().batchUpdate(
        documentId=DOC_ID,
        body={"requests": requests}
    ).execute()


# ── Deal entry sync (Drive dashboard_entry.json -> compiled) ──────────────────

def seconds_since_deal_entry_sync():
    if not DEAL_ENTRY_SYNC_LOCK.exists():
        return float("inf")
    try:
        return datetime.now().timestamp() - float(DEAL_ENTRY_SYNC_LOCK.read_text().strip())
    except Exception:
        return float("inf")


def run_deal_entry_sync():
    """Pull dashboard_entry.json for all deals from Drive → local compiled data."""
    if not os.path.exists(DEAL_ENTRY_SYNC_SCRIPT):
        return False
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", DEAL_ENTRY_SYNC_SCRIPT],
            capture_output=True, text=True, timeout=60
        )
        DEAL_ENTRY_SYNC_LOCK.write_text(str(datetime.now().timestamp()))
        output = result.stdout.strip()
        if output and "Nothing synced" not in output:
            print(f"[dash-state-hook] Deal entry sync: {output.splitlines()[-1]}")
        return True
    except Exception as e:
        print(f"[dash-state-hook] Deal entry sync error (non-fatal): {e}", file=sys.stderr)
        return False


def run_deal_intel_index():
    """Index newly captured deal log.json entries into ChromaDB deal_intel collection.
    Runs after run_deal_entry_sync() on the same 2h cadence — keeps deal_intel
    collection fresh for /knowledge-query --collection deal_intel and load_pipeline_context()."""
    indexer = os.path.expanduser("~/cos-pipeline/tools/deal_intel_indexer.py")
    if not os.path.exists(indexer):
        return False
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", indexer],
            capture_output=True, text=True, timeout=120
        )
        output = result.stdout.strip()
        # Only log if something was actually indexed
        for line in output.splitlines():
            if "indexed" in line and "0 indexed" not in line:
                print(f"[dash-state-hook] Deal intel index: {line}")
                break
        return True
    except Exception as e:
        print(f"[dash-state-hook] Deal intel index error (non-fatal): {e}", file=sys.stderr)
        return False


# ── Deal extract sync (/deal-sync slash command in headless session) ──────────

def seconds_since_deal_extract_sync():
    if not DEAL_EXTRACT_SYNC_LOCK.exists():
        return float("inf")
    try:
        return datetime.now().timestamp() - float(DEAL_EXTRACT_SYNC_LOCK.read_text().strip())
    except Exception:
        return float("inf")


def any_deal_has_new_files():
    """Fast pre-check: returns True if at least one deal has new source
    files since its last_run. Saves the cost of booting a headless Claude
    session for an idle cycle."""
    helpers = os.path.expanduser("~/cos-pipeline/tools/deal_extract_helpers.py")
    if not os.path.exists(helpers):
        return True  # fail open — let the slash command decide
    try:
        listed = subprocess.run(
            ["/opt/homebrew/bin/python3", helpers, "list-deals"],
            capture_output=True, text=True, timeout=20,
        )
        import json as _json
        deals = _json.loads(listed.stdout)
        for d in deals:
            r = subprocess.run(
                ["/opt/homebrew/bin/python3", helpers, "list-new-files", d["deal_id"]],
                capture_output=True, text=True, timeout=20,
            )
            if r.stdout.strip() and r.stdout.strip() != "[]":
                return True
        return False
    except Exception:
        return True  # fail open


def run_deal_extract_sync():
    """Spawn a headless Claude Code session that runs /deal-sync.

    AI work happens INSIDE that session (subscription-backed). The
    DEAL_SYNC_CHILD env var prevents the spawned session's own Stop
    hook from re-triggering this code → infinite loop.

    Fast-skip when no deal has new files — avoids session-boot cost on
    idle cycles.
    """
    if not os.path.exists(CLAUDE_BIN):
        print(f"[dash-state-hook] claude CLI not found at {CLAUDE_BIN} — skip extract sync", file=sys.stderr)
        return False

    if not any_deal_has_new_files():
        # Bump lock so we don't re-check for another DEAL_EXTRACT_SYNC_INTERVAL.
        DEAL_EXTRACT_SYNC_LOCK.write_text(str(datetime.now().timestamp()))
        return True  # idle is success

    env = os.environ.copy()
    env["DEAL_SYNC_CHILD"] = "1"
    os.makedirs(os.path.dirname(DEAL_EXTRACT_LOG), exist_ok=True)
    started = datetime.now().isoformat()
    try:
        with open(DEAL_EXTRACT_LOG, "a") as logf:
            logf.write(f"\n=== {started} extract-sync START ===\n")
            result = subprocess.run(
                [
                    CLAUDE_BIN, "-p", "/deal-sync",
                    "--allow-dangerously-skip-permissions",
                ],
                env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                timeout=1800,  # 30 min cap
            )
            ended = datetime.now().isoformat()
            logf.write(f"=== {ended} extract-sync END (exit={result.returncode}) ===\n")
        DEAL_EXTRACT_SYNC_LOCK.write_text(str(datetime.now().timestamp()))
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("[dash-state-hook] deal extract sync timed out (30 min)", file=sys.stderr)
        DEAL_EXTRACT_SYNC_LOCK.write_text(str(datetime.now().timestamp()))
        return False
    except Exception as e:
        print(f"[dash-state-hook] deal extract sync error (non-fatal): {e}", file=sys.stderr)
        return False


# ── Reference docs Drive → git mirror ────────────────────────────────────────

def seconds_since_ref_doc_sync():
    if not REF_DOC_SYNC_LOCK.exists():
        return float("inf")
    try:
        return datetime.now().timestamp() - float(REF_DOC_SYNC_LOCK.read_text().strip())
    except Exception:
        return float("inf")


def _load_ref_doc_state():
    if not REF_DOC_STATE_PATH.exists():
        return {}
    try:
        import json as _json
        return _json.loads(REF_DOC_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_ref_doc_state(state):
    import json as _json
    REF_DOC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REF_DOC_STATE_PATH.write_text(_json.dumps(state, indent=2))


def _drive_export_markdown(drive_svc, doc_id):
    """Export a native Google Doc as markdown. Falls back to text/plain
    if markdown export isn't supported."""
    try:
        data = drive_svc.files().export(fileId=doc_id, mimeType="text/markdown").execute()
    except Exception:
        data = drive_svc.files().export(fileId=doc_id, mimeType="text/plain").execute()
    return data.decode("utf-8") if isinstance(data, bytes) else data


def run_reference_docs_sync():
    """For each reference doc, check Drive modifiedTime and snapshot to the
    git mirror_path if it has changed. One commit per changed doc."""
    if not os.path.isdir(REF_DOC_REPO):
        return False
    try:
        import yaml
        cfg = yaml.safe_load(open(DRIVE_DOCS_YAML))
    except Exception as e:
        print(f"[dash-state-hook] ref-doc sync: cannot load drive-docs.yaml — {e}", file=sys.stderr)
        return False

    ref_docs = cfg.get("reference_docs") or {}
    if not ref_docs:
        return False

    try:
        drive_svc = get_drive_service_for_refsync()
    except Exception as e:
        print(f"[dash-state-hook] ref-doc sync: drive auth failed — {e}", file=sys.stderr)
        return False

    state = _load_ref_doc_state()
    changed = []

    for key, entry in ref_docs.items():
        doc_id = entry.get("doc_id")
        mirror_path = entry.get("mirror_path")
        if not (doc_id and mirror_path):
            continue
        mirror_path = os.path.expanduser(mirror_path)
        try:
            meta = drive_svc.files().get(fileId=doc_id, fields="id,name,modifiedTime").execute()
        except Exception as e:
            print(f"[dash-state-hook] ref-doc sync: cannot fetch {key} — {e}", file=sys.stderr)
            continue

        modified = meta["modifiedTime"]
        if state.get(doc_id, {}).get("modifiedTime") == modified and os.path.exists(mirror_path):
            continue  # unchanged + file present

        try:
            text = _drive_export_markdown(drive_svc, doc_id)
        except Exception as e:
            print(f"[dash-state-hook] ref-doc sync: cannot export {key} — {e}", file=sys.stderr)
            continue

        os.makedirs(os.path.dirname(mirror_path), exist_ok=True)
        Path(mirror_path).write_text(text)
        state[doc_id] = {"modifiedTime": modified, "name": meta.get("name"), "mirror_path": mirror_path}
        changed.append((key, mirror_path, meta.get("name")))

    if not changed:
        REF_DOC_SYNC_LOCK.write_text(str(datetime.now().timestamp()))
        return True

    _save_ref_doc_state(state)

    # Commit each changed file (one commit per doc for clarity).
    for key, mirror_path, doc_name in changed:
        rel = os.path.relpath(mirror_path, REF_DOC_REPO)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            subprocess.run(["git", "add", rel], cwd=REF_DOC_REPO, timeout=10, check=True)
            # Skip empty commits if git add staged nothing meaningful
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REF_DOC_REPO, timeout=10)
            if diff.returncode == 0:
                continue
            subprocess.run(
                ["git", "commit", "-m", f"auto: {key} sync from Drive [{ts}]"],
                cwd=REF_DOC_REPO, timeout=15, check=True,
                capture_output=True,
            )
            print(f"[dash-state-hook] ref-doc sync: committed {rel}")
        except Exception as e:
            print(f"[dash-state-hook] ref-doc sync: git commit failed for {rel} — {e}", file=sys.stderr)

    REF_DOC_SYNC_LOCK.write_text(str(datetime.now().timestamp()))
    return True


# ── Project-instructions auto-refresh (claude.ai via Chrome MCP) ─────────────

def should_run_project_inst_sync():
    """Fire only when both: (a) at least PROJECT_INST_SYNC_INTERVAL has elapsed
    since last successful run, and (b) reference_docs_state.json was modified
    after that last run (i.e. at least one ref doc changed in Drive)."""
    if not PROJECT_INST_SYNC_LOCK.exists():
        # First-ever run: only fire if ref-docs-state has been initialized.
        return REF_DOC_STATE_PATH.exists()
    try:
        last = float(PROJECT_INST_SYNC_LOCK.read_text().strip())
    except Exception:
        return False
    now = datetime.now().timestamp()
    if now - last < PROJECT_INST_SYNC_INTERVAL:
        return False
    if not REF_DOC_STATE_PATH.exists():
        return False
    return REF_DOC_STATE_PATH.stat().st_mtime > last


def run_project_instructions_sync():
    """Spawn a headless Claude Code session that runs
    `/refresh-project-instructions all`. Reuses the DEAL_SYNC_CHILD env guard
    so the spawned session's Stop hook doesn't re-enter this code.

    Browser automation requires Chrome to be running with the Claude in Chrome
    extension active. If Chrome isn't reachable, the spawned session logs a
    failure and exits cleanly — we'll retry next cycle.
    """
    if not os.path.exists(CLAUDE_BIN):
        return False
    env = os.environ.copy()
    env["DEAL_SYNC_CHILD"] = "1"
    os.makedirs(os.path.dirname(PROJECT_INST_LOG), exist_ok=True)
    started = datetime.now().isoformat()
    try:
        with open(PROJECT_INST_LOG, "a") as logf:
            logf.write(f"\n=== {started} project-inst-sync START ===\n")
            result = subprocess.run(
                [
                    CLAUDE_BIN, "-p", "/refresh-project-instructions all",
                    "--allow-dangerously-skip-permissions",
                ],
                env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                timeout=1800,  # 30 min cap
            )
            ended = datetime.now().isoformat()
            logf.write(f"=== {ended} project-inst-sync END (exit={result.returncode}) ===\n")
        # Only update lock on success — failures will retry next cycle (still
        # gated by 24h cadence + ref-docs-changed trigger).
        if result.returncode == 0:
            PROJECT_INST_SYNC_LOCK.write_text(str(datetime.now().timestamp()))
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("[dash-state-hook] project-inst sync timed out (30 min)", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[dash-state-hook] project-inst sync error (non-fatal): {e}", file=sys.stderr)
        return False


def run_intel_capture_scan():
    """Scan Claude Code transcripts for new DEAL-INTEL blocks and route them
    into per-deal log.json. Cheap — file grep, no LLM, no network. Runs every
    Stop hook fire so deal intel emitted in any CC session reaches log.json
    before the next /deal-sync cycle."""
    if not os.path.exists(INTEL_CAPTURE_SCRIPT):
        return False
    try:
        subprocess.run(
            ["/opt/homebrew/bin/python3", INTEL_CAPTURE_SCRIPT, "scan-claude-code"],
            capture_output=True, text=True, timeout=30,
        )
        return True
    except Exception as e:
        print(f"[dash-state-hook] intel_capture scan failed (non-fatal): {e}", file=sys.stderr)
        return False


def seconds_since_chat_capture():
    if not CHAT_CAPTURE_LOCK.exists():
        return float("inf")
    try:
        return datetime.now().timestamp() - float(CHAT_CAPTURE_LOCK.read_text().strip())
    except Exception:
        return float("inf")


def run_chat_capture():
    """Spawn headless `claude -p /capture-deal-chats all` to scrape claude.ai
    deal-project chats for ---DEAL-INTEL--- blocks (block-only, never full
    transcripts). Routes through intel_capture.py to per-deal log.json.
    Reuses DEAL_SYNC_CHILD env guard."""
    if not os.path.exists(CLAUDE_BIN):
        return False
    env = os.environ.copy()
    env["DEAL_SYNC_CHILD"] = "1"
    os.makedirs(os.path.dirname(CHAT_CAPTURE_LOG), exist_ok=True)
    started = datetime.now().isoformat()
    try:
        with open(CHAT_CAPTURE_LOG, "a") as logf:
            logf.write(f"\n=== {started} chat-capture START ===\n")
            result = subprocess.run(
                [
                    CLAUDE_BIN, "-p", "/capture-deal-chats all",
                    "--allow-dangerously-skip-permissions",
                ],
                env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                timeout=1200,  # 20 min cap
            )
            ended = datetime.now().isoformat()
            logf.write(f"=== {ended} chat-capture END (exit={result.returncode}) ===\n")
        # Lock on every attempt; failures retry next 4h cycle.
        CHAT_CAPTURE_LOCK.write_text(str(datetime.now().timestamp()))
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("[dash-state-hook] chat-capture timed out (20 min)", file=sys.stderr)
        CHAT_CAPTURE_LOCK.write_text(str(datetime.now().timestamp()))
        return False
    except Exception as e:
        print(f"[dash-state-hook] chat-capture error (non-fatal): {e}", file=sys.stderr)
        return False


def maybe_mirror_actions_to_drive():
    """For each registered deal, if its local data/deals/<deal>/actions.md is
    newer than the tracker, push the content to the Drive actions doc
    (deal_docs.<deal>.actions.doc_id in drive-docs.yaml). Cheap: only fires
    on real local edits. Idempotent."""
    try:
        import yaml as _yaml, json as _json
        cfg = _yaml.safe_load(open(DRIVE_DOCS_YAML))
    except Exception:
        return False
    deal_docs = (cfg or {}).get("deal_docs") or {}
    if not deal_docs:
        return False

    state = {}
    if ACTIONS_MIRROR_STATE_PATH.exists():
        try:
            state = _json.loads(ACTIONS_MIRROR_STATE_PATH.read_text())
        except Exception:
            state = {}

    pushed = 0
    for deal_id, entry in deal_docs.items():
        actions_id = (entry.get("actions") or {}).get("doc_id")
        if not actions_id:
            continue
        local_path = COS_DATA_DIR / "data" / "deals" / deal_id / "actions.md"
        if not local_path.exists():
            continue
        mtime = local_path.stat().st_mtime
        if state.get(deal_id) == mtime:
            continue  # no change since last mirror
        try:
            from googleapiclient.http import MediaInMemoryUpload
            svc = get_drive_service_for_refsync()
            content = local_path.read_text()
            media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
            svc.files().update(fileId=actions_id, media_body=media).execute()
            state[deal_id] = mtime
            pushed += 1
        except Exception as e:
            print(f"[dash-state-hook] actions.md mirror failed for {deal_id}: {e}", file=sys.stderr)

    if pushed:
        ACTIONS_MIRROR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ACTIONS_MIRROR_STATE_PATH.write_text(_json.dumps(state, indent=2))
        print(f"[dash-state-hook] mirrored {pushed} actions.md → Drive")
    return pushed > 0


def maybe_regen_system_map():
    """Regenerate SYSTEM-MAP.md if any of its source configs are newer than
    the map. Cheap (no LLM, no network). Runs at end of every hook fire."""
    if not os.path.exists(SYSTEM_MAP_GENERATOR):
        return False
    map_mtime = SYSTEM_MAP_OUTPUT.stat().st_mtime if SYSTEM_MAP_OUTPUT.exists() else 0
    for src in SYSTEM_MAP_INPUTS:
        if src.exists() and src.stat().st_mtime > map_mtime:
            try:
                subprocess.run(
                    ["/opt/homebrew/bin/python3", SYSTEM_MAP_GENERATOR],
                    capture_output=True, text=True, timeout=20,
                )
                return True
            except Exception as e:
                print(f"[dash-state-hook] system-map regen failed: {e}", file=sys.stderr)
                return False
    return False


def get_drive_service_for_refsync():
    """Same creds as get_docs_service() but Drive scope."""
    with open(CREDS_PATH, "rb") as f:
        creds = pickle.load(f)
    if hasattr(creds, "expired") and creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(CREDS_PATH, "wb") as f:
            pickle.dump(creds, f)
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Recursion guard: this hook fires at the end of every Claude Code turn.
    # When run_deal_extract_sync() spawns a child `claude -p /deal-sync` session,
    # that child fires its own Stop hook on exit. Without this guard, that would
    # re-enter run_deal_extract_sync(), which would spawn another child, ad
    # infinitum. The DEAL_SYNC_CHILD env var is set by run_deal_extract_sync()
    # before exec'ing claude.
    if os.environ.get("DEAL_SYNC_CHILD") == "1":
        return 0

    elapsed = seconds_since_last_run()

    # Deal entry sync — pulls per-deal dashboard_entry.json from Drive (every 2h)
    if seconds_since_deal_entry_sync() >= DEAL_ENTRY_SYNC_INTERVAL:
        run_deal_entry_sync()
        run_deal_intel_index()

    # Deal extract sync — runs /deal-sync slash command in a headless Claude
    # Code session. Reactive: fires whenever any deal has new files OR another
    # pipeline drops a trigger flag (e.g. transcript hook routing a new file
    # into a deal folder). 10 min minimum gap between spawns to avoid
    # thrashing during bursts. The any_deal_has_new_files() short-circuit
    # makes idle cycles cost ~0.
    if seconds_since_deal_extract_sync() >= DEAL_EXTRACT_SYNC_INTERVAL or DEAL_EXTRACT_TRIGGER_FLAG.exists():
        if DEAL_EXTRACT_TRIGGER_FLAG.exists():
            try:
                DEAL_EXTRACT_TRIGGER_FLAG.unlink()
            except Exception:
                pass
        run_deal_extract_sync()

    # Reference-docs Drive → git mirror — snapshots TCIP firm/personal context
    # docs into ~/cos-pipeline-config-tomac/ on Drive modifiedTime change.
    if seconds_since_ref_doc_sync() >= REF_DOC_SYNC_INTERVAL:
        run_reference_docs_sync()

    # Project-instructions sync — auto-refreshes claude.ai project Instructions
    # for all 6 deals via Chrome MCP browser automation. Fires at most once per
    # 24h, AND only when at least one ref doc changed in Drive since the last
    # successful sync. Idle days = zero browser activity.
    if should_run_project_inst_sync():
        run_project_instructions_sync()

    # Chat-capture — every 4h, scrape claude.ai deal-project chats for
    # ---DEAL-INTEL--- blocks. Block-only (privacy + cost). Routes through
    # intel_capture.py to per-deal log.json. /deal-sync folds them in next cycle.
    if seconds_since_chat_capture() >= CHAT_CAPTURE_INTERVAL:
        run_chat_capture()

    # DEAL-INTEL capture from Claude Code transcripts — cheap (file grep).
    # Routes any ---DEAL-INTEL--- blocks emitted during CC sessions into the
    # corresponding deal's log.json before next /deal-sync cycle.
    run_intel_capture_scan()

    # actions.md local→Drive mirror — pushes per-deal actions.md to its Drive
    # _Claude Context/ doc whenever the local file mtime is newer than the
    # tracker. Local is canonical (compile-dashboard reads it); Drive copy is
    # the Claude-readable mirror.
    maybe_mirror_actions_to_drive()

    # SYSTEM-MAP.md regen — cheap; only runs when source configs newer than map.
    maybe_regen_system_map()

    # Hard rate limit — never write more than once per 30 min
    if elapsed < RATE_LIMIT_SECONDS:
        return 0

    commit_groups = get_recent_commits(since_hours=2)
    has_new_commits = bool(commit_groups) and commits_changed(commit_groups)
    needs_heartbeat = elapsed >= HEARTBEAT_SECONDS

    if not has_new_commits and not needs_heartbeat:
        return 0

    # Write to Drive
    try:
        service = get_docs_service()
        current_text = read_doc_text(service)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_text = patch_doc_text(current_text, commit_groups, now_str)
        write_doc_text(service, new_text)

        # Update lock files
        LOCK_FILE.write_text(str(datetime.now().timestamp()))
        if commit_groups:
            COMMIT_LOCK_FILE.write_text(repr(commit_groups))

        reason = "new commits" if has_new_commits else "heartbeat"
        print(f"[dash-state-hook] Updated Dashboard State doc ({reason}) at {now_str}")

    except Exception as e:
        # Never block Claude Code — silent failure
        print(f"[dash-state-hook] Error (non-fatal): {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
