#!/usr/bin/env python3
"""
setup.py — New-firm setup validator for the COS Pipeline.

Checks that all required config files are in place and correctly filled in.
Does NOT make any network calls or create Google Docs — just validates locally.

Usage:
    python3 setup.py          # full validation
    python3 setup.py --fix    # create missing config files from templates
"""
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_HOME = Path.home()
_CREDS = _HOME / "credentials"
_DASHBOARDS_CONFIG = _HOME / "dashboards/config"

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"
INFO = "\033[94m→\033[0m"


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    line = f"  {icon}  {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return ok


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


errors = []
warnings = []


# ─────────────────────────────────────────────────────────────
# 1. firm_context.yaml
# ─────────────────────────────────────────────────────────────
section("1 / 5  firm_context.yaml — Firm identity")

FC_PATH = _HERE / "firm_context.yaml"

if not FC_PATH.exists():
    print(f"  {FAIL}  firm_context.yaml not found")
    print(f"  {INFO}  Run: cp firm_context.template.yaml firm_context.yaml")
    errors.append("firm_context.yaml missing")
else:
    try:
        import yaml
        with open(FC_PATH) as f:
            ctx = yaml.safe_load(f) or {}

        PLACEHOLDER_STRINGS = {"YOUR NAME HERE", "YOUR FIRM FULL NAME", "YOUR ROLE HERE",
                               "SECTOR 1 (e.g. power & utilities — ERCOT/PJM/MISO)"}

        p = ctx.get("principal", {})
        f = ctx.get("firm", {})

        ok_name  = check("principal.name set",      bool(p.get("name"))  and p.get("name") not in PLACEHOLDER_STRINGS, p.get("name", "(empty)"))
        ok_role  = check("principal.role set",      bool(p.get("role"))  and p.get("role") not in PLACEHOLDER_STRINGS)
        ok_firm  = check("firm.name set",           bool(f.get("name"))  and f.get("name") not in PLACEHOLDER_STRINGS, f.get("name", "(empty)"))
        ok_focus = check("investment_focus has items", bool(p.get("investment_focus")))
        ok_team  = check("team has ≥1 member",     len(ctx.get("team", [])) >= 1)
        ok_owners = check("owner_whitelist non-empty", bool(ctx.get("owner_whitelist")))

        if not all([ok_name, ok_role, ok_firm, ok_focus]):
            errors.append("firm_context.yaml has placeholder values — edit it with your firm's details")

    except ImportError:
        print(f"  {WARN}  PyYAML not installed — cannot validate firm_context.yaml")
        print(f"  {INFO}  Run: pip install pyyaml")
        warnings.append("pyyaml not installed")
    except Exception as e:
        print(f"  {FAIL}  Could not parse firm_context.yaml: {e}")
        errors.append(f"firm_context.yaml parse error: {e}")


# ─────────────────────────────────────────────────────────────
# 2. firm_config.json
# ─────────────────────────────────────────────────────────────
section("2 / 5  firm_config.json — Email and Doc IDs")

CFG_PATH = _HERE / "firm_config.json"

if not CFG_PATH.exists():
    print(f"  {FAIL}  firm_config.json not found")
    print(f"  {INFO}  Run: cp firm_config.template.json firm_config.json")
    errors.append("firm_config.json missing")
else:
    try:
        with open(CFG_PATH) as f:
            cfg = json.load(f)

        PLACEHOLDER_IDS = {"GOOGLE_DOC_ID_FOR_FOLLOWUPS", "GOOGLE_DOC_ID"}
        docs = cfg.get("docs", {})

        ok_followups  = check("docs.followups set",  bool(docs.get("followups"))  and docs.get("followups") not in PLACEHOLDER_IDS)
        ok_pipeline   = check("docs.pipeline set",   bool(docs.get("pipeline"))   and docs.get("pipeline")  not in PLACEHOLDER_IDS)
        ok_people     = check("docs.people set",     bool(docs.get("people"))     and docs.get("people")    not in PLACEHOLDER_IDS)
        ok_recruiting = check("docs.recruiting set", bool(docs.get("recruiting")) and docs.get("recruiting") not in PLACEHOLDER_IDS)
        ok_packages   = check("packages field present", bool(cfg.get("packages")), str(cfg.get("packages", [])))
        ok_keywords   = check("deal_keywords non-empty", bool(cfg.get("deal_keywords")))

        if not all([ok_followups, ok_pipeline, ok_people, ok_recruiting]):
            errors.append("firm_config.json has placeholder Google Doc IDs — fill in real IDs")

    except Exception as e:
        print(f"  {FAIL}  Could not parse firm_config.json: {e}")
        errors.append(f"firm_config.json error: {e}")


# ─────────────────────────────────────────────────────────────
# 3. Credentials
# ─────────────────────────────────────────────────────────────
section("3 / 5  Credentials")

gdrive_creds = _CREDS / "gdrive_credentials.json"
gmail_creds  = _CREDS / "gmail_mini_token.pickle"
gdrive_token = _CREDS / "gdrive_token.pickle"

check("~/credentials/ directory exists",   _CREDS.exists())
check("gdrive_credentials.json present",   gdrive_creds.exists(),
      "run OAuth setup if missing")
check("gmail_mini_token.pickle present",   gmail_creds.exists(),
      "run: python3 cos_gmail_mini_v2.py --list --backfill 1h  to create")
check("gdrive_token.pickle present",       gdrive_token.exists(),
      "run: python3 cos_otter_backfill.py --list  to create")

if not gdrive_creds.exists():
    warnings.append("gdrive_credentials.json missing — download from Google Cloud Console")


# ─────────────────────────────────────────────────────────────
# 4. Environment / Keychain
# ─────────────────────────────────────────────────────────────
section("4 / 5  Environment variables")

anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
dashboard_user = os.environ.get("DASHBOARD_USERNAME", "")
dashboard_pass = os.environ.get("DASHBOARD_PASSWORD", "")

check("ANTHROPIC_API_KEY set",    bool(anthropic_key),    "sk-ant-..." if not anthropic_key else f"{anthropic_key[:12]}...")
check("DASHBOARD_USERNAME set",   bool(dashboard_user),   dashboard_user or "(not set)")
check("DASHBOARD_PASSWORD set",   bool(dashboard_pass),   "(set)" if dashboard_pass else "(not set)")

if anthropic_key:
    # Quick format check — not a network call
    ok_format = anthropic_key.startswith("sk-ant-")
    check("ANTHROPIC_API_KEY format looks correct", ok_format,
          "should start with sk-ant-")
    if not ok_format:
        errors.append("ANTHROPIC_API_KEY format unexpected — check the value")

if not anthropic_key:
    errors.append("ANTHROPIC_API_KEY not set — export it or load via load-secrets.sh")


# ─────────────────────────────────────────────────────────────
# 5. Python dependencies
# ─────────────────────────────────────────────────────────────
section("5 / 5  Python dependencies")

REQUIRED_PACKAGES = {
    "yaml":               "pyyaml",
    "google.auth":        "google-auth",
    "google.oauth2":      "google-auth",
    "googleapiclient":    "google-api-python-client",
    "anthropic":          "anthropic",
}

for import_name, pip_name in REQUIRED_PACKAGES.items():
    try:
        __import__(import_name)
        check(f"{pip_name}", True)
    except ImportError:
        check(f"{pip_name}", False, f"pip install {pip_name}")
        warnings.append(f"Missing: pip install {pip_name}")


# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────
print(f"\n{'═' * 60}")
if errors:
    print(f"  {FAIL}  SETUP INCOMPLETE — {len(errors)} error(s):")
    for e in errors:
        print(f"       • {e}")
    if warnings:
        print(f"\n  {WARN}  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"       • {w}")
    print(f"{'═' * 60}\n")
    sys.exit(1)
elif warnings:
    print(f"  {WARN}  SETUP OK WITH WARNINGS — {len(warnings)} item(s):")
    for w in warnings:
        print(f"       • {w}")
    print(f"{'═' * 60}\n")
else:
    print(f"  {PASS}  ALL CHECKS PASSED — ready to run pipelines")
    print(f"{'═' * 60}\n")
