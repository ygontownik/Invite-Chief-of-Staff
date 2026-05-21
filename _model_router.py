#!/usr/bin/env python3
# noqa: claude-dispatch-exempt — this file IS the canonical dispatcher.
# Per L0023, all callers must route through `from _claude_dispatch import call`,
# which then routes through this file's `call_claude()` to either the
# subscription path or the raw anthropic SDK on the API path. The api-path
# `from anthropic import Anthropic` further down (~line 639) is the legitimate
# terminal call — adding the noqa marker here so check_l0023.py skips this file.
"""
_model_router.py — Phase 2 Track C model router + subscription dispatch.

Public API:
    call_claude(task_type, system, messages, mode='auto',
                max_tokens=None, cache=True, tenant=None) -> dict

Per PLAN_v3.1 §Track C and CLAUDE.md "Per-pass model assignments" table.

Dispatch modes (per routine `mode` field in routines.yaml):
    subscription -> claude_agent_sdk.query() async generator (Path b
                    per CSPIKE_PLAN.md decision section, locked
                    2026-05-03 22:00). Uses minimal ClaudeAgentOptions
                    to keep cache_creation_input_tokens at ~0 per call.
    api          -> anthropic SDK (genuine implementation)
    daemon       -> ValueError (daemons are not callable Claude tasks)

Cost tracking (PLAN C5/C6):
    Each api call appends a JSON line to
        ~/cos-pipeline/data-<tenant>/costs/YYYY-MM-DD.jsonl
    Each subscription call appends a JSON line to
        ~/cos-pipeline/data-<tenant>/dispatch.jsonl
    (subscription est_usd is always 0 — no per-token billing.)

Quotas (PLAN C6):
    sum today's est_usd; warn at max_daily_usd (stderr);
    hard-stop at 3x cap (raise QuotaExceeded). Quotas only apply to
    api mode — subscription mode bills against the 5-hour window, not
    USD, and surfaces window state via the structured RateLimitEvent
    captured in the dispatch ledger.

Self-test:
    python3 _model_router.py --dry-run

Stdlib + anthropic SDK (api mode) + claude_agent_sdk (subscription mode).
The subscription import is lazy so tenants on api mode never need the
SDK installed. Falls back to a minimal embedded YAML reader if PyYAML
is unavailable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────
# Constants — model IDs from CLAUDE.md (Claude 4.x family).
# ─────────────────────────────────────────────────────────────────────

MODEL_OPUS_4_7   = "claude-opus-4-7"
MODEL_SONNET_4_6 = "claude-sonnet-4-6"
MODEL_HAIKU_4_5  = "claude-haiku-4-5-20251001"

# Per-1M-token pricing in USD. Mirrors costs.py.
_PRICING = {
    MODEL_OPUS_4_7:   {"in": 15.00, "out": 75.00},
    MODEL_SONNET_4_6: {"in":  3.00, "out": 15.00},
    MODEL_HAIKU_4_5:  {"in":  0.80, "out":  4.00},
}
_DEFAULT_PRICING = {"in": 3.00, "out": 15.00}
# Cache-read tokens billed at 10% of input rate (Anthropic spec).
_CACHE_READ_FACTOR = 0.10

# Default per-task daily soft cap in USD (overridable by routines.yaml).
_DEFAULT_MAX_DAILY_USD = 5.00
_HARD_STOP_MULTIPLIER = 3.0

# Default tenant — read from environment with generic fallback so the
# public codebase carries no firm-specific slug.
_DEFAULT_TENANT = os.environ.get("COS_TENANT_SLUG", "default")

_PIPELINE_ROOT = Path.home() / "cos-pipeline"
_ROUTINES_YAML = _PIPELINE_ROOT / "routines.yaml"
_FIRM_CONTEXT  = _PIPELINE_ROOT / "firm_context.yaml"


# ─────────────────────────────────────────────────────────────────────
# Subscription fallback policy — per-task classification.
# Locked 2026-05-03 evening. Time-insensitive routines queue and retry
# when the 5-hour window resets; time-sensitive routines hard-fail and
# rely on the next launchd fire to retry.
# ─────────────────────────────────────────────────────────────────────

TIME_INSENSITIVE_TASKS = {
    # Briefing pipeline — daily/weekly digests; +1h delay tolerable.
    "morning-briefing", "cos-personal-briefing", "briefing-morning",
    "daily-intelligence-digest", "weekly-intelligence-digest",
    "notebooklm-daily-briefing", "notebooklm-sunday-weekly-briefing",
    "weekly-summary-email", "sunday-weekly-email", "briefing-weekly", "master-daily-update",
    # Podcast transcription — overnight batch.
    "podcast-processing", "podcast-transcribe-daily", "podcasts-transcribe",
    # Deal pipeline — weekly cadence; queue safe.
    "deal-pipeline-scan", "deals-weekly-scan",
    "deal-dashboard-compile", "deals-compile",
    # Research feeds — daily / weekly batches.
    "gs-research-fetch", "gs-research-process",
    "gs-research-daily-download", "gs-research-pdf-processor",
    "jefferies-research-fetch", "jefferies-research-process",
    "jefferies-pdf-downloader", "jefferies-pdf-processor",
    "rbn-energy-daily", "rbn-daily-sync",
    "substack-sync", "run-syncall-gas",
    "peakload-weekly", "peakload-weekly-sync",
}

TIME_SENSITIVE_TASKS = {
    # Otter post-call hook — must process while transcript is fresh.
    "cos-otter-transcripts", "otter-backfill", "capture-call-transcripts",
    "cos-transcript-hook",
    # Gmail-mini triage — fires every 2h; missing one fire degrades
    # inbox responsiveness; next fire retries.
    "cos-gmail-mini", "capture-email-triage", "gmail-mini",
    # Capture pipeline — morning fire only; can't queue past breakfast.
    "inbox-capture", "cos-capture-pipeline", "capture-inbox",
}


# ─────────────────────────────────────────────────────────────────────
# Dataclasses.
# ─────────────────────────────────────────────────────────────────────

@dataclass
class ModelRoute:
    """Resolved routing decision for a task_type."""
    task_type: str
    model: str
    mode: str            # 'subscription' | 'api' | 'daemon'
    max_tokens: int
    package: Optional[str] = None
    domain: Optional[str] = None
    source: str = "default"   # where the route came from (debug)


@dataclass
class Quotas:
    """Per-task daily spend caps (PLAN C6)."""
    task_type: str
    max_daily_usd: float = _DEFAULT_MAX_DAILY_USD
    hard_stop_usd: float = field(init=False)

    def __post_init__(self) -> None:
        self.hard_stop_usd = self.max_daily_usd * _HARD_STOP_MULTIPLIER


class QuotaExceeded(RuntimeError):
    """Raised when today's spend for a task_type exceeds 3x the soft cap."""


# ─────────────────────────────────────────────────────────────────────
# CLAUDE.md per-pass defaults (deal pipeline) + general defaults.
# ─────────────────────────────────────────────────────────────────────

# Per-pass deal-pipeline assignments straight from CLAUDE.md table.
_PASS_DEFAULTS: dict[str, tuple[str, int]] = {
    "pass1_source_scanner":   (MODEL_SONNET_4_6, 2048),
    "pass2_pipeline_analyst": (MODEL_OPUS_4_7,   4096),
    "pass3_ic_memo":          (MODEL_SONNET_4_6, 4096),
}

# Package-level defaults. CLAUDE.md says "default Sonnet 4.6 for
# non-pipeline scripts; max_tokens 2048 for memos, 1024 for shorter
# summaries." We pick conservative middle defaults per package.
_PACKAGE_DEFAULTS: dict[str, tuple[str, int]] = {
    "capture":  (MODEL_SONNET_4_6, 2048),
    "briefing": (MODEL_SONNET_4_6, 2048),
    "research": (MODEL_SONNET_4_6, 2048),
    "deals":    (MODEL_OPUS_4_7,   4096),  # deal ideation = Pass 2 class
    "server":   (MODEL_HAIKU_4_5,  1024),
    "infra":    (MODEL_HAIKU_4_5,  1024),
}

_OVERALL_DEFAULT = (MODEL_SONNET_4_6, 2048)


# ─────────────────────────────────────────────────────────────────────
# Minimal YAML reader (works for the flat-ish shapes in routines.yaml
# and firm_context.yaml; falls back to PyYAML if installed).
# ─────────────────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    text = path.read_text()
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except Exception:
        return _tiny_yaml(text)


def _tiny_yaml(text: str) -> Any:
    """
    Bare-bones YAML reader sufficient for routines.yaml structure.
    Supports nested mappings and lists-of-mappings keyed by indentation
    (2 spaces). Strings are unquoted. Comments and blank lines stripped.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    def _coerce(v: str) -> Any:
        v = v.strip()
        if v in ("", "null", "~"):
            return None
        if v in ("true", "True"):  return True
        if v in ("false", "False"): return False
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            return v[1:-1]
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()

        # Pop until the parent at < indent.
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root

        if body.startswith("- "):
            item_body = body[2:]
            if ":" in item_body:
                key, _, val = item_body.partition(":")
                new_item: dict[str, Any] = {key.strip(): _coerce(val)}
            else:
                new_item = {"_value": _coerce(item_body)}
            if not isinstance(parent, list):
                # Convert: parent dict's most-recent-key list — we don't
                # have that info, so attach under a synthetic list at
                # whatever the prior mapping pointed to. Caller tolerant.
                parent.setdefault("_list", []).append(new_item)
            else:
                parent.append(new_item)
            stack.append((indent, new_item))
        elif body.endswith(":"):
            key = body[:-1].strip()
            child: list[Any] = []
            if isinstance(parent, dict):
                parent[key] = child
            stack.append((indent, child))
            # Next line decides if it stays a list or becomes a dict.
            stack.append((indent + 1, child))
        else:
            if ":" in body:
                key, _, val = body.partition(":")
                if isinstance(parent, dict):
                    parent[key.strip()] = _coerce(val)
                elif isinstance(parent, list) and parent and isinstance(parent[-1], dict):
                    parent[-1][key.strip()] = _coerce(val)
    return root


# ─────────────────────────────────────────────────────────────────────
# Routine + tenant config lookup.
# ─────────────────────────────────────────────────────────────────────

def _load_routines() -> list[dict]:
    """Flatten routines.yaml :: skills + daemons + server into one list."""
    data = _load_yaml(_ROUTINES_YAML)
    if not data:
        return []
    out: list[dict] = []
    for section in ("server", "skills", "daemons"):
        block = data.get(section) if isinstance(data, dict) else None
        if isinstance(block, list):
            out.extend([r for r in block if isinstance(r, dict)])
    return out


def _find_routine(task_type: str, routines: list[dict]) -> Optional[dict]:
    """Match task_type against routine `name`, `rename_to`, or `package`."""
    for r in routines:
        name = (r.get("name") or "")
        rename = (r.get("rename_to") or "")
        if name == task_type or rename == task_type:
            return r
    # Fallback — match by package (returns first; package defaults
    # provide model selection regardless).
    for r in routines:
        if r.get("package") == task_type:
            return r
    return None


def _load_tenant_override(tenant: str) -> dict:
    """Per-tenant override at ~/cos-pipeline-config-<tenant>/model_router.yaml."""
    cfg = Path.home() / f"cos-pipeline-config-{tenant}" / "model_router.yaml"
    return _load_yaml(cfg) or {}


def _load_domain() -> Optional[str]:
    fc = _load_yaml(_FIRM_CONTEXT)
    if isinstance(fc, dict):
        return fc.get("domain")
    return None


def _load_auth_mode() -> Optional[str]:
    """Read firm_context.yaml :: auth_mode (subscription|api|None).

    Prefers the canonical accessor in _firm_context.py once that module
    exposes load_auth_mode() (post pass-3 cutover). Falls back to a
    direct YAML read so this file works against today's _firm_context.py
    too. Returns None when the field is absent.
    """
    # Prefer the canonical accessor when available.
    try:
        import _firm_context as _fc  # type: ignore
        getter = getattr(_fc, "load_auth_mode", None)
        if callable(getter):
            return getter()
    except Exception:
        pass

    # Stdlib fallback — direct YAML read.
    fc = _load_yaml(_FIRM_CONTEXT)
    if not isinstance(fc, dict):
        return None
    val = fc.get("auth_mode")
    if val in ("subscription", "api"):
        return val
    return None


def _load_claude_projects(tenant: str) -> dict:
    """Read firm_config.json :: claude_projects ({} when absent).

    Prefers _firm_context.load_claude_projects() when available (post
    pass-3 cutover). Falls back to a direct JSON read across the
    standard candidate paths. Empty values are treated as "no project
    assigned, inline preamble." See SUBSCRIPTION_INSTALL.md for the
    install-time provisioning flow that populates this.
    """
    try:
        import _firm_context as _fc  # type: ignore
        getter = getattr(_fc, "load_claude_projects", None)
        if callable(getter):
            return getter() or {}
    except Exception:
        pass

    # Stdlib fallback — direct JSON read across candidate locations.
    # Mirrors _firm_context.py's _find_config_dir() heuristic.
    candidates = [
        Path.home() / f"cos-pipeline-config-{tenant}" / "firm_config.json",
        Path.home() / "cos-pipeline-config" / "firm_config.json",
        _PIPELINE_ROOT / "firm_config.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        cp = data.get("claude_projects")
        if isinstance(cp, dict):
            return cp
    return {}


# ─────────────────────────────────────────────────────────────────────
# Route resolution.
# ─────────────────────────────────────────────────────────────────────


def _apply_auth_mode_override(routine_mode: Optional[str],
                              auth_mode: Optional[str]) -> Optional[str]:
    """Resolve the effective mode given routine + tenant auth_mode.

    Rules:
      - daemon routines are NEVER overridden (they're not callable).
      - auth_mode='subscription' forces non-daemon routines to subscription.
      - auth_mode='api' forces non-daemon routines to api.
      - auth_mode=None (absent) leaves routine_mode untouched.
    """
    if routine_mode == "daemon":
        return routine_mode
    if auth_mode in ("subscription", "api"):
        return auth_mode
    return routine_mode

def resolve_route(task_type: str, mode: str = "auto",
                  tenant: Optional[str] = None) -> ModelRoute:
    """
    Resolve a ModelRoute for `task_type`.

    Precedence (highest -> lowest):
      1. explicit `mode` arg passed by the caller (anything other than 'auto')
      2. firm_context.yaml :: auth_mode (tenant-level hard override; only
         applies when caller passed mode='auto'; never overrides daemon)
      3. per-tenant model_router.yaml override (mode='auto' only)
      4. routines.yaml entry (provides package + mode + max_daily_usd)
      5. CLAUDE.md per-pass defaults (pass1_/pass2_/pass3_ keys)
      6. CLAUDE.md package defaults
      7. overall default (Sonnet 4.6 / 2048)
    """
    tenant = tenant or _DEFAULT_TENANT
    domain = _load_domain()
    auth_mode = _load_auth_mode() if mode == "auto" else None

    def _final_mode(routine_mode: Optional[str]) -> str:
        # Caller's explicit mode wins everything else.
        if mode != "auto":
            return mode
        # Then tenant auth_mode (HARD override, except daemon).
        overridden = _apply_auth_mode_override(routine_mode, auth_mode)
        return overridden or "api"

    # 1. Tenant override (per-tenant model_router.yaml)
    tenant_cfg = _load_tenant_override(tenant)
    routes_cfg = (tenant_cfg or {}).get("routes", {}) or {}
    if isinstance(routes_cfg, dict) and task_type in routes_cfg:
        entry = routes_cfg[task_type] or {}
        if isinstance(entry, dict):
            model = entry.get("model") or _OVERALL_DEFAULT[0]
            r_mode = _final_mode(entry.get("mode"))
            max_t  = int(entry.get("max_tokens") or _OVERALL_DEFAULT[1])
            return ModelRoute(task_type, model, r_mode, max_t,
                              package=entry.get("package"),
                              domain=domain, source="tenant_override")

    # 2. routines.yaml entry
    routines = _load_routines()
    routine = _find_routine(task_type, routines)
    package = (routine or {}).get("package")
    routine_mode = (routine or {}).get("mode")

    # 3. Per-pass defaults
    if task_type in _PASS_DEFAULTS:
        model, max_t = _PASS_DEFAULTS[task_type]
        return ModelRoute(task_type, model, _final_mode(routine_mode), max_t,
                          package=package, domain=domain,
                          source="claudemd_per_pass")

    # 4. Package defaults
    if package and package in _PACKAGE_DEFAULTS:
        model, max_t = _PACKAGE_DEFAULTS[package]
    else:
        model, max_t = _OVERALL_DEFAULT

    return ModelRoute(task_type, model, _final_mode(routine_mode), max_t,
                      package=package, domain=domain,
                      source="package_default" if package else "overall_default")


# ─────────────────────────────────────────────────────────────────────
# Cost tracking (PLAN C5/C6).
# ─────────────────────────────────────────────────────────────────────

def _costs_path(tenant: str, day: Optional[datetime] = None) -> Path:
    day = day or datetime.now(timezone.utc)
    p = _PIPELINE_ROOT / f"data-{tenant}" / "costs" / f"{day.strftime('%Y-%m-%d')}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _estimate_cost(model: str, in_tok: int, out_tok: int,
                   cached_in_tok: int) -> float:
    p = _PRICING.get(model, _DEFAULT_PRICING)
    fresh_in = max(in_tok - cached_in_tok, 0)
    cost = (fresh_in / 1e6) * p["in"]
    cost += (cached_in_tok / 1e6) * p["in"] * _CACHE_READ_FACTOR
    cost += (out_tok / 1e6) * p["out"]
    return round(cost, 6)


def _today_spend(task_type: str, tenant: str) -> float:
    path = _costs_path(tenant)
    if not path.exists():
        return 0.0
    total = 0.0
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("task_type") == task_type:
            total += float(row.get("est_usd", 0.0))
    return total


def _record_cost(*, tenant: str, task_type: str, model: str, mode: str,
                 in_tok: int, out_tok: int, cached_in_tok: int) -> float:
    est = _estimate_cost(model, in_tok, out_tok, cached_in_tok)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_type": task_type,
        "model": model,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cached_input_tokens": cached_in_tok,
        "mode": mode,
        "est_usd": est,
    }
    with _costs_path(tenant).open("a") as f:
        f.write(json.dumps(row) + "\n")
    return est


def _resolve_quota(task_type: str, tenant: str) -> Quotas:
    # Tenant override takes precedence.
    tcfg = _load_tenant_override(tenant)
    quotas_cfg = (tcfg or {}).get("quotas", {}) or {}
    if isinstance(quotas_cfg, dict) and task_type in quotas_cfg:
        cap = float(quotas_cfg[task_type])
        return Quotas(task_type=task_type, max_daily_usd=cap)
    # Otherwise routines.yaml's max_daily_usd field.
    routine = _find_routine(task_type, _load_routines())
    if routine and routine.get("max_daily_usd"):
        return Quotas(task_type=task_type,
                      max_daily_usd=float(routine["max_daily_usd"]))
    return Quotas(task_type=task_type)


def _check_quota(task_type: str, tenant: str) -> Quotas:
    q = _resolve_quota(task_type, tenant)
    spend = _today_spend(task_type, tenant)
    if spend >= q.hard_stop_usd:
        raise QuotaExceeded(
            f"task_type={task_type} tenant={tenant} "
            f"today_spend=${spend:.2f} >= hard_stop=${q.hard_stop_usd:.2f}"
        )
    if spend >= q.max_daily_usd:
        sys.stderr.write(
            f"[model_router] WARN: {task_type} ({tenant}) at "
            f"${spend:.2f} of soft cap ${q.max_daily_usd:.2f}\n"
        )
    return q


# ─────────────────────────────────────────────────────────────────────
# Cache discipline (PLAN C3).
# ─────────────────────────────────────────────────────────────────────

def _attach_cache_control(system: Any) -> Any:
    """
    Wrap the firm_context preamble with cache_control={'type':'ephemeral'}.
    Anthropic SDK accepts `system` as either a string or a list of blocks;
    we normalize to the block form when caching is requested.
    """
    if system is None:
        return None
    if isinstance(system, list):
        # Caller already supplied blocks — mark the first one ephemeral
        # if not already marked.
        if system and isinstance(system[0], dict) and "cache_control" not in system[0]:
            system[0] = {**system[0], "cache_control": {"type": "ephemeral"}}
        return system
    return [{
        "type": "text",
        "text": str(system),
        "cache_control": {"type": "ephemeral"},
    }]


# ─────────────────────────────────────────────────────────────────────
# Public entry point.
# ─────────────────────────────────────────────────────────────────────

def call_claude(task_type: str,
                system: Any,
                messages: list[dict],
                mode: str = "auto",
                max_tokens: Optional[int] = None,
                cache: bool = True,
                tenant: Optional[str] = None,
                extract_json: bool = False) -> dict:
    """
    Dispatch a Claude call per the resolved route.

    Returns a dict:
        {
          'route':  asdict(ModelRoute),
          'text':   <assistant text>,
          'usage':  {'input_tokens', 'output_tokens', 'cached_input_tokens'},
          'est_usd': <float>,
          # subscription mode also populates:
          'subscription_meta': {'rate_limit_status', 'rate_limit_resets_at'},
          # if subscription dispatch hits a rate-limit on a TIME_INSENSITIVE
          # task, the call is queued and returns:
          'queued':       True,
          'queue_until':  <unix ts or ISO string>,
        }

    Raises:
      ValueError    — mode='daemon' (daemons aren't callable).
      QuotaExceeded — daily spend >= 3x soft cap for this task_type
                      (api mode only).
      RuntimeError  — claude_agent_sdk not installed when subscription
                      dispatch is requested.
      ProcessError / ClaudeSDKError —
                      subscription dispatch on a TIME_SENSITIVE or
                      unrecognized task; surfaces the raw failure so
                      the launchd retry can reschedule.
    """
    tenant = tenant or _DEFAULT_TENANT
    route = resolve_route(task_type, mode=mode, tenant=tenant)
    if max_tokens:
        route.max_tokens = int(max_tokens)

    if route.mode == "daemon":
        raise ValueError(
            f"task_type={task_type!r} resolves to mode='daemon'; "
            "daemons are long-running processes, not Claude calls. "
            "Pick an api/subscription routine."
        )

    if route.mode == "subscription":
        return _dispatch_subscription(
            route=route,
            system=system,
            messages=messages,
            cache=cache,
            tenant=tenant,
            extract_json=extract_json,
        )

    if route.mode != "api":
        raise ValueError(f"unknown mode {route.mode!r}")

    # — API path —
    _check_quota(task_type, tenant)

    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK not installed. `pip install anthropic` "
            "or set mode='subscription' (requires claude-agent-sdk)."
        ) from e

    client = Anthropic()
    sys_param = _attach_cache_control(system) if cache else system
    kwargs: dict[str, Any] = {
        "model": route.model,
        "max_tokens": route.max_tokens,
        "messages": messages,
    }
    if sys_param is not None:
        kwargs["system"] = sys_param

    resp = client.messages.create(**kwargs)

    # Extract text + usage in a tolerant way (works for SDK objects and
    # dicts returned by mocks).
    def _g(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    content = _g(resp, "content", []) or []
    text = ""
    for block in content:
        btext = _g(block, "text", "")
        if btext:
            text += btext

    usage = _g(resp, "usage", {}) or {}
    in_tok       = int(_g(usage, "input_tokens", 0) or 0)
    out_tok      = int(_g(usage, "output_tokens", 0) or 0)
    cached_in    = int(_g(usage, "cache_read_input_tokens", 0) or 0)

    est = _record_cost(
        tenant=tenant, task_type=task_type, model=route.model,
        mode=route.mode, in_tok=in_tok, out_tok=out_tok,
        cached_in_tok=cached_in,
    )

    return {
        "route": asdict(route),
        "text": text,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cached_input_tokens": cached_in,
        },
        "est_usd": est,
    }


# ─────────────────────────────────────────────────────────────────────
# Subscription dispatch (Path b per CSPIKE_PLAN.md decision section).
#
# Uses claude_agent_sdk.query() with minimal ClaudeAgentOptions to
# avoid loading plugins / MCPs / skills / settings that pipelines do
# not need. Measured 2026-05-03: this drops cache_creation_input_tokens
# from 52,384 → 0 per call (>99% reduction), the cost story for new
# tenants picking subscription mode.
# ─────────────────────────────────────────────────────────────────────

def _dispatch_subscription(*, route: ModelRoute, system: Any,
                           messages: list[dict], cache: bool,
                           tenant: str, extract_json: bool = False) -> dict:
    """Production subscription dispatch via claude_agent_sdk.query()."""
    import asyncio
    try:
        from claude_agent_sdk import ClaudeAgentOptions  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "claude_agent_sdk not installed. Run "
            "`/opt/homebrew/bin/python3 -m pip install --break-system-packages "
            "claude-agent-sdk` (requires Python >= 3.10). Or set "
            "firm_context.yaml :: auth_mode=api to use the anthropic SDK path."
        ) from e

    # Convert (system, messages) into a single prompt for query().
    # API mode passes system + messages separately; subscription dispatch
    # via the SDK takes a single prompt string. We inline the system at
    # the top of the prompt — this is the v1 pattern. v2 (CSPIKE_PLAN
    # Probe 5) will move the system into a Claude.ai project.
    prompt = _build_subscription_prompt(system, messages)

    # `cache` is honored implicitly: subscription dispatch does not bill
    # per token, so cache_control attachment is moot. Argument retained
    # for call-site parity with the api path.
    _ = cache

    return asyncio.run(_run_subscription_query(
        route=route, prompt=prompt, tenant=tenant,
        original_system=system, original_messages=messages,
        extract_json=extract_json,
    ))


async def _run_subscription_query(*, route: ModelRoute, prompt: str,
                                   tenant: str,
                                   original_system: Any = None,
                                   original_messages: Optional[list[dict]] = None,
                                   extract_json: bool = False) -> dict:
    """Async helper: drives the SDK async generator and accumulates state.

    original_system / original_messages are persisted into queue.jsonl on
    rate-limit so the queue-drain daemon can re-fire the call as-is.
    """
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, ProcessError, ClaudeSDKError,
    )

    # Read claude_projects from firm_config.json — populated by the
    # subscription installer (setup.sh.subscription.next, S3). v1
    # records the project_id in telemetry only; v2 (Probe 5 deferred)
    # will pass it to the SDK once a project field is exposed.
    projects = _load_claude_projects(tenant)
    project_id = projects.get(route.package or "") if route.package else None

    # Clear ANTHROPIC_API_KEY in the CLI subprocess env so the bundled CLI
    # uses Claude.ai OAuth (Max subscription) instead of the API key even
    # when the parent process has the key loaded (e.g. from load-secrets.sh).
    # An API key in env takes precedence over OAuth; clearing it restores
    # the intended subscription auth path.
    _sub_env: dict[str, str] = {}
    if os.environ.get("ANTHROPIC_API_KEY"):
        _sub_env["ANTHROPIC_API_KEY"] = ""

    options = ClaudeAgentOptions(
        model=route.model,
        # Bare-mode options — measured 52,384 -> 0 cache_creation tokens
        # per CSPIKE_PLAN.md path-comparison matrix (2026-05-03 22:00).
        # Explicit empty values short-circuit the loader paths that
        # would otherwise pull in user / project / plugin configuration.
        tools=[],
        skills=None,
        mcp_servers={},
        setting_sources=[],
        plugins=[],
        system_prompt=None,       # already inlined into prompt
        env=_sub_env,
    )

    response_parts: list[str] = []
    rate_limit_status = "unknown"   # last seen status; persists across chunks
    rate_limit_resets_at = None
    usage_data: dict = {}

    try:
        async for chunk in query(prompt=prompt, options=options):
            chunk_type = type(chunk).__name__
            if chunk_type == "AssistantMessage":
                for block in getattr(chunk, "content", None) or []:
                    text = getattr(block, "text", None)
                    if text:
                        response_parts.append(text)
            elif chunk_type == "RateLimitEvent":
                info = getattr(chunk, "rate_limit_info", None)
                if info is not None:
                    rate_limit_status = getattr(info, "status",
                                                rate_limit_status)
                    rate_limit_resets_at = getattr(info, "resets_at",
                                                   rate_limit_resets_at)
                else:
                    # Older SDKs may attach status fields directly.
                    rate_limit_status = getattr(chunk, "status",
                                                rate_limit_status)
                    rate_limit_resets_at = getattr(chunk, "resets_at",
                                                   rate_limit_resets_at)
            elif chunk_type == "ResultMessage":
                usage = getattr(chunk, "usage", None) or {}
                # ResultMessage.usage may be a dict or an object.
                def _u(key: str) -> int:
                    if isinstance(usage, dict):
                        return int(usage.get(key, 0) or 0)
                    return int(getattr(usage, key, 0) or 0)
                usage_data = {
                    "input_tokens": _u("input_tokens"),
                    "output_tokens": _u("output_tokens"),
                    "cache_creation_input_tokens":
                        _u("cache_creation_input_tokens"),
                    "cache_read_input_tokens":
                        _u("cache_read_input_tokens"),
                }
    except ProcessError as e:
        return _handle_subscription_failure(
            route=route, error=e, error_type="ProcessError", tenant=tenant,
            rate_limit_status=rate_limit_status,
            rate_limit_resets_at=rate_limit_resets_at,
            original_system=original_system,
            original_messages=original_messages,
            project_id=project_id,
        )
    except ClaudeSDKError as e:
        return _handle_subscription_failure(
            route=route, error=e, error_type="ClaudeSDKError", tenant=tenant,
            rate_limit_status=rate_limit_status,
            rate_limit_resets_at=rate_limit_resets_at,
            original_system=original_system,
            original_messages=original_messages,
            project_id=project_id,
        )

    # Telemetry — write to data-<tenant>/dispatch.jsonl regardless of
    # success path (lets the dashboard tally call counts + window state).
    _record_subscription_call(
        tenant=tenant, task_type=route.task_type, model=route.model,
        usage=usage_data, rate_limit_status=rate_limit_status,
        rate_limit_resets_at=rate_limit_resets_at,
        outcome="ok", project_id=project_id,
    )

    text = "".join(response_parts).strip()
    if extract_json:
        text = _extract_json_from_subscription(text)
    return {
        "route": asdict(route),
        "text": text,
        "usage": usage_data,
        "est_usd": 0.0,   # subscription mode = no per-token billing
        "subscription_meta": {
            "rate_limit_status": rate_limit_status,
            "rate_limit_resets_at": rate_limit_resets_at,
            "project_id": project_id,
        },
    }


def _extract_json_from_subscription(text: str) -> str:
    """Strip preamble text from subscription responses when JSON is expected.

    The subscription path (claude_agent_sdk) sometimes prefixes the JSON
    payload with an explanatory sentence before the code fence, e.g.:

        Gmail search isn't permissioned, so I'll proceed...

        ```json
        {"follow_ups_to_add": [...]}
        ```

    Rules (applied in order):
    1. If text already starts with '{' or '[' → pure JSON, return as-is.
    2. Find the LAST ```json\\n...\\n``` or ```\\n...\\n``` fence and return
       its inner content (stripped).
    3. No fence found → return text as-is (caller's json.loads will raise
       with a useful error including the actual text).
    """
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        return stripped

    import re
    # Match the last ```json or ``` fence block in the text.
    pattern = r"```(?:json)?\n(.*?)```"
    matches = re.findall(pattern, stripped, re.DOTALL)
    if matches:
        return matches[-1].strip()

    return stripped


def _build_subscription_prompt(system: Any, messages: list[dict]) -> str:
    """
    Flatten (system, messages) into a single prompt string for query().
    System is inlined at the top; each message gets a [role] marker.
    Deterministic — no random IDs. Length cap not enforced (caller's
    job).
    """
    parts: list[str] = []

    if system is not None:
        if isinstance(system, list):
            for block in system:
                if isinstance(block, dict):
                    text = block.get("text") or ""
                else:
                    text = str(block)
                if text:
                    parts.append(text)
        else:
            parts.append(str(system))

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                (b.get("text") or "" if isinstance(b, dict) else str(b))
                for b in content
            )
        if content:
            parts.append(f"[{role}]\n{content}")

    return "\n\n".join(parts)


def _handle_subscription_failure(*, route: ModelRoute, error: Exception,
                                 error_type: str, tenant: str,
                                 rate_limit_status: str,
                                 rate_limit_resets_at: Any,
                                 original_system: Any = None,
                                 original_messages: Optional[list[dict]] = None,
                                 project_id: Optional[str] = None) -> dict:
    """
    Per-task fallback policy on subscription dispatch failure.

    TIME_INSENSITIVE -> enqueue to data-<tenant>/queue.jsonl; return a
                        sentinel result. The queue-drain daemon (see
                        _subscription_queue.py) re-fires after the
                        window resets.
    TIME_SENSITIVE   -> re-raise. The launchd schedule retries on the
                        next fire.
    Unrecognized     -> re-raise (conservative default — don't silently
                        swallow unknown failure modes).
    """
    task = route.task_type
    error_msg = str(error)

    # Telemetry first — failure ledger entry, with outcome marker.
    _record_subscription_call(
        tenant=tenant, task_type=task, model=route.model,
        usage={}, rate_limit_status=rate_limit_status,
        rate_limit_resets_at=rate_limit_resets_at,
        outcome=f"failure:{error_type}", error_msg=error_msg,
        project_id=project_id,
    )

    if task in TIME_INSENSITIVE_TASKS:
        _enqueue_subscription_task(
            tenant=tenant, route=route,
            error_type=error_type, error_msg=error_msg,
            queue_until=rate_limit_resets_at,
            original_system=original_system,
            original_messages=original_messages,
        )
        return {
            "route": asdict(route),
            "text": "",
            "usage": {},
            "est_usd": 0.0,
            "queued": True,
            "queue_until": rate_limit_resets_at,
            "subscription_meta": {
                "rate_limit_status": rate_limit_status,
                "rate_limit_resets_at": rate_limit_resets_at,
                "error_type": error_type,
                "project_id": project_id,
            },
        }

    # TIME_SENSITIVE or unrecognized: re-raise. Caller / launchd handles.
    raise error


def _data_dir(tenant: str) -> Path:
    p = _PIPELINE_ROOT / f"data-{tenant}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _record_subscription_call(*, tenant: str, task_type: str, model: str,
                               usage: dict, rate_limit_status: str,
                               rate_limit_resets_at: Any,
                               outcome: str = "ok",
                               error_msg: str = "",
                               project_id: Optional[str] = None) -> None:
    """Append one row to data-<tenant>/dispatch.jsonl (subscription ledger)."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_type": task_type,
        "model": model,
        "mode": "subscription",
        "outcome": outcome,
        "usage": usage or {},
        "rate_limit_status": rate_limit_status,
        "rate_limit_resets_at": rate_limit_resets_at,
        "project_id": project_id,
    }
    if error_msg:
        row["error_msg"] = error_msg
    path = _data_dir(tenant) / "dispatch.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _enqueue_subscription_task(*, tenant: str, route: ModelRoute,
                               error_type: str, error_msg: str,
                               queue_until: Any,
                               original_system: Any = None,
                               original_messages: Optional[list[dict]] = None) -> None:
    """Append one row to data-<tenant>/queue.jsonl for later retry.

    The row carries enough context for _subscription_queue.drain() to
    re-fire the call cold: task_type drives route resolution, and the
    original system + messages reproduce the prompt verbatim. Note that
    queue.jsonl can therefore contain prompt-shaped data on disk —
    pipeline operators must treat it with the same confidentiality as
    the source documents that produced the prompt.
    """
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_type": route.task_type,
        "model": route.model,
        "package": route.package,
        "queue_until": queue_until,
        "attempts": 1,
        "error_type": error_type,
        "error_msg": error_msg,
        "system": original_system,
        "messages": original_messages or [],
    }
    path = _data_dir(tenant) / "queue.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


# ─────────────────────────────────────────────────────────────────────
# CLI self-test.
# ─────────────────────────────────────────────────────────────────────

_KNOWN_TASK_TYPES = [
    # CLAUDE.md per-pass keys
    "pass1_source_scanner", "pass2_pipeline_analyst", "pass3_ic_memo",
    # routines.yaml package keys
    "capture", "briefing", "research", "deals", "server", "infra",
    # routine names / rename_to keys (from routines.yaml)
    "morning-briefing", "cos-personal-briefing", "briefing-morning",
    "deal-dashboard-compile", "deals-compile",
    "podcast-processing", "podcast-transcribe-daily", "podcasts-transcribe",
    "inbox-capture", "cos-capture-pipeline", "capture-inbox",
    # unknown — should fall back to overall default
    "totally-made-up-task",
]


def _main() -> int:
    ap = argparse.ArgumentParser(description="model_router self-test")
    ap.add_argument("--dry-run", action="store_true",
                    help="print resolved routes; no API calls")
    ap.add_argument("--tenant", default=_DEFAULT_TENANT)
    args = ap.parse_args()

    if not args.dry_run:
        sys.stderr.write("Use --dry-run; live calls require explicit caller.\n")
        return 2

    print(f"# tenant={args.tenant}  domain={_load_domain()!r}")
    print(f"# routines.yaml entries: {len(_load_routines())}")
    print()
    print(f"{'task_type':<32} {'mode':<13} {'model':<22} {'max_tok':>7}  source")
    print("-" * 90)
    for t in _KNOWN_TASK_TYPES:
        try:
            r = resolve_route(t, mode="auto", tenant=args.tenant)
            print(f"{t:<32} {r.mode:<13} {r.model:<22} {r.max_tokens:>7}  {r.source}")
        except Exception as e:  # noqa: BLE001
            print(f"{t:<32} ERROR: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
