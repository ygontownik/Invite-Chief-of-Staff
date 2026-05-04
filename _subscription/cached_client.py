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

BATCH_STATE_FILE = pathlib.Path.home() / "credentials" / "pending_batches.json"


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


def _build_system_blocks(tenant_bundle: str,
                         routine_prompt: str = "") -> list[dict[str, Any]]:
    """Render system segments with cache markers.

    Blocks 1 + 2: investor identity and Tomac bundle — always cached.
    Block 3: static tail of system_prompt_v1.md — NOT cached (volatile slot).
    Block 4 (optional): per-routine format prompt (MEMO_PREAMBLE, JSON schema,
    capture ruleset) — cached when provided. This is the third breakpoint:
    stable within a routine, different per routine, much cheaper at subscriber
    scale since it rides the same cache window as blocks 1+2.
    """
    seg1, seg2, seg3 = _split_static_core()
    seg2_filled = seg2.replace("{{TENANT_BUNDLE}}", tenant_bundle)
    blocks: list[dict[str, Any]] = [
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
    if routine_prompt and routine_prompt.strip():
        blocks.append({
            "type": "text",
            "text": routine_prompt.strip(),
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


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
    routine_prompt: str = "",
) -> dict[str, Any]:
    """Send one Messages API call against the static-core prefix.

    routine_prompt — per-routine format template (MEMO_PREAMBLE, JSON schema,
    capture ruleset). When provided, sent as a fourth cached system block so it
    shares the cache window with the static core. The user message then carries
    only variable data (source_content), reducing uncached token spend.

    Returns:
        {"text": str,
         "usage": <Anthropic usage object>,
         "cache_metrics": {"creation": int, "read": int,
                           "uncached_input": int, "output": int}}
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if user_query and user_query.strip():
        user_content = (
            f"Today's date: {today}\n\n"
            f"User query: {user_query}\n\n"
            f"Source content:\n{source_content}"
        )
    else:
        user_content = f"Today's date: {today}\n\nSource content:\n{source_content}"

    api_key = _load_api_key()
    client = anthropic.Anthropic(api_key=api_key)
    system_blocks = _build_system_blocks(tenant_bundle, routine_prompt=routine_prompt)

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
        "routine_prompt_tokens": len(routine_prompt.split()) * 4 // 3 if routine_prompt else 0,
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


# ── Batch API helpers ─────────────────────────────────────────────────────────

def _load_batch_state() -> dict:
    try:
        if BATCH_STATE_FILE.exists():
            return json.loads(BATCH_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"batches": []}


def _save_batch_state(state: dict) -> None:
    try:
        BATCH_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def submit_batch(
    requests: list[dict[str, Any]],
    routine: str = "",
    tenant_bundle: str = "",
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    routine_prompt: str = "",
) -> str:
    """Submit one or more requests to the Anthropic Batch API.

    Each request dict must have:
        custom_id   – stable unique string (e.g. "podcast-Catalyst-<guid>")
        user_query  – the format/ruleset prompt (goes into user message)
        source_content – variable data (doc text, transcript, etc.)
        metadata    – arbitrary dict stored in pending_batches.json so the
                      retrieval path can reconstruct enough context to write results

    Returns the Anthropic batch_id string. State is persisted to BATCH_STATE_FILE.
    50% cheaper than synchronous calls; results available within minutes-to-hours.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    api_key = _load_api_key()
    client = anthropic.Anthropic(api_key=api_key)
    system_blocks = _build_system_blocks(tenant_bundle, routine_prompt=routine_prompt)

    batch_requests = []
    for req in requests:
        uq = req.get("user_query", "")
        if uq and uq.strip():
            user_content = (
                f"Today's date: {today}\n\n"
                f"User query: {uq}\n\n"
                f"Source content:\n{req['source_content']}"
            )
        else:
            user_content = f"Today's date: {today}\n\nSource content:\n{req['source_content']}"
        batch_requests.append({
            "custom_id": req["custom_id"],
            "params": {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_blocks,
                "messages": [{"role": "user", "content": user_content}],
            },
        })

    batch = client.messages.batches.create(requests=batch_requests)
    batch_id = batch.id

    state = _load_batch_state()
    state["batches"].append({
        "batch_id": batch_id,
        "routine": routine,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "status": "in_progress",
        "results_written": False,
        "request_count": len(requests),
        "requests": [
            {"custom_id": r["custom_id"], "metadata": r.get("metadata", {})}
            for r in requests
        ],
    })
    _save_batch_state(state)
    _write_telemetry({
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "batch_submitted",
        "batch_id": batch_id,
        "routine": routine,
        "request_count": len(requests),
        "model": model,
    })
    return batch_id


def retrieve_batch(batch_id: str) -> dict | None:
    """Check status and retrieve results for a batch.

    Returns {"batch_id": str, "results": [{custom_id, text, usage}]} when ended.
    Returns None when still in_progress.
    Updates status in BATCH_STATE_FILE.
    """
    api_key = _load_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    batch = client.messages.batches.retrieve(batch_id)
    status = batch.processing_status  # "in_progress" | "ended" | "canceling" | "canceled"

    state = _load_batch_state()
    for b in state["batches"]:
        if b["batch_id"] == batch_id:
            b["status"] = status
            break
    _save_batch_state(state)

    if status != "ended":
        return None

    results = []
    for item in client.messages.batches.results(batch_id):
        if item.result.type == "succeeded":
            msg = item.result.message
            text = next(
                (block.text for block in msg.content if block.type == "text"), ""
            )
            usage = msg.usage
            results.append({
                "custom_id": item.custom_id,
                "text": text,
                "usage": usage,
            })
            _write_telemetry({
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "batch_result",
                "batch_id": batch_id,
                "custom_id": item.custom_id,
                "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                "uncached_input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            })
        elif item.result.type == "errored":
            _write_telemetry({
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "batch_error",
                "batch_id": batch_id,
                "custom_id": item.custom_id,
                "error": str(item.result.error),
            })

    return {"batch_id": batch_id, "results": results}


def load_pending_batches(routine: str = "") -> list[dict]:
    """Return batches that haven't had results written yet, optionally filtered by routine."""
    state = _load_batch_state()
    batches = [b for b in state["batches"] if not b.get("results_written")]
    if routine:
        batches = [b for b in batches if b.get("routine") == routine]
    return batches


def mark_batch_written(batch_id: str) -> None:
    """Mark a batch as done — results have been written to their destination."""
    state = _load_batch_state()
    for b in state["batches"]:
        if b["batch_id"] == batch_id:
            b["results_written"] = True
            b["completed_at"] = datetime.now(timezone.utc).isoformat()
            break
    _save_batch_state(state)


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
