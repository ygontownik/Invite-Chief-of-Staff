#!/usr/bin/env python3
"""
setup_new_firm.py — Auto-creates the Google Drive folder/Doc structure for a new COS Pipeline firm.

Run AFTER setup.sh (which handles OAuth, Keychain, LaunchAgents).

Usage:
    python3 setup_new_firm.py [--config /path/to/config] [--force]
"""

import argparse
import json
import os
import pickle
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
def step(msg): print(f"\n{BOLD}{CYAN}▶ {msg}{RESET}")
def err(msg):  print(f"  {RED}✗{RESET} {msg}", file=sys.stderr)
def head(msg): print(f"\n{BOLD}{msg}{RESET}")

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_PODCAST_FEEDS = [
    {"show": "Infrastructure Investor", "rss": "https://feeds.megaphone.fm/MSNBCbusiness-7594684"},
    {"show": "Catalyst",                "rss": "https://feeds.megaphone.fm/FRHI3783594776"},
    {"show": "Acquired",                "rss": "https://feeds.acquired.fm/acquired"},
    {"show": "The Gist (PitchBook)",    "rss": "https://feeds.megaphone.fm/pitchbook"},
]

TOKEN_PATH = Path.home() / "credentials" / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/gmail.readonly",
]

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
        import google.auth.exceptions

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


def build_services(creds):
    from googleapiclient.discovery import build
    drive = build("drive", "v3", credentials=creds)
    docs  = build("docs",  "v1", credentials=creds)
    return drive, docs


# ── Drive helpers ─────────────────────────────────────────────────────────────

def create_folder(drive, name, parent_id=None):
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        meta["parents"] = [parent_id]
    result = drive.files().create(body=meta, fields="id").execute()
    return result["id"]


def create_doc(drive, docs_svc, name, parent_id=None):
    """Create a blank Google Doc inside a Drive folder."""
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
    }
    if parent_id:
        meta["parents"] = [parent_id]
    result = drive.files().create(body=meta, fields="id").execute()
    return result["id"]


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


def prompt_yn(label, default=True):
    suffix = " [Y/n]" if default else " [y/N]"
    val = input(f"  {BOLD}{label}{RESET}{suffix}: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


# ── YAML writer (no external dep needed for simple cases) ─────────────────────

def _yaml_str(s):
    """Wrap a string in double quotes, escaping embedded quotes."""
    s = str(s).replace('"', '\\"')
    return f'"{s}"'


def write_firm_context_yaml(config_dir: Path, data: dict, existing: dict):
    """Write firm_context.yaml, preserving wizard-generated keys from existing file."""

    # Keys from the configure.html wizard that we preserve verbatim
    PRESERVE_KEYS = [
        "investment_focus", "analytical_style", "draft_voice",
        "prompt_overrides", "deal_keywords", "recruit_keywords",
        "principal", "firm", "team", "owner_whitelist",
        "key_people", "peer_firms",
    ]

    fi = data["folder_ids"]
    di = data["doc_ids"]
    fs = data["firm_short"]
    fn = data["firm_name"]
    yn = data["your_name"]
    yr = data["your_role"]
    em = data["your_email"]

    # Podcast feeds: use existing if already set, else defaults
    if "podcast_feeds" in existing.get("personal", {}):
        podcast_feeds = existing["personal"]["podcast_feeds"]
        podcast_yaml = "\n".join(
            f'    - show: {_yaml_str(f["show"])}\n      rss:  {_yaml_str(f["rss"])}'
            for f in podcast_feeds
        )
    else:
        podcast_yaml = "\n".join(
            f'    - show: {_yaml_str(f["show"])}\n      rss:  {_yaml_str(f["rss"])}'
            for f in DEFAULT_PODCAST_FEEDS
        )

    lines = []
    lines.append("# ── GOOGLE DRIVE STRUCTURE ────────────────────────────────────────")
    lines.append("# Generated by setup_new_firm.py — do not edit IDs manually")
    lines.append("google_drive:")
    lines.append(f"  root_folder_id:     {_yaml_str(fi['root'])}")
    lines.append(f"  cos_folder_id:      {_yaml_str(fi['cos'])}")
    lines.append(f"  transcripts_folder_id: {_yaml_str(fi['transcripts'])}")
    lines.append(f"  otter_folder_id:    {_yaml_str(fi['otter'])}")
    lines.append(f"  firefly_folder_id:  {_yaml_str(fi['firefly'])}")
    lines.append(f"  recordings_folder_id: {_yaml_str(fi['recordings'])}")
    lines.append(f"  recruiting_folder_id: {_yaml_str(fi['recruiting'])}")
    lines.append(f"  deals_folder_id:    {_yaml_str(fi['deals'])}")
    lines.append("")
    lines.append("# ── GOOGLE DOCS ────────────────────────────────────────────────────")
    lines.append("google_docs:")
    lines.append(f"  briefing_log:       {_yaml_str(di['briefing_log'])}")
    lines.append(f"  follow_ups:         {_yaml_str(di['follow_ups'])}")
    lines.append(f"  people_crm:         {_yaml_str(di['people_crm'])}")
    lines.append(f"  recruiting:         {_yaml_str(di['recruiting'])}")
    lines.append(f"  deal_pipeline:      {_yaml_str(di['deal_pipeline'])}")
    lines.append(f"  call_transcripts:   {_yaml_str(di['call_transcripts'])}")
    lines.append("")

    # Merge preserved keys from existing file
    for key in PRESERVE_KEYS:
        if key in existing:
            lines.append(f"# ── {key.upper()} (from configure.html wizard) ─────────────────────────")
            # Re-serialize the preserved block (best-effort — dict → yaml)
            lines.append(_dict_to_yaml(key, existing[key], indent=0))
            lines.append("")

    # Personal block
    lines.append("# ── PERSONAL ────────────────────────────────────────────────────────")
    lines.append("personal:")
    lines.append(f"  name:           {_yaml_str(yn)}")
    lines.append(f"  email:          {_yaml_str(em)}")
    lines.append(f"  role:           {_yaml_str(yr)}")
    lines.append(f"  briefing_email: {_yaml_str(em)}")
    lines.append("  podcast_feeds:")
    lines.append(podcast_yaml)
    lines.append("")

    # Transcript sources using the created folders
    lines.append("# ── TRANSCRIPT SOURCES ────────────────────────────────────────────")
    lines.append("transcript_sources:")
    lines.append("  - type: google_drive_folder")
    lines.append("    name: Otter AI")
    lines.append("    folder_ids:")
    lines.append(f"      - {_yaml_str(fi['otter'])}")
    lines.append("    category_hint: auto")
    lines.append("  - type: google_drive_folder")
    lines.append("    name: Firefly")
    lines.append("    folder_ids:")
    lines.append(f"      - {_yaml_str(fi['firefly'])}")
    lines.append("    category_hint: auto")
    lines.append("")

    path = config_dir / "firm_context.yaml"
    path.write_text("\n".join(lines))
    return path


def _dict_to_yaml(key, val, indent=0):
    """Very simple dict/list/scalar → YAML serializer (enough for preserved blocks)."""
    pad = "  " * indent
    if isinstance(val, dict):
        lines = [f"{pad}{key}:"]
        for k, v in val.items():
            lines.append(_dict_to_yaml(k, v, indent + 1))
        return "\n".join(lines)
    elif isinstance(val, list):
        lines = [f"{pad}{key}:"]
        for item in val:
            if isinstance(item, dict):
                sub = "\n".join(
                    _dict_to_yaml(k, v, indent + 2) for k, v in item.items()
                )
                lines.append(f"{'  ' * (indent+1)}-")
                lines.append(sub)
            else:
                lines.append(f"{'  ' * (indent+1)}- {_yaml_str(item)}")
        return "\n".join(lines)
    else:
        return f"{pad}{key}: {_yaml_str(val)}"


def write_firm_config_json(config_dir: Path, data: dict, existing_config: dict):
    """Write firm_config.json."""
    fs = data["firm_short"]
    prefix = f"cos-pipeline-{fs.lower().replace(' ', '-')}"

    config = {
        "keychain_service_prefix": existing_config.get("keychain_service_prefix", prefix),
        "first_run_lookback_hours": existing_config.get("first_run_lookback_hours", 168),
        "packages": existing_config.get("packages", [
            "pyyaml",
            "google-auth",
            "google-auth-oauthlib",
            "google-api-python-client",
            "anthropic",
            "pypdf",
            "assemblyai",
        ]),
        "deal_keywords": existing_config.get("deal_keywords", []),
        "recruit_keywords": existing_config.get("recruit_keywords", []),
    }

    path = config_dir / "firm_config.json"
    path.write_text(json.dumps(config, indent=2))
    return path


# ── Load existing config ──────────────────────────────────────────────────────

def load_existing_yaml(path: Path) -> dict:
    """Load existing firm_context.yaml if present. Returns {} on any error."""
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def load_existing_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="COS Pipeline — New Firm Setup")
    parser.add_argument("--config", default=os.environ.get("COS_CONFIG_DIR", str(Path.home() / "cos-pipeline-config")),
                        help="Path to config directory (default: $COS_CONFIG_DIR or ~/cos-pipeline-config)")
    parser.add_argument("--force", action="store_true", help="Re-create even if config already exists")
    args = parser.parse_args()

    config_dir = Path(args.config)
    config_dir.mkdir(parents=True, exist_ok=True)

    yaml_path   = config_dir / "firm_context.yaml"
    config_path = config_dir / "firm_config.json"

    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  COS Pipeline — New Firm Setup{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}")
    print(f"  Config dir: {CYAN}{config_dir}{RESET}")

    if yaml_path.exists() and not args.force:
        print(f"\n  {YELLOW}firm_context.yaml already exists.{RESET}")
        print(f"  Use {BOLD}--force{RESET} to overwrite, or press Enter to continue and update IDs.")
        cont = input("  Continue? [Y/n]: ").strip().lower()
        if cont == "n":
            print("  Aborted.")
            sys.exit(0)

    # Load existing config to pre-fill and preserve wizard-generated keys
    existing_yaml   = load_existing_yaml(yaml_path)
    existing_config = load_existing_json(config_path)

    existing_principal = existing_yaml.get("principal", {})
    existing_firm      = existing_yaml.get("firm", {})
    existing_personal  = existing_yaml.get("personal", {})

    # ── Prompt for identity ───────────────────────────────────────────────────
    step("Firm identity")

    firm_name  = prompt("Firm full name",  existing_firm.get("name", ""))
    firm_short = prompt("Firm short name (2-5 chars)", existing_firm.get("short_name", ""))
    your_name  = prompt("Your full name",  existing_principal.get("name", ""))
    your_email = prompt("Your email",      existing_personal.get("email", ""))
    your_role  = prompt("Your role/title", existing_principal.get("role", ""))

    data = {
        "firm_name":   firm_name,
        "firm_short":  firm_short,
        "your_name":   your_name,
        "your_email":  your_email,
        "your_role":   your_role,
        "folder_ids":  {},
        "doc_ids":     {},
    }

    # ── Load Google APIs ──────────────────────────────────────────────────────
    step("Authenticating with Google")
    creds = load_credentials()
    ok("Token loaded")

    try:
        drive, docs_svc = build_services(creds)
        ok("Drive and Docs API clients ready")
    except Exception as e:
        err(f"Failed to build API clients: {e}")
        print("\n  Install missing packages: pip3 install google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    # ── Create folder structure ───────────────────────────────────────────────
    step("Creating Google Drive folder structure")

    created = 0
    failed  = 0

    def safe_create_folder(name, parent=None, key=None):
        nonlocal created, failed
        try:
            fid = create_folder(drive, name, parent)
            ok(f"Folder created: {name}  ({fid})")
            created += 1
            if key:
                data["folder_ids"][key] = fid
            return fid
        except Exception as e:
            err(f"Failed to create folder '{name}': {e}")
            failed += 1
            if key:
                data["folder_ids"][key] = "ERROR"
            return None

    def safe_create_doc(name, parent=None, key=None):
        nonlocal created, failed
        try:
            did = create_doc(drive, docs_svc, name, parent)
            ok(f"Doc created: {name}  ({did})")
            created += 1
            if key:
                data["doc_ids"][key] = did
            return did
        except Exception as e:
            err(f"Failed to create doc '{name}': {e}")
            failed += 1
            if key:
                data["doc_ids"][key] = "ERROR"
            return None

    # Root folder
    root_id = safe_create_folder(f"COS Pipeline — {firm_short}", key="root")

    # Sub-folders
    cos_id         = safe_create_folder("Chief of Staff",   root_id, "cos")
    transcripts_id = safe_create_folder("Transcripts",      root_id, "transcripts")
    recordings_id  = safe_create_folder("Call Recordings",  root_id, "recordings")
    recruiting_id  = safe_create_folder("Recruiting",       root_id, "recruiting")
    deals_id       = safe_create_folder(f"{firm_short} Deals", root_id, "deals")

    # Transcripts sub-folders
    otter_id   = safe_create_folder("Otter AI", transcripts_id, "otter")
    firefly_id = safe_create_folder("Firefly",  transcripts_id, "firefly")

    # ── Create Google Docs ────────────────────────────────────────────────────
    step("Creating Google Docs in Chief of Staff folder")

    safe_create_doc("Personal Briefing Log",       cos_id, "briefing_log")
    safe_create_doc("Follow-ups",                  cos_id, "follow_ups")
    safe_create_doc("People / CRM",                cos_id, "people_crm")
    safe_create_doc("Recruiting Pipeline",         cos_id, "recruiting")
    safe_create_doc(f"{firm_short} Deal Pipeline", cos_id, "deal_pipeline")
    safe_create_doc("Call Transcripts & Memos",    cos_id, "call_transcripts")

    # ── Write config files ────────────────────────────────────────────────────
    step("Writing config files")

    try:
        yaml_out = write_firm_context_yaml(config_dir, data, existing_yaml)
        ok(f"Written: {yaml_out}")
    except Exception as e:
        err(f"Failed to write firm_context.yaml: {e}")
        failed += 1

    try:
        json_out = write_firm_config_json(config_dir, data, existing_config)
        ok(f"Written: {json_out}")
    except Exception as e:
        err(f"Failed to write firm_config.json: {e}")
        failed += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    fi = data["folder_ids"]
    di = data["doc_ids"]

    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  Setup complete — {GREEN}{created} created{RESET}{BOLD} | {RED}{failed} failed{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}")

    head("Google Drive folders")
    for label, key in [
        ("Root",           "root"),
        ("Chief of Staff", "cos"),
        ("Transcripts",    "transcripts"),
        ("Call Recordings","recordings"),
        ("Recruiting",     "recruiting"),
        (f"{firm_short} Deals", "deals"),
    ]:
        fid = fi.get(key, "ERROR")
        print(f"  {label:<22} {CYAN}{fid}{RESET}")

    head("Google Docs")
    for label, key in [
        ("Personal Briefing Log",       "briefing_log"),
        ("Follow-ups",                  "follow_ups"),
        ("People / CRM",                "people_crm"),
        ("Recruiting Pipeline",         "recruiting"),
        (f"{firm_short} Deal Pipeline", "deal_pipeline"),
        ("Call Transcripts & Memos",    "call_transcripts"),
    ]:
        did = di.get(key, "ERROR")
        print(f"  {label:<28} {CYAN}{did}{RESET}")

    # Prominent Otter / Firefly callout
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  TRANSCRIPT APP CONNECTION{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

    otter_id_val   = fi.get("otter",   "ERROR")
    firefly_id_val = fi.get("firefly", "ERROR")

    print(f"""
  {BOLD}Otter AI{RESET}
  Folder ID : {YELLOW}{otter_id_val}{RESET}
  Path      : Otter app → Settings → Integrations → Google Drive → Connect

  {BOLD}Firefly{RESET}
  Folder ID : {YELLOW}{firefly_id_val}{RESET}
  Path      : Firefly app → Settings → Sync → Google Drive

  Paste the folder ID shown above into each app's Google Drive field.
  From now on, any transcript saved by Otter or Firefly will be picked
  up automatically by the pipeline within 24 hours.
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
    {config_dir}/firm_context.yaml
    {config_dir}/firm_config.json
""")

    if failed:
        print(f"  {YELLOW}Warning: {failed} item(s) failed. Review errors above and re-run with --force.{RESET}\n")
    else:
        print(f"  {GREEN}All items created successfully.{RESET}\n")


if __name__ == "__main__":
    main()
