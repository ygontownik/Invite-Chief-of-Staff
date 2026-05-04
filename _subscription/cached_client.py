"""cached_client.py — sandbox SDK wrapper for the static-core system prompt.

Splits `system_prompt_v1.md` on its two `<!-- CACHE_BREAKPOINT_N -->` markers
into three text segments and sends them as a typed `system=[...]` array with
`cache_control={"type":"ephemeral"}` on the first two. Logs one JSONL row per
call to `cache_telemetry.jsonl` so we can measure cache_read economics.

This file is sandbox-only: no imports from the live cos-pipeline tree, no
wiring into any daemon. Reads `ANTHROPIC_API_KEY` via `_secrets.load_secret`.
"""
import hashlib
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone
from typing import Any

import anthropic

_HERE = pathlib.Path(__file__).resolve().parent
_PROMPT_FILE = _HERE / "system_prompt_v1.md"
_TELEMETRY_FILE = _HERE / "cache_telemetry.jsonl"

_BP1 = "<!-- CACHE_BREAKPOINT_1 -->"
_BP2 = "<!-- CACHE_BREAKPOINT_2 -->"


def _load_api_key() -> str:
    """Resolve ANTHROPIC_API_KEY via _secrets (keychain on Mac, env fallback).

    Imports _secrets from ~/cos-pipeline/ at call time so the import path is
    not baked into the module top-level — keeps this file standalone if the
    parent tree moves.
    """
    pipeline_dir = _HERE.parent
    sys.path.insert(0, str(pipeline_dir))
    try:
        import _secrets  # type: ignore
        key = _secrets.load_secret("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not found via _secrets.load_secret"
            )
        return key
    finally:
        if str(pipeline_dir) in sys.path:
            sys.path.remove(str(pipeline_dir))


def _split_static_core() -> tuple[str, str, str]:
    """Read the markdown and split on the two breakpoint markers.

    Returns (segment_1, segment_2, segment_3). seg1 is sections 1–5 (the
    truly stable core); seg2 is the per-tenant bundle slot; seg3 is the
    per-request variable slot.
    """
    text = _PROMPT_FILE.read_text(encoding="utf-8")
    if _BP1 not in text or _BP2 not in text:
        raise RuntimeError(
            f"system_prompt_v1.md missing one or both breakpoint markers"
        )
    pre, rest = text.split(_BP1, 1)
    mid, post = rest.split(_BP2, 1)
    return pre.strip(), mid.strip(), post.strip()


def _build_system_blocks(tenant_bundle: str) -> list[dict[str, Any]]:
    """Render the three system segments with cache markers on the first two.

    Order on the wire matches render order: blocks 1 and 2 are the cacheable
    prefix; block 3 carries volatile per-request content (date, query, source)
    and is intentionally NOT marked cache_control.
    """
    seg1, seg2, seg3 = _split_static_core()
    seg2_filled = seg2.replace("{{TENANT_BUNDLE}}", tenant_bundle)
    return [
        {
            "type": "text",
            "text": seg1,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": seg2_filled,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": seg3,
        },
    ]


def _bundle_hash(tenant_bundle: str) -> str:
    """First 8 hex chars of sha256(tenant_bundle). Stable per-bundle ID."""
    return hashlib.sha256(tenant_bundle.encode("utf-8")).hexdigest()[:8]


def _write_telemetry(row: dict[str, Any]) -> None:
    """Append one JSONL row. Best-effort — never raise from telemetry."""
    try:
        with _TELEMETRY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def complete(
    user_query: str,
    source_content: str,
    tenant_bundle: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Send one Messages API call against the static-core prefix.

    Returns:
        {"text": str,
         "usage": <Anthropic usage object>,
         "cache_metrics": {"creation": int, "read": int,
                           "uncached_input": int, "output": int}}
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_content = (
        f"Today's date: {today}\n\n"
        f"User query: {user_query}\n\n"
        f"Source content:\n{source_content}"
    )

    api_key = _load_api_key()
    client = anthropic.Anthropic(api_key=api_key)
    system_blocks = _build_system_blocks(tenant_bundle)

    t0 = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
    )
    latency_ms = int((time.monotonic() - t0) * 1000)

    usage = response.usage
    cache_metrics = {
        "creation": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "uncached_input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
    }

    text = next(
        (block.text for block in response.content if block.type == "text"),
        "",
    )

    # ttft_ms (time-to-first-token) is not measurable on a non-streaming call;
    # omit with a comment in the row so the field's absence is intentional.
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "tenant_bundle_hash": _bundle_hash(tenant_bundle),
        "cache_creation_tokens": cache_metrics["creation"],
        "cache_read_tokens": cache_metrics["read"],
        "uncached_input_tokens": cache_metrics["uncached_input"],
        "output_tokens": cache_metrics["output"],
        "latency_ms": latency_ms,
        # ttft_ms omitted: requires streaming; non-streaming call here.
    }
    _write_telemetry(row)

    return {
        "text": text,
        "usage": usage,
        "cache_metrics": cache_metrics,
    }


if __name__ == "__main__":
    # Smoke test: two identical calls; second should show cache_read > 0.
    r1 = complete(
        "Respond with the single word 'ack' and nothing else.",
        "placeholder source",
        "smoke test bundle: power/utilities focus",
    )
    print(f"R1 cache_metrics: {r1['cache_metrics']}")
    r2 = complete(
        "Respond with the single word 'ack' and nothing else.",
        "placeholder source",
        "smoke test bundle: power/utilities focus",
    )
    print(f"R2 cache_metrics: {r2['cache_metrics']}")
    print(f"R2 text: {r2['text'][:80]}")
