#!/usr/bin/env python3
"""
_model_router.py — Phase 2 Track C model router + subscription dispatch.

Public API:
    call_claude(task_type, system, messages, mode='auto',
                max_tokens=None, cache=True, tenant=None) -> dict

Per PLAN_v3.1 §Track C and CLAUDE.md "Per-pass model assignments" table.

Dispatch modes (per routine `mode` field in routines.yaml):
    subscription -> Claude Agent SDK / `claude -p`
                    (NotImplementedError tonight; explicit TODO branch
                     pending C-spike GREEN/RED decision — see CSPIKE_PLAN.md)
    api          -> anthropic SDK (genuine implementation)
    daemon       -> ValueError (daemons are not callable Claude tasks)

Cost tracking (PLAN C5/C6):
    Each api call appends a JSON line to
        ~/cos-pipeline/data-<tenant>/costs/YYYY-MM-DD.jsonl
    Schema:
        {ts, task_type, model, input_tokens, output_tokens,
         cached_input_tokens, mode, est_usd}

Quotas (PLAN C6):
    sum today's est_usd; warn at max_daily_usd (stderr);
    hard-stop at 3x cap (raise QuotaExceeded).

Self-test:
    python3 _model_router.py --dry-run

Stdlib + anthropic SDK only. No third-party YAML loader required —
falls back to a minimal embedded YAML reader if PyYAML is unavailable.
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

# Default tenant (matches PATH_TOPOLOGY.md and PLAN §J).
_DEFAULT_TENANT = "tomac"

_PIPELINE_ROOT = Path.home() / "cos-pipeline"
_ROUTINES_YAML = _PIPELINE_ROOT / "routines.yaml"
_FIRM_CONTEXT  = _PIPELINE_ROOT / "firm_context.yaml"


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


# ─────────────────────────────────────────────────────────────────────
# Route resolution.
# ─────────────────────────────────────────────────────────────────────

def resolve_route(task_type: str, mode: str = "auto",
                  tenant: Optional[str] = None) -> ModelRoute:
    """
    Resolve a ModelRoute for `task_type`.

    Precedence (highest -> lowest):
      1. per-tenant model_router.yaml override (if mode='auto')
      2. routines.yaml entry (provides package + mode + max_daily_usd)
      3. CLAUDE.md per-pass defaults (pass1_/pass2_/pass3_ keys)
      4. CLAUDE.md package defaults
      5. overall default (Sonnet 4.6 / 2048)
    """
    tenant = tenant or _DEFAULT_TENANT
    domain = _load_domain()

    # 1. Tenant override
    tenant_cfg = _load_tenant_override(tenant)
    routes_cfg = (tenant_cfg or {}).get("routes", {}) or {}
    if isinstance(routes_cfg, dict) and task_type in routes_cfg:
        entry = routes_cfg[task_type] or {}
        if isinstance(entry, dict):
            model = entry.get("model") or _OVERALL_DEFAULT[0]
            r_mode = entry.get("mode") or (mode if mode != "auto" else "api")
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
        resolved_mode = mode if mode != "auto" else (routine_mode or "api")
        return ModelRoute(task_type, model, resolved_mode, max_t,
                          package=package, domain=domain,
                          source="claudemd_per_pass")

    # 4. Package defaults
    if package and package in _PACKAGE_DEFAULTS:
        model, max_t = _PACKAGE_DEFAULTS[package]
    else:
        model, max_t = _OVERALL_DEFAULT

    resolved_mode = mode if mode != "auto" else (routine_mode or "api")
    return ModelRoute(task_type, model, resolved_mode, max_t,
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
                tenant: Optional[str] = None) -> dict:
    """
    Dispatch a Claude call per the resolved route.

    Returns a dict:
        {
          'route':  asdict(ModelRoute),
          'text':   <assistant text>,
          'usage':  {'input_tokens', 'output_tokens', 'cached_input_tokens'},
          'est_usd': <float>,
        }

    Raises:
      NotImplementedError — mode='subscription' (deferred per HARD RULES;
                            see CSPIKE_PLAN.md).
      ValueError          — mode='daemon' (daemons aren't callable).
      QuotaExceeded       — daily spend >= 3x soft cap for this task_type.
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
        # TODO(C-spike GREEN): replace this branch with the verified
        # subscription dispatch path. Two candidates documented in
        # CSPIKE_PLAN.md:
        #   (a) Claude Agent SDK — `from claude_agent_sdk import query`
        #       async generator; map task_type -> SKILL invocation.
        #   (b) `claude -p` CLI — subprocess.run(["claude","-p",...]).
        # If C-spike is RED, leave this NotImplementedError in place
        # and force callers to set mode='api' explicitly.
        raise NotImplementedError(
            f"subscription dispatch for task_type={task_type!r} not "
            "wired tonight (PLAN C-spike deferred to paper test — see "
            "~/cos-pipeline/CSPIKE_PLAN.md). Set mode='api' to use the "
            "anthropic SDK path, or wait for the C-spike GREEN cutover."
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
            "or set mode='subscription' once C-spike is GREEN."
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
# CLI self-test.
# ─────────────────────────────────────────────────────────────────────

_KNOWN_TASK_TYPES = [
    # CLAUDE.md per-pass keys
    "pass1_source_scanner", "pass2_pipeline_analyst", "pass3_ic_memo",
    # routines.yaml package keys
    "capture", "briefing", "research", "deals", "server", "infra",
    # routine names / rename_to keys (from routines.yaml)
    "cos-personal-briefing", "briefing-morning",
    "tomac-deal-compile",    "deals-compile",
    "podcast-transcribe-daily", "podcasts-transcribe",
    "cos-capture-pipeline",  "capture-inbox",
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
