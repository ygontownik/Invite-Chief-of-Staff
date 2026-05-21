#!/opt/homebrew/bin/python3
"""
local_file_router.py — Downloads Folder to Deal Drive Router
==================================================================
Watches ~/Downloads every 30 seconds and routes new files to Google Drive:

  - Session artifacts (.jsx, .html, .tsx) matched to a deal
        → upload to deal's _Outputs/ folder with date-prefixed name
  - Session artifacts not matched to a deal
        → upload to staging folder (Drive Organizer handles routing)
  - Documents (.pdf, .docx, .xlsx, .pptx, .txt, .md) matched to a deal by filename
    OR by content keyword scan (first 4 KB of .md/.txt files)
        → upload to staging folder with original name (Drive Organizer routes)
  - Documents not matched to any deal
        → skip with log entry

State is persisted to ~/credentials/local_file_router_state.json.
All actions (including skips) logged to ~/dashboards/logs/local_file_router.log.
Shared-state writes are bracketed by coordination.lock().

USAGE:
  python3 local_file_router.py            # daemon mode (runs forever)
  python3 local_file_router.py --once     # single scan pass and exit
  python3 local_file_router.py --dry-run  # scan + classify without uploading
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Sibling import: coordination.py lives in the same directory ───────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    from coordination import lock as coord_lock
    _COORD_AVAILABLE = True
except ImportError:
    _COORD_AVAILABLE = False

# ── Auth / Drive imports ──────────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install google-auth google-auth-oauthlib google-api-python-client")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME = Path.home()
DOWNLOADS_DIR = HOME / "Downloads"
CREDS_PATH    = HOME / "credentials" / "gdrive_credentials.json"
TOKEN_PATH    = HOME / "credentials" / "gdrive_token.pickle"
STATE_PATH    = HOME / "credentials" / "local_file_router_state.json"
LOG_PATH      = HOME / "dashboards" / "logs" / "local_file_router.log"

SCOPES = ["https://www.googleapis.com/auth/drive"]

# ── Staging folder (Drive Organizer picks up from here daily) ─────────────────
STAGING_FOLDER_ID = "11iBM6-gJ4IderdsJJ7LwBGpOgovXSDhI"

# ── Deal registry — loaded from tenant drive-docs.yaml at import time ─────────
# Multi-tenant: reads via $COS_CONFIG_DIR or glob discovery (PD1 invariant —
# no hardcoded tenant slugs in this public-repo file).
def _load_deal_registry() -> dict:
    """Build the DEALS dict from drive-docs.yaml deal_docs section.
    Each entry needs `alias_regex` + `drive_folder_id` + `outputs_folder_id`."""
    import glob as _glob
    import yaml as _yaml

    env = os.environ.get("COS_CONFIG_DIR")
    candidates = []
    if env:
        candidates.append(Path(env) / "drive-docs.yaml")
    candidates.extend(Path(p) for p in _glob.glob(str(Path.home() / "cos-pipeline-config-*/drive-docs.yaml")))

    drive_docs_path = next((p for p in candidates if p.exists()), None)
    if not drive_docs_path:
        return {}

    try:
        docs = _yaml.safe_load(drive_docs_path.read_text())
    except Exception:
        return {}

    deals = {}
    for deal_id, entry in (docs.get("deal_docs") or {}).items():
        alias_regex = entry.get("alias_regex")
        if not alias_regex:
            continue
        root_folder_id = entry.get("drive_folder_id")
        outputs_folder_id = entry.get("outputs_folder_id")
        if not (root_folder_id and outputs_folder_id):
            continue
        deals[deal_id] = {
            "re": re.compile(alias_regex, re.IGNORECASE),
            "root_folder_id": root_folder_id,
            "outputs_folder_id": outputs_folder_id,
        }
    return deals

DEALS = _load_deal_registry()

# ── File classification ───────────────────────────────────────────────────────
SESSION_ARTIFACT_EXTS = {".jsx", ".html", ".tsx"}
DOCUMENT_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md"}

SKIP_EXTS = {".crdownload", ".part", ".tmp", ".dmg"}
SKIP_PATTERNS = re.compile(
    r'^\.'                         # hidden files
    r'|\.dmg$'
    r'|[-_](?:darwin|linux|win(?:dows)?|x86_64|arm64|amd64)'  # GitHub release archives
    r'|_(?:amd64|arm64|x86_64)\.',
    re.IGNORECASE,
)
MIN_SIZE_BYTES = 200

# ── MIME type map ─────────────────────────────────────────────────────────────
MIME_MAP = {
    ".jsx":  "application/javascript",
    ".tsx":  "application/javascript",
    ".html": "text/html",
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt":  "text/plain",
    ".md":   "text/markdown",
}

POLL_INTERVAL = 30  # seconds

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("local_file_router")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log

log = setup_logging()

# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"State file corrupt, resetting: {e}")
    return {"processed": {}, "last_scan": None}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")

    def _write():
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        tmp.replace(STATE_PATH)

    if _COORD_AVAILABLE:
        with coord_lock("local-file-router-state", holder="local_file_router.py", ttl_seconds=10):
            _write()
    else:
        _write()


def file_key(path: Path) -> str:
    st = path.stat()
    return f"{path.name}:{st.st_size}:{int(st.st_mtime)}"

# ── Google Drive auth ─────────────────────────────────────────────────────────

_drive_service = None  # cached after first auth

def get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    if not CREDS_PATH.exists():
        log.error(f"OAuth credentials not found at {CREDS_PATH}")
        raise FileNotFoundError(f"Missing {CREDS_PATH}")

    creds = None
    # Use JSON token (google-auth style) if available, fall back to pickle for
    # backward compat with the rest of the pipeline (which may write .pickle).
    if TOKEN_PATH.exists():
        try:
            # Try reading as JSON first (newer google-auth style)
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            # Fall back: maybe it's a pickle from an older pipeline run
            try:
                import pickle
                with open(TOKEN_PATH, "rb") as fh:
                    creds = pickle.load(fh)
            except Exception as pe:
                log.warning(f"Could not load token from {TOKEN_PATH}: {pe}")
                creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired Drive token")
            creds.refresh(Request())
        else:
            log.info("Launching OAuth flow for Drive access")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        # Persist as JSON (matches rest of pipeline)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service

# ── Deal matching ─────────────────────────────────────────────────────────────

def match_deal(filename: str) -> str | None:
    """Return deal_id if filename matches any deal regex, else None."""
    stem = Path(filename).stem  # strip extension before matching
    name_lower = filename  # match against full name too
    for deal_id, cfg in DEALS.items():
        if cfg["re"].search(stem) or cfg["re"].search(name_lower):
            return deal_id
    return None


def classify_document(path: Path) -> str | None:
    """
    Match a document file to a deal. Two-stage:
      1. Filename keyword match (fast, covers most cases).
      2. Content keyword scan of first 4 KB for .md/.txt files (fallback for
         generically named docs whose deal identity is in the body text).
    Returns deal_id or None.
    """
    deal_id = match_deal(path.name)
    if deal_id:
        return deal_id

    # Content fallback — only for plain-text formats worth reading inline
    if path.suffix.lower() in {".md", ".txt"}:
        try:
            head = path.read_text(errors="ignore")[:4096]
            for did, cfg in DEALS.items():
                if cfg["re"].search(head):
                    return did
        except OSError:
            pass

    return None

# ── Drive upload ──────────────────────────────────────────────────────────────

def upload_to_drive(local_path: Path, drive_name: str, parent_folder_id: str) -> str:
    """Upload local_path to Drive under parent_folder_id with drive_name. Returns file ID."""
    service = get_drive_service()
    suffix = local_path.suffix.lower()
    mime = MIME_MAP.get(suffix, "application/octet-stream")

    metadata = {
        "name": drive_name,
        "parents": [parent_folder_id],
    }
    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
    result = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink",
    ).execute()
    return result["id"]

# ── Mac notification ──────────────────────────────────────────────────────────

def notify(title: str, message: str):
    try:
        script = (
            f'display notification "{message}" with title "{title}" '
            f'subtitle "TCIP File Router"'  # noqa: tenant-leak (TCIP is the product name)
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception as e:
        log.debug(f"Notification failed (non-fatal): {e}")

# ── File filter ──────────────────────────────────────────────────────────────

def should_skip(path: Path) -> str | None:
    """
    Returns a skip reason string if the file should be ignored, else None.
    """
    name = path.name
    suffix = path.suffix.lower()

    if name.startswith("."):
        return "hidden file"
    if suffix in SKIP_EXTS:
        return f"skip extension {suffix}"
    if suffix == ".dmg":
        return "dmg installer"
    if SKIP_PATTERNS.search(name):
        return f"GitHub release archive pattern: {name}"

    try:
        size = path.stat().st_size
    except OSError:
        return "stat failed (file gone?)"

    if size < MIN_SIZE_BYTES:
        return f"too small ({size} bytes)"

    return None

# ── Core routing logic ────────────────────────────────────────────────────────

def route_file(path: Path, state: dict, dry_run: bool = False) -> dict | None:
    """
    Decide what to do with a file and execute it.
    Returns a state entry dict on success, None on skip (non-error).
    In dry_run mode: classifies and logs but never uploads.
    Raises on upload error (caller handles per-file).
    """
    name = path.name
    suffix = path.suffix.lower()
    today = datetime.now().strftime("%Y-%m-%d")

    is_session_artifact = suffix in SESSION_ARTIFACT_EXTS
    is_document = suffix in DOCUMENT_EXTS

    if is_session_artifact:
        deal_id = match_deal(name)
        if deal_id:
            drive_name = f"{today} -- {name}"
            folder_id = DEALS[deal_id]["outputs_folder_id"]
            dest_label = "outputs"
            log.info(f"[{deal_id}] SESSION ARTIFACT → _Outputs/: {name}"
                     + (" [dry-run]" if dry_run else ""))
        else:
            drive_name = name
            folder_id = STAGING_FOLDER_ID
            dest_label = "staging"
            log.info(f"[unmatched] SESSION ARTIFACT → staging: {name}"
                     + (" [dry-run]" if dry_run else ""))

        if dry_run:
            return {
                "uploaded_at": None,
                "dest": dest_label,
                "deal": deal_id,
                "drive_file_id": None,
                "drive_name": drive_name,
                "dry_run": True,
            }

        file_id = upload_to_drive(path, drive_name, folder_id)
        log.info(f"  Uploaded → Drive file ID {file_id} (name: {drive_name})")
        notif_deal = deal_id or "unmatched"
        notify(
            f"TCIP: {notif_deal}",
            f"{name} → {'_Outputs/' if dest_label == 'outputs' else 'staging'}",
        )
        return {
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "dest": dest_label,
            "deal": deal_id,
            "drive_file_id": file_id,
            "drive_name": drive_name,
        }

    elif is_document:
        # Use two-stage classifier: filename then content (for .md/.txt)
        deal_id = classify_document(path)
        match_source = "filename" if match_deal(name) else "content"

        if deal_id:
            drive_name = name
            folder_id = STAGING_FOLDER_ID
            dest_label = "staging"
            log.info(f"[{deal_id}] DOCUMENT ({match_source} match) → staging: {name}"
                     + (" [dry-run]" if dry_run else ""))

            if dry_run:
                return {
                    "uploaded_at": None,
                    "dest": dest_label,
                    "deal": deal_id,
                    "drive_file_id": None,
                    "drive_name": drive_name,
                    "match_source": match_source,
                    "dry_run": True,
                }

            file_id = upload_to_drive(path, drive_name, folder_id)
            log.info(f"  Uploaded → Drive file ID {file_id}")
            notify(f"TCIP: {deal_id}", f"{name} → staging (Drive Organizer will route)")
            return {
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "dest": dest_label,
                "deal": deal_id,
                "drive_file_id": file_id,
                "drive_name": drive_name,
                "match_source": match_source,
            }
        else:
            log.info(f"[skip] No deal match for document: {name}"
                     + (" [dry-run]" if dry_run else ""))
            return None

    else:
        log.debug(f"[skip] Unrecognized extension {suffix}: {name}")
        return None

# ── Scan pass ─────────────────────────────────────────────────────────────────

def scan_once(state: dict, dry_run: bool = False):
    processed = state["processed"]
    new_entries = 0
    skipped = 0
    errors = 0

    try:
        files = sorted(DOWNLOADS_DIR.iterdir())
    except OSError as e:
        log.error(f"Cannot read Downloads folder: {e}")
        return

    for path in files:
        if not path.is_file():
            continue

        skip_reason = should_skip(path)
        if skip_reason:
            log.debug(f"[skip] {path.name} — {skip_reason}")
            skipped += 1
            continue

        key = file_key(path)
        if key in processed:
            continue

        log.info(f"New file detected: {path.name}")
        try:
            entry = route_file(path, state, dry_run=dry_run)
            if entry is not None:
                new_entries += 1
                if not dry_run:
                    processed[key] = entry
                    save_state(state)
            else:
                skipped += 1
                if not dry_run:
                    # Mark as seen so we don't re-evaluate it on every pass
                    processed[key] = {
                        "uploaded_at": None,
                        "dest": "skipped",
                        "deal": None,
                        "drive_file_id": None,
                    }
                    save_state(state)
        except Exception as e:
            errors += 1
            log.error(f"[error] Failed to route {path.name}: {e}", exc_info=True)
            # Do NOT mark as processed — will retry next pass

    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        save_state(state)

    suffix = " [dry-run, no uploads]" if dry_run else ""
    if new_entries or errors:
        log.info(f"Scan complete — {new_entries} routed | {skipped} skipped | {errors} errors{suffix}")
    elif dry_run:
        log.info(f"Scan complete [dry-run] — {new_entries} would route | {skipped} skipped")

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="TCIP Downloads → Drive file router daemon"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan pass and exit (useful for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and log files without uploading or writing state (implies --once)",
    )
    args = parser.parse_args()

    dry_run = args.dry_run
    run_once = args.once or dry_run

    log.info("=" * 60)
    log.info(f"local_file_router starting (once={run_once}, dry_run={dry_run})")
    log.info(f"  Watching: {DOWNLOADS_DIR}")
    log.info(f"  State:    {STATE_PATH}")
    log.info(f"  Log:      {LOG_PATH}")
    log.info("=" * 60)

    if not dry_run:
        # Pre-flight: ensure Drive token is usable before entering loop
        try:
            get_drive_service()
            log.info("Google Drive auth OK")
        except Exception as e:
            log.error(f"Drive auth failed at startup: {e}")
            sys.exit(1)
    else:
        log.info("Dry-run mode: skipping Drive auth (no uploads will be made)")

    state = load_state()

    if run_once:
        scan_once(state, dry_run=dry_run)
        log.info(f"--{'dry-run' if dry_run else 'once'} complete, exiting.")
        return

    # Daemon loop
    while True:
        try:
            scan_once(state)
        except Exception as e:
            log.error(f"Unexpected error in scan loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
