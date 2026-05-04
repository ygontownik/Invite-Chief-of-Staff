# ARCHITECTURE.md — Generic architecture map for the COS dashboard

This file describes the structural pattern of a COS dashboard install.
Specific routine names, config doc IDs, and external services depend on
your tenant configuration. Concrete examples in this doc are illustrative.

## Top-level flow

```
  External sources              Ingest/Process routines      Compile routines       Serve
  ─────────────────             ────────────────────────     ─────────────────      ─────
  Research feeds (PDF) ─────┐                                                       /
  Authenticated SPAs ───────┤                                                       /deals
  Newsletter / blog feeds ──┼──▶  routines/ingest/*  ─┐                             /portfolio
  Podcasts (RSS) ───────────┘                         ▼                             /briefing
  Transcript sources ─────────▶  routines/process/*  ─┼─▶ data/compiled/*.json ─▶ app/
  Email (small slice) ───────▶   routines/process/*  ─┤    (dashboard-data.json,
  Call recordings ───────────▶   routines/process/*  ─┘     deal-system-data.json,
                                                            deal-pipeline-data.json,
  Drive docs (CoS) ◀───────── routines/process/*            cos-run-state.json)
  Drive docs (intel) ◀─────── routines/brief/*
```

## Folder inventory

| Folder | Purpose | Source of truth? |
|---|---|---|
| `app/` | `cos-dashboard-server.py`, `*-refresh.py`, `*-fetch.py`, `templates/*.html` | Yes — the live server |
| `routines/ingest/` | Research / newsletter / podcast ingestion scripts | No (reads external) |
| `routines/process/` | Capture pipeline (Drive prefetch, batch write, transcript backfill) | No (transforms) |
| `routines/brief/` | Briefing aggregators | No |
| `routines/compile/` | Deal-system compile, dashboard compile, briefing | No (aggregates) |
| `routines/send/` | Outbound email senders | No |
| `data/deals/<TICKER>/` | `deal.md`, profit-model.xlsx, notes/ | **YES** — deal source of truth |
| `data/compiled/` | `dashboard-data.json`, `deal-system-data.json`, `deal-pipeline-data.json`, `cos-run-state.json`, `*.md` | No — regenerable |
| `config/schedule.yaml` | Master schedule | **YES** — schedule source |
| `config/drive-docs.yaml` | Drive doc IDs, folder IDs, local state paths | **YES** — Drive registry |
| `config/launchd-plists/` | Canonical LaunchAgent plists + generate.sh | **YES** — plist source |
| `docs/` | `CLAUDE.md`, `ARCHITECTURE.md`, `RUNBOOK.md`, templates, CHANGELOG.md | Yes |
| `scripts/` | `verify-system.sh` + one-shots | No |
| `archive/` | Retired code, backups, snapshots | No (never imported) |
| `logs/` | Routine stdout/stderr | No |

## Server architecture

`app/cos-dashboard-server.py` is a single Python `ThreadingHTTPServer`
with Basic Auth middleware. Route table is data-driven from
`config/dashboard-tiles.yaml`:

| Route pattern | Tier | Source |
|---|---|---|
| `/` | owner | `templates/cos-dashboard.html` + `data/compiled/dashboard-data.json` |
| `/deals/` | per `dashboard-tiles.yaml :: tiles[id=deals].allowed` | `templates/deal-dashboard.html` + `data/compiled/deal-system-data.json` |
| Other tile routes | per `dashboard-tiles.yaml :: tiles[].allowed` | as configured per tile |
| `POST /warmup` | localhost only | refreshes the cache |
| `POST /refresh` | localhost only | HTML inject from cache (~2ms) |
| `GET /cache-status` | owner | cache metadata |

LaunchAgent label is tenant-specific (e.g. `com.cospipeline.<slug>.dashboard`).
`keep_alive: true`, env vars `OWNER_PASSWORD` / `PARTNER_PASSWORD` injected
by the plist.

## Shared design system

All dashboards render through a single shared visual language. Details live
in `docs/DESIGN-SYSTEM.md`; the short version:

| Concern | Location |
|---|---|
| CSS tokens + component classes | `app/static/design-system.css` |
| Shared top nav partial | `app/templates/_topnav.html` |
| Injector (adds CSS link + topnav to every served HTML page) | `_inject_shared_chrome()` in `cos-dashboard-server.py` |
| Static asset serving | `/static/*` route in `do_GET` |

Every text/html route flows through the injector. The partner tier is
allowed `/static/*` unconditionally so CSS loads even when partner access
is restricted to a single tile's route.

A page can opt out of the shared chrome by placing
`<!-- NO_TC_CHROME -->` in its `<head>`.

## Routines

The active routine set for any tenant is defined in `config/schedule.yaml`
(generated from `config/schedule.template.yaml` at install time). See the
template for the universal-core routines that every subscriber gets and
the optional blocks that can be enabled.

Universal core: transcript backfill, inbox capture, personal briefing,
Gmail mini-triage, podcast transcription, deal compile, weekly deal-pipeline
scan.

Optional / tenant-provided: research-PDF ingestion, authenticated-site
scrapes, newsletter sync, intelligence-digest aggregators, weekly summary
emails. The backing scripts for these are NOT shipped in the public repo —
each tenant provides their own.

## Always-on daemons (launchd)

Labels are namespaced by tenant slug: `com.cospipeline.<slug>.<role>`. Common
roles:

| Role | Purpose |
|---|---|
| `dashboard` | The dashboard HTTP server |
| `call-scheduler` | Call recorder scheduler (optional) |
| `cloudflared` | Cloudflare tunnel for call webhook (optional) |
| `recorder-menu` | Menu-bar call recorder UI (optional) |
| `calendar-renew` | Re-registers push subscriptions every 2 days (optional) |

## Config map — where every piece of manually-curated data lives

This is the authoritative map. When updating dashboard content, find the
right file here rather than searching the template.

### Config files (edit these directly)

| File | What's inside | Injected as |
|---|---|---|
| `config/recruit-config.yaml` | Recruiting pipeline buckets + recruiter firms | `window.__RECRUIT_CONFIG__` |
| `config/deal-config.yaml` | Deal activity: `liveDeals`, `dealOrigination`, `capitalRaisingAdvisors`, `prospectiveInvestors`, `investors[]` | `window.__DEAL_CONFIG__` |
| `config/strings.yaml` | All UI strings: button labels, tooltips, topnav text | `{{STR:dot.path}}` placeholder substitution |
| `config/drive-docs.yaml` | Google Drive doc IDs and folder IDs used by pipeline scripts | Read directly by pipeline scripts |
| `config/schedule.yaml` | Cron schedule for all active routines | Read by scheduler |
| `config/dashboard-tiles.yaml` | Tile registry for the unified landing page | Read by server |

The server also injects firm identity (principal name, team, firm name,
tile-label overrides) as `window.__FIRM_CONTEXT__` from `firm_context.yaml`.

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
| `data/compiled/dashboard-data.json` | Follow-ups, awaitingExternal, upcoming calls, pipeline status | CoS view |
| `data/compiled/deal-system-data.json` | All deals with health scores, actions, profit models | Deals view |
| `data/compiled/deal-pipeline-data.json` | Weekly pipeline targets + IC memos | Portfolio view |
| `data/compiled/cos-run-state.json` | Capture chain state: lastFullRunAt, lastMiniRunAt, lastFetchAt | Freshness badge |

### How config YAML reaches the browser

```
config/*.yaml
    ↓
_load_recruit_config() / _load_deal_config() / _load_firm_context_public()   ← server reads at request time
    ↓
_deletions_script()                                                           ← injected into <head> as inline <script>
    ↓
window.__RECRUIT_CONFIG__ / window.__DEAL_CONFIG__ / window.__FIRM_CONTEXT__
    ↓
const RECRUIT_CONFIG = window.__RECRUIT_CONFIG__ || fallback                 ← template one-liner
```

Server fails gracefully: if a YAML file is missing or unparseable, the
loader returns an empty-structure fallback and logs the error. The
dashboard renders with empty sections rather than crashing.

## Key data contracts

- **`data/compiled/dashboard-data.json`** — CoS view payload. Producer:
  `cos-dashboard-fetch.py`. Consumer: `/` + `/briefing/`.
- **`data/compiled/deal-system-data.json`** — all deals rollup with
  health scores, actions, profit models. Producer: `deal-system-compile.py`.
  Consumer: `/deals/`.
- **`data/compiled/deal-pipeline-data.json`** — weekly pipeline targets
  + IC memos. Producer: weekly pipeline scan. Consumer: `/portfolio/`.
- **`data/compiled/cos-run-state.json`** — transient state for the
  capture chain (what was processed when).

## External systems referenced

- Google Drive / Docs / Gmail / Calendar (OAuth: `~/credentials/token.json`)
- Microsoft Graph (Outlook) (`~/credentials/ms_token.json`)
- Anthropic API (env: `ANTHROPIC_API_KEY`)
- AssemblyAI (env: `ASSEMBLYAI_API_KEY`)
- Twilio (call webhook via Cloudflare tunnel) — optional
- Authenticated SPAs via Chrome MCP — optional

## Not in the dashboard tree (out of scope)

- Call recorder stack (separate repo / install)
- Raw call audio
- `~/credentials/` — secrets (intentionally outside the tree)
- `~/.claude/scheduled-tasks/` — SKILL.md definitions (stays, but
  `calls:` paths point into the dashboard tree)
