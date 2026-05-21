#!/usr/bin/env python3
# noqa: claude-dispatch-exempt — Track G experimental spike harness.
# Spike code, not production; runs are manual + ad-hoc. Will move to
# _claude_dispatch when promoted to a scheduled routine.
"""
Track G — briefing-morning spike harness.

Builds the morning briefing prompt for the configured domain and (optionally) calls
Claude Opus 4.7 to produce the memo. Default mode is --dry-run: prints the assembled
prompt with first-80-char source excerpts and exits without hitting any API.

Usage:
    python3 run_spike.py                 # dry-run (default)
    python3 run_spike.py --no-dry-run    # actually call Claude API
    python3 run_spike.py --out PATH      # write memo to PATH (real run only)

This is a SPIKE. It is intentionally read-only with respect to Drive: it pulls
source doc text via the Drive/Docs API and never modifies anything. It does NOT
write to the master Doc and does NOT send email — those are downstream steps in
SKILL.md (sections 5–6) and remain the responsibility of the existing helpers
(notebooklm_doc_writer.py, send_briefing_email.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

REPO = Path.home() / "cos-pipeline"
FIRM_CONTEXT = REPO / "firm_context.yaml"
DEFAULT_DOMAIN = "infra-pe"
MODEL = "claude-opus-4-7"          # CLAUDE.md Pass 2 — synthesis grade
MAX_TOKENS = 4096                   # CLAUDE.md: 2048 truncates 1,500+ word memos

# Mirror of the Drive doc IDs the NotebookLM "Substack - Markets" notebook syncs
# from. These are the same source docs the legacy SKILL uses; pulled directly
# from Drive here instead of going through the NotebookLM browser auto-sync.
# Doc IDs sourced from /Users/ygontownik/.claude/CLAUDE.md "Intelligence /
# Daily Briefing Docs" table.
SOURCE_DOCS = [
    ("RBN Energy Daily Archive",  "1N6mqhMJn1IJP-5EwByYccEb0uaoBeUDXKRNT8BUbfW4"),
    ("FVR Energy Finance",        "1Jg_-LamIsKVKXBrWlZICZGrQTkOoXBeAT2U2rNldLoA"),
    ("GS Energy",                 "1NGKZXv0MgkbBXXQRPQ2Kiq1AgKg88v5bmyPlpz4Pi9c"),
    ("GS Macro Market",           "19wGIr8UoxiRuL2jEFOp-aGLA4sHj_rGP5DeILWDyKCQ"),
    ("Jefferies General",         "1sLTPtueXMp0a80ZHiGWqT-wD0QvubUy5WtS4OMCqefE"),
    ("Energy Pipeline (Gemini)",  "1olCXFTHX0tv3Bqb29x02s7Oa7aryW1SNNFJQQtndvmQ"),
    ("a16z",                      "1fkH1X6HQw-ruogp54Zq77SjgcD2kvptc-CDcHsxxdiY"),
]

DEFAULT_MASTER_DOC_ID = "1UZ1t4bhgzll5VcAuP3Mj1CyYb-4xjgmbUK1xg6oUS_k"  # Daily Market Update
EXCERPT_PER_SOURCE_CHARS = 8000       # cap to keep prompt under ~80k chars
DRY_RUN_EXCERPT_CHARS = 80            # PER instruction: first 80 chars only in dry-run output


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_firm_context() -> dict:
    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML not installed. pip install pyyaml", file=sys.stderr)
        sys.exit(2)
    if not FIRM_CONTEXT.exists():
        print(f"ERROR: {FIRM_CONTEXT} missing", file=sys.stderr)
        sys.exit(2)
    return yaml.safe_load(FIRM_CONTEXT.read_text()) or {}


def resolve_domain(ctx: dict) -> str:
    return ctx.get("domain") or DEFAULT_DOMAIN


def load_prompt_template(domain: str) -> str:
    p = REPO / "domains" / domain / "prompts" / "briefing-morning.txt"
    if not p.exists():
        print(f"ERROR: prompt template not found: {p}", file=sys.stderr)
        sys.exit(2)
    return p.read_text()


def fetch_drive_doc_text(doc_id: str) -> str:
    """Read full text of a Google Doc via the Docs API. Returns "" on failure
    (e.g. no creds in spike environment). Errors are isolated per source."""
    try:
        from googleapiclient.discovery import build
        import pickle
        token_pickle = Path.home() / "credentials" / "gdrive_token.pickle"
        if not token_pickle.exists():
            return ""
        with open(token_pickle, "rb") as f:
            creds = pickle.load(f)
        docs = build("docs", "v1", credentials=creds, cache_discovery=False)
        doc = docs.documents().get(documentId=doc_id).execute()
        chunks: list[str] = []
        for el in doc.get("body", {}).get("content", []):
            para = el.get("paragraph")
            if not para:
                continue
            for run in para.get("elements", []):
                tr = run.get("textRun")
                if tr and tr.get("content"):
                    chunks.append(tr["content"])
        return "".join(chunks)
    except Exception as e:
        print(f"  WARN: failed to read doc {doc_id}: {e}", file=sys.stderr)
        return ""


def load_sources(dry_run: bool) -> list[tuple[str, str]]:
    out = []
    for label, doc_id in SOURCE_DOCS:
        if dry_run:
            # Dry-run: do not hit Drive API; placeholder excerpt
            out.append((label, f"[DRY-RUN PLACEHOLDER — would read Doc {doc_id}; "
                               f"first {DRY_RUN_EXCERPT_CHARS} chars of real content "
                               f"would appear here in --no-dry-run mode]"))
        else:
            text = fetch_drive_doc_text(doc_id)
            text = text[:EXCERPT_PER_SOURCE_CHARS] if text else "[empty / unreadable]"
            out.append((label, text))
    return out


def build_source_block(sources: list[tuple[str, str]], dry_run: bool) -> str:
    lines = []
    for label, text in sources:
        excerpt = text[:DRY_RUN_EXCERPT_CHARS] if dry_run else text
        lines.append(f"--- SOURCE: {label} ---")
        lines.append(excerpt.strip())
        lines.append("")
    return "\n".join(lines)


def render_prompt(template: str, ctx: dict, source_block: str) -> str:
    principal = ctx.get("principal", {}) or {}
    firm = ctx.get("firm", {}) or {}
    sectors = principal.get("investment_focus", []) or []
    counterparties = [p.get("name", "") for p in (ctx.get("key_people") or []) if p.get("name")]
    today = date.today().isoformat()

    subs = {
        "{{date}}":               today,
        "{{firm_name}}":          firm.get("name", ""),
        "{{principal_name}}":     principal.get("name", ""),
        "{{sector_focus_csv}}":   ", ".join(sectors),
        "{{counterparties_csv}}": ", ".join(counterparties[:25]) or "(none configured)",
        "{{open_deals_csv}}":     "(loaded at runtime from deal-pipeline-data.json — not wired in spike)",
        "{{recent_actions_csv}}": "(loaded at runtime from Follow-ups Doc — not wired in spike)",
        "{{source_excerpts}}":    source_block,
    }
    out = template
    for k, v in subs.items():
        out = out.replace(k, v)
    return out


def call_claude(prompt: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError:
        print("ERROR: anthropic SDK not installed. pip install anthropic", file=sys.stderr)
        sys.exit(2)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in env", file=sys.stderr)
        sys.exit(2)
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
    return "\n".join(parts).strip()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="briefing-morning spike harness (Track G)")
    # DEFAULT --dry-run per task spec. Use --no-dry-run to actually call API.
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                    help="Print prompt only; no API call (DEFAULT).")
    ap.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                    help="Actually call Claude API and write output.")
    ap.add_argument("--out", default=f"/tmp/briefing_spike_{date.today().isoformat()}.txt",
                    help="Output path for memo (real run only).")
    args = ap.parse_args()

    ctx = load_firm_context()
    domain = resolve_domain(ctx)
    template = load_prompt_template(domain)
    sources = load_sources(dry_run=args.dry_run)
    source_block = build_source_block(sources, dry_run=args.dry_run)
    prompt = render_prompt(template, ctx, source_block)

    print(f"=== briefing-morning SPIKE ===")
    print(f"Domain        : {domain}")
    print(f"Model         : {MODEL}  (max_tokens={MAX_TOKENS})")
    print(f"Sources       : {len(sources)} docs")
    print(f"Prompt chars  : {len(prompt):,}")
    print(f"Dry-run       : {args.dry_run}")
    print(f"Output path   : {args.out}")
    print(f"================================\n")

    if args.dry_run:
        print("--- ASSEMBLED PROMPT (DRY-RUN; first 80 chars per source excerpt) ---")
        print(prompt)
        print("\n--- END PROMPT ---")
        print("\n[DRY-RUN] No API call made. Re-run with --no-dry-run to call Claude.")
        return 0

    print(f"Calling {MODEL}...")
    memo = call_claude(prompt)
    Path(args.out).write_text(memo)
    print(f"\nMemo written: {args.out}  ({len(memo):,} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
