#!/usr/bin/env python3
"""
_research_envelope.py — Routing-v2 extraction pass for research pipelines.

Every research processor (Jefferies, GS, RBN, podcast transcripts) calls
extract_and_route() on its cleaned markdown/memo output. This module
converts the free-form research text into envelope items and hands them
to _envelope_writer.append_items() — the same router the transcript
pipeline uses.

Goal: every piece of content entering the dashboard speaks the same
vocabulary. Research is additive to dealIntel[], originationInbox[],
and themes[]; research does NOT emit my_action or awaiting_external
(those come from direct interactions only).

Shape of each item — see docs/ROUTING-SPEC-2026-04-21.md §4.2.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# Firm context — loaded lazily so the module still works without a config file
# (e.g. in unit tests or isolated runs). `_PRINCIPAL_NAME` is used as the
# default owner on envelope items; falls back to "principal" if config missing.
try:
    import _firm_context as _fc
    _CTX = _fc.load_firm_context()
    _PRINCIPAL_NAME: str = (_fc._principal(_CTX).get("name") or "principal")
except Exception:
    _CTX = {}
    _PRINCIPAL_NAME = "principal"

# Research-doc envelope extraction runs on Gemini 2.5 Pro. Owner is always
# the principal on these items by construction (research sources cannot emit
# my_action / awaiting_external / status_update per config/routing-rules.md
# and the _envelope_writer._validate() contract), so the harder Claude
# attribution work is not required here. Transcript pipelines
# (cos_otter_backfill.py) stay on Sonnet where speaker attribution matters.
GEMINI_MODEL = "gemini-2.5-pro"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# Shared single-source-of-truth routing rules. Loaded at prompt-assembly
# time so every pipeline using this module sees the same contract as
# cos_otter_backfill and any future Gemini-routed pipeline.
_ROUTING_RULES_PATH = Path.home() / "dashboards/config/routing-rules.md"


def _load_routing_rules() -> str:
    try:
        return _ROUTING_RULES_PATH.read_text()
    except Exception as e:
        print(f"  [routing-v2] WARN — could not load {_ROUTING_RULES_PATH}: {e}",
              file=sys.stderr)
        return ""


# ─── Pipeline context (shared with transcript extractor) ────────────────────

_DASHBOARD_DATA = Path.home() / "dashboards/data/compiled/dashboard-data.json"


def _load_pipeline_context() -> str:
    """Build a compact string listing known deals and LPs so the model can
    attach parent_ids. Returns '' if dashboard-data not available.
    """
    try:
        d = json.loads(_DASHBOARD_DATA.read_text())
    except Exception:
        return ""
    lines = ["ACTIVE DEAL PIPELINE (resolve parent_id to one of these slugs when relevant):"]
    for t in (d.get("deals") or d.get("tomac") or []):  # noqa: tenant-leak — backward-compat read of old key
        name = t.get("name") or ""
        ticker = t.get("ticker") or ""
        sector = t.get("sector") or ""
        stage = t.get("stage") or ""
        if name:
            lines.append(f"  - {ticker or name}  |  {name}  |  {sector} / {stage}")
    lines.append("")
    lines.append("LP INVESTOR INTEL (resolve LP parent_id to one of these):")
    for lp in (d.get("lpData") or []):
        name = lp.get("name") or ""
        if name:
            lines.append(f"  - {name}")
    return "\n".join(lines)


# ─── Envelope writer loader ─────────────────────────────────────────────────

def _load_envelope_writer():
    """Import _envelope_writer.py by path (avoids package-layout assumptions)."""
    path = Path.home() / "dashboards/routines/process/_envelope_writer.py"
    spec = importlib.util.spec_from_file_location("_envelope_writer", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ─── Prompt ─────────────────────────────────────────────────────────────────

_RESEARCH_PREAMBLE_SUFFIX = """
You are the routing layer for a research document (bank note, sell-side summary, podcast transcript memo, or market briefing). Apply the rules above to this source type.

Because this is a research source (not a direct interaction), you must ONLY emit: deal_takeaway, origination_idea, theme_note. Do NOT emit my_action, awaiting_external, status_update, or lp_intel — those content types come from calls and emails only. A post-filter will drop any forbidden items, but your job is to emit clean input.

Ignore descriptive backgrounds, disclaimers, macroeconomic generics, and company primers with no incremental insight. Keep output lean.

**PARENT_ID ATTRIBUTION (critical for dashboard wiring).** The ACTIVE DEAL PIPELINE block above lists every tracked deal as `ticker | name | sector/stage`. For a `deal_takeaway` item, you MUST set `parent_id` to the ticker (or exact name if no ticker) of the tracked deal when the research note discusses that specific asset, company, sponsor, or counterparty — even when the reference is indirect (e.g. a Rio Grande LNG note attaches to the NextDecade deal if NextDecade/Rio Grande is a tracked deal). Match on: deal name, ticker, counterparty firm, asset names mentioned in the deal's thesis, sector+geography overlap with explicit naming. Do NOT fabricate a parent_id — if no tracked deal is a clear match, leave parent_id empty and the item flows to general dealIntel[]. For `origination_idea` on a new target (not yet a tracked deal), leave parent_id empty; the item routes to originationInbox[]. For `theme_note`, parent_id is always empty.

RESPOND WITH JSON ONLY (no markdown, no explanation):
{"envelope_items":[{"content_type":"deal_takeaway|origination_idea|theme_note","owner":"<principal-name>","counterparty":"...","parent_id":"...","due":"","context":"...","dashboard_path":"","content":"..."}]}
"""


def _build_routing_preamble() -> str:
    """Assemble the full preamble: shared rules file + research-specific suffix."""
    rules = _load_routing_rules()
    if not rules:
        # Fallback — module still usable if the rules file is missing,
        # but log it loudly.
        print("  [routing-v2] WARN — running without shared routing-rules.md",
              file=sys.stderr)
    return rules + "\n\n---\n\n" + _RESEARCH_PREAMBLE_SUFFIX


# ─── Gemini API key ─────────────────────────────────────────────────────────

def _get_gemini_key() -> str:
    """Return the Gemini API key from env or ~/credentials/gemini_api_key.txt.

    Same fallback chain the other Gemini-based processors
    (jefferies_processor.py, gs_processor.py) use. Returns "" if neither
    source is available — caller logs and returns an empty envelope.
    """
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    p = Path("~/credentials/gemini_api_key.txt").expanduser()
    if p.exists():
        return p.read_text().strip()
    return ""


# ─── Gemini call ────────────────────────────────────────────────────────────

def _call_llm(title: str, markdown: str, pipeline_context: str,
              max_chars: int = 18000) -> list[dict]:
    """Emit envelope items for a research document via Gemini 2.5 Pro.

    Returns []:
      - Gemini key missing
      - Network / 5xx / timeout
      - JSON parse failure
    The caller (extract_and_route) treats an empty list as "no envelope
    items from this doc" and continues, so a transient Gemini outage
    never crashes the research pipeline.
    """
    api_key = _get_gemini_key()
    if not api_key:
        print("  [routing-v2] GEMINI_API_KEY not set and "
              "~/credentials/gemini_api_key.txt absent — skipping envelope extraction",
              file=sys.stderr)
        return []

    preamble = _build_routing_preamble()
    prompt_parts: list[str] = [preamble]
    if pipeline_context:
        prompt_parts.append(pipeline_context)
    prompt_parts.append(
        f"RESEARCH TITLE: {title}\n"
        f"TODAY: {datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"RESEARCH CONTENT (markdown):\n{(markdown or '')[:max_chars]}"
    )
    prompt = "\n\n---\n\n".join(prompt_parts)

    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            # 4096 matches the IC-memo allowance in ~/.claude/CLAUDE.md;
            # 2048 truncated long JSON arrays in testing (Gemini JSON mode
            # counts every brace/comma toward the output budget).
            "maxOutputTokens": 4096,
            # Force structured JSON output — matches the envelope shape
            # the old Claude prompt emitted. The validator in
            # _envelope_writer._validate() still catches any drift.
            "response_mime_type": "application/json",
        },
    }
    url = f"{GEMINI_URL}?key={api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read())
    except Exception as e:
        print(f"  [routing-v2] Gemini call failed: {e}", file=sys.stderr)
        return []

    try:
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        print(f"  [routing-v2] Gemini response shape unexpected: {e} "
              f"body={str(resp)[:200]!r}", file=sys.stderr)
        return []

    text = (text or "").strip()
    # JSON mode should return clean JSON, but tolerate stray code fences
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            print(f"  [routing-v2] Could not parse Gemini output: {text[:200]!r}",
                  file=sys.stderr)
            return []
        try:
            parsed = json.loads(m.group(0))
        except Exception as e:
            print(f"  [routing-v2] JSON parse error: {e}", file=sys.stderr)
            return []

    items = parsed.get("envelope_items") or []
    # Defensive: filter out content types that research sources must not emit.
    # _envelope_writer._validate() would catch these downstream, but dropping
    # them here keeps the routingExceptions log focused on real issues.
    FORBIDDEN = {"my_action", "awaiting_external", "status_update", "lp_intel", "contact"}
    items = [it for it in items if it.get("content_type") not in FORBIDDEN]
    return items


# Back-compat alias — any external caller that imported _call_claude directly
# (none in-tree as of 2026-04-21, but harmless insurance) keeps working.
_call_claude = _call_llm


# ─── Public entry point ─────────────────────────────────────────────────────

def extract_and_route(title: str, markdown: str, source_type: str,
                      doc_url: str = "", date: str = "",
                      pipeline_context: str | None = None) -> dict:
    """
    Extract envelope items from research markdown and route them to
    dashboard-data.json via _envelope_writer.append_items().

    Parameters:
      title:       short human-readable title (e.g. "Jefferies — NEE Q3 Recap")
      markdown:    the cleaned research content (already distilled by the
                   primary processor's Gemini/Claude extraction)
      source_type: 'research' | 'podcast' | 'briefing' — tagged on source_ref
      doc_url:     link to the Google Doc / Drive URL of the full content
      date:        YYYY-MM-DD of the source material

    Returns summary dict from append_items(), or {} on skip/error.
    """
    if not markdown or not markdown.strip():
        return {}

    ctx = pipeline_context if pipeline_context is not None else _load_pipeline_context()
    items = _call_llm(title, markdown, ctx)
    if not items:
        return {"routed": {}, "exceptions": 0, "skipped_dupes": 0}

    src_ref = {
        "type":    source_type,
        "title":   title,
        "doc_url": doc_url,
        "date":    date or datetime.now().strftime("%Y-%m-%d"),
    }
    for it in items:
        it.setdefault("source_ref", src_ref)
        it.setdefault("owner", _PRINCIPAL_NAME)
        it.setdefault("due", "")

    ew = _load_envelope_writer()
    summary = ew.append_items(items)
    _print_summary(title, summary)
    return summary


def _print_summary(title: str, summary: dict) -> None:
    routed = summary.get("routed") or {}
    total = sum(routed.values())
    exc = summary.get("exceptions", 0)
    if total == 0 and exc == 0:
        return
    parts = ", ".join(f"{k}={v}" for k, v in sorted(routed.items()))
    print(f"  📬  Routing-v2 [{title[:40]}]: {total} routed"
          f"{' / ' + str(exc) + ' exc' if exc else ''}  ({parts})")


# ─── CLI: ad-hoc test from stdin ────────────────────────────────────────────

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Test routing-v2 extraction on markdown from stdin.")
    ap.add_argument("--title", required=True)
    ap.add_argument("--source-type", default="research")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print items, do not write to dashboard-data")
    args = ap.parse_args()

    markdown = sys.stdin.read()
    if args.dry_run:
        items = _call_llm(args.title, markdown, _load_pipeline_context())
        print(json.dumps({"envelope_items": items}, indent=2, ensure_ascii=False))
        return 0
    summary = extract_and_route(args.title, markdown, args.source_type)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
