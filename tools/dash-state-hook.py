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
from datetime import datetime, timezone
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

# Artifact pull — Chrome MCP walks each deal's claude.ai project, downloads new
# artifacts to ~/Downloads; local_file_router.py (30s daemon) routes them.
ARTIFACT_PULL_LOCK     = Path("/tmp/dash-state-hook-artifact-pull.last")
ARTIFACT_PULL_INTERVAL = 14400  # 4 hours (matches CHAT_CAPTURE_INTERVAL)
ARTIFACT_PULL_LOG      = str(COS_DATA_DIR / "logs" / "artifact_pull.log")
ARTIFACT_PULL_STATE    = Path(os.path.expanduser("~/credentials/processed_artifacts.json"))

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


def seconds_since_artifact_pull() -> float:
    if not ARTIFACT_PULL_LOCK.exists():
        return float("inf")
    try:
        return datetime.now().timestamp() - float(ARTIFACT_PULL_LOCK.read_text().strip())
    except Exception:
        return float("inf")


def _load_artifact_state() -> dict:
    if ARTIFACT_PULL_STATE.exists():
        try:
            import json as _json
            return _json.loads(ARTIFACT_PULL_STATE.read_text())
        except Exception:
            pass
    return {}


def _save_artifact_state(state: dict) -> None:
    import json as _json
    # Bracket the write with a coordination lock so concurrent runs don't race.
    try:
        _tool_dir = os.path.dirname(os.path.abspath(__file__))
        if _tool_dir not in sys.path:
            sys.path.insert(0, _tool_dir)
        from coordination import lock as _coord_lock
        def _write():
            ARTIFACT_PULL_STATE.parent.mkdir(parents=True, exist_ok=True)
            tmp = ARTIFACT_PULL_STATE.with_suffix(".tmp")
            tmp.write_text(_json.dumps(state, indent=2))
            tmp.replace(ARTIFACT_PULL_STATE)
        with _coord_lock("processed-artifacts", holder="dash-state-hook.py", ttl_seconds=30):
            _write()
    except ImportError:
        # coordination.py not importable — write directly (graceful degradation)
        ARTIFACT_PULL_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ARTIFACT_PULL_STATE.with_suffix(".tmp")
        tmp.write_text(_json.dumps(state, indent=2))
        tmp.replace(ARTIFACT_PULL_STATE)


def run_artifact_pull(dry_run: bool = False) -> bool:
    """Walk each registered deal's claude.ai project via Chrome MCP, download any
    new artifacts to ~/Downloads. local_file_router.py (running every 30s) picks
    them up and routes by deal alias — zero extra routing code needed here.

    Uses the same Chrome MCP profile as /capture-deal-chats (ensure_real_chrome.sh
    backs both, keeping the headless Chrome session alive on Desktop 2).

    State: ~/credentials/processed_artifacts.json  (keyed by deal_id)
    Log:   ~/dashboards/logs/artifact_pull.log
    """
    if not DRIVE_DOCS_YAML or not os.path.exists(DRIVE_DOCS_YAML):
        return False

    try:
        import yaml as _yaml
        cfg = _yaml.safe_load(open(DRIVE_DOCS_YAML))
    except Exception as e:
        print(f"[dash-state-hook] artifact-pull: cannot load drive-docs.yaml — {e}",
              file=sys.stderr)
        return False

    deal_docs = (cfg or {}).get("deal_docs") or {}
    targets = [
        (deal_id, entry["project_url"])
        for deal_id, entry in deal_docs.items()
        if entry.get("project_url")
    ]

    if not targets:
        return True  # nothing to pull

    state = _load_artifact_state()

    if dry_run:
        from datetime import datetime as _dt
        print(f"[dash-state-hook] artifact-pull [dry-run]: {len(targets)} deal(s) with project_url:")
        for deal_id, url in targets:
            last = state.get(deal_id, {}).get("last_pull", "never")
            print(f"  {deal_id:20}  last_pull={last}  url={url[:60]}")
        return True

    if not os.path.exists(CLAUDE_BIN):
        print(f"[dash-state-hook] artifact-pull: claude CLI not found at {CLAUDE_BIN} — skip",
              file=sys.stderr)
        return False

    env = os.environ.copy()
    env["DEAL_SYNC_CHILD"] = "1"
    os.makedirs(os.path.dirname(ARTIFACT_PULL_LOG), exist_ok=True)
    started = datetime.now().isoformat()

    try:
        with open(ARTIFACT_PULL_LOG, "a") as logf:
            logf.write(f"\n=== {started} artifact-pull START ({len(targets)} deals) ===\n")
            result = subprocess.run(
                [
                    CLAUDE_BIN, "-p", "/artifact-pull all",
                    "--allow-dangerously-skip-permissions",
                ],
                env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                timeout=1200,  # 20 min cap — same as chat-capture
            )
            ended = datetime.now().isoformat()
            logf.write(f"=== {ended} artifact-pull END (exit={result.returncode}) ===\n")

        ARTIFACT_PULL_LOCK.write_text(str(datetime.now().timestamp()))

        # Record pull timestamp per deal
        now_iso = datetime.now().isoformat()
        for deal_id, _ in targets:
            state.setdefault(deal_id, {})["last_pull"] = now_iso
        _save_artifact_state(state)

        return result.returncode == 0

    except subprocess.TimeoutExpired:
        print("[dash-state-hook] artifact-pull timed out (20 min)", file=sys.stderr)
        ARTIFACT_PULL_LOCK.write_text(str(datetime.now().timestamp()))
        return False
    except Exception as e:
        print(f"[dash-state-hook] artifact-pull error (non-fatal): {e}", file=sys.stderr)
        return False


def run_learning_capture_scan():
    """Scan the most recent Claude Code session transcript for rule-shaped
    statements and queue them for the morning briefing's "Review proposed
    learnings" section. High-precision regex; false-positive rate kept low
    by requiring directive phrasing.

    State: ~/credentials/learning_capture_state.json — {last_scanned_uuid, last_ts}
    Queue: ~/dashboards/data/compiled/proposed-learnings.jsonl (append-only)

    Cheap — pure file I/O + regex. No LLM calls.
    """
    import re as _re
    import json as _json
    import glob as _glob

    projects_dir = Path.home() / ".claude/projects/-Users-ygontownik-Documents-Claude"
    state_path = Path.home() / "credentials/learning_capture_state.json"
    queue_path = Path.home() / "dashboards/data/compiled/proposed-learnings.jsonl"

    if not projects_dir.exists():
        return False

    state = {}
    if state_path.exists():
        try:
            state = _json.loads(state_path.read_text())
        except _json.JSONDecodeError:
            state = {}
    last_scanned = state.get("last_scanned_uuid", "")
    last_ts = state.get("last_ts", 0)

    # Find transcripts modified since last scan
    transcripts = sorted(
        _glob.glob(str(projects_dir / "*.jsonl")),
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True,
    )
    if not transcripts:
        return False

    # Only scan transcripts newer than last_ts (skip everything already processed)
    new_transcripts = [t for t in transcripts if Path(t).stat().st_mtime > last_ts]
    if not new_transcripts:
        return False

    # High-precision directive patterns (low recall by design — better than false positives)
    PATTERNS = [
        _re.compile(r"\bgoing forward[,:]?\s+([^.\n]{20,200}\.)", _re.IGNORECASE),
        _re.compile(r"\bfrom now on[,:]?\s+([^.\n]{20,200}\.)", _re.IGNORECASE),
        _re.compile(r"\bnew rule[,:]?\s+([^.\n]{20,200}\.)", _re.IGNORECASE),
        _re.compile(r"\bnever\s+(do\s+)?([^.\n]{20,200}\.)\s*(?:that|this)?", _re.IGNORECASE),
        _re.compile(r"\balways\s+(do\s+)?([^.\n]{20,200}\.)\s*(?:that|this)?", _re.IGNORECASE),
        _re.compile(r"\bthe rule is[,:]?\s+([^.\n]{20,200}\.)", _re.IGNORECASE),
    ]

    proposed = []
    newest_uuid = last_scanned
    newest_ts = last_ts
    for t_path in new_transcripts[:3]:  # cap at 3 most-recent to bound work
        try:
            content = Path(t_path).read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        session_id = Path(t_path).stem
        t_mtime = Path(t_path).stat().st_mtime
        if t_mtime > newest_ts:
            newest_ts = t_mtime
            newest_uuid = session_id

        # Only look at USER turns (more directive than assistant turns)
        # Simple heuristic: split by JSON lines, pick user-role entries.
        for line in content.splitlines():
            try:
                turn = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if turn.get("type") != "user":
                continue
            msg = turn.get("message", {})
            text = msg.get("content", "")
            if isinstance(text, list):
                text = " ".join(c.get("text", "") for c in text if isinstance(c, dict))
            if not isinstance(text, str) or len(text) < 30:
                continue

            for pattern in PATTERNS:
                for m in pattern.finditer(text):
                    snippet = m.group(0).strip()
                    # Dedup within the session
                    if any(p.get("snippet") == snippet and p.get("session_id") == session_id
                           for p in proposed):
                        continue
                    # Skip if it looks like a question rather than a directive
                    if "?" in snippet[-20:]:
                        continue
                    proposed.append({
                        "session_id": session_id,
                        "ts": t_mtime,
                        "snippet": snippet[:400],
                        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    })

    if proposed:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with open(queue_path, "a") as f:
            for p in proposed:
                f.write(_json.dumps(p) + "\n")
        print(f"[learning-capture] queued {len(proposed)} candidate(s) to {queue_path.name}",
              file=sys.stderr)

    state["last_scanned_uuid"] = newest_uuid
    state["last_ts"] = newest_ts
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_json.dumps(state, indent=2))
    return len(proposed) > 0


def run_skill_telemetry_scan():
    """Scan recent Claude Code transcripts for Skill tool invocations and
    append one record per call to ~/dashboards/data/compiled/skill-telemetry.jsonl.

    Cheap — pure JSON parsing, no LLM, no network. Counter, not analyzer.

    State: ~/credentials/skill_telemetry_state.json —
        {"last_offset_by_session": {"<uuid>": <byte_offset>}}
    Output: ~/dashboards/data/compiled/skill-telemetry.jsonl (append-only)

    Signal: assistant turns whose message.content contains a tool_use block
    with name == "Skill". Unambiguous — explicit tool calls only.

    Cadence: every ~30 min via the periodic-job runner. Coordination lock
    "skill-telemetry" prevents racing writes when hook fires concurrently.
    """
    import json as _json
    import glob as _glob

    projects_dir = Path.home() / ".claude/projects/-Users-ygontownik-Documents-Claude"
    state_path = Path.home() / "credentials/skill_telemetry_state.json"
    out_path = Path.home() / "dashboards/data/compiled/skill-telemetry.jsonl"

    if not projects_dir.exists():
        return False

    # Coordination lock — append-only writer, short TTL
    try:
        _tool_dir = os.path.dirname(os.path.abspath(__file__))
        if _tool_dir not in sys.path:
            sys.path.insert(0, _tool_dir)
        from coordination import lock as _coord_lock
    except ImportError:
        _coord_lock = None

    def _do_scan():
        state = {}
        if state_path.exists():
            try:
                state = _json.loads(state_path.read_text())
            except _json.JSONDecodeError:
                state = {}
        offsets = state.get("last_offset_by_session", {}) or {}

        transcripts = sorted(_glob.glob(str(projects_dir / "*.jsonl")))
        if not transcripts:
            return 0

        new_records = []
        for t_path in transcripts:
            session_id = Path(t_path).stem
            try:
                size = Path(t_path).stat().st_size
            except OSError:
                continue
            start = offsets.get(session_id, 0)
            if start >= size:
                continue  # nothing new
            try:
                with open(t_path, "rb") as f:
                    f.seek(start)
                    chunk = f.read()
                    new_end = start + len(chunk)
            except OSError:
                continue

            # Parse line-by-line; on the LAST line, if it's incomplete (no
            # trailing \n), back off so we re-read it next pass when complete.
            text = chunk.decode("utf-8", errors="ignore")
            lines = text.split("\n")
            if not text.endswith("\n") and lines:
                # Last element is a partial line — drop and rewind the offset
                partial = lines.pop()
                new_end -= len(partial.encode("utf-8"))

            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    turn = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                # Per-item error isolation — wrap each turn
                try:
                    if turn.get("type") != "assistant":
                        continue
                    msg = turn.get("message", {})
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue
                    ts = turn.get("timestamp", "")
                    cwd = turn.get("cwd", "")
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        if block.get("name") != "Skill":
                            continue
                        inp = block.get("input") or {}
                        skill_name = inp.get("skill") or ""
                        if not skill_name:
                            continue
                        args_val = inp.get("args", "")
                        if not isinstance(args_val, str):
                            args_val = _json.dumps(args_val, ensure_ascii=False)
                        new_records.append({
                            "ts": ts,
                            "session": session_id,
                            "skill": skill_name,
                            "args": args_val,
                            "cwd": cwd,
                        })
                except Exception:
                    # one bad turn must not stop the scan
                    continue

            offsets[session_id] = new_end

        if new_records:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "a") as f:
                for r in new_records:
                    f.write(_json.dumps(r) + "\n")
            print(f"[skill-telemetry] appended {len(new_records)} record(s) to {out_path.name}",
                  file=sys.stderr)

        state["last_offset_by_session"] = offsets
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(_json.dumps(state, indent=2))
        return len(new_records)

    try:
        if _coord_lock is not None:
            with _coord_lock("skill-telemetry", holder="dash-state-hook.py", ttl_seconds=60):
                return _do_scan() > 0
        return _do_scan() > 0
    except Exception as e:
        print(f"[dash-state-hook] skill-telemetry scan failed (non-fatal): {e}", file=sys.stderr)
        return False


# Skill telemetry — every ~30 min (cheap, append-only)
SKILL_TELEMETRY_LOCK = Path("/tmp/dash-state-hook-skill-telemetry.last")
SKILL_TELEMETRY_INTERVAL = 1800  # 30 min


def seconds_since_skill_telemetry():
    if not SKILL_TELEMETRY_LOCK.exists():
        return float("inf")
    try:
        return datetime.now().timestamp() - float(SKILL_TELEMETRY_LOCK.read_text().strip())
    except Exception:
        return float("inf")


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
    import argparse as _argparse
    _parser = _argparse.ArgumentParser(
        description="dash-state-hook — Claude Code Stop hook",
        add_help=False,  # keep silent for normal invocations from the hook
    )
    _parser.add_argument("--dry-run", action="store_true",
                         help="Print what would run without spawning subprocesses")
    _args, _ = _parser.parse_known_args()
    _dry_run = _args.dry_run

    # Recursion guard: this hook fires at the end of every Claude Code turn.
    # When run_deal_extract_sync() spawns a child `claude -p /deal-sync` session,
    # that child fires its own Stop hook on exit. Without this guard, that would
    # re-enter run_deal_extract_sync(), which would spawn another child, ad
    # infinitum. The DEAL_SYNC_CHILD env var is set by run_deal_extract_sync()
    # before exec'ing claude.
    if os.environ.get("DEAL_SYNC_CHILD") == "1":
        return 0

    elapsed = seconds_since_last_run()

    if _dry_run:
        print("[dash-state-hook] [dry-run] Checking which jobs would fire:")
        print(f"  deal_entry_sync:     {'WOULD RUN' if seconds_since_deal_entry_sync() >= DEAL_ENTRY_SYNC_INTERVAL else 'skip (too soon)'}")
        print(f"  deal_extract_sync:   {'WOULD RUN' if seconds_since_deal_extract_sync() >= DEAL_EXTRACT_SYNC_INTERVAL else 'skip (too soon)'}")
        print(f"  ref_doc_sync:        {'WOULD RUN' if seconds_since_ref_doc_sync() >= REF_DOC_SYNC_INTERVAL else 'skip (too soon)'}")
        print(f"  project_inst_sync:   {'WOULD RUN' if should_run_project_inst_sync() else 'skip'}")
        print(f"  chat_capture:        {'WOULD RUN' if seconds_since_chat_capture() >= CHAT_CAPTURE_INTERVAL else 'skip (too soon)'}")
        print(f"  artifact_pull:       {'WOULD RUN' if seconds_since_artifact_pull() >= ARTIFACT_PULL_INTERVAL else 'skip (too soon)'}")
        print(f"  skill_telemetry:     {'WOULD RUN' if seconds_since_skill_telemetry() >= SKILL_TELEMETRY_INTERVAL else 'skip (too soon)'}")
        print()
        run_artifact_pull(dry_run=True)
        return 0

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

    # Artifact pull — every 4h, walk each deal's claude.ai project via Chrome MCP,
    # download new artifacts to ~/Downloads. local_file_router.py routes them by
    # deal alias within its next 30s poll. State in processed_artifacts.json.
    if seconds_since_artifact_pull() >= ARTIFACT_PULL_INTERVAL:
        run_artifact_pull()

    # DEAL-INTEL capture from Claude Code transcripts — cheap (file grep).
    # Routes any ---DEAL-INTEL--- blocks emitted during CC sessions into the
    # corresponding deal's log.json before next /deal-sync cycle.
    run_intel_capture_scan()

    # LEARNING-CAPTURE scan — find rule-shaped statements in the session
    # transcript ("going forward, X" / "always Y" / "from now on Z"), queue
    # them for review in next morning briefing. Closes the dynamic-learnings
    # loop (cheap regex; high-precision/low-recall by design).
    run_learning_capture_scan()

    # SKILL-TELEMETRY scan — every ~30 min, scan recent CC transcripts for
    # Skill tool invocations and append one record per call. Pure regex/JSON,
    # no LLM. Lets us see which skills are actually used (vs declared) over
    # time. State at ~/credentials/skill_telemetry_state.json keeps
    # per-session byte offsets so each line is counted once.
    if seconds_since_skill_telemetry() >= SKILL_TELEMETRY_INTERVAL:
        run_skill_telemetry_scan()
        SKILL_TELEMETRY_LOCK.write_text(str(datetime.now().timestamp()))

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
