#!/usr/bin/env python3
"""
_firm_context.py — Firm identity loader and prompt-preamble builder.

All COS pipeline scripts import this module to eliminate hardcoded
principal/firm/team references. Reads firm_context.yaml from the
config directory; falls back to firm_context.json if PyYAML is not
installed.

── Config search order ───────────────────────────────────────────────
1. $COS_CONFIG_DIR/firm_context.yaml   (env var — team config repo)
2. ~/cos-pipeline-config/firm_context.yaml  (default team config location)
3. <pipeline_dir>/firm_context.yaml    (legacy: config in code dir)
4. <pipeline_dir>/firm_context.json    (JSON fallback, stdlib only)

For a team sharing one dashboard, put firm_context.yaml in a private
GitHub repo (e.g. github.com/yourfirm/cos-config) and clone it to
~/cos-pipeline-config/. Each team member pulls that repo to stay in
sync. The code repo (this file) stays universal and public.

Usage in a pipeline script:
    import _firm_context as _fc
    _CTX = _fc.load_firm_context()

    MEMO_PREAMBLE     = _fc.build_memo_header(_CTX) + _fc.build_memo_body(_CTX)
    BACKFILL_PREAMBLE = _fc.build_backfill_header(_CTX) + BACKFILL_BODY
"""
import os
from pathlib import Path
import json

# Current schema version — bump when adding required fields to the template.
# firm_context.yaml should carry a matching schema_version field.
# setup.py --check warns when the file's version is behind this constant.
SCHEMA_VERSION = 2

_PIPELINE_DIR = Path(__file__).parent

# ── Config search path ────────────────────────────────────────────────────────

def _find_config_dir() -> Path:
    """Return the directory that contains firm_context.yaml.

    Search order (per DECISIONS C3 + C4):
      1. $COS_CONFIG_DIR env var (explicit override — set in ~/.zshrc)
      2. ~/cos-pipeline-config-tomac/ (canonical: slug-suffixed per C3)
      3. ~/cos-pipeline-config/       (legacy: pre-C3 default; symlinked to -tomac/)
      4. <pipeline_dir>/              (legacy: config living alongside code)
    """
    env = os.environ.get("COS_CONFIG_DIR")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p

    canonical_tomac = Path.home() / "cos-pipeline-config-tomac"
    if canonical_tomac.is_dir() and (canonical_tomac / "firm_context.yaml").exists():
        return canonical_tomac

    legacy_team = Path.home() / "cos-pipeline-config"
    if legacy_team.is_dir() and (legacy_team / "firm_context.yaml").exists():
        return legacy_team

    return _PIPELINE_DIR  # legacy fallback


# ── Loader ────────────────────────────────────────────────────────────────────

def load_firm_context() -> dict:
    """Load firm_context.yaml from the config directory.

    Falls back to firm_context.json if PyYAML is not installed.
    Emits a warning (does not crash) if schema_version is behind SCHEMA_VERSION.
    """
    config_dir = _find_config_dir()
    ctx_yaml = config_dir / "firm_context.yaml"
    ctx_json = config_dir / "firm_context.json"

    ctx = None

    # Try YAML first (preferred: human-readable, supports comments)
    try:
        import yaml  # pip install pyyaml
        if ctx_yaml.exists():
            with open(ctx_yaml) as f:
                ctx = yaml.safe_load(f) or {}
    except ImportError:
        pass

    # JSON fallback — works with stdlib only
    if ctx is None:
        if ctx_json.exists():
            with open(ctx_json) as f:
                ctx = json.load(f)

    if ctx is None:
        raise FileNotFoundError(
            f"No firm context file found. Searched:\n"
            f"  {ctx_yaml}\n  {ctx_json}\n\n"
            f"Options:\n"
            f"  1. Set COS_CONFIG_DIR in ~/.zshrc to point to your config directory.\n"
            f"  2. Clone your firm config repo to ~/cos-pipeline-config/\n"
            f"  3. Copy firm_context.template.yaml → {_PIPELINE_DIR}/firm_context.yaml "
            f"and fill in your firm's details."
        )

    # Schema version check — warn but don't crash so pipelines keep running
    file_version = ctx.get("schema_version", 1)
    if file_version < SCHEMA_VERSION:
        import sys
        print(
            f"[firm_context] WARNING: your firm_context.yaml is schema version "
            f"{file_version}, current is {SCHEMA_VERSION}. "
            f"Run `python3 setup.py --check` to see what's new. "
            f"Pipelines will continue with defaults for any missing fields.",
            file=sys.stderr,
        )

    return ctx


def load_drive_docs(drive_docs_path=None) -> dict:
    """Load drive-docs.yaml and return a flat key→id mapping.

    Keys from docs section map to doc_id; keys from folders section map to folder_id.
    Falls back gracefully — returns {} if the file is missing or unparseable.

    Search order (when drive_docs_path is not explicitly provided):
      1. $COS_CONFIG_DIR/drive-docs.yaml
      2. ~/cos-pipeline-config/drive-docs.yaml
      3. ~/dashboards/config/drive-docs.yaml  (legacy)

    Usage in scripts:
        _DOCS = _fc.load_drive_docs()
        FOLLOW_UPS_DOC = _DOCS.get("followups", "")
        TOMAC_DOC      = _DOCS.get("tomac_pipeline", "")
    """
    if drive_docs_path is None:
        config_dir = _find_config_dir()
        candidate = config_dir / "drive-docs.yaml"
        if candidate.exists():
            drive_docs_path = candidate
        else:
            drive_docs_path = Path.home() / "dashboards/config/drive-docs.yaml"
    drive_docs_path = Path(drive_docs_path)
    result = {}
    if not drive_docs_path.exists():
        return result
    try:
        import yaml
        with open(drive_docs_path) as f:
            data = yaml.safe_load(f) or {}
    except ImportError:
        # Minimal fallback: can't parse YAML without PyYAML.
        # Callers should pip install pyyaml or hardcode fallback IDs.
        return result
    for key, entry in (data.get("docs") or {}).items():
        if isinstance(entry, dict):
            result[key] = entry.get("doc_id", "")
    for key, entry in (data.get("folders") or {}).items():
        if isinstance(entry, dict):
            result[key] = entry.get("folder_id", "")
    return result


def find_firm_config() -> Path:
    """Return the path to firm_config.json, respecting COS_CONFIG_DIR.

    Search order:
      1. $COS_CONFIG_DIR/firm_config.json
      2. ~/cos-pipeline-config/firm_config.json
      3. <pipeline_dir>/firm_config.json  (legacy)
    """
    config_dir = _find_config_dir()
    candidate = config_dir / "firm_config.json"
    if candidate.exists():
        return candidate
    return _PIPELINE_DIR / "firm_config.json"


def load_firm_config(firm_config_path=None) -> dict:
    """Load and return the full firm_config.json as a dict.

    Falls back to {} if the file is missing or unparseable.
    """
    path = Path(firm_config_path) if firm_config_path else find_firm_config()
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_active_packages(firm_config_path=None) -> list:
    """Return the list of active package names from firm_config.json.

    Returns ["market_intelligence", "operations"] by default (both active).
    A firm that only deploys Package B would set: "packages": ["operations"]
    Respects COS_CONFIG_DIR — looks in the team config repo first.
    """
    cfg = load_firm_config(firm_config_path)
    return cfg.get("packages", ["market_intelligence", "operations"])


# ── Features (scope A — per-tenant + per-user toggles) ───────────────────────

# Default values for known features. Conservative: every feature off by default
# so a brand-new tenant gets a clean stripped-down dashboard. Tomac and other
# established tenants override via firm_context.yaml :: features.
_FEATURE_DEFAULTS = {
    "job_search":            False,
    "call_recording":        False,
    "podcast_transcription": False,
    "research_pdfs":         False,
    "fundraising":           False,
}


def get_features(ctx: dict, user: dict | None = None) -> dict:
    """Resolve features for the current request: tenant default ⊕ user override.

    Lookup chain (per session-4 scope A):
      1. _FEATURE_DEFAULTS (every feature off)
      2. ctx['features'] (tenant defaults from firm_context.yaml)
      3. user['features'] (per-user override from users.json :: users[N].features)

    Each layer merges over the previous; later layers win per-key.
    Returns a dict with the full known-feature set populated.
    """
    out = dict(_FEATURE_DEFAULTS)
    out.update((ctx or {}).get("features") or {})
    out.update((user or {}).get("features") or {})
    return out


def feature_enabled(ctx: dict, name: str, user: dict | None = None) -> bool:
    """Convenience: True iff feature `name` is enabled for the user/tenant."""
    return bool(get_features(ctx, user).get(name, False))


def get_tile_label(ctx: dict, tile_id: str, default: str = "") -> str:
    """Per-tenant tile label override from firm_context.yaml :: tile_labels.

    Returns the override if present, else the supplied default (typically
    dashboard-tiles.yaml :: title).
    """
    labels = (ctx or {}).get("tile_labels") or {}
    return str(labels.get(tile_id, default))


def is_read_only(ctx: dict) -> bool:
    """True if this install should skip write pipelines (per scope A read_only).

    Use to gate write-side daemons (capture, gmail-mini, otter-backfill) on
    partner/secondary tomac installs that share the primary's Drive.
    """
    return bool((ctx or {}).get("read_only", False))


# ── Internal accessors ────────────────────────────────────────────────────────

def _principal(ctx: dict) -> dict:
    return ctx.get("principal", {})


def _firm(ctx: dict) -> dict:
    return ctx.get("firm", {})


def _deal_lead(ctx: dict) -> dict:
    """Return the deal-lead team member (first non-principal member with 'deal' in role)."""
    p_name = _principal(ctx).get("name", "")
    team = ctx.get("team", [])
    for m in team:
        if m.get("name") != p_name and "deal" in m.get("role", "").lower():
            return m
    # Fallback: first non-principal team member
    for m in team:
        if m.get("name") != p_name:
            return m
    return {}


# ── Public helpers ────────────────────────────────────────────────────────────

def owner_whitelist_str(ctx: dict) -> str:
    """Pipe-separated owner whitelist for JSON schema: 'Yoni|Mark|Nik'."""
    owners = ctx.get("owner_whitelist", [_principal(ctx).get("name", "Principal")])
    return "|".join(owners)


def workstream_deal(ctx: dict) -> str:
    return ctx.get("workstream_categories", {}).get("deal", "Deal")


def workstream_recruiting(ctx: dict) -> str:
    return ctx.get("workstream_categories", {}).get("recruiting", "Recruiting")


def peer_firms_str(ctx: dict) -> str:
    return ", ".join(ctx.get("peer_firms", []))


def cp_aliases(ctx: dict) -> list:
    """Return counterparty alias pairs as a flat list of (needle, canonical) tuples.

    Reads counterparty_aliases from firm_context.yaml. Each entry has:
      needles:   list of lowercase substrings to match (company names, person names,
                 deal nicknames, abbreviations — anything that refers to this entity)
      canonical: the display name to show in the dashboard

    Returns a list of (needle, canonical) tuples in the order they appear in the
    config — ordering matters since the first match wins in _normalize_cp().
    If counterparty_aliases is absent, returns an empty list (no aliases applied).
    """
    out = []
    for entry in ctx.get("counterparty_aliases", []):
        canon = entry.get("canonical", "")
        if not canon:
            continue
        for needle in entry.get("needles", []):
            if needle:
                out.append((str(needle).lower(), canon))
    return out


def principal_first_name(ctx: dict) -> str:
    return _principal(ctx).get("name", "Principal").split()[0]


# ── Analytical defaults ───────────────────────────────────────────────────────
# The memo section headers and optional focus supplement are the shipped defaults.
# They live here (not hardcoded in pipeline scripts) so new users get them for
# free, and any improvement pushed to GitHub propagates on the next git pull.
# A user who wants different sections overrides them in firm_context.yaml under
# prompt_overrides — their customization is never touched by upstream updates.

DEFAULT_MEMO_SECTIONS = [
    "THE CORE ARGUMENT",
    "POINTS OF CONSENSUS",
    "POINTS OF DISAGREEMENT OR TENSION",
    "OPEN QUESTIONS AND UNRESOLVED ISSUES",
    "WHAT YOU WOULD NEED TO FORM A VIEW",
    "KEY NAMES AND FIRMS",
]

# Per-section guidance appended in brackets after each header.
# Universal — applies to any firm using the default section set.
# If a user defines custom sections without matching keys here, the fallback
# instruction "[content for this section]" is used — still valid, just unguided.
DEFAULT_SECTION_GUIDANCE = {
    "THE CORE ARGUMENT": (
        "What does this call establish or change for the firm? "
        "One to two paragraphs. Lead with the so-what."
    ),
    "POINTS OF CONSENSUS": (
        "What was agreed with conviction? Attribute by name. "
        "Be specific — named firms, numbers, terms."
    ),
    "POINTS OF DISAGREEMENT OR TENSION": (
        "Where was there pushback, hedging, or conspicuous vagueness? "
        "What wasn't said?"
    ),
    "OPEN QUESTIONS AND UNRESOLVED ISSUES": (
        "Explicit uncertainty, missing data, pending decisions, "
        "regulatory/timing dependencies."
    ),
    "WHAT YOU WOULD NEED TO FORM A VIEW": (
        "Specific data, diligence questions, market checks, or expert "
        "conversations needed before acting. Verb-first."
    ),
    "KEY NAMES AND FIRMS": (
        "Every person and organization named in the call. One line each. "
        "Format: Name / Firm — role or context."
    ),
}


def get_memo_sections(ctx: dict) -> list:
    """Return memo section headers — from prompt_overrides if set, else defaults.

    Override in firm_context.yaml:
        prompt_overrides:
          memo_sections:
            - "EXECUTIVE SUMMARY"
            - "KEY ISSUES"
            - "NEXT STEPS"
    """
    return (
        ctx.get("prompt_overrides", {}).get("memo_sections")
        or DEFAULT_MEMO_SECTIONS
    )


def get_section_guidance(ctx: dict) -> dict:
    """Return per-section guidance dict — from prompt_overrides if set, else defaults.

    Override in firm_context.yaml:
        prompt_overrides:
          section_guidance:
            "EXECUTIVE SUMMARY": "One paragraph. Lead with the investment implication."
            "KEY ISSUES": "Bullet points. Specific risks, named counterparties."
    """
    return (
        ctx.get("prompt_overrides", {}).get("section_guidance")
        or DEFAULT_SECTION_GUIDANCE
    )


def get_memo_focus_supplement(ctx: dict) -> str:
    """Return optional extra instruction appended to memo prompt body.

    Override in firm_context.yaml:
        prompt_overrides:
          memo_focus_supplement: "Always flag any mention of FERC Order 1920."
    """
    return ctx.get("prompt_overrides", {}).get("memo_focus_supplement", "")


def build_memo_body(ctx: dict) -> str:
    """Build the static portion of the memo preamble from firm_context.yaml.

    Replaces the hardcoded _MEMO_BODY string in pipeline scripts. Callers do:
        MEMO_PREAMBLE = _fc.build_memo_header(ctx) + _fc.build_memo_body(ctx)
    """
    sections = get_memo_sections(ctx)
    guidance = get_section_guidance(ctx)
    supplement = get_memo_focus_supplement(ctx)

    lines = [
        "\nWrite a structured investment memo for the call transcript below. "
        "Use EXACTLY this format and section headers:\n"
    ]
    for section in sections:
        hint = guidance.get(section, "content for this section")
        lines.append(f"{section}\n[{hint}]\n")

    if supplement:
        lines.append(f"\nADDITIONAL FOCUS: {supplement}\n")

    lines.append(
        "Do NOT include an ACTION ITEMS section — that is handled separately downstream.\n"
        "Respond with the memo text only. No preamble, no commentary."
    )
    return "\n".join(lines)


# ── Preamble builders ─────────────────────────────────────────────────────────

def build_memo_header(ctx: dict) -> str:
    """
    Build the identity/instruction header for the Pass-1 (six-section memo) preamble.
    Caller appends the static memo-format instructions below this block.
    """
    p = _principal(ctx)
    f = _firm(ctx)
    dl = _deal_lead(ctx)
    team = ctx.get("team", [])

    p_name = p.get("name", "Principal")
    p_first = p_name.split()[0]
    dl_name = dl.get("name", "your co-founder")
    dl_bg = dl.get("background", "")
    dl_with_bg = f"{dl_name} ({dl_bg})" if dl_bg else dl_name

    focus_list = p.get("investment_focus", [])
    focus_str = (
        ", ".join(focus_list) if isinstance(focus_list, list) else str(focus_list)
    )

    team_parts = [f"{m['name']} ({m['role']})" for m in team]
    team_str = ", ".join(team_parts)

    dl_internal = dl.get("internal_call_role", "drives deal-status updates and agenda")
    # Collapse multi-line YAML block scalars to a single line
    dl_internal = " ".join(dl_internal.split())

    return (
        f"You are the Chief of Staff AI for {p_name} — "
        f"{p.get('role', 'senior investor')}, {p.get('background', '')}, "
        f"co-founding {f.get('name', 'the firm')} ({f.get('short_name', '')}) "
        f"with {dl_with_bg}.\n\n"
        f"INVESTMENT FOCUS (priority order): {focus_str}. "
        f"Write as a {p.get('investor_frame', 'principal investor')}: "
        f"so-what first, named assets and firms over themes, "
        f"investment implications over descriptions.\n\n"
        f"KEY PEOPLE: {team_str}.\n\n"
        f"SPEAKER IDENTIFICATION — CRITICAL:\n"
        f"- On {f.get('name', 'FIRM')} INTERNAL CALLS (weekly calls, internal debriefs): "
        f"{dl_name} is the primary speaker driving the agenda. "
        f"{dl_internal.rstrip('.')}. "
        f"{p_name} is consulted for external perspective, market context from "
        f"relationships, and validation. "
        f"Do NOT assign deal-driving statements to {p_name} on internal calls.\n"
        f"- On EXTERNAL CALLS (LP meetings, counterparty calls, expert calls, "
        f"recruiter calls): {p_name} is typically the primary "
        f"{f.get('short_name', 'firm')} representative. "
        f"{dl_name} may or may not be present.\n"
        f'- Identify {p_name} using: addressed by name as "{p_first}"; '
        f"on internal calls speaks from a listener/validator frame; "
        f'brings external relationship context ("when I was at [prior firm]...", '
        f'"I know X at that fund").\n'
        f"- If uncertain which speaker is {p_name} vs. {dl_name} on an internal "
        f"call, default to attributing deal-driving statements to {dl_name}."
    )


def build_backfill_header(ctx: dict) -> str:
    """
    Build the identity/instruction header for the Pass-2 (JSON extraction) preamble.
    Caller appends the static extraction-task instructions below this block.
    """
    p = _principal(ctx)
    f = _firm(ctx)
    dl = _deal_lead(ctx)

    p_name = p.get("name", "Principal")
    p_first = p_name.split()[0]
    dl_name = dl.get("name", "your co-founder")
    dl_role = dl.get("role", "deal lead")
    dl_bg = dl.get("background", "")
    dl_internal = dl.get("internal_call_role", "drives deal-status updates and agenda")
    dl_internal = " ".join(dl_internal.split())  # collapse YAML block scalars

    focus_list = p.get("investment_focus", [])
    focus_str = (
        ", ".join(focus_list) if isinstance(focus_list, list) else str(focus_list)
    )

    kp_parts = [f"{kp['name']} ({kp['context']})" for kp in ctx.get("key_people", [])]
    kp_str = ", ".join(kp_parts)

    peer_str = peer_firms_str(ctx)

    job_search = p.get("job_search_active", ctx.get("job_search_active", False))
    job_search_line = ""
    if job_search:
        job_search_line = (
            f"\n\nACTIVE JOB SEARCH: {p_name} is seeking MD-level roles at "
            f"infrastructure GPs. Any recruiter outreach, firm interest, or "
            f"interview activity is high-priority recruiting intel."
        )

    return (
        f"You are the Chief of Staff AI for {p_name} — "
        f"{p.get('role', 'senior investor')}, {p.get('background', '')}, "
        f"co-founding {f.get('name', 'the firm')} with {dl_name} ({dl_bg}).\n\n"
        f"INVESTMENT FOCUS (priority order): {focus_str}. "
        f"Frame all analysis as a {p.get('investor_frame', 'principal investor')}, "
        f"not an analyst — so-what first, named assets and firms over themes, "
        f"investment implications over descriptions.\n\n"
        f"KEY PEOPLE: {kp_str}. Flag any contact with these people in action_items.\n\n"
        f"SPEAKER TAG HANDLING — CRITICAL FOR OWNER ATTRIBUTION:\n"
        f'Otter.ai transcripts label speakers with GENERIC tags like "Speaker 1", '
        f'"Speaker 2", "Speaker 3", "Unknown Speaker" — almost never with real names. '
        f"Before extracting ANY action, identify which generic tag corresponds to "
        f"which real person:\n\n"
        f"1. FIRST PASS — identify {p_first}'s speaker tag:\n"
        f'   (a) Scan for explicit self-identification ("I\'m {p_first}", '
        f'"This is {p_name}", "{p_first} here").\n'
        f"   (b) If no explicit self-ID, use the PARTICIPANTS hint in the dynamic "
        f"block (extracted from the call title) to constrain candidates, then infer "
        f"from content: {p_name} is the {f.get('name', 'firm')} co-founder / "
        f"{p.get('role', 'investor')}. They ask diligence questions, reference past "
        f"deals and firm strategy, or — on recruiting calls — discuss their job "
        f"search and prior experience.\n"
        f"   (c) On {f.get('name', 'FIRM')} INTERNAL CALLS, the non-{p_first} "
        f"speaker is {dl_name} ({dl_role}). On external calls, the non-{p_first} "
        f"speaker is the named counterparty.\n"
        f"   (d) If you CANNOT confidently identify {p_first}'s speaker tag, "
        f"DO NOT GUESS — emit items with owner=\"unknown\" and the validator will "
        f"route them to routingExceptions for manual review.\n\n"
        f"2. APPLY ATTRIBUTION RULES:\n"
        f'   - Commitments spoken from {p_first}\'s tag → owner="{p_first}"\n'
        f"   - Commitments spoken from the other participant's tag → "
        f'owner="external", counterparty=their firm/name\n'
        f"   - This is the ONLY reliable way to distinguish awaiting_external "
        f'("they owe me") from my_action ("I owe them"). A commitment like '
        f'"I\'ll send you the FEA" means owner=external iff that line came from '
        f"the counterparty's speaker tag.\n\n"
        f"3. {f.get('short_name', 'FIRM')} INTERNAL CALL DEFAULT "
        f"(fallback when speakers are ambiguous):\n"
        f"   {dl_name} drives the agenda — {dl_internal}. "
        f"{p_name} validates and adds external perspective. "
        f"Do NOT assign deal-driving statements to {p_name} on internal calls.\n\n"
        f"4. EXTERNAL CALL DEFAULT: {p_name} is the primary "
        f"{f.get('short_name', 'firm')} voice.\n\n"
        f"FIRMS {p_first.upper()} TRACKS AS PEERS / CO-INVESTORS: {peer_str}."
        f"{job_search_line}"
    )


def build_extraction_header(ctx: dict) -> str:
    """
    Build the one-line identity header for the real-time transcript hook preamble.
    Caller appends the static extraction-task list below this block.
    """
    p = _principal(ctx)
    f = _firm(ctx)
    dl = _deal_lead(ctx)
    team = ctx.get("team", [])

    p_name = p.get("name", "Principal")
    dl_name = dl.get("name", "your co-founder")

    team_parts = [f"{p_name} (co-founder)"] + [
        f"{m['name']} ({m['role']})"
        for m in team
        if m.get("name") != p_name
    ]
    team_str = ", ".join(team_parts)

    return (
        f"You are the Chief of Staff AI for {p_name}, "
        f"{p.get('role', 'senior investor')} ({p.get('background', '')}), "
        f"co-founding {f.get('name', 'the firm')} ({f.get('short_name', '')}) "
        f"with {dl_name}. "
        f"Core {f.get('short_name', 'firm')} team: {team_str}."
    )
