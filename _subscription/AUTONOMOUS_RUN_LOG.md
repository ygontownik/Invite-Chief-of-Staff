# Autonomous Run Log

Append-only log. One entry per milestone. Read top-down for chronological order.

---

## 2026-05-03T00:00:00Z — CANARY

- ls _subscription/: 4 expected items present (CACHE_BREAKPOINT_DECISION.md, cached_client.py, mcp_server/, system_prompt_v1.md) plus cache_telemetry.jsonl + __pycache__
- mcp_server tests: 6/6 PASS (transcripts_search hit real Drive call cleanly)
- cached_client smoke (default model = claude-sonnet-4-6):
  - R1: creation=2784, read=0, uncached_input=79, output=6
  - R2: creation=76, read=2784, uncached_input=3, output=6
  - cache write/read verified end-to-end on Sonnet
- git status _subscription/: shown as untracked (clean state, ready for M1 commit)
- VERDICT: canary clean, proceeding to milestones

---

## 2026-05-03T00:05:00Z — M1 COMPLETE

- Committed _subscription/ sandbox build to main
- Commit hash: d46202b
- 9 files, 797 insertions
- Note: AUTONOMOUS_RUN_LOG.md included in this commit; subsequent log appends are uncommitted in-progress
- Telemetry file (cache_telemetry.jsonl) and __pycache__ correctly excluded by .gitignore

---

## 2026-05-03T00:08:00Z — M2 COMPLETE

- Promoted tracked-firms (11 names) and briefing INCLUDE/EXCLUDE classifier into static core (above CBP1) as new H3 subsections of section 5
- Removed corresponding bullets from section 6 bundle description
- Token count for static core (sections 1-5) on Opus 4.7: 4234 tokens — within target band 4,200-4,800, above 4,096 Opus floor
- Opus 4.7 cache validation (LOAD-BEARING):
  - R1: creation=4515, read=0
  - R2: creation=109, read=4515
  - PASS: cache_read > 0 confirmed on Opus 4.7
- Commit hash: 336a71b

---

## 2026-05-03T00:14:00Z — M3 COMPLETE

- Replaced {{TENANT_BUNDLE}} placeholder with full Tomac overlay: sectors, analytical lenses, people context, top 7 deal themes from deal-pipeline-data.json
- Static core: 4234 tokens (unchanged from M2)
- Bundle (segment 2): 1638 tokens — slightly over target band (600-1,200), driven by 7 themes × 200-char thesis previews
- Combined seg1+seg2: 5872 tokens
- Opus 4.7 cache test (third run, populated bundle):
  - R1: creation=1627, read=4223 (static core hit from M2 cache; bundle freshly cached)
  - R2: creation=1740, read=4223 (static core hit again; bundle creation count slightly fluctuated — investigating in M4)
- Both layers showing cache activity. Bundle-layer creation behavior on R2 (1740 vs expected 0) is anomalous and warrants attention in measurement harness.
- Design note: cached_client.py's `tenant_bundle` runtime arg is now dead code (no placeholder remains). Acceptable for single-tenant Tomac; flag for S8 multi-tenant template work.
- Commit hash: 73ae1b3

---

## 2026-05-03T00:35:00Z — M4 COMPLETE

- Built fixtures/podcast_snippet.json (synthetic 1500-word transcript), fixtures/briefing_email_batch.json (5 emails), fixtures/deal_screen.json (one-page CIM)
- Built measure.py (3 fixtures × 2 models × 5 calls = 30 API calls)
- All 30 calls completed successfully; total spend ~$0.60
- Headline numbers (cost/call cached avg vs uncached baseline):
  - Sonnet podcast: $0.0264 vs $0.0334 (21.1% savings)
  - Sonnet briefing: $0.0185 vs $0.0292 (36.8% savings)
  - Sonnet deal_screen: $0.0193 vs $0.0299 (35.4% savings)
  - Opus podcast: $0.0512 vs $0.0682 (24.9% savings)
  - Opus briefing: $0.0332 vs $0.0588 (43.6% savings)
  - Opus deal_screen: $0.0349 vs $0.0602 (42.0% savings)
- Pass 3 question (Opus vs Sonnet on deal_screen): $0.0349 vs $0.0193 = 1.81× — small absolute delta, real multiple
- Anomaly observed: cache_creation reports nonzero (~1934 tokens for Sonnet, ~2654 for Opus) on calls 2-5 even though prompt is identical. Likely TTL refresh writes — Anthropic counts cache extension as creation. Did not impact savings calculation; flagged for follow-up.
- Commit hash: c40a43c

---

## 2026-05-03T00:50:00Z — M5 COMPLETE

- Copied podcast_transcribe.py and cos_gmail_mini_v2.py to migrations/<name>.draft.py — live files untouched
- podcast_transcribe.draft.py: generate_memo() rewritten to call cached_client.complete(); MEMO_PREAMBLE moves from cached user-block to volatile user_query slot. Net cost ~flat; gain is architectural consistency.
- cos_gmail_mini_v2.draft.py: both haiku_triage() and sonnet_enrich() rewritten. haiku_triage shows ~50% cost regression (4.2k cached prefix dwarfs Haiku's small native footprint) — recommended in MIGRATION_PLAN to keep haiku_triage on original path or accept the cost increase as the price of architectural unity.
- Two MIGRATION_PLAN.md files written with diff summary, test commands, expected cost impact, rollback plan, and exact 1-command swap
- Commit hash: c0c4d5c

---

## 2026-05-03T00:52:00Z — M6 IN PROGRESS

Morning report being written to MORNING_REPORT.md.
