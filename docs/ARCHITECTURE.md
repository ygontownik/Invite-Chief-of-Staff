# ARCHITECTURE.md — Current-state map of ~/dashboards/

Last updated: 2026-05-01.

## Top-level flow

```
  External sources           Ingest/Process routines        Compile routines         Serve
  ─────────────────          ────────────────────────       ─────────────────        ─────
  Jefferies PDFs ─────┐                                                              /cos
  GS PDFs ────────────┤                                                              /deals
  RBN blog ───────────┼─▶  routines/ingest/*  ──┐                                    /tomac
  Substacks ──────────┤                         │                                    /briefing
  Podcasts (RSS) ─────┘                         ▼                                    ▲
  Otter transcripts ─────▶  routines/process/* ─┼─▶ data/compiled/*.json ─▶ app/ ────┘
  Gmail (small slice) ──▶   routines/process/* ─┤     (dashboard-data.json,
  Call recordings ──────▶   routines/process/* ─┘      deal-system-data.json,
                                                       deal-pipeline-data.json,
  Drive docs (CoS) ◀─────── routines/process/*          cos-run-state.json)
  Drive docs (intel) ◀───── routines/brief/*
```

## Folder inventory

| Folder | Purpose | Source of truth? |
|---|---|---|
| `app/` | `cos-dashboard-server.py`, `*-refresh.py`, `*-fetch.py`, `templates/*.html` | Yes — the live server |
| `routines/ingest/` | jefferies_downloader, jefferies_processor, gs_processor, podcast_transcribe | No (reads external) |
| `routines/process/` | cos_prefetch_drive, cos_batch_write (capture), otter transcripts | No (transforms) |
| `routines/brief/` | notebooklm_doc_writer | No |
| `routines/compile/` | deal-system-compile, compile-dashboard, cos-briefing | No (aggregates) |
| `routines/send/` | sunday_weekly_email (planned) | No |
| `data/deals/<TICKER>/` | deal.md, profit-model.xlsx, notes/ | **YES** — deal source of truth |
| `data/compiled/` | dashboard-data.json, deal-system-data.json, deal-pipeline-data.json, cos-run-state.json, *.md | No — regenerable |
| `config/schedule.yaml` | Master schedule | **YES** — schedule source |
| `config/drive-docs.yaml` | Drive doc IDs, folder IDs, local state paths | **YES** — Drive registry |
| `config/launchd-plists/` | Canonical LaunchAgent plists + generate.sh | **YES** — plist source |
| `docs/` | CLAUDE.md, ARCHITECTURE.md, RUNBOOK.md, templates, CHANGELOG.md | Yes |
| `scripts/` | verify-system.sh + one-shots | No |
| `archive/` | Retired code, backups, snapshots | No (never imported) |
| `logs/` | Routine stdout/stderr | No |

## Server architecture

`app/cos-dashboard-server.py` is a single Python `ThreadingHTTPServer`
with Basic Auth middleware and four views:

| Route | Tier | Source |
|---|---|---|
| `/` | owner | `templates/cos-dashboard.html` + `data/compiled/dashboard-data.json` |
| `/deals/` | owner + partner | `templates/deal-dashboard.html` + `data/compiled/deal-system-data.json` |
| `/tomac/` | owner + partner | (planned Step 3) firm/pipeline view |
| `/briefing/` | owner only | (planned Step 4) mobile-friendly briefing |
| `POST /warmup` | localhost only | refreshes the cache |
| `POST /refresh` | localhost only | HTML inject from cache (~2ms) |
| `GET /cache-status` | owner | cache metadata |

LaunchAgent: `com.yoni.cosdashboard` — `keep_alive: true`, env vars
`OWNER_PASSWORD` / `PARTNER_PASSWORD` injected by plist.

## Shared design system (deployed 2026-04-17)

All dashboards render through a single shared visual language. Details live
in `docs/DESIGN-SYSTEM.md`; the short version:

| Concern | Location |
|---|---|
| CSS tokens + component classes | `app/static/design-system.css` |
| Shared top nav partial | `app/templates/_topnav.html` |
| Injector (adds CSS link + topnav to every served HTML page) | `_inject_shared_chrome()` in `cos-dashboard-server.py` |
| Static asset serving (cascades `app/static/` → `tomac-cove-build/static/`) | `/static/*` route in `do_GET` |

Every text/html route — `/`, `/deals/`, `/tomac/`, `/briefing/`,
`/tomac-cove/`, `/all`, `/admin` — flows through the injector. Partner tier
is allowed `/static/*` unconditionally so CSS loads even when partner access
is restricted to `/deals/`.

A page can opt out of the shared chrome by placing
`<!-- NO_TC_CHROME -->` in its `<head>`.

## The 17 active routines

Grouped by cron order (see `config/schedule.yaml` for authoritative cron).

### Overnight (daily)
1. **jefferies-pdf-downloader** — 02:10 — Chrome MCP → `folders.jefferies_pdfs`
2. **jefferies-pdf-processor** — 03:08 — PDFs → `folders.sector_docs`
3. **gs-research-daily-download** — 03:38 — Chrome MCP → `folders.gs_pdfs`
4. **gs-research-pdf-processor** — 04:03 — PDFs → `docs.gs_energy`, `docs.gs_macro_market`
5. **podcast-transcribe-daily** — 05:00 — RSS → `folders.podcasts`
6. **tomac-deal-compile** — 07:09 — `data/deals/*` → `data/compiled/deal-system-data.json`

### Morning chain (M–F)
7. **rbn-daily-sync** — 06:24 — Chrome MCP → `docs.rbn_archive`
8. **run-syncall-gas** — 06:39 — Chrome MCP → Substack library
9. **cos-otter-transcripts** — 07:07 — Otter → `docs.followups`, `docs.recruiting`, `docs.tomac_pipeline`
10. **cos-capture-pipeline** — 07:22 — Gmail/Drive → same
11. **notebooklm-daily-briefing** — 07:36 (→ 07:15 proposed) — NotebookLM → `docs.daily_market_update`
12. **cos-personal-briefing** — 07:51 — → `docs.briefing_log` + dashboard refresh

### Business hours (M–F)
13. **cos-gmail-mini** — 08/10/12/14/16/18 — Gmail → `docs.followups`

### Weekly
14. **notebooklm-sunday-weekly-briefing** — Sun 18:07
15. **tomac-cove-weekly-pipeline** — Sun 19:30 — 3-pass Sonnet/Opus/Sonnet → `docs.tomac_pipeline`, `docs.energy_pipeline_gemini`
16. **sunday-weekly-email** — Sun 20:00 (planned Step 9)

### On-demand
17. **master-daily-update** — fallback orchestrator

## Always-on daemons (launchd)

| Label | Purpose |
|---|---|
| `com.yoni.cosdashboard` | The dashboard HTTP server |
| `com.tomaccove.scheduler` | Call recorder scheduler |
| `com.tomaccove.cloudflared` | Cloudflare tunnel for call webhook |
| `com.tomaccove.ngrok` | ngrok (legacy) |
| `com.tomaccove.recorder.menu` | Menu-bar call recorder UI |
| `com.tomaccove.calendar.renew` | Re-registers push subscriptions every 2 days |

## Config map — where every piece of manually-curated data lives

This is the authoritative map. When updating dashboard content, find the
right file here rather than searching the template.

### Config files (edit these directly)

| File | What's inside | Injected as |
|---|---|---|
| `config/recruit-config.yaml` | Recruiting pipeline: `inDiscussion`, `waitingToHear`, `doIChase` buckets + recruiter firms | `window.__RECRUIT_CONFIG__` |
| `config/tomac-config.yaml` | TC deal activity: `liveDeals`, `dealOrigination`, `capitalRaisingAdvisors`, `prospectiveInvestors` | `window.__TOMAC_CONFIG__` |
| `config/strings.yaml` | All UI strings: button labels, tooltips, topnav text | `{{STR:dot.path}}` placeholder substitution |
| `config/drive-docs.yaml` | Google Drive doc IDs and folder IDs used by pipeline scripts | Read directly by pipeline scripts |
| `config/schedule.yaml` | Cron schedule for all 17 routines | Read by scheduler |
| `config/dashboard-tiles.yaml` | Tile layout config for the deals dashboard | Read by server |

### User-state files (never edit directly — written by server endpoints)

These live in `data/user-state/` and are gitignored. Server endpoints write
them; client JS reads them via `window.__*` injection at page load.

| File | What's inside | Window var | Written by |
|---|---|---|---|
| `data/user-state/deletions.json` | Tombstoned item IDs (dismissed rows) | `window.__DELETIONS__` | `POST /item/delete` |
| `data/user-state/personal-items.json` | Personal tab manual items | `window.__PERSONAL_ITEMS_INITIAL__` | `POST /personal/save` |
| `data/user-state/build-backlog.json` | Claude Code task backlog items | `window.__BUILD_BACKLOG_INITIAL__` | `POST /_build_backlog_append_` |
| `data/user-state/order.json` | User-defined card sort order | `window.__ORDER_INITIAL__` | `POST /order/save` |
| `data/user-state/topics.json` | Focus topics text | `window.__TOPICS_INITIAL__` | `POST /topics/save` |
| `data/user-state/fundraising.json` | LP/fundraising bucket overrides | merged into `DATA.fundraising` | `POST /fundraising/save` |

### Pipeline-compiled data (never edit directly — regenerated by routines)

| File | What's inside | Consumer |
|---|---|---|
| `data/compiled/dashboard-data.json` | Follow-ups, awaitingExternal, upcoming calls, pipeline status | `/` CoS view |
| `data/compiled/deal-system-data.json` | All deals with health scores, actions, profit models | `/deals/` |
| `data/compiled/deal-pipeline-data.json` | Weekly pipeline targets + IC memos | `/tomac-cove/` |
| `data/compiled/cos-run-state.json` | Capture chain state: lastFullRunAt, lastMiniRunAt, lastFetchAt | Freshness badge |

### How config YAML reaches the browser

```
config/*.yaml
    ↓
_load_recruit_config() / _load_tomac_config()   ← server reads at request time
    ↓
_deletions_script()                              ← injected into <head> as inline <script>
    ↓
window.__RECRUIT_CONFIG__ / window.__TOMAC_CONFIG__
    ↓
const RECRUIT_CONFIG = window.__RECRUIT_CONFIG__ || fallback   ← template one-liner
```

Server fails gracefully: if a YAML file is missing or unparseable, the
loader returns an empty-structure fallback and logs the error. The
dashboard renders with empty sections rather than crashing.

## Key data contracts

- **`data/compiled/dashboard-data.json`** — CoS view payload.
  Producer: `cos-dashboard-fetch.py`. Consumer: `/` + `/briefing/`.
- **`data/compiled/deal-system-data.json`** — all deals rollup with
  health scores, actions, profit models. Producer:
  `compile-dashboard.py`. Consumer: `/deals/` + `cos-briefing.py`.
- **`data/compiled/deal-pipeline-data.json`** — weekly pipeline targets
  + IC memos. Producer: `tomac-cove-weekly-pipeline`. Consumer: `/tomac/`.
- **`data/compiled/cos-run-state.json`** — transient state for the
  capture chain (what was processed when).

## Dependencies between routines

```
  run-syncall-gas ──▶ notebooklm-daily-briefing ──┐
  cos-otter-transcripts ──▶ cos-capture-pipeline ─┼──▶ cos-personal-briefing
                                                   ┘
  tomac-deal-compile ─────▶ (reads from data/deals/) ──▶ /deals view
  tomac-cove-weekly-pipeline ──▶ sunday-weekly-email
```

## External systems referenced

- Google Drive / Docs / Gmail / Calendar (OAuth: `~/credentials/token.json`)
- Microsoft Graph (Outlook) (`~/credentials/ms_token.json`)
- Anthropic API (env: `ANTHROPIC_API_KEY`)
- AssemblyAI (env: `ASSEMBLYAI_API_KEY`)
- Twilio (call webhook via Cloudflare tunnel)
- NotebookLM (web via Chrome MCP)

## Not in ~/dashboards/ (out of scope)

- `~/tomac-cove-pipeline/` — call recorder stack (stays)
- `~/recordings/` — raw call audio (stays)
- `~/credentials/` — secrets (stays)
- `~/.claude/scheduled-tasks/` — SKILL.md definitions (stays, but
  `calls:` paths point into `~/dashboards/`)
