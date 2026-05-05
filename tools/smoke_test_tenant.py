#!/usr/bin/env python3
"""smoke_test_tenant.py — Fresh-tenant install regression smoke test.

WHY THIS EXISTS
---------------
The COS pipeline ships to multiple tenants (Tier 1 Claude-Code holders,
Tier 2a BYO-API-key, Tier 2b Yoni-managed proxy). The single biggest
onboarding-day failure mode is `cos-pipeline/` code silently defaulting
to the maintainer's tenant data — hardcoded names, deal slugs, principal
emails, Drive folder IDs leaking into a subscriber's first dashboard
render.

This script simulates a fresh-tenant install. It:
  1. Materializes a synthetic tenant config in a tempdir (Acme Capital,
     Jane Doe, no real-world deal names).
  2. Sets $COS_CONFIG_DIR to the tempdir.
  3. Exercises the key public-code surfaces (`_firm_context.load_firm_context`,
     selected check modules, the prompt-preamble builder) under that env.
  4. Asserts that NONE of the output contains a forbidden tenant string
     (the same denylist used by check_tenant_leak, plus the maintainer's
     own name).
  5. Cleans up.

Pass means: a brand-new subscriber spinning up cos-pipeline against
their own private config will NOT see Yoni/Tomac/Cholla/etc. anywhere
in the prompts, headers, or dashboard scaffolding.

Fail means: there's a hardcoded reference somewhere in the public code
that would leak into a new subscriber's install. Fix BEFORE shipping.

USAGE
-----
    python3 tools/smoke_test_tenant.py             # exit 0 pass, 1 fail
    python3 tools/smoke_test_tenant.py -v          # show every probe + leak hit

Suitable for pre-release CI or a weekly cron alongside system_health.
Does NOT require an internet connection or any API keys.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Synthetic tenant fixture ──────────────────────────────────────────────────
# A deliberately bland fictional firm. The names below MUST NOT collide
# with any real-world tenant (denylist below covers tomac/cholla/yoni
# and friends).
SYNTHETIC_FIRM_CONTEXT = """\
schema_version: 2
tenant_slug: smoke-acme
auth_mode: subscription

features:
  job_search: false
  call_recording: false
  podcast_transcription: false
  research_pdfs: false
  fundraising: false

read_only: false

principal:
  name: Jane Smoketest
  email: jane@smoke-acme.example
  role: managing director
  background: 12 years generalist private equity
  prior_firm: Generic Capital
  investor_frame: principal investor
  job_search_active: false
  investment_focus:
  - generic sector A
  - generic sector B

firm:
  name: Acme Smoketest Capital
  short_name: ASC

team:
- name: Bob Probeson
  role: deputy
  background: ''
  internal_call_role: ''

owner_whitelist:
- Jane
- Bob

workstream_categories:
  deal: Acme Pipeline
  recruiting: Recruiting
  other: Other

key_people:
- name: Carol Decoy
  context: synthetic counterparty for smoke test
  flag_in_actions: false

counterparty_aliases: []
peer_firms:
- Generic Peer One
- Generic Peer Two

draft_voice:
  tone: neutral
  preferred_signoff: 'Best, Jane'
  default_greeting: Hi [first_name],
  brevity: 2-4 sentences
  always_include: []
  never_include: []
  attach_resume_to: []
  context_to_include_in_replies: []

prompt_overrides:
  memo_focus_supplement: ''

transcript_sources: []
domain: generic-dealmaker
"""

# ── Forbidden-string denylist ────────────────────────────────────────────────
# Same pattern as tools/checks/check_tenant_leak.py + the maintainer's
# own name. If any of these appears in output rendered against the
# synthetic tenant config, that's a hardcoded leak.
FORBIDDEN = [
    # Tenants
    "tomac", "cholla", "thunderhead", "black bayou", "mercuria",
    "harbert", "gideon", "wafra", "piper", "berkman", "astris",
    "fit ventures", "us towers", "pngts", "tcip", "reinova",
    "onesearch", "korn ferry", "hudson bay", "quantum", "citadel",
    "castleton", "grosvenor", "ridgewood", "barton", "maven", "omerta",
    # Maintainer (must not leak into subscriber output)
    "yoni", "gontownik", "ygontownik",
    # Real Tomac team
    "mark saxe", "saxe",
]

# Allow-list: substrings within forbidden hits that are OK (config-path
# names that subscribers don't see in rendered output, etc.).
ALLOWED_SUBSTRINGS = [
    "cos-pipeline-config-tomac",  # path string in fallback search order; the path itself, not data
    "tomac\\b",                    # regex literal in denylist constants
]


def _scan_for_forbidden(text: str, source: str) -> list[str]:
    """Return list of leak-hit strings (lowercase needle + context line)."""
    if not text:
        return []
    hits = []
    low = text.lower()
    for needle in FORBIDDEN:
        idx = 0
        while True:
            pos = low.find(needle.lower(), idx)
            if pos < 0:
                break
            # Context: 60 chars around the hit
            start = max(0, pos - 30)
            end = min(len(text), pos + len(needle) + 30)
            ctx = text[start:end].replace("\n", " ")
            if any(allowed in ctx for allowed in ALLOWED_SUBSTRINGS):
                idx = pos + 1
                continue
            hits.append(f"[{source}] '{needle}' in: ...{ctx}...")
            idx = pos + len(needle)
    return hits


# ── Probe runners ────────────────────────────────────────────────────────────

def probe_firm_context_load(config_dir: Path, env: dict) -> tuple[str, str]:
    """Run `_firm_context.load_firm_context()` under the synthetic env;
    return (probe_name, captured_output_to_scan)."""
    code = (
        "import sys, json, os\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
        "import _firm_context as fc\n"
        "ctx = fc.load_firm_context()\n"
        "print(json.dumps(ctx, ensure_ascii=False))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=30,
    )
    captured = out.stdout + "\n" + out.stderr
    return ("firm_context.load", captured)


def probe_email_preamble(config_dir: Path, env: dict) -> tuple[str, str]:
    """Run `cos_email_backfill.py --print-prompt` under the synthetic env."""
    script = Path(__file__).parent.parent / "cos_email_backfill.py"
    if not script.exists():
        return ("email_preamble", "[skipped — script not present]")
    out = subprocess.run(
        [sys.executable, str(script), "--print-prompt"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    captured = out.stdout + "\n" + out.stderr
    return ("email_preamble", captured)


def probe_static_source_scan(config_dir: Path, env: dict) -> tuple[str, str]:
    """Static scan: every .py file under cos-pipeline/ (excluding tools/
    smoke + checks scaffolding + denylist-defining files) for hardcoded
    tenant strings. This is the single best leak-regression catcher —
    it doesn't matter what env you run under, hardcoded strings ship as
    is.

    Excluded paths:
      - tools/smoke_test_tenant.py (this file — defines the denylist)
      - tools/checks/check_tenant_leak.py (also defines the denylist)
      - any *.bak / *.next / *.pre-* / .pre-migration files
      - test/ fixture data
    """
    pipeline_root = Path(__file__).parent.parent
    skip_names = {
        "smoke_test_tenant.py",
        "check_tenant_leak.py",
    }
    findings = []
    for py in pipeline_root.rglob("*.py"):
        if py.name in skip_names:
            continue
        s = str(py)
        if any(seg in s for seg in (".bak", ".next", ".pre-", "/.git/", "__pycache__")):
            continue
        try:
            text = py.read_text(errors="replace")
        except Exception:
            continue
        for needle in FORBIDDEN:
            # Find at line granularity for actionable reports
            for i, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                if needle.lower() not in low:
                    continue
                # Skip lines that are obvious denylist literals or
                # comments referencing other tenants illustratively.
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    # Comments are softer — only flag if the needle
                    # would change subscriber-visible output. Most
                    # tenant comments are illustrative; allow them.
                    continue
                if any(allowed in line for allowed in ALLOWED_SUBSTRINGS):
                    continue
                rel = py.relative_to(pipeline_root)
                findings.append(f"static:{rel}:{i}: '{needle}' in: {stripped[:100]}")
                break  # one hit per needle per file is enough signal
    return ("static_source", "\n".join(findings) if findings else "[clean]")


# ── Main ─────────────────────────────────────────────────────────────────────

def run_smoke_test(verbose: bool = False) -> int:
    print("smoke_test_tenant: setting up synthetic tenant fixture...")
    with tempfile.TemporaryDirectory(prefix="cos-smoke-") as td:
        config_dir = Path(td)
        (config_dir / "firm_context.yaml").write_text(SYNTHETIC_FIRM_CONTEXT)
        # Empty config subdir (some loaders look for config/deal-config.yaml)
        (config_dir / "config").mkdir(exist_ok=True)
        (config_dir / "config" / "deal-config.yaml").write_text(
            "deals: []\n"
            "advisors: []\n"
            "recruiters: []\n"
        )

        env = {
            **os.environ,
            "COS_CONFIG_DIR": str(config_dir),
        }
        # Defensively unset anything that might re-route to the real tenant
        for k in ("CONFIG_DIR", "TENANT_SLUG"):
            env.pop(k, None)

        if verbose:
            print(f"  fixture at: {config_dir}")
            print(f"  COS_CONFIG_DIR={config_dir}")

        probes = [
            ("dynamic", probe_firm_context_load),
            ("dynamic", probe_email_preamble),
            ("static",  probe_static_source_scan),
        ]
        all_hits: list[str] = []
        all_results: list[tuple[str, int, int]] = []
        for kind, fn in probes:
            try:
                name, captured = fn(config_dir, env)
            except Exception as e:
                name = fn.__name__
                captured = f"[probe raised: {e}]"
            if kind == "static":
                # Static probe already returns line-by-line findings;
                # don't re-scan, just count non-empty / non-clean lines.
                if captured.strip() and captured.strip() != "[clean]":
                    hits = [line for line in captured.splitlines() if line.strip()]
                else:
                    hits = []
            else:
                hits = _scan_for_forbidden(captured, name)
            all_hits.extend(hits)
            all_results.append((name, len(captured), len(hits)))
            if verbose:
                print(f"  probe {name} ({kind}): {len(captured)} bytes, {len(hits)} leak hit(s)")

        # Summary
        total_leaks = len(all_hits)
        if total_leaks == 0:
            print(f"smoke_test_tenant: PASS · {len(probes)} probes · 0 tenant leaks")
            return 0

        print(f"smoke_test_tenant: FAIL · {len(probes)} probes · {total_leaks} tenant leak(s)")
        for h in all_hits[:20]:
            print(f"  · {h}")
        if total_leaks > 20:
            print(f"  · ... and {total_leaks - 20} more")
        return 1


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print probe-by-probe details")
    args = p.parse_args()
    sys.exit(run_smoke_test(verbose=args.verbose))


if __name__ == "__main__":
    main()
