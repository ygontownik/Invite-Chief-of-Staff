"""Shared Anthropic usage logger.

Appends one JSONL line per API call to ~/dashboards/data/anthropic-usage.jsonl.
Read by costs.py and the dashboard /api/costs endpoint. Fails silently — never raises.

Import pattern from a sibling routines/ subdir:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _usage import log_usage

API key taxonomy (env var names):
    ANTHROPIC_API_KEY_OTTER      — Otter transcript pipeline
    ANTHROPIC_API_KEY_EMAIL      — Email backfill, resolver, gmail-mini
    ANTHROPIC_API_KEY_PIPELINE   — Deal pipeline (Opus pass2)
    ANTHROPIC_API_KEY_RESEARCH   — Podcasts, GS/Jefferies research
    ANTHROPIC_API_KEY_BRIEFING   — Daily capture + personal briefing
    ANTHROPIC_API_KEY_DEV        — Interactive dev / one-off scripts
    ANTHROPIC_API_KEY            — Default fallback
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

_LOG = Path.home() / "dashboards/data/anthropic-usage.jsonl"

# Pricing per 1M tokens (input / output) — used only for spend-cap checks.
_PRICING = {
    "claude-opus-4-7":           {"in": 15.0, "out": 75.0, "cr": 1.5,  "cw": 18.75},
    "claude-sonnet-4-6":         {"in": 3.0,  "out": 15.0, "cr": 0.3,  "cw": 3.75},
    "claude-haiku-4-5-20251001": {"in": 0.8,  "out": 4.0,  "cr": 0.08, "cw": 1.0},
}
_DEFAULT_PRICING = {"in": 3.0, "out": 15.0, "cr": 0.3, "cw": 3.75}

# Ordered list of env var names to try when resolving an API key.
# The first non-empty value wins. Callers can pass key_env to pick a named key.
KEY_TAXONOMY = [
    "ANTHROPIC_API_KEY_OTTER",
    "ANTHROPIC_API_KEY_EMAIL",
    "ANTHROPIC_API_KEY_PIPELINE",
    "ANTHROPIC_API_KEY_RESEARCH",
    "ANTHROPIC_API_KEY_BRIEFING",
    "ANTHROPIC_API_KEY_DEV",
    "ANTHROPIC_API_KEY",
]

# Site → preferred key env var. Used by _claude_dispatch to auto-pick the right key.
SITE_KEY_MAP: dict[str, str] = {
    "cos_otter_backfill":       "ANTHROPIC_API_KEY_OTTER",
    "cos_otter_backfill_memo":  "ANTHROPIC_API_KEY_OTTER",
    "cos_email_backfill":       "ANTHROPIC_API_KEY_EMAIL",
    "cos_email_resolver":       "ANTHROPIC_API_KEY_EMAIL",
    "cos_gmail_mini_haiku":     "ANTHROPIC_API_KEY_EMAIL",
    "cos_gmail_mini_sonnet":    "ANTHROPIC_API_KEY_EMAIL",
    "pass1_source_scanner":     "ANTHROPIC_API_KEY_PIPELINE",
    "pass2_pipeline_analyst":   "ANTHROPIC_API_KEY_PIPELINE",
    "pass3_ic_memo":            "ANTHROPIC_API_KEY_PIPELINE",
    "podcast_transcribe":       "ANTHROPIC_API_KEY_RESEARCH",
    "gs_research":              "ANTHROPIC_API_KEY_RESEARCH",
    "jefferies_research":       "ANTHROPIC_API_KEY_RESEARCH",
    "cos_capture_pipeline":     "ANTHROPIC_API_KEY_BRIEFING",
    "cos_personal_briefing":    "ANTHROPIC_API_KEY_BRIEFING",
    "dash_corrections_proposer": "ANTHROPIC_API_KEY_RESEARCH",
    "call_recorder_memo":        "ANTHROPIC_API_KEY_BRIEFING",
}


def resolve_api_key(site: str = "", key_env: str = "") -> tuple[str, str]:
    """Return (api_key_value, key_env_name) for the given site or explicit key_env.

    Lookup order:
      1. Explicit key_env arg (if provided and non-empty).
      2. SITE_KEY_MAP entry for the site.
      3. ANTHROPIC_API_KEY fallback.
    Returns ("", "") if no key is found.
    """
    candidates = []
    if key_env:
        candidates.append(key_env)
    if site and site in SITE_KEY_MAP:
        candidates.append(SITE_KEY_MAP[site])
    candidates.append("ANTHROPIC_API_KEY")

    for env_name in candidates:
        val = os.environ.get(env_name, "")
        if val:
            return val, env_name
    return "", ""


def _entry_cost(e: dict) -> float:
    p = _PRICING.get(e.get("model", ""), _DEFAULT_PRICING)
    return (
        e.get("in", 0) * p["in"] +
        e.get("out", 0) * p["out"] +
        e.get("cache_read", 0) * p["cr"] +
        e.get("cache_create", 0) * p["cw"]
    ) / 1_000_000


def get_today_spend(key_name: str = "") -> float:
    """Return USD spent today (UTC), optionally filtered to a specific key_name."""
    today = datetime.now(timezone.utc).date().isoformat()
    total = 0.0
    try:
        if not _LOG.exists():
            return 0.0
        with open(_LOG) as f:
            for line in f:
                try:
                    e = json.loads(line.strip())
                    if not e.get("ts", "").startswith(today):
                        continue
                    if key_name and e.get("key_name", "") != key_name:
                        continue
                    total += _entry_cost(e)
                except Exception:
                    pass
    except Exception:
        pass
    return total


def check_daily_cap(cap_usd: float, site: str = "", key_name: str = "") -> None:
    """Raise RuntimeError if today's spend for key_name already meets or exceeds cap_usd.

    Called by _claude_dispatch._api_call() before every API call when
    firm_context.yaml sets daily_api_cap_usd. Silently passes if the log
    is unreadable so a missing log never blocks pipeline runs.
    """
    try:
        spent = get_today_spend(key_name=key_name)
        if spent >= cap_usd:
            raise RuntimeError(
                f"[_usage] daily API cap hit: ${spent:.3f} >= ${cap_usd:.2f} "
                f"(key={key_name or 'default'}, site={site}). "
                "Set a higher daily_api_cap_usd in firm_context.yaml or pass "
                "CLAUDE_AUTH_MODE=subscription to use subscription path."
            )
    except RuntimeError:
        raise
    except Exception:
        pass  # log unreadable — don't block


def log_usage(site: str, model: str, resp: dict, key_name: str = "") -> None:
    """Append one JSONL usage record. key_name is the env var that supplied the API key."""
    try:
        u = resp.get("usage", {}) if isinstance(resp, dict) else {}
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts":           datetime.now(timezone.utc).isoformat(),
            "site":         site,
            "model":        model,
            "in":           u.get("input_tokens", 0),
            "out":          u.get("output_tokens", 0),
            "cache_read":   u.get("cache_read_input_tokens", 0),
            "cache_create": u.get("cache_creation_input_tokens", 0),
        }
        if key_name:
            record["key_name"] = key_name
        with open(_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
