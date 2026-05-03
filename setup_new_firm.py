#!/usr/bin/env python3
"""
setup_new_firm.py — Auto-creates the Google Drive folder/Doc structure for a new COS Pipeline firm.

Run AFTER setup.sh (which handles OAuth, Keychain, LaunchAgents).

Usage:
    python3 setup_new_firm.py [--config /path/to/config] [--force] [--dry-run]

Idempotent: if a "COS Pipeline — {short}" root folder already exists, the script
detects it and (with --force) refreshes IDs in place rather than creating duplicates.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ── ANSI color helpers ────────────────────────────────────────────────────────

RESET  = "\033[0m"
GREEN  = "\033[32m"
BLUE   = "\033[34m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BOLD   = "\033[1m"
CYAN   = "\033[36m"

def ok(msg):   print(f"  {GREEN}✓{RESET} {msg}")
def info(msg): print(f"  {BLUE}·{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}!{RESET} {msg}")
def step(msg): print(f"\n{BOLD}{CYAN}▶ {msg}{RESET}")
def err(msg):  print(f"  {RED}✗{RESET} {msg}", file=sys.stderr)
def head(msg): print(f"\n{BOLD}{msg}{RESET}")

# ── Paths & defaults ──────────────────────────────────────────────────────────

TOKEN_PATH = Path.home() / "credentials" / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Empty by default — user adds real RSS feeds via the configure.html wizard
# (which generates the intelligence_sources.podcasts block). Shipping fake
# placeholder URLs would 404 every time the pipeline runs.
DEFAULT_PODCAST_FEEDS = []

# Folder structure: list of (logical_key, display_name, parent_logical_key_or_None)
FOLDER_TREE = [
    ("root",        None,                    None),          # name set dynamically
    ("cos",         "Chief of Staff",        "root"),
    ("transcripts", "Transcripts",           "root"),
    ("recordings",  "Call Recordings",       "root"),
    ("recruiting",  "Recruiting",            "root"),
    ("deals",       None,                    "root"),         # name set dynamically
    ("otter",       "Otter AI",              "transcripts"),
    ("firefly",     "Firefly",               "transcripts"),
]

# Doc list: (logical_key, display_name_template, parent_logical_key)
DOC_TREE = [
    ("briefing_log",     "Personal Briefing Log",       "cos"),
    ("follow_ups",       "Follow-ups",                  "cos"),
    ("people_crm",       "People / CRM",                "cos"),
    ("recruiting",       "Recruiting Pipeline",         "cos"),
    ("deal_pipeline",    "{short} Deal Pipeline",       "cos"),
    ("call_transcripts", "Call Transcripts & Memos",    "cos"),
]

# ── Dependency checks ────────────────────────────────────────────────────────

try:
    import yaml
except ImportError:
    err("Missing required package: pyyaml")
    print(f"\n  Install with: pip3 install pyyaml")
    sys.exit(1)

# ── Google auth ───────────────────────────────────────────────────────────────

def load_credentials():
    """Load OAuth2 credentials from token.json (written by setup.sh)."""
    if not TOKEN_PATH.exists():
        err(f"Token not found at {TOKEN_PATH}")
        print(f"\n  Run {BOLD}./setup.sh{RESET} first — it handles Google OAuth and writes the token.")
        sys.exit(1)

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.expired and creds.refresh_token:
            info("Refreshing expired token…")
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
        return creds
    except Exception as e:
        err(f"Failed to load credentials: {e}")
        print(f"\n  Delete {TOKEN_PATH} and re-run {BOLD}./setup.sh{RESET} to re-authenticate.")
        sys.exit(1)


def build_drive(creds):
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds)


# ── Drive helpers ─────────────────────────────────────────────────────────────

def find_folder(drive, name, parent_id=None):
    """Return folder ID if a folder with this name exists under parent_id, else None."""
    q = (
        f"name = '{name.replace(chr(39), chr(92)+chr(39))}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"
    resp = drive.files().list(q=q, fields="files(id,name)", pageSize=10).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def find_doc(drive, name, parent_id=None):
    """Return doc ID if a Google Doc with this name exists under parent_id."""
    q = (
        f"name = '{name.replace(chr(39), chr(92)+chr(39))}' "
        f"and mimeType = 'application/vnd.google-apps.document' "
        f"and trashed = false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"
    resp = drive.files().list(q=q, fields="files(id,name)", pageSize=10).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def create_folder(drive, name, parent_id=None):
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    return drive.files().create(body=meta, fields="id").execute()["id"]


def create_doc(drive, name, parent_id=None):
    meta = {"name": name, "mimeType": "application/vnd.google-apps.document"}
    if parent_id:
        meta["parents"] = [parent_id]
    return drive.files().create(body=meta, fields="id").execute()["id"]


def get_or_create_folder(drive, name, parent_id, dry_run=False):
    """Idempotent: returns existing folder ID if present, else creates."""
    existing = find_folder(drive, name, parent_id)
    if existing:
        info(f"Folder exists: {name}  ({existing})")
        return existing, "found"
    if dry_run:
        info(f"DRY RUN — would create folder: {name}")
        return "DRY-RUN", "would-create"
    fid = create_folder(drive, name, parent_id)
    ok(f"Folder created: {name}  ({fid})")
    return fid, "created"


def get_or_create_doc(drive, name, parent_id, dry_run=False):
    """Idempotent: returns existing doc ID if present, else creates."""
    existing = find_doc(drive, name, parent_id)
    if existing:
        info(f"Doc exists: {name}  ({existing})")
        return existing, "found"
    if dry_run:
        info(f"DRY RUN — would create doc: {name}")
        return "DRY-RUN", "would-create"
    did = create_doc(drive, name, parent_id)
    ok(f"Doc created: {name}  ({did})")
    return did, "created"


# ── Prompt helpers ────────────────────────────────────────────────────────────

def prompt(label, default=None, required=True):
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"  {BOLD}{label}{RESET}{suffix}: ").strip()
        if not val and default:
            return default
        if val:
            return val
        if not required:
            return ""
        print(f"  {YELLOW}This field is required.{RESET}")


# ── Config I/O ────────────────────────────────────────────────────────────────

def load_existing_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        warn(f"Could not parse existing {path.name}: {e} — starting fresh")
        return {}


def load_existing_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        warn(f"Could not parse existing {path.name}: {e} — starting fresh")
        return {}


def write_firm_context_yaml(path: Path, existing: dict, identity: dict, folder_ids: dict, doc_ids: dict):
    """
    Build the merged firm_context.yaml using yaml.safe_dump (no hand-rolled serialization).

    Order of precedence:
      1. Wizard-generated keys (principal, firm, team, draft_voice, prompt_overrides,
         intelligence_sources, etc.) are preserved verbatim from existing.
      2. We overwrite/add: google_drive, google_docs, transcript_sources, personal.
      3. Identity fields (name, email, role) update principal/personal/team if missing.
    """
    merged = dict(existing)  # shallow copy — existing top-level keys preserved

    # ── Identity: ensure principal block reflects what user just typed ──
    principal = dict(merged.get("principal", {}))
    principal.setdefault("name",        identity["name"])
    principal.setdefault("role",        identity["role"])
    principal.setdefault("background",  principal.get("background", ""))
    principal.setdefault("investor_frame", principal.get("investor_frame", ""))
    merged["principal"] = principal

    # ── Firm block ──
    firm = dict(merged.get("firm", {}))
    firm.setdefault("name",       identity["firm_name"])
    firm.setdefault("short_name", identity["firm_short"])
    merged["firm"] = firm

    # ── Team block: ensure user is listed if team is empty ──
    if not merged.get("team"):
        merged["team"] = [{
            "name":               identity["name"],
            "role":               identity["role"],
            "internal_call_role": "host (drives the conversation, takes most action items)",
        }]

    # ── Personal block ──
    personal = dict(merged.get("personal", {}))
    personal.setdefault("email",          identity["email"])
    personal.setdefault("briefing_email", identity["email"])
    # Only set podcast_feeds if intelligence_sources.podcasts not already present
    if not (merged.get("intelligence_sources", {}) or {}).get("podcasts"):
        personal.setdefault("podcast_feeds", DEFAULT_PODCAST_FEEDS)
    merged["personal"] = personal

    # ── Drive folders (always overwritten with fresh IDs) ──
    merged["google_drive"] = {
        "root_folder_id":         folder_ids.get("root"),
        "cos_folder_id":          folder_ids.get("cos"),
        "transcripts_folder_id":  folder_ids.get("transcripts"),
        "otter_folder_id":        folder_ids.get("otter"),
        "firefly_folder_id":      folder_ids.get("firefly"),
        "recordings_folder_id":   folder_ids.get("recordings"),
        "recruiting_folder_id":   folder_ids.get("recruiting"),
        "deals_folder_id":        folder_ids.get("deals"),
    }

    # ── Docs (always overwritten) ──
    merged["google_docs"] = {
        "briefing_log":     doc_ids.get("briefing_log"),
        "follow_ups":       doc_ids.get("follow_ups"),
        "people_crm":       doc_ids.get("people_crm"),
        "recruiting":       doc_ids.get("recruiting"),
        "deal_pipeline":    doc_ids.get("deal_pipeline"),
        "call_transcripts": doc_ids.get("call_transcripts"),
    }

    # ── Transcript sources (use the freshly-created Otter / Firefly folder IDs) ──
    merged["transcript_sources"] = [
        {
            "type":           "google_drive_folder",
            "name":           "Otter AI",
            "folder_ids":     [folder_ids.get("otter")],
            "category_hint":  "auto",
        },
        {
            "type":           "google_drive_folder",
            "name":           "Firefly",
            "folder_ids":     [folder_ids.get("firefly")],
            "category_hint":  "auto",
        },
    ]

    # Header comment
    header = (
        "# firm_context.yaml — generated by setup_new_firm.py\n"
        "# Wizard-generated keys (principal, firm, team, draft_voice, intelligence_sources)\n"
        "# are preserved on re-run. google_drive / google_docs / transcript_sources are\n"
        "# regenerated each time setup_new_firm.py is run.\n\n"
    )
    body = yaml.safe_dump(merged, sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)
    path.write_text(header + body)


def write_firm_config_json(path: Path, existing: dict, firm_short: str):
    prefix = f"cos-pipeline-{firm_short.lower().replace(' ', '-')}"
    config = {
        "keychain_service_prefix":   existing.get("keychain_service_prefix", prefix),
        "first_run_lookback_hours":  existing.get("first_run_lookback_hours", 168),
        "packages":                  existing.get("packages", [
            "pyyaml",
            "google-auth",
            "google-auth-oauthlib",
            "google-api-python-client",
            "anthropic",
            "pypdf",
            "assemblyai",
        ]),
        "deal_keywords":    existing.get("deal_keywords", []),
        "recruit_keywords": existing.get("recruit_keywords", []),
    }
    path.write_text(json.dumps(config, indent=2) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="COS Pipeline — New Firm Setup")
    parser.add_argument("--config",
                        default=os.environ.get("COS_CONFIG_DIR", str(Path.home() / "cos-pipeline-config")),
                        help="Path to config directory (default: $COS_CONFIG_DIR or ~/cos-pipeline-config)")
    parser.add_argument("--force",   action="store_true",
                        help="Refresh folder/doc IDs in place even if root folder already exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be created without creating anything")
    args = parser.parse_args()

    config_dir = Path(args.config).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

    yaml_path = config_dir / "firm_context.yaml"
    json_path = config_dir / "firm_config.json"

    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  COS Pipeline — New Firm Setup{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}")
    print(f"  Config dir : {CYAN}{config_dir}{RESET}")
    if args.dry_run:
        print(f"  Mode       : {YELLOW}DRY RUN — no changes will be made{RESET}")

    # ── Load existing config ──
    existing_yaml   = load_existing_yaml(yaml_path)
    existing_config = load_existing_json(json_path)

    if existing_yaml:
        info(f"Found existing firm_context.yaml — will preserve wizard-generated keys")

    # ── Identity prompts (pre-fill from existing) ──
    step("Firm identity")

    existing_principal = existing_yaml.get("principal", {}) or {}
    existing_firm      = existing_yaml.get("firm", {})      or {}
    existing_personal  = existing_yaml.get("personal", {})  or {}

    firm_name  = prompt("Firm full name",            existing_firm.get("name"))
    firm_short = prompt("Firm short name (2-8 chars)", existing_firm.get("short_name"))
    your_name  = prompt("Your full name",            existing_principal.get("name"))
    your_email = prompt("Your email",                existing_personal.get("email"))
    your_role  = prompt("Your role/title",           existing_principal.get("role", "Principal"))

    identity = {
        "name":        your_name,
        "email":       your_email,
        "role":        your_role,
        "firm_name":   firm_name,
        "firm_short":  firm_short,
    }

    # ── Auth ──
    step("Authenticating with Google")
    creds = load_credentials()
    ok("OAuth token loaded")

    try:
        drive = build_drive(creds)
        ok("Drive API client ready")
    except Exception as e:
        err(f"Failed to build Drive client: {e}")
        print(f"\n  Install missing packages: pip3 install google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    # ── Idempotency check: does the root folder already exist? ──
    root_name = f"COS Pipeline — {firm_short}"
    existing_root = find_folder(drive, root_name, parent_id=None)

    if existing_root and not args.force and not args.dry_run:
        warn(f"Root folder already exists: {root_name} ({existing_root})")
        print()
        print(f"  Re-running would create duplicate folders. Choose one:")
        print(f"    • Re-use the existing structure and refresh IDs in YAML:")
        print(f"        {BOLD}python3 setup_new_firm.py --force{RESET}")
        print(f"    • Move/rename the existing folder in Drive, then re-run this script.")
        print()
        sys.exit(2)

    # ── Create / find folders ──
    step("Creating Google Drive folder structure")

    folder_ids = {}
    counts = {"created": 0, "found": 0, "would-create": 0}

    for key, name, parent_key in FOLDER_TREE:
        # Resolve dynamic names
        if key == "root":
            name = root_name
        elif key == "deals":
            name = f"{firm_short} Deals"

        parent_id = folder_ids.get(parent_key) if parent_key else None
        # If parent failed (None) but was expected, skip
        if parent_key and not parent_id:
            err(f"Skipping {name} — parent folder missing")
            folder_ids[key] = None
            continue

        try:
            fid, status = get_or_create_folder(drive, name, parent_id, dry_run=args.dry_run)
            folder_ids[key] = fid
            counts[status] = counts.get(status, 0) + 1
        except Exception as e:
            err(f"Failed on folder '{name}': {e}")
            folder_ids[key] = None

    # ── Create / find docs ──
    step("Creating Google Docs in Chief of Staff folder")

    doc_ids = {}
    cos_id = folder_ids.get("cos")

    if not cos_id and not args.dry_run:
        err("Cannot create docs — Chief of Staff folder ID missing")
    else:
        for key, name_tmpl, parent_key in DOC_TREE:
            name = name_tmpl.format(short=firm_short)
            parent_id = folder_ids.get(parent_key)
            if not parent_id and not args.dry_run:
                err(f"Skipping doc '{name}' — parent folder missing")
                doc_ids[key] = None
                continue
            try:
                did, status = get_or_create_doc(drive, name, parent_id, dry_run=args.dry_run)
                doc_ids[key] = did
                counts[status] = counts.get(status, 0) + 1
            except Exception as e:
                err(f"Failed on doc '{name}': {e}")
                doc_ids[key] = None

    # ── Write config files ──
    if args.dry_run:
        info("Dry-run mode — skipping config file writes")
    else:
        step("Writing config files")
        try:
            write_firm_context_yaml(yaml_path, existing_yaml, identity, folder_ids, doc_ids)
            ok(f"Wrote: {yaml_path}")
        except Exception as e:
            err(f"Failed to write firm_context.yaml: {e}")

        try:
            write_firm_config_json(json_path, existing_config, firm_short)
            ok(f"Wrote: {json_path}")
        except Exception as e:
            err(f"Failed to write firm_config.json: {e}")

    # ── Summary ──
    print(f"\n{BOLD}{'═'*60}{RESET}")
    summary_bits = []
    if counts.get("created"):       summary_bits.append(f"{GREEN}{counts['created']} created{RESET}")
    if counts.get("found"):         summary_bits.append(f"{BLUE}{counts['found']} already existed{RESET}")
    if counts.get("would-create"):  summary_bits.append(f"{YELLOW}{counts['would-create']} would be created{RESET}")
    print(f"{BOLD}  Setup {'preview' if args.dry_run else 'complete'} — {' | '.join(summary_bits) if summary_bits else 'nothing to do'}")
    print(f"{BOLD}{'═'*60}{RESET}")

    head("Folder IDs")
    for key, _, _ in FOLDER_TREE:
        label = {
            "root":        "Root",
            "cos":         "Chief of Staff",
            "transcripts": "Transcripts",
            "recordings":  "Call Recordings",
            "recruiting":  "Recruiting",
            "deals":       f"{firm_short} Deals",
            "otter":       "  └─ Otter AI",
            "firefly":     "  └─ Firefly",
        }[key]
        print(f"  {label:<22} {CYAN}{folder_ids.get(key)}{RESET}")

    head("Doc IDs")
    for key, name_tmpl, _ in DOC_TREE:
        label = name_tmpl.format(short=firm_short)
        print(f"  {label:<28} {CYAN}{doc_ids.get(key)}{RESET}")

    # ── Transcript app callout ──
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  CONNECT YOUR TRANSCRIPT APP{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")
    print(f"""
  {BOLD}Otter AI{RESET}
    Folder ID : {YELLOW}{folder_ids.get('otter')}{RESET}
    Path      : Otter app → Settings → Apps → Google Drive → Connect

  {BOLD}Firefly{RESET}
    Folder ID : {YELLOW}{folder_ids.get('firefly')}{RESET}
    Path      : Firefly app → Settings → Integrations → Google Drive

  Paste the folder ID into each app. From now on, any transcript saved
  by Otter or Firefly will be picked up by the pipeline within 24 hours.
""")

    print(f"{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  NEXT STEPS{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")
    print(f"""
  {BOLD}Validate everything is wired up:{RESET}
    python3 ~/cos-pipeline/setup.py --validate

  {BOLD}First pipeline run (processes last 7 days):{RESET}
    python3 ~/cos-pipeline/cos_capture_pipeline.py --since 168h

  {BOLD}Open your dashboard:{RESET}
    http://localhost:7777

  {BOLD}Config written to:{RESET}
    {yaml_path}
    {json_path}
""")


if __name__ == "__main__":
    main()
