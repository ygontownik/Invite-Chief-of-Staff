# ~/cos-pipeline/tools/ — inventory + ownership map

> **Before adding script #29: check this README.** Most likely there's an existing
> tool that already covers your concern. If you must add a new one, drop it in the
> right overlap group below and update this file in the same commit.

This directory holds the **public** pipeline + tooling code. No tenant data,
no hardcoded slugs (enforced by `check_tenant_leak`). Tenant config lives in
`~/cos-pipeline-config-<tenant>/`; runtime state in `~/dashboards/`.

Last reviewed: **2026-05-20** (Block E). 28 scripts inventoried below.

---

## How to use this file

- **Reading order:** Find the concern your task touches in §1. The scripts
  listed there are the ones to read/extend. Don't re-implement.
- **Adding a new tool:** First, see §2 (overlap groups). If your idea fits an
  existing group, extend; don't fork. If it's genuinely new, pick a domain
  in §1 and add it there.
- **Naming:** snake_case for Python, kebab-case for shell wrappers. Verb-first
  (`sync_*`, `audit_*`, `cleanup_*`, `migrate_*`). Avoid generic names.

---

## §1 — Scripts by owner concern

### Coordination + system state

| Script | Purpose |
|---|---|
| [`coordination.py`](coordination.py) | Shared advisory locks + last-run state. Import `lock()` ctx-manager before any shared-state write (drive-docs.yaml, GAS files via clasp, log.json, deal status docs). CLI: `python3 coordination.py status \| clear-stale`. |
| [`dash-state-hook.py`](dash-state-hook.py) | Claude Code Stop hook. Orchestrates auto-pipelines: deal-entry sync (2h), deal-extract sync (2h), reference docs mirror (2h), project instructions push (24h), intel capture (per-session), chat capture (4h). The meta-cadence. |

### Audits + health checks

| Script | Purpose |
|---|---|
| [`system_health.py`](system_health.py) | Aggregator. Dynamically imports every `checks/check_*.py`, runs each, writes consolidated report to `~/dashboards/data/system-health/`. The pre-push confidence check (rule L0020). |
| [`audit_config_drift.py`](audit_config_drift.py) | Audits config-path topology (cos-pipeline / cos-pipeline-config / dashboards). Detects symlink breakage between the three repos. |
| [`reference_integrity_audit.py`](reference_integrity_audit.py) | Daily Drive reference integrity. Confirms every registered ID in drive-docs.yaml still resolves; cross-checks claude.ai project instructions. Enforces I11/EP1. **DO NOT MODIFY — Chat B owns.** |
| [`smoke_test_tenant.py`](smoke_test_tenant.py) | Fresh-tenant install regression. Materializes synthetic tenant config in tempdir, exercises public-code surfaces, asserts no maintainer-tenant strings leak. Enforces PD1. |

### Sync + writeback (Drive ↔ local ↔ GAS)

| Script | Purpose |
|---|---|
| [`sync_registry.py`](sync_registry.py) | Regenerates downstream registries from `drive-docs.yaml` (canonical): `tc_config.gs/getDeals()`, Drive Organizer `F`/`DEAL_FOLDERS`/`OTTER_DEAL_MAP`, `local_file_router.py/DEALS`, `deal-system-data.json`. |
| [`sync_learnings.py`](sync_learnings.py) | Regenerates downstream views from `LEARNINGS-LEDGER.yaml` (canonical): `~/.claude/CLAUDE.md` universal-rules section, Drive Practice Patterns + Yoni Personal Context gdocs, LEARNINGS-INDEX.md. |
| [`sync_deals_from_drive.py`](sync_deals_from_drive.py) | Pulls per-deal `dashboard_entry.json` from Drive, writes `_drive_overlay.json` for `compile-dashboard.py` to merge. |
| [`compile_drive_writeback.py`](compile_drive_writeback.py) | Maps deal_id → Drive status file ID for writeback. Auto-updated by `tcip_new_deal.py` Phase 4. |
| [`fetch_project_instructions.py`](fetch_project_instructions.py) | Fetches each deal's `project_instructions` gdoc, strips non-ASCII, writes to `/tmp/`. Companion to `refresh-project-instructions` skill. |

### Deal lifecycle (create / extract / synthesize)

| Script | Purpose |
|---|---|
| [`tcip_new_deal.py`](tcip_new_deal.py) | THE new-deal entry point. Scaffolds Drive folders, creates status/brief/log/session_log/dashboard_entry, updates registry. Invoked by `/new-deal` skill. **DO NOT MODIFY — Chat C owns.** |
| [`setup_deal_outputs.py`](setup_deal_outputs.py) | Retroactive `_Outputs/` setup for deals missing outputs_folder_id. **DO NOT MODIFY — Chat C owns.** |
| [`deal_extract_helpers.py`](deal_extract_helpers.py) | File I/O primitives invoked by `/deal-sync`. Wraps Deal Sync Writer (setContent on registered fileId — enforces EP1/I11). |
| [`intel_capture.py`](intel_capture.py) | Scans Claude Code transcripts (and later claude.ai chats) for `---DEAL-INTEL---` blocks, routes to per-deal `log.json`. Stop-hook job. |
| [`deal_intel_indexer.py`](deal_intel_indexer.py) | Reads captured log.json entries, builds local index for search/cross-ref. |
| [`log_compaction.py`](log_compaction.py) | Archives folded log.json entries older than N days → `log.archive.json`. Enforces I12 (80KB cap). |

### Knowledge base + intelligence

| Script | Purpose |
|---|---|
| [`knowledge_indexer.py`](knowledge_indexer.py) | Indexes ~41 source gdocs from NotebookLM folder → local ChromaDB. |
| [`knowledge_query.py`](knowledge_query.py) | Retrieval against ChromaDB index. Powers `/knowledge-query` skill. |
| [`cross_reference_briefing.py`](cross_reference_briefing.py) | Reads daily briefing sources JSON, embeds articles locally, cross-references with knowledge base. |

### Drive cleanup + routing

| Script | Purpose |
|---|---|
| [`local_file_router.py`](local_file_router.py) | Watches `~/Downloads` every 30s. Routes .jsx/.tsx/.html artifacts to deal `_Outputs/`. LaunchAgent: `com.tcip.local_file_router`. **DO NOT MODIFY — Chat B owns.** |
| [`cleanup_my_drive_root.py`](cleanup_my_drive_root.py) | Manifest-driven cleanup of loose files at My Drive root. **DO NOT MODIFY — Chat D owns.** |
| [`drive_cleanup.py`](drive_cleanup.py) | One-shot cleanup: moves loose files from Downloads staging, deal roots, TC_DEALS root to correct `_Outputs/` / Archive. |
| [`migrate_to_gdocs.py`](migrate_to_gdocs.py) | Collapses dual-ID state (status/brief have both .md ID and gdoc ID) into a single native gdoc per concept. **DO NOT MODIFY — Chat D owns.** |
| [`screenshot_archiver.sh`](screenshot_archiver.sh) | Daily Desktop screenshot triage. Moves `~/Desktop/Screenshot *.png` → `~/Desktop/Screenshots/YYYY-MM/`. Trashes >30d. **DO NOT MODIFY — Chat C owns.** |

### Content processing

| Script | Purpose |
|---|---|
| [`strip_footer_boilerplate.py`](strip_footer_boilerplate.py) | **v1** — older email-footer stripper. Schedule for retirement once v2 has 30-day clean record. |
| [`strip_footer_v2.py`](strip_footer_v2.py) | **v2 (canonical)** — current footer stripper. Uses paragraph-split heuristic for large single-paragraph email blocks. |
| [`generate-system-map.py`](generate-system-map.py) | Walks config + code surfaces, produces `~/dashboards/docs/SYSTEM-MAP.md`. |

### Deal modeling + decks

| Script | Purpose |
|---|---|
| [`create_deal_template.py`](create_deal_template.py) | One-shot. Generates the 5-tab `TCIP_Deal_Model_Template.xlsx` that every new deal starts from. |
| [`deck_base.py`](deck_base.py) | Library imported by deal-specific `build_deck_*.py`. Universal deck engine for TCIP deal modeling. |

### Onboarding

| Script | Purpose |
|---|---|
| [`tcip_onboard.sh`](tcip_onboard.sh) | Interactive deal-onboarding launcher. Checks prereqs, then drops into `tcip_new_deal.py`. User-facing wrapper. |

---

## §2 — Overlap groups (areas where past tools collided)

These four groups have multiple scripts because of historical accretion.
Boundaries are now documented; **before adding a new script in any of these
domains, audit the existing ones first**.

### 2.1 Footer strippers

- [`strip_footer_boilerplate.py`](strip_footer_boilerplate.py) — v1
- [`strip_footer_v2.py`](strip_footer_v2.py) — **v2 (canonical)**

**Boundary:** v1 used Docs API paragraph identification; v2 uses
single-paragraph split heuristic for large blocks. v2 is the keeper. v1
stays alive only until a 30-day clean record on v2.

**Don't add v3** — extend v2.

### 2.2 Audits + health checks

- [`system_health.py`](system_health.py) — **aggregator (canonical entry)**
- [`audit_config_drift.py`](audit_config_drift.py) — config-path topology only
- [`reference_integrity_audit.py`](reference_integrity_audit.py) — Drive reference IDs
- [`smoke_test_tenant.py`](smoke_test_tenant.py) — fresh-tenant install

**Boundary:** `system_health.py` is the umbrella; new audits should land
as `checks/check_<name>.py` and be picked up automatically. Stand-alone
audit scripts (the other three) exist because they're sub-system-specific:
config drift is meta (it audits the audit-config layout itself), reference
integrity needs Drive API not present in checks/, smoke-test bootstraps a
synthetic tenant env which checks/ can't.

**Don't add new top-level `audit_*.py` or `check_*.py` at this directory level**
— add to `checks/`.

### 2.3 Sync / writeback

- [`sync_registry.py`](sync_registry.py) — drive-docs.yaml → GAS + python registries (one-way down)
- [`sync_learnings.py`](sync_learnings.py) — LEARNINGS-LEDGER.yaml → CLAUDE.md + gdocs (one-way down)
- [`sync_deals_from_drive.py`](sync_deals_from_drive.py) — Drive `dashboard_entry.json` → local overlay (one-way up)
- [`compile_drive_writeback.py`](compile_drive_writeback.py) — deal_id → status file ID map (registry data only)
- [`fetch_project_instructions.py`](fetch_project_instructions.py) — Drive project_instructions → /tmp (one-way up, instruction-side only)

**Boundary:** the `sync_*` prefix means "regenerate downstream from canonical
source" (one-way push). `fetch_*` means "pull current state for use elsewhere"
(one-way read). `compile_drive_writeback.py` is data, not behavior, despite
the .py extension — it carries the deal_id → file_id map consumed by
`/deal-sync`.

**Don't add bidirectional sync scripts** — direction must be clear from the name.

### 2.4 Drive cleanup

- [`local_file_router.py`](local_file_router.py) — **continuous (LaunchAgent)** Downloads → Drive
- [`drive_cleanup.py`](drive_cleanup.py) — **one-shot** legacy cleanup
- [`cleanup_my_drive_root.py`](cleanup_my_drive_root.py) — **one-shot** My Drive root tidy
- [`migrate_to_gdocs.py`](migrate_to_gdocs.py) — **one-shot** dual-ID collapse

**Boundary:** only `local_file_router.py` is continuous; the other three
are one-shot migrations. The three one-shots will be retired once the
backlog they target is empty and `local_file_router.py` + Drive Organizer
+ Drive Invariant Checker hold the line going forward.

**Don't add new one-shot cleanup scripts** without first checking whether
the underlying invariant violation can be prevented at the source.

---

## §3 — Tenant data + state files (not scripts, but live here)

| File | Purpose |
|---|---|
| `deal-system-data.json` | Derived view (regenerated by `sync_registry.py` from drive-docs.yaml). Consumed by Claude Code skills. **NEVER hand-edit.** |
| `deal-system-data.template.json` | Template marker (placeholder for tenant subscribers). |
| `sync-state.json` | Per-tool sync state (last_synced timestamps etc.). |
| `sync-state.template.json` | Template marker. |
| `credentials.json` | Symlink → `~/credentials/gdrive_credentials.json`. |
| `token.json` | OAuth token cache. Refreshed by gdrive helpers. |
| `data/` | Per-tool runtime data subdirs. |
| `checks/` | Auto-discovered health-check modules. Add new `check_<name>.py` here. |
| `__pycache__/` | Python bytecode cache. |

---

## §4 — Conventions

- **Public-first (PD1/L0003):** no tenant slugs, no hardcoded names, no
  maintainer Drive folder IDs. Pull tenant context from
  `~/cos-pipeline-config-<tenant>/`.
- **Edit-in-place (EP1/L0005, I11):** never recreate a Drive doc whose ID
  is in drive-docs.yaml. Use Deal Sync Writer (`setContent`).
- **Coordination locks:** shared-state writes must use
  `from coordination import lock` ctx-manager.
- **`from __future__ import annotations`:** required at the top of any tool
  scheduled as a LaunchAgent (system Python is 3.9; type-hint syntax fails
  without it).
- **Claude Code over API (CC1/L0001):** any Claude call must go through
  `_claude_dispatch.call()`, not raw `anthropic` SDK.

---

*Generated 2026-05-20 by Block E inefficiency cleanup. Re-run scope: walk
the directory, regenerate this file when adding/retiring scripts.*
