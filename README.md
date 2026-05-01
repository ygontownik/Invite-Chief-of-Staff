# COS Pipeline — Chief of Staff AI System

> **Repo:** https://github.com/ygontownik/Dashboard
> **Per-package docs:** [Package A — Market Intelligence](docs/PACKAGE_A.md) · [Package B — Operations](docs/PACKAGE_B.md)

A local AI pipeline that processes emails, call transcripts, and market research into structured action items, deal intelligence, and daily briefings — all routed to Google Docs and a local dashboard.

Built as two packages that can be deployed independently:

| Package | What it does |
|---------|-------------|
| **Package B — Operations** | Triages Gmail, processes Otter AI + call transcripts, extracts action items and deal intel, writes to Follow-ups / Pipeline / Recruiting / People docs |
| **Package A — Market Intelligence** | Transcribes podcasts, processes Jefferies/GS/RBN research PDFs, generates IC memos and deal ideas via a 3-pass pipeline |

The dashboard (always full) shows all tiles. Tabs without an active package display an empty state.

---

## Quick Start — New Firm Setup

### 1. Copy and fill in the identity configs

```bash
cp firm_context.template.yaml firm_context.yaml
cp firm_config.template.json firm_config.json
cp config/drive-docs.template.yaml ~/dashboards/config/drive-docs.yaml
```

Edit `firm_context.yaml` — your name, firm, team, investment focus, peer firms.
Edit `firm_config.json` — your Google Doc IDs, email keywords, active packages.
Edit `drive-docs.yaml` — your specific Google Doc and folder IDs.

All three files are gitignored and never committed.

### 2. Install dependencies

```bash
pip install pyyaml google-auth google-auth-oauthlib google-auth-httplib2 anthropic
```

### 3. Set required secrets

```bash
# For interactive/manual runs — set environment variables:
export ANTHROPIC_API_KEY="sk-ant-..."
export DASHBOARD_USERNAME="admin"
export DASHBOARD_PASSWORD="your-password"

# For scheduled LaunchAgents (macOS) — store in Keychain:
security add-generic-password -s "dashboards/ANTHROPIC_API_KEY"    -a "$USER" -w "sk-ant-..."
security add-generic-password -s "dashboards/DASHBOARD_USERNAME"   -a "$USER" -w "admin"
security add-generic-password -s "dashboards/DASHBOARD_PASSWORD"   -a "$USER" -w "your-password"
```

### 4. Run the setup validator

```bash
python3 setup.py
```

Checks all required config fields are filled, credential files exist, and the Anthropic API key responds.

### 5. Authorize Google APIs (first run)

The first run of each script triggers a browser OAuth flow. Tokens are saved to `~/credentials/` and reused.

```bash
python3 cos_gmail_mini_v2.py --list --backfill 2h   # Gmail OAuth
python3 cos_otter_backfill.py --list                  # Drive/Docs OAuth
```

### 6. Install scheduled LaunchAgents

The pipelines need to run on a schedule. Install the 3 essential LaunchAgents (dashboard server, daily capture pipeline, every-2h email triage):

```bash
./setup_launchagents.sh
```

This generates and loads:
- `com.cos-pipeline.dashboard-server` — always-on HTTP server (port 7777)
- `com.cos-pipeline.capture-pipeline` — daily 7:22am Mon–Fri
- `com.cos-pipeline.gmail-mini` — every 2h on the :05 minute mark, Mon–Fri 8am–8pm

Verify they're loaded:
```bash
launchctl list | grep cos-pipeline
```

To uninstall them (e.g., when migrating machines):
```bash
./setup_launchagents.sh --uninstall
```

### 7. Open the dashboard

```bash
open http://localhost:7777
```

You should see all tiles. Day-1 tiles will be empty until pipelines run; populated by the next 7:22am capture run or whenever you trigger one manually:

```bash
python3 cos_capture_pipeline.py --since 24h
```

---

## What Gets Captured — Where to Save Things

**→ [docs/WHAT_GETS_CAPTURED.md](docs/WHAT_GETS_CAPTURED.md)**

The short version:

| Content | What to do |
|---------|-----------|
| Call transcript (Otter, Beside, Fireflies, etc.) | Configure the service's Drive sync to point at a folder ID in your `transcript_sources` config |
| Manual transcript | Paste text into a `.txt` file → save to any configured Drive folder |
| Desktop recording | `call_recorder.py` saves automatically; add a `local_folder` source pointing at `~/recordings` |
| Podcast / blog | Add RSS feed to `personal.content_feeds` in `firm_context.yaml` |
| Email | Nothing — your inbox is scanned automatically every 2 hours |
| Research PDF | Not yet supported |

The pipeline **only** reads the specific folder IDs listed in `transcript_sources`. No other Drive content is touched.

---

## File Map — What Goes Where

### Firm-specific (fill these in, never committed to git)

| File | Purpose | Location |
|------|---------|----------|
| `firm_context.yaml` | Principal identity, team, investment focus, peer firms | `~/tomac-cove-pipeline/` |
| `firm_config.json` | Email keywords, Google Doc IDs, active packages | `~/tomac-cove-pipeline/` |
| `drive-docs.yaml` | Registry of all Drive doc/folder IDs | `~/dashboards/config/` |
| `~/credentials/*.pickle` | Google OAuth tokens | `~/credentials/` |
| `~/credentials/*.json` | OAuth client credentials | `~/credentials/` |

### Templates (committed — copy these to produce the above)

| Template | Produces |
|----------|----------|
| `firm_context.template.yaml` | `firm_context.yaml` |
| `firm_config.template.json` | `firm_config.json` |
| `config/drive-docs.template.yaml` | `~/dashboards/config/drive-docs.yaml` |

### Package B — Operations (committed)

| Script | What it does | Schedule |
|--------|-------------|---------|
| `cos_gmail_mini_v2.py` | Gmail triage — Haiku classifies all, Sonnet enriches DEAL/RECRUIT | Every 2h, Mon–Fri |
| `cos_otter_backfill.py` | Otter AI + call transcript processor — Sonnet memo, Opus deal extraction | Daily |
| `cos_transcript_hook.py` | Real-time hook triggered after each new recording | Post-call |

### Package A — Market Intelligence (committed)

| Script | What it does | Schedule |
|--------|-------------|---------|
| `podcast_transcribe.py` | Transcribes + summarizes podcast episodes via AssemblyAI + Claude | Daily |
| `deal-dashboard-refresh.py` | 3-pass pipeline: scan → analyze → IC memo | Weekly |

### Core shared modules (committed)

| Module | Purpose |
|--------|---------|
| `_firm_context.py` | Loads `firm_context.yaml`; builds all model preambles; `load_drive_docs()` |
| `_entity_normalizer.py` | Canonical entity resolution (firms, people, deal names) |
| `_envelope_writer.py` | Writes structured action/intel envelopes to dashboard data |
| `_usage.py` | Logs Anthropic API usage to `~/dashboards/data/anthropic-usage.jsonl` |

### Dashboard (committed)

| Script/File | Purpose |
|-------------|---------|
| `cos-dashboard-server.py` | Local HTTP server on :7777; reads `dashboard-tiles.yaml` + `firm_config.json` |
| `cos-dashboard-fetch.py` | Fetches Google Docs → `dashboard-data.json` cache |
| `cos-dashboard-refresh.py` | Fast HTML inject from cache (~2ms) |
| `config/dashboard-tiles.yaml` | Tile registry — titles, URLs, auth tiers, `requires_package` |
| `config/routing-rules.md` | LLM-agnostic envelope routing contract |

---

## Architecture — How the Prompts Work

Every script that sends transcripts to Claude builds its model preamble at import time from `firm_context.yaml` via `_firm_context.py`. No name, firm, team member, or investment focus is hardcoded in any script.

```
firm_context.yaml
      ↓
_firm_context.py → build_memo_header()       → MEMO_PREAMBLE
                 → build_backfill_header()   → BACKFILL_PREAMBLE
                 → build_extraction_header() → EXTRACTION_PREAMBLE
                 → load_drive_docs()         → FOLLOW_UPS_DOC, TOMAC_DOC, ...
                 → load_active_packages()    → dashboard tile visibility
```

**Model routing:**

| Pass | Script | Model | Rationale |
|------|--------|-------|-----------|
| Email triage | `cos_gmail_mini_v2.py` | `claude-haiku-4-5-20251001` | Fast, cheap — every email |
| Email enrich | `cos_gmail_mini_v2.py` | `claude-sonnet-4-6` | DEAL/RECRUIT only |
| Transcript memo | `cos_otter_backfill.py` | `claude-sonnet-4-6` | Format-constrained prose |
| Deal extraction | `cos_otter_backfill.py` | `claude-opus-4-7` | Multi-hop deal/LP inference |
| Real-time hook | `cos_transcript_hook.py` | `claude-sonnet-4-6` | Fast, runs post-call |

---

## Package Deployment

Set `"packages"` in `firm_config.json` to control which pipelines are active:

```json
{ "packages": ["operations"] }                               // Package B only
{ "packages": ["market_intelligence"] }                      // Package A only
{ "packages": ["market_intelligence", "operations"] }        // Both (default)
```

The dashboard always shows all tiles. Tiles whose `requires_package` is not in the active list render with `package_active: false` — the server shows an empty state for those tabs.

---

## Team Setup — Shared Dashboard

Multiple people at the same firm can share one dashboard. The pipeline runs on one machine; others access it via the network. Firm config is shared via a private GitHub repo so any update (new team member, updated peer firms, draft voice change) propagates to everyone on `git pull`.

### Two-repo model

```
Public repo (this one)              Private config repo (you create)
github.com/ygontownik/Dashboard     github.com/yourfirm/your-firm-config
────────────────────────────        ───────────────────────────────────
Universal code — anyone can use     Firm identity — your team only

_firm_context.py                    firm_context.yaml
cos_otter_backfill.py               firm_config.json
cos-dashboard-server.py             drive-docs.yaml
setup.sh, costs.py, etc.
```

### Setup steps

**1. Create the private config repo** (one-time, done by whoever runs the pipeline machine):

```bash
# On GitHub: New repository → private → e.g. "tcip-config"
mkdir ~/cos-pipeline-config
cp ~/cos-pipeline/firm_context.yaml ~/cos-pipeline-config/
cp ~/cos-pipeline/firm_config.json  ~/cos-pipeline-config/
cp ~/dashboards/config/drive-docs.yaml ~/cos-pipeline-config/

cd ~/cos-pipeline-config
git init && git add . && git commit -m "Initial firm config"
git remote add origin https://github.com/yourfirm/your-firm-config.git
git push -u origin main
```

Add team members as collaborators: `github.com/yourfirm/your-firm-config → Settings → Collaborators`

**2. Point the pipeline at the config repo** (on every machine):

```bash
# Add to ~/.zshrc:
export COS_CONFIG_DIR="$HOME/cos-pipeline-config"
source ~/.zshrc
```

**3. Each team member's setup:**

```bash
git clone https://github.com/yourfirm/your-firm-config ~/cos-pipeline-config
git clone https://github.com/ygontownik/Dashboard ~/cos-pipeline
echo 'export COS_CONFIG_DIR="$HOME/cos-pipeline-config"' >> ~/.zshrc
source ~/.zshrc
cd ~/cos-pipeline && ./setup.sh   # OAuth + LaunchAgents on their machine
```

**4. Dashboard access for remote team members** — the dashboard binds to your local network IP. For remote access, install [Tailscale](https://tailscale.com) (free for small teams) on each machine. Your Mac Mini gets a stable Tailscale IP accessible from anywhere.

### Updating shared config

When you change `firm_context.yaml` (new hire, new peer firm, updated draft voice):

```bash
cd ~/cos-pipeline-config
git add firm_context.yaml
git commit -m "Add Sarah to team"
git push
```

Each team member picks it up with:
```bash
cd ~/cos-pipeline-config && git pull
```

The pipeline reads fresh config on the next run — no restart needed.

### Multiple firms

Each firm gets its own private config repo. They share the same public code repo but have zero visibility into each other's config, credentials, or dashboard data.

---

## Cost Profile (approximate, prompt caching enabled)

| Pipeline | Model | Cost/run |
|----------|-------|----------|
| Gmail mini (50 emails) | Haiku + Sonnet | ~$0.02–0.08 |
| Transcript backfill | Sonnet + Opus | ~$0.10–0.50/transcript |
| Real-time hook | Sonnet | ~$0.02–0.05/call |
| Podcast transcription | AssemblyAI + Sonnet | ~$0.009/min audio |

Prompt caching is enabled on all stable preambles. The firm identity header caches across all items in a single run, reducing effective input cost by ~90%.
