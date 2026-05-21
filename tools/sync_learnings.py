#!/usr/bin/env python3
"""
sync_learnings.py — Regenerate downstream views from LEARNINGS-LEDGER.yaml.

The ledger at ~/dashboards/docs/LEARNINGS-LEDGER.yaml is the canonical source for
every behavioral rule / preference / hard-won lesson. This script regenerates:

  1. ~/.claude/CLAUDE.md — universal rules section between sentinel markers
  2. ~/dashboards/docs/LEARNINGS-INDEX.md — human-readable index of ALL learnings
  3. The local Claude memory MEMORY.md — index (under ~/.claude/projects/...)
  4. Drive: <firm> -- Principal Context gdoc — (optional, with --push-drive)
  5. Drive: <firm> -- Practice Patterns gdoc — (optional, with --push-drive)

The Drive updates happen via Deal Sync Writer setContent (edit-in-place, invariant I11).

By default this is local-only. Drive sync is opt-in. claude.ai project instructions
auto-update on the next dash-state-hook 24h cycle (because the gdoc IDs that feed
ref-doc-sync are the same IDs we update).

Usage:
    python3 sync_learnings.py                        # dry-run
    python3 sync_learnings.py --apply                # local files only
    python3 sync_learnings.py --apply --push-drive   # also update Drive gdocs
    python3 sync_learnings.py --apply --push-now     # also kick dash-state-hook
                                                      to force immediate claude.ai sync
"""

from __future__ import annotations
import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from coordination import lock, mark_run  # noqa: E402

HOLDER = "sync_learnings.py"

LEDGER_PATH = Path.home() / "dashboards/docs/LEARNINGS-LEDGER.yaml"
CLAUDE_MD_GLOBAL = Path.home() / ".claude/CLAUDE.md"
LEARNINGS_INDEX = Path.home() / "dashboards/docs/LEARNINGS-INDEX.md"
# Claude Code uses ~/.claude/projects/<encoded-cwd>/memory/MEMORY.md, where the
# encoded-cwd is the full cwd path with '/' replaced by '-'. We derive the cwd
# for a Documents/Claude project so this works on any subscriber's machine.
_HOME = Path.home()
_DOC_CLAUDE_PATH = str(_HOME / "Documents" / "Claude")
_ENCODED_CWD = _DOC_CLAUDE_PATH.replace("/", "-")
MEMORY_MD = _HOME / ".claude" / "projects" / _ENCODED_CWD / "memory" / "MEMORY.md"

BEGIN_MARKER = "<!-- ─── BEGIN GENERATED FROM LEARNINGS-LEDGER — DO NOT EDIT ─── -->"
END_MARKER = "<!-- ─── END GENERATED ─── -->"

# Drive gdoc IDs (canonical from drive-docs.yaml)
PRINCIPAL_CONTEXT_DOC_ID = "1DMlnylTPI4OArDYaXVDqsS22AhbQvcwbTxJnoHp0wyA"
PRACTICE_PATTERNS_DOC_ID = "1C3z_6hnKtYZcpQM4Ffh2qN4EiVEwThNDC9NwHlt-zqY"


# ── load ──────────────────────────────────────────────────────────────────────

def load_ledger() -> dict:
    return yaml.safe_load(LEDGER_PATH.read_text())


def active_learnings(ledger: dict) -> list[dict]:
    return [
        L for L in ledger.get("learnings", [])
        if L.get("status", "active") == "active"
    ]


# ── replace-between-markers helper ────────────────────────────────────────────

def replace_between_markers(text: str, begin: str, end: str, body: str,
                            file_label: str = "") -> str:
    """Replace whatever lives between BEGIN and END markers with body.
    If markers don't exist, insert at the end with a newline prefix."""
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end),
        flags=re.DOTALL,
    )
    replacement = begin + "\n" + body.rstrip() + "\n" + end
    if pattern.search(text):
        return pattern.sub(replacement, text)
    # Markers absent → append at end
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + replacement + "\n"


# ── render: ~/.claude/CLAUDE.md universal rules section ──────────────────────

def render_global_claude_md_section(learnings: list[dict]) -> str:
    """Render the universal rules block for ~/.claude/CLAUDE.md.
    Only includes rule-coded entries (rule_code is not null)."""
    rule_coded = [L for L in learnings if L.get("rule_code")]
    rule_coded.sort(key=lambda L: L["rule_code"])

    lines = [
        f"_{len(rule_coded)} rule-coded learnings, regenerated from "
        f"[LEARNINGS-LEDGER.yaml](file://{Path.home()}/dashboards/docs/LEARNINGS-LEDGER.yaml). "
        "Run `python3 ~/cos-pipeline/tools/sync_learnings.py --apply` after edits._",
        "",
    ]
    for L in rule_coded:
        lines.append(f"### {L['title']} (Rule {L['rule_code']})")
        lines.append("")
        rule_text = L.get("rule", "").strip()
        lines.append(rule_text)
        lines.append("")
        meta_parts = []
        if L.get("applies_to"):
            meta_parts.append(f"**Applies to:** {', '.join(L['applies_to'])}")
        if L.get("enforced_by"):
            meta_parts.append(f"**Enforced by:** {L['enforced_by']}")
        if meta_parts:
            lines.append(" · ".join(meta_parts))
            lines.append("")
    return "\n".join(lines)


# ── render: LEARNINGS-INDEX.md (the human-readable full index) ───────────────

def render_learnings_index(learnings: list[dict], ledger_meta: dict) -> str:
    by_domain: dict[str, list[dict]] = {}
    for L in learnings:
        by_domain.setdefault(L.get("domain", "uncategorized"), []).append(L)

    out = [
        "# LEARNINGS-INDEX — generated",
        "",
        f"_Auto-generated from [`LEARNINGS-LEDGER.yaml`](LEARNINGS-LEDGER.yaml) "
        f"({len(learnings)} active learnings). Do not edit this file directly._",
        "",
        f"**Last synced:** {ledger_meta.get('last_migrated', 'unknown')}  ·  "
        f"**Next ID:** {ledger_meta.get('next_id', '?')}",
        "",
    ]

    domain_order = ["universal", "deal", "drive", "dashboard", "cos_pipeline",
                    "financial_modeling", "personal", "meta", "uncategorized"]
    for domain in domain_order:
        if domain not in by_domain:
            continue
        entries = by_domain[domain]
        out.append(f"## Domain: {domain} ({len(entries)})")
        out.append("")
        out.append("| ID | Rule | Title | Confidence | Learned | Applies to |")
        out.append("|---|---|---|---|---|---|")
        for L in sorted(entries, key=lambda x: x["id"]):
            applies = ", ".join(L.get("applies_to", []))[:50]
            out.append(
                f"| {L['id']} | "
                f"{L.get('rule_code', '—')} | "
                f"{L['title']} | "
                f"{L.get('confidence', '?')} | "
                f"{L.get('learned', '—')} | "
                f"{applies} |"
            )
        out.append("")
    return "\n".join(out)


# ── render: MEMORY.md (the auto-memory index) ────────────────────────────────

def render_memory_md(learnings: list[dict]) -> str:
    """Single-line entries per learning, with link to source feedback/project file."""
    lines = []
    for L in sorted(learnings, key=lambda x: x["id"]):
        source = L.get("source_file", "")
        # If source is in the memory/ dir, link to it; otherwise just mention
        source_link = ""
        if source.startswith("memory/"):
            source_link = f"({source})"
        title = L["title"]
        rule_code = L.get("rule_code")
        if rule_code:
            title = f"{title} — {rule_code}"
        rule_summary = (L.get("rule", "").strip().split("\n")[0])[:160]
        lines.append(f"- [{title}]{source_link} — {rule_summary}")
    return "\n".join(lines)


# ── write local files ────────────────────────────────────────────────────────

def apply_global_claude_md(body: str, dry: bool) -> bool:
    text = CLAUDE_MD_GLOBAL.read_text()
    new_text = replace_between_markers(text, BEGIN_MARKER, END_MARKER, body,
                                       file_label=".claude/CLAUDE.md")
    if new_text == text:
        print("  ~/.claude/CLAUDE.md:        no change")
        return False
    if dry:
        print(f"  ~/.claude/CLAUDE.md:        WOULD UPDATE ({len(text)} → {len(new_text)} chars)")
    else:
        CLAUDE_MD_GLOBAL.write_text(new_text)
        print(f"  ~/.claude/CLAUDE.md:        UPDATED ({len(text)} → {len(new_text)} chars)")
    return True


def apply_index(body: str, dry: bool) -> bool:
    if LEARNINGS_INDEX.exists() and LEARNINGS_INDEX.read_text() == body:
        print("  LEARNINGS-INDEX.md:         no change")
        return False
    if dry:
        print(f"  LEARNINGS-INDEX.md:         WOULD WRITE ({len(body)} chars)")
    else:
        LEARNINGS_INDEX.write_text(body)
        print(f"  LEARNINGS-INDEX.md:         WRITTEN ({len(body)} chars)")
    return True


def apply_memory_md(body: str, dry: bool) -> bool:
    """MEMORY.md gets the generated index. Preserves nothing — fully regenerated."""
    if MEMORY_MD.exists() and MEMORY_MD.read_text() == body:
        print("  MEMORY.md:                  no change")
        return False
    if dry:
        print(f"  MEMORY.md:                  WOULD WRITE ({len(body)} chars)")
    else:
        MEMORY_MD.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_MD.write_text(body)
        print(f"  MEMORY.md:                  WRITTEN ({len(body)} chars)")
    return True


# ── Drive gdoc updates (opt-in via --push-drive) ─────────────────────────────

def update_drive_gdoc(file_id: str, content: str, label: str) -> bool:
    """Call Deal Sync Writer (setContent on the existing gdoc ID — edit-in-place)."""
    import requests
    import yaml as _yaml
    config_yaml = Path.home() / "cos-pipeline-config-tomac/config/deal_sync.yaml"
    if not config_yaml.exists():
        print(f"  Drive {label}: SKIP — deal_sync.yaml not found")
        return False
    cfg = _yaml.safe_load(config_yaml.read_text())
    url = cfg.get("url")
    secret = cfg.get("secret")
    if not url or not secret:
        print(f"  Drive {label}: SKIP — deal_sync.yaml missing url/secret")
        return False
    try:
        r = requests.post(url, json={"secret": secret, "fileId": file_id, "content": content},
                          timeout=30, allow_redirects=True)
        result = r.json() if r.headers.get("content-type","").startswith("application/json") else {"status":"unknown","raw":r.text[:200]}
        if result.get("status") == "ok":
            print(f"  Drive {label}:  ✓ ({result.get('bytes')} bytes)")
            return True
        print(f"  Drive {label}:  FAIL — {result.get('message', result)}")
        return False
    except Exception as e:
        print(f"  Drive {label}:  ERROR {e}")
        return False


def render_principal_context_doc(learnings: list[dict]) -> str:
    """Render the principal's Personal Context gdoc body — analytical defaults
    + rules that apply personally (not to the firm).

    The "WHO I AM" block here is a default template; tenant-specific content
    is layered on by the gdoc's manual sections (the script only updates the
    rules block between sentinel markers in production)."""
    personal = [L for L in learnings if L.get("domain") in ("universal", "meta")]
    out = [
        "# PRINCIPAL PERSONAL CONTEXT",
        "",
        "_Generated from [LEARNINGS-LEDGER.yaml](https://github.com/) by sync_learnings.py. "
        "Edit-in-place per invariant I11._",
        "",
        "---",
        "",
        "## WHO I AM",
        "",
        "Senior infrastructure PE professional. Co-founding <firm> with <partner_a>",
        "and <partner_b>. Think like a principal investor and board",
        "director — not an analyst. All output should reflect that frame: so-what first,",
        "specifics over generalities, named assets and firms not themes, investment",
        "implications not just descriptions.",
        "",
        "---",
        "",
        "## UNIVERSAL & META RULES",
        "",
    ]
    for L in sorted(personal, key=lambda x: x["id"]):
        title = L["title"]
        if L.get("rule_code"):
            title = f"{title} (Rule {L['rule_code']})"
        out.append(f"### {title}")
        out.append("")
        out.append(L.get("rule", "").strip())
        out.append("")
    return "\n".join(out)


def render_practice_patterns_doc(learnings: list[dict]) -> str:
    """Render the Practice Patterns gdoc body — how the work gets done."""
    practice = [L for L in learnings if L.get("domain") in ("deal", "drive", "cos_pipeline", "dashboard", "financial_modeling")]
    out = [
        "# TCIP -- PRACTICE PATTERNS",  # noqa: tenant-leak (TCIP is the product name)
        "",
        "_Generated from [LEARNINGS-LEDGER.yaml](https://github.com/) by sync_learnings.py. "
        "Edit-in-place per invariant I11._",
        "",
        "This document captures how the work runs — diligence sequence, communication",
        "patterns, output preferences, deal-pipeline / Drive discipline. Loaded at every",
        "session alongside Firm Context and Personal Context.",
        "",
        "---",
        "",
    ]
    by_domain: dict[str, list[dict]] = {}
    for L in practice:
        by_domain.setdefault(L["domain"], []).append(L)
    for domain in ("deal", "drive", "cos_pipeline", "dashboard", "financial_modeling"):
        if domain not in by_domain:
            continue
        out.append(f"## {domain.replace('_', ' ').title()}")
        out.append("")
        for L in sorted(by_domain[domain], key=lambda x: x["id"]):
            title = L["title"]
            if L.get("rule_code"):
                title = f"{title} (Rule {L['rule_code']})"
            out.append(f"### {title}")
            out.append("")
            out.append(L.get("rule", "").strip())
            out.append("")
        out.append("---")
        out.append("")
    return "\n".join(out)


# ── kick dash-state-hook (force immediate claude.ai sync) ────────────────────

def push_now() -> bool:
    """Force dash-state-hook to fire the project-instructions sync immediately.
    Mechanism: invalidate the project-inst-sync state file so next hook fire runs."""
    state_path = Path.home() / "dashboards/data/state/project-inst-sync-state.json"
    if not state_path.exists():
        print("  push-now: state file not found; next dash-state-hook run will sync naturally")
        return False
    try:
        state_path.unlink()
        print("  push-now: project-instructions-sync state invalidated; next hook fire will push")
        return True
    except Exception as e:
        print(f"  push-now: FAIL — {e}")
        return False


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    p.add_argument("--push-drive", action="store_true",
                   help="Also update Principal Context + Practice Patterns gdocs via Deal Sync Writer")
    p.add_argument("--push-now", action="store_true",
                   help="Also invalidate dash-state-hook state so claude.ai projects "
                        "sync on next hook fire (rather than waiting 24h)")
    args = p.parse_args(argv)

    if not LEDGER_PATH.exists():
        sys.exit(f"ERROR: ledger not found at {LEDGER_PATH}")

    print(f"sync_learnings.py — source: {LEDGER_PATH}")
    ledger = load_ledger()
    learnings = active_learnings(ledger)
    print(f"  {len(learnings)} active learnings loaded")

    body_global = render_global_claude_md_section(learnings)
    body_index = render_learnings_index(learnings, ledger.get("meta", {}))
    body_memory = render_memory_md(learnings)

    dry = not args.apply
    with lock("learnings-ledger", HOLDER, ttl_seconds=120, timeout_seconds=60):
        apply_global_claude_md(body_global, dry)
        apply_index(body_index, dry)
        apply_memory_md(body_memory, dry)

    if args.apply and args.push_drive:
        print()
        principal_body = render_principal_context_doc(learnings)
        practice_body = render_practice_patterns_doc(learnings)
        with lock(f"drive-gdoc:{PRINCIPAL_CONTEXT_DOC_ID}", HOLDER):
            update_drive_gdoc(PRINCIPAL_CONTEXT_DOC_ID, principal_body, "Principal Personal Context")
        with lock(f"drive-gdoc:{PRACTICE_PATTERNS_DOC_ID}", HOLDER):
            update_drive_gdoc(PRACTICE_PATTERNS_DOC_ID, practice_body, "Practice Patterns")

    if args.apply and args.push_now:
        print()
        push_now()

    if args.apply:
        mark_run(HOLDER)

    print()
    print("Done." if not dry else "Dry-run complete. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
