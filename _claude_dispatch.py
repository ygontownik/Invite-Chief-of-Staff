"""_claude_dispatch.py — auth-mode aware Claude call wrapper.

A thin shim that lets any extraction script work in BOTH:
  • subscription mode — routes via _model_router.call_claude → claude-
    agent-sdk → bundled `claude` CLI → Pro/Max OAuth window.
    Zero per-token billing.
  • api mode — falls back to the legacy raw urllib POST against
    api.anthropic.com/v1/messages with ANTHROPIC_API_KEY. Pay-per-
    token, billed against the principal's API budget.

Mode resolution (highest priority first):
  1. `mode=` argument to call() — explicit per-call override.
  2. firm_context.yaml :: auth_mode (read once, cached).
  3. Default 'subscription' — matches the load-secrets.sh default and
     the firm_context.template.yaml ship-default. Tenants on api mode
     must opt in explicitly.

Usage:
    from _claude_dispatch import call

    text = call(
        task_type="cos-otter-extract",
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": MY_PREAMBLE,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dynamic},
        ]}],
        max_tokens=8192,
    )

Caller-provided cache_control blocks are honored on the api path
(prompt caching) and ignored on the subscription path (subscription
billing doesn't track per-token, so caching is moot — the SDK joins
everything into one prompt anyway).

Codified 2026-05-05 to close the dual-mode migration for the four
remaining raw-Anthropic scripts (cos_otter_backfill, cos_email_backfill,
cos_gmail_mini_v2, dash_corrections_proposer).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional


_CACHED_AUTH_MODE: Optional[str] = None


def _resolve_auth_mode() -> str:
    """Read auth_mode from CLAUDE_AUTH_MODE env / firm_context.yaml.

    Resolution order (mirrors shell-side load-secrets.sh):
      1. CLAUDE_AUTH_MODE env var (per-process override; useful for
         ad-hoc testing or running a single script in the opposite mode).
      2. firm_context.yaml :: auth_mode (canonical tenant config).
      3. Default 'subscription'.
    """
    global _CACHED_AUTH_MODE
    if _CACHED_AUTH_MODE is not None:
        return _CACHED_AUTH_MODE
    env_mode = os.environ.get("CLAUDE_AUTH_MODE", "").strip()
    if env_mode in ("subscription", "api"):
        _CACHED_AUTH_MODE = env_mode
        return env_mode
    # Try the canonical _firm_context loader first — picks up the right
    # config file per tenant. Fall back to a direct YAML scan if that
    # module isn't on the path.
    try:
        if str(Path(__file__).resolve().parent) not in sys.path:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _firm_context as _fc  # noqa: PLC0415
        ctx = _fc.load_firm_context()
        mode = (ctx.get("auth_mode") if isinstance(ctx, dict) else None)
        if mode in ("subscription", "api"):
            _CACHED_AUTH_MODE = mode
            return mode
    except Exception:
        pass
    # Direct fallback — scan a few standard paths.
    for p in (
        Path.home() / "cos-pipeline-config-tomac" / "firm_context.yaml",
        Path.home() / "cos-pipeline" / "firm_context.yaml",
    ):
        try:
            if not p.is_file():
                continue
            for ln in p.read_text().splitlines():
                ln = ln.strip()
                if ln.startswith("auth_mode:"):
                    val = ln.split(":", 1)[1].strip().strip('"').strip("'")
                    if val in ("subscription", "api"):
                        _CACHED_AUTH_MODE = val
                        return val
        except Exception:
            continue
    _CACHED_AUTH_MODE = "subscription"
    return "subscription"


def _api_call(*, model: str, system: Any, messages: list,
              max_tokens: int, task_type: str, cache: bool,
              api_key: str, timeout: int) -> str:
    """Direct POST to api.anthropic.com — the legacy raw-urllib path.

    Preserved verbatim so api-mode tenants see identical behavior to
    pre-migration. Includes prompt-caching beta header when cache=True.
    """
    payload: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system is not None:
        payload["system"] = system
    headers = {
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    if cache:
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    # Optional usage logging — best-effort, swallow failures.
    try:
        from _usage import log_usage  # type: ignore  # noqa: PLC0415
        log_usage(task_type, model, resp)
    except Exception:
        pass
    return resp["content"][0]["text"].strip()


def _subscription_call(*, task_type: str, system: Any, messages: list,
                       max_tokens: int, cache: bool, tenant: Optional[str]) -> str:
    """Subscription dispatch via _model_router → claude-agent-sdk."""
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _model_router as mr  # noqa: PLC0415
    result = mr.call_claude(
        task_type=task_type,
        system=system,
        messages=messages,
        mode="subscription",
        max_tokens=max_tokens,
        cache=cache,
        tenant=tenant or "tomac",
    )
    return (result.get("text") or "").strip()


def call(*, task_type: str, model: str, messages: list,
         max_tokens: int, system: Any = None, cache: bool = True,
         mode: Optional[str] = None, tenant: Optional[str] = None,
         api_timeout: int = 120) -> str:
    """Dispatch a Claude call.

    Returns the assistant text. Raises on transport errors.

    Subscription mode silently falls back to api mode when the SDK is
    not installed OR claude_agent_sdk's bundled CLI is missing OAuth
    credentials. The fallback is logged to stderr but doesn't raise so
    a single deploy-time misconfiguration doesn't take down the whole
    pipeline.
    """
    active_mode = mode or _resolve_auth_mode()

    if active_mode == "subscription":
        try:
            return _subscription_call(
                task_type=task_type, system=system, messages=messages,
                max_tokens=max_tokens, cache=cache, tenant=tenant,
            )
        except Exception as e:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                raise RuntimeError(
                    f"subscription dispatch failed ({e}) and "
                    "ANTHROPIC_API_KEY is not set in env, so no api "
                    "fallback is possible. Either install claude-agent-"
                    "sdk + run `claude /login`, or set "
                    "auth_mode=api in firm_context.yaml + ensure the "
                    "key is in keychain."
                ) from e
            print(
                f"  [_claude_dispatch] subscription failed ({e!r}); "
                f"falling back to api for task={task_type}",
                file=sys.stderr,
            )
            return _api_call(
                model=model, system=system, messages=messages,
                max_tokens=max_tokens, task_type=task_type, cache=cache,
                api_key=api_key, timeout=api_timeout,
            )

    if active_mode == "api":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "auth_mode=api but ANTHROPIC_API_KEY is not set in env. "
                "Confirm load-secrets.sh exported it (it does so under "
                "auth_mode=api OR in non-interactive shells without "
                "CLAUDE_CODE_OAUTH_TOKEN), or set FORCE_LOAD_ANTHROPIC_KEYS=1."
            )
        return _api_call(
            model=model, system=system, messages=messages,
            max_tokens=max_tokens, task_type=task_type, cache=cache,
            api_key=api_key, timeout=api_timeout,
        )

    raise ValueError(f"unknown auth_mode={active_mode!r}")
