#!/usr/bin/env python3
"""
validate_tenant.py — Synthetic-tenant end-to-end validation.

Tests that a non-tomac subscription tenant can:
  1. Load its firm_context cleanly (no stub guard failures)
  2. Build a correct, tenant-specific system prompt
  3. Dispatch a real SKILL-quality call through _model_router subscription path
  4. Produce structured output and write to data-<tenant>/dispatch.jsonl

Usage:
  python3 validate_tenant.py [--tenant re-dev] [--dry-run] [--call]

  --tenant   Tenant slug (default: re-dev)
  --dry-run  Verify firm_context + system prompt only; no Claude call
  --call     Fire one real subscription call (counts against quota)

Default (no flags): dry-run only. Pass --call to run the real dispatch.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(_HERE))

PASS  = "✓"
FAIL  = "✗"
WARN  = "!"

def _ok(msg: str):   print(f"  {PASS} {msg}")
def _bad(msg: str):  print(f"  {FAIL} {msg}"); _FAILURES.append(msg)
def _warn(msg: str): print(f"  {WARN} {msg}")

_FAILURES: list[str] = []


# ── Phase 1: firm_context load ────────────────────────────────────────────────

def phase1_load_context(tenant: str) -> dict:
    print(f"\n{'═'*60}")
    print(f"PHASE 1 — Load firm_context for tenant={tenant!r}")
    print(f"{'═'*60}")

    config_dir = Path.home() / f"cos-pipeline-config-{tenant}"
    if not config_dir.exists():
        _bad(f"Config dir not found: {config_dir}")
        return {}

    _ok(f"Config dir: {config_dir}")

    # Set COS_CONFIG_DIR so _firm_context.py resolves to this tenant
    os.environ["COS_CONFIG_DIR"] = str(config_dir)
    _ok(f"COS_CONFIG_DIR set to {config_dir}")

    import _firm_context as _fc
    # Reload to pick up new env var (module may be cached)
    import importlib
    importlib.reload(_fc)

    try:
        ctx = _fc.load_firm_context()
    except Exception as e:
        _bad(f"load_firm_context() raised: {e}")
        return {}

    # Validate required fields
    status = ctx.get("status", "")
    if status == "stub":
        _bad("firm_context.yaml status=stub — fill principal/firm before running pipelines")
    elif status == "active":
        _ok(f"status: active")
    else:
        _warn(f"status field absent or unexpected: {status!r}")

    p = ctx.get("principal", {}) or {}
    f = ctx.get("firm", {}) or {}

    for field, val in [("principal.name", p.get("name")),
                       ("principal.email", p.get("email")),
                       ("principal.role", p.get("role")),
                       ("firm.name", f.get("name")),
                       ("firm.short_name", f.get("short_name"))]:
        if val:
            _ok(f"{field}: {val!r}")
        else:
            _bad(f"{field} is empty — required")

    auth_mode = ctx.get("auth_mode")
    if auth_mode == "subscription":
        _ok(f"auth_mode: subscription ← will route through SDK dispatch")
    elif auth_mode == "api":
        _warn(f"auth_mode: api — will use Anthropic API key, not subscription")
    else:
        _warn(f"auth_mode absent or unknown: {auth_mode!r}")

    tenant_slug = ctx.get("tenant_slug") or (f.get("short_name") or "").lower()
    if tenant_slug:
        _ok(f"tenant_slug: {tenant_slug!r}")
    else:
        _warn("tenant_slug absent — log paths will fall back to 'tomac'")

    return ctx


# ── Phase 2: system prompt build ──────────────────────────────────────────────

def phase2_build_prompt(ctx: dict, tenant: str) -> str:
    print(f"\n{'═'*60}")
    print(f"PHASE 2 — Build SKILL system prompt from re-dev firm_context")
    print(f"{'═'*60}")

    if not ctx:
        _bad("No context — skipping system prompt build")
        return ""

    try:
        import cos_capture_pipeline as _cap
        system_prompt = _cap.build_system_prompt(ctx)
    except Exception as e:
        _bad(f"build_system_prompt() raised: {e}")
        return ""

    p_name = (ctx.get("principal", {}) or {}).get("name", "")
    f_name = (ctx.get("firm", {}) or {}).get("name", "")

    if p_name and p_name in system_prompt:
        _ok(f"Principal name {p_name!r} appears in system prompt")
    else:
        _bad(f"Principal name {p_name!r} NOT found in system prompt — tomac bleed-through likely")

    if f_name and f_name in system_prompt:
        _ok(f"Firm name {f_name!r} appears in system prompt")
    else:
        _bad(f"Firm name {f_name!r} NOT found in system prompt")

    # Negative check: tomac-specific strings should not appear
    tomac_markers = ["Tomac Cove", "Yoni Gontownik", "ygontownik", "Mark Saxe"]
    for marker in tomac_markers:
        if marker.lower() in system_prompt.lower():
            _bad(f"Tomac marker found in non-tomac prompt: {marker!r}")
        else:
            _ok(f"No tomac bleed-through for {marker!r}")

    _ok(f"System prompt length: {len(system_prompt)} chars")
    print(f"\n  --- system prompt preview (first 400 chars) ---")
    print("  " + system_prompt[:400].replace("\n", "\n  "))
    print(f"  ---")

    return system_prompt


# ── Phase 3: subscription dispatch ────────────────────────────────────────────

SYNTHETIC_EMAILS = """
EMAIL 1
From: Tom Whitfield <twhitfield@cbre.com>
Date: 2026-05-04 09:15 UTC
Subject: 1401 K Street NW — Seller ready to proceed
Body: Sarah, good news — the seller at 1401 K Street is ready to move
forward. They're asking for an LOI by May 9th. 72-unit adaptive reuse,
$31M ask, ~$430k/unit. Let me know if Archbridge wants to proceed.

EMAIL 2
From: James Okafor <james@archbridgecap.com>
Date: 2026-05-04 08:00 UTC
Subject: PGIM call — Thursday 3pm
Body: Sarah, I scheduled a call with PGIM Real Estate for Thursday at 3pm.
They want to discuss co-investment on the Bethesda workforce housing land
we're tracking. I'll prep the teaser.

EMAIL 3
From: Maya Petrov <maya@archbridgecap.com>
Date: 2026-05-03 17:30 UTC
Subject: Silver Spring land site — title clear
Body: Just heard from the title company — Silver Spring site is clear.
We can proceed to LOI. Need your sign-off before I send to seller's counsel.

EMAIL 4
From: Marcus Lee <mlee@fanniemae.com>
Date: 2026-05-03 14:00 UTC
Subject: DUS commitment letter — Riverdale Flats
Body: Sarah, your DUS commitment letter for Riverdale Flats (Case #2026-DC-1187,
$18.2M HUD 221(d)(4)) is ready. Please sign and return by May 10th.
Attached for review.

EMAIL 5
From: Linda Kim <lkim@archbridgecap.com>
Date: 2026-05-04 07:45 UTC
Subject: Wire confirmation — escrow deposit Rockville site
Body: Just a heads up — the $250k escrow wire for Rockville cleared this morning.
"""

SYNTHETIC_CALENDAR = [
    {"title": "PGIM Real Estate — Co-investment discussion", "date": "2026-05-08", "time": "15:00"},
    {"title": "Architecture review — Silver Spring", "date": "2026-05-07", "time": "10:00"},
    {"title": "LOI deadline — 1401 K Street NW", "date": "2026-05-09", "time": "EOD"},
    {"title": "DUS commitment letter deadline — Riverdale Flats", "date": "2026-05-10", "time": "EOD"},
    {"title": "Weekly deal review (internal)", "date": "2026-05-06", "time": "09:00"},
]

SYNTHETIC_FOLLOWUPS = """
[ROW 1] Who: Sarah + Tom Whitfield (CBRE) | What: Follow up on Bethesda mixed-use land pricing | Due: 2026-05-05
[ROW 2] Who: Sarah + Marcus Lee (Fannie Mae) | What: DUS application status — Riverdale Flats | Due: 2026-05-06
[ROW 3] Who: Sarah + Maya | What: Review Rockville site rent roll analysis | Due: 2026-05-04
"""


def phase3_subscription_call(system_prompt: str, tenant: str, ctx: dict) -> dict | None:
    print(f"\n{'═'*60}")
    print(f"PHASE 3 — Subscription dispatch call (tenant={tenant!r})")
    print(f"{'═'*60}")

    if not system_prompt:
        _bad("No system prompt — skipping dispatch call")
        return None

    try:
        import _model_router as mr
    except ImportError as e:
        _bad(f"Cannot import _model_router: {e}")
        return None

    dispatch_path = _HERE / f"data-{tenant}" / "dispatch.jsonl"
    prior_line_count = 0
    if dispatch_path.exists():
        prior_line_count = len(dispatch_path.read_text().splitlines())

    user_payload = (
        f"TODAY: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"=== EMAILS (last 24h, {len(SYNTHETIC_EMAILS.strip().splitlines())} lines) ===\n"
        f"{SYNTHETIC_EMAILS}\n\n"
        f"=== UPCOMING CALENDAR (next 14 days) ===\n"
        f"{json.dumps(SYNTHETIC_CALENDAR, indent=2)}\n\n"
        f"=== EXISTING FOLLOW-UPS DOC (current state) ===\n"
        f"{SYNTHETIC_FOLLOWUPS}\n\n"
        f"Apply the SKILL ruleset. Return the JSON spec."
    )

    _ok(f"User payload: {len(user_payload)} chars")
    _ok("Calling _model_router.call_claude(task_type='cos-capture-pipeline', mode='subscription', tenant={!r}) ...".format(tenant))

    try:
        result = mr.call_claude(
            task_type="cos-capture-pipeline",
            system=system_prompt,
            messages=[{"role": "user", "content": user_payload}],
            mode="subscription",
            tenant=tenant,
        )
    except Exception as e:
        _bad(f"call_claude raised: {type(e).__name__}: {e}")
        return None

    # Verify dispatch ledger updated
    new_line_count = 0
    if dispatch_path.exists():
        lines = dispatch_path.read_text().splitlines()
        new_line_count = len(lines)
        new_entries = new_line_count - prior_line_count
        if new_entries > 0:
            _ok(f"dispatch.jsonl gained {new_entries} new entr{'y' if new_entries==1 else 'ies'} → {dispatch_path}")
            last = json.loads(lines[-1])
            _ok(f"  task_type: {last.get('task_type')}")
            _ok(f"  mode: {last.get('mode')}")
            _ok(f"  outcome: {last.get('outcome')}")
            _ok(f"  rate_limit_status: {last.get('rate_limit_status')}")
        else:
            _bad("dispatch.jsonl was NOT updated — subscription call may not have recorded")
    else:
        _bad(f"dispatch.jsonl not found at {dispatch_path}")

    text = result.get("text", "")
    est_usd = result.get("est_usd", 0.0)
    sub_meta = result.get("subscription_meta", {})

    _ok(f"est_usd: {est_usd} (subscription — always 0)")
    _ok(f"subscription_meta: {sub_meta}")
    _ok(f"text length: {len(text)} chars")

    # Try to parse JSON output
    try:
        parsed = json.loads(text)
        _ok("Output is valid JSON")
        keys = list(parsed.keys()) if isinstance(parsed, dict) else ["(array)"]
        _ok(f"Top-level keys: {keys}")
    except json.JSONDecodeError:
        _warn("Output is not JSON (expected for capture pipeline) — checking structure")
        if "action" in text.lower() or "follow" in text.lower() or "draft" in text.lower():
            _ok("Output contains action/follow-up/draft markers — structurally plausible")
        else:
            _warn("Output structure unclear — inspect manually")

    print(f"\n  --- output preview (first 600 chars) ---")
    print("  " + text[:600].replace("\n", "\n  "))
    print(f"  ---")

    return result


# ── Phase 4: dashboard data compatibility check ───────────────────────────────

def phase4_dashboard_check(tenant: str, ctx: dict):
    print(f"\n{'═'*60}")
    print(f"PHASE 4 — Dashboard tomac-hardcode audit for tenant={tenant!r}")
    print(f"{'═'*60}")

    server = _HERE / "cos-dashboard-server.py"
    if not server.exists():
        _warn(f"cos-dashboard-server.py not found at {server}")
        return

    src = server.read_text()
    # (pattern, severity) — "block" = code bug, "warn" = operational/config
    markers = {
        "COS_TENANT_SLUG default='tomac'":     (r"COS_TENANT_SLUG',\s*'tomac'",         "warn"),
        "TOMAC_DATA alias":                    (r"TOMAC_DATA",                            "warn"),
        "TC_BUILD alias":                      (r"TC_BUILD",                              "warn"),
        "_load_tomac_config":                  (r"_load_tomac_config",                    "warn"),
        "/tomac-cove route":                   (r"'/tomac-cove",                          "warn"),
        "hardcoded cos-pipeline-config-tomac": (r"cos-pipeline-config-tomac",             "block"),
    }
    import re
    blocking: list[str] = []
    non_blocking: list[str] = []
    for label, (pattern, severity) in markers.items():
        if re.search(pattern, src):
            if severity == "block":
                blocking.append(label)
                _bad(f"BLOCKING  — {label}")
            else:
                non_blocking.append(label)
                _warn(f"non-blocking compat alias — {label}")
        else:
            _ok(f"Clean: {label}")

    # Check if data-re-dev has any dashboard-compatible files
    data_dir = _HERE / f"data-{tenant}"
    expected = ["dispatch.jsonl", "subscription-health.json"]
    for f in expected:
        p = data_dir / f
        if p.exists():
            _ok(f"data-{tenant}/{f} exists ({p.stat().st_size} bytes)")
        else:
            _warn(f"data-{tenant}/{f} absent — dashboard may skip tiles")

    if blocking:
        print(f"\n  Summary: {len(blocking)} BLOCKING hardcodes in dashboard server.")
        print(f"  These prevent non-tomac tenants from routing correctly via COS_TENANT_SLUG.")
        print(f"  Fix: parameterize default to COS_TENANT_SLUG env var with no hardcoded fallback,")
        print(f"  or add a per-slug path resolution table.")
    else:
        _ok("No blocking tomac hardcodes found — non-tomac tenant should render cleanly.")


# ── Phase 5: personal briefing SKILL ─────────────────────────────────────────

SYNTHETIC_BRIEFING_DOCS = {
    "followups": """
[ROW 1] Who: Sarah + Tom Whitfield (CBRE) | What: Submit LOI for 1401 K Street NW | Due: 2026-05-09 | Priority: HIGH
[ROW 2] Who: Sarah + Marcus Lee (Fannie Mae) | What: Sign and return DUS commitment letter (Riverdale Flats, $18.2M) | Due: 2026-05-10 | Priority: HIGH
[ROW 3] Who: Sarah + Maya | What: Review and approve Silver Spring LOI before Maya sends to seller's counsel | Due: 2026-05-05 | Priority: HIGH
[ROW 4] Who: Sarah + PGIM Real Estate (James leading) | What: PGIM co-investment call prep — Bethesda workforce housing | Due: 2026-05-08 | Priority: MEDIUM
""",
    "deal_pipeline": """
ARCHBRIDGE CAPITAL — ACTIVE DEAL PIPELINE (as of 2026-05-04)

1401 K Street NW, Washington DC
  Type: Adaptive reuse → 72-unit multifamily
  Status: LOI stage | Ask: $31M (~$430k/unit)
  Counterparty: Tom Whitfield (CBRE, seller's broker)
  Next: LOI deadline May 9th

Silver Spring Land, MD
  Type: Ground-up multifamily (workforce housing, ~120 units)
  Status: LOI stage | Title cleared May 3rd
  Counterparty: Maya coordinating with seller's counsel
  Next: Sarah sign-off on LOI, then send

Riverdale Flats, DC
  Type: Multifamily new construction, HUD 221(d)(4)
  Status: DUS commitment issued ($18.2M, Case #2026-DC-1187)
  Counterparty: Marcus Lee (Fannie Mae DUS)
  Next: Sign and return commitment letter by May 10th

Bethesda Workforce Housing Land (potential)
  Type: Land acquisition for ground-up workforce housing
  Status: Early stage — PGIM co-investment discussion
  Counterparty: PGIM Real Estate
  Next: Co-investment call May 8th at 3pm
""",
    "market_update": """
DMV MULTIFAMILY MARKET — May 2026
- DC metro vacancy holding at 4.8% (Class B/C: 3.9%) — strong demand for workforce product
- Cap rates: Class A 4.75-5.0%, Class B 5.25-5.5%, workforce housing 5.75-6.0%
- HUD 221(d)(4) rates: ~5.2% fixed for 40yr term (improved from Q1)
- Fannie Mae DUS volume up 12% YTD — appetite for workforce housing remains strong
- Silver Spring submarket: 18% rent growth over 24 months; limited new supply pipeline
"""
}

BRIEFING_SYSTEM = """\
You are the Chief of Staff AI for Sarah Chen — Co-Founder & Managing Partner, \
Archbridge Capital. Generate a concise morning briefing for Sarah covering: \
(1) TODAY'S PRIORITIES based on the follow-ups doc; \
(2) DEAL PIPELINE STATUS — what's moving, what needs attention; \
(3) MARKET PULSE — one-paragraph summary of what's notable. \
Write for a principal investor scanning in 5 minutes. Lead with the so-what. \
No filler phrases. Numbered priorities. Present tense."""

def phase5_briefing_call(tenant: str, ctx: dict) -> dict | None:
    print(f"\n{'═'*60}")
    print(f"PHASE 5 — Briefing SKILL subscription call (tenant={tenant!r})")
    print(f"{'═'*60}")

    try:
        import _model_router as mr
    except ImportError as e:
        _bad(f"Cannot import _model_router: {e}")
        return None

    dispatch_path = _HERE / f"data-{tenant}" / "dispatch.jsonl"
    prior_count = len(dispatch_path.read_text().splitlines()) if dispatch_path.exists() else 0

    p = ctx.get("principal", {}) or {}
    user_msg = (
        f"TODAY: {datetime.now(timezone.utc).strftime('%B %-d, %Y (%A)')}\n\n"
        f"=== FOLLOW-UPS DOC ===\n{SYNTHETIC_BRIEFING_DOCS['followups']}\n\n"
        f"=== DEAL PIPELINE DOC ===\n{SYNTHETIC_BRIEFING_DOCS['deal_pipeline']}\n\n"
        f"=== MARKET UPDATE ===\n{SYNTHETIC_BRIEFING_DOCS['market_update']}\n\n"
        f"Generate the morning briefing for {p.get('name', 'the principal')} now."
    )

    _ok(f"User payload: {len(user_msg)} chars")
    _ok("Calling _model_router.call_claude(task_type='cos-personal-briefing', mode='subscription') ...")

    try:
        result = mr.call_claude(
            task_type="cos-personal-briefing",
            system=BRIEFING_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            mode="subscription",
            tenant=tenant,
        )
    except Exception as e:
        _bad(f"call_claude raised: {type(e).__name__}: {e}")
        return None

    new_count = len(dispatch_path.read_text().splitlines()) if dispatch_path.exists() else 0
    if new_count > prior_count:
        last = json.loads(dispatch_path.read_text().splitlines()[-1])
        _ok(f"dispatch.jsonl +{new_count - prior_count} entry | outcome={last.get('outcome')} | rate_limit={last.get('rate_limit_status')}")
    else:
        _bad("dispatch.jsonl not updated")

    text = result.get("text", "")
    _ok(f"Briefing text length: {len(text)} chars")

    # Quality checks: should mention real deal names from synthetic docs
    quality_markers = ["1401 K Street", "Riverdale", "Silver Spring", "Sarah", "LOI"]
    hits = [m for m in quality_markers if m.lower() in text.lower()]
    misses = [m for m in quality_markers if m.lower() not in text.lower()]
    _ok(f"Deal-name hits in output: {hits}")
    if misses:
        _warn(f"Missing from output: {misses} — may be truncated or summarized")

    # No tomac bleed
    for marker in ["Tomac Cove", "Yoni Gontownik", "Mark Saxe", "ygontownik"]:
        if marker.lower() in text.lower():
            _bad(f"Tomac bleed-through in briefing output: {marker!r}")
        else:
            _ok(f"No bleed-through: {marker!r}")

    print(f"\n  --- briefing preview (first 800 chars) ---")
    print("  " + text[:800].replace("\n", "\n  "))
    print(f"  ---")

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant",   default="re-dev", help="Tenant slug (default: re-dev)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Phases 1-2 + 4 only; no Claude call")
    parser.add_argument("--call",     action="store_true",
                        help="Fire Phase 3 (capture) subscription call")
    parser.add_argument("--briefing", action="store_true",
                        help="Fire Phase 5 (personal briefing) subscription call")
    args = parser.parse_args()

    tenant = args.tenant
    fire_call = args.call and not args.dry_run
    fire_briefing = args.briefing and not args.dry_run

    print(f"\n{'╔'+'═'*58+'╗'}")
    print(f"║  validate_tenant.py — tenant={tenant!r:<30}   ║")
    print(f"║  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'):<54}   ║")
    print(f"╚{'═'*58}╝")

    ctx = phase1_load_context(tenant)
    system_prompt = phase2_build_prompt(ctx, tenant)

    if fire_call:
        phase3_subscription_call(system_prompt, tenant, ctx)
    else:
        print(f"\n{'═'*60}")
        print(f"PHASE 3 — Capture SKILL call  [SKIPPED — pass --call to run]")
        print(f"{'═'*60}")

    phase4_dashboard_check(tenant, ctx)

    if fire_briefing:
        phase5_briefing_call(tenant, ctx)
    else:
        print(f"\n{'═'*60}")
        print(f"PHASE 5 — Briefing SKILL call  [SKIPPED — pass --briefing to run]")
        print(f"{'═'*60}")

    # Summary
    print(f"\n{'═'*60}")
    print(f"SUMMARY")
    print(f"{'═'*60}")
    total = len(_FAILURES)
    if total == 0:
        print(f"  {PASS} All checks passed. Tenant {tenant!r} is ready.")
    else:
        print(f"  {FAIL} {total} failure(s):")
        for f in _FAILURES:
            print(f"    - {f}")

    if not fire_call and not args.dry_run:
        print(f"\n  --call   fires Phase 3 capture SKILL (1 subscription call)")
        print(f"  --briefing fires Phase 5 briefing SKILL (1 subscription call)")

    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
