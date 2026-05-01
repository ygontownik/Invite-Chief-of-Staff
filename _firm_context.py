#!/usr/bin/env python3
"""
_firm_context.py — Firm identity loader and prompt-preamble builder.

All COS pipeline scripts import this module to eliminate hardcoded
principal/firm/team references. Reads firm_context.yaml from the
tomac-cove-pipeline directory; falls back to firm_context.json if
PyYAML is not installed.

Usage in a pipeline script:
    from pathlib import Path
    import sys
    _PIPELINE_DIR = Path.home() / "tomac-cove-pipeline"
    if str(_PIPELINE_DIR) not in sys.path:
        sys.path.insert(0, str(_PIPELINE_DIR))
    import _firm_context as _fc
    _CTX = _fc.load_firm_context()

    MEMO_PREAMBLE    = _fc.build_memo_header(_CTX) + MEMO_BODY
    BACKFILL_PREAMBLE = _fc.build_backfill_header(_CTX) + BACKFILL_BODY
"""
from pathlib import Path
import json

_PIPELINE_DIR = Path(__file__).parent
_CONTEXT_YAML = _PIPELINE_DIR / "firm_context.yaml"
_CONTEXT_JSON = _PIPELINE_DIR / "firm_context.json"


# ── Loader ────────────────────────────────────────────────────────────────────

def load_firm_context() -> dict:
    """Load firm_context.yaml. Falls back to firm_context.json if PyYAML not installed."""
    # Try YAML first (preferred: human-readable, supports comments)
    try:
        import yaml  # pip install pyyaml
        if _CONTEXT_YAML.exists():
            with open(_CONTEXT_YAML) as f:
                return yaml.safe_load(f) or {}
    except ImportError:
        pass
    # JSON fallback — works with stdlib only
    if _CONTEXT_JSON.exists():
        with open(_CONTEXT_JSON) as f:
            return json.load(f)
    raise FileNotFoundError(
        f"No firm context file found. Expected one of:\n"
        f"  {_CONTEXT_YAML}\n  {_CONTEXT_JSON}\n"
        "Copy firm_context.yaml from the repository and fill in your firm's details."
    )


def load_drive_docs(drive_docs_path=None) -> dict:
    """Load drive-docs.yaml and return a flat key→id mapping.

    Keys from docs section map to doc_id; keys from folders section map to folder_id.
    Falls back gracefully — returns {} if the file is missing or unparseable.

    Usage in scripts:
        _DOCS = _fc.load_drive_docs()
        FOLLOW_UPS_DOC = _DOCS.get("followups", "")
        TOMAC_DOC      = _DOCS.get("tomac_pipeline", "")
    """
    if drive_docs_path is None:
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


def load_active_packages(firm_config_path=None) -> list:
    """Return the list of active package names from firm_config.json.

    Returns ["market_intelligence", "operations"] by default (both active).
    A firm that only deploys Package B would set: "packages": ["operations"]
    """
    if firm_config_path is None:
        firm_config_path = _PIPELINE_DIR / "firm_config.json"
    try:
        with open(firm_config_path) as f:
            cfg = json.load(f)
        return cfg.get("packages", ["market_intelligence", "operations"])
    except (FileNotFoundError, json.JSONDecodeError):
        return ["market_intelligence", "operations"]


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


def principal_first_name(ctx: dict) -> str:
    return _principal(ctx).get("name", "Principal").split()[0]


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
