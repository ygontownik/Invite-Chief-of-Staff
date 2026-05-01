# GitHub Manifest — What Gets Committed vs What Stays Local

Every file in this repo falls into one of three buckets:

---

## ✅ COMMIT — Safe to push to GitHub

These files contain no personal data, credentials, or Drive IDs. They work for any firm.

### Identity layer
| File | What it is |
|------|-----------|
| `firm_context.template.yaml` | Fill-in template for principal/firm/team/investment config |
| `firm_config.template.json` | Fill-in template for email keywords, Doc IDs, packages |
| `config/drive-docs.template.yaml` | Fill-in template for Google Drive doc/folder IDs |

### Core shared modules
| File | What it is |
|------|-----------|
| `_firm_context.py` | Loads `firm_context.yaml`, builds all model preambles, `load_drive_docs()` |
| `_entity_normalizer.py` | Canonical entity resolution (firms, people, deal names) |
| `_envelope_writer.py` | Writes structured envelopes to dashboard data |
| `_usage.py` | Anthropic API usage logger |
| `_capture_query_builder.py` | Builds Claude capture queries |
| `_resolved_row_sweep.py` | Sweeps resolved action rows |

### Package B — Operations
| File | What it is |
|------|-----------|
| `cos_gmail_mini_v2.py` | Gmail triage pipeline (Haiku + Sonnet) |
| `cos_otter_backfill.py` | Transcript backfill processor (Sonnet memo + Opus extraction) |
| `cos_transcript_hook.py` | Real-time post-call hook |
| `cos_batch_write.py` | Batch Google Docs writer |
| `cos_prefetch_drive.py` | Drive prefetch helper |

### Package A — Market Intelligence
| File | What it is |
|------|-----------|
| `podcast_transcribe.py` | AssemblyAI + Claude podcast transcription |
| `deal-dashboard-refresh.py` | 3-pass IC memo pipeline |
| `deal-system-compile.py` | Deal system data compiler |
| `peakload_sync.py` | Peak load data sync |

### Dashboard
| File | What it is |
|------|-----------|
| `cos-dashboard-server.py` | Local HTTP server (port 7777), tile and package gating |
| `cos-dashboard-fetch.py` | Google Docs → JSON cache fetcher |
| `cos-dashboard-refresh.py` | Fast HTML inject from cache |
| `config/dashboard-tiles.yaml` | Tile registry with `requires_package` tags |
| `config/routing-rules.md` | LLM routing contract (shared by all pipelines) |

### Call recording
| File | What it is |
|------|-----------|
| `call_recorder.py` | Desktop call recorder (Zoom/Teams/Meet) |
| `call_recorder_menu.py` | Menu bar UI for call recorder |
| `call_recording_system.py` | Core recording system |
| `call_scheduler.py` | Calendar-based recording scheduler |

### Setup and docs
| File | What it is |
|------|-----------|
| `setup.py` | New-firm setup validator |
| `README.md` | System overview, quick start, file map |
| `GITHUB_MANIFEST.md` | This file |
| `.gitignore` | Exclusions |

### Config (generic)
| File | What it is |
|------|-----------|
| `config/targets.json` | Deal pipeline targets (firm-specific — review before committing) |
| `config/search-queries.json` | Search queries for deal pipeline scanner |
| `SKILL_pass1_scanner.md` | Pass 1 scanner skill |
| `SKILL_pass2_analyst.md` | Pass 2 analyst skill |
| `SKILL_pass3_ic_memo.md` | Pass 3 IC memo skill |
| `scripts/run-weekly.sh` | Weekly pipeline runner |

---

## ❌ DO NOT COMMIT — Personal/firm-specific, stays local

These are gitignored. Never push them.

| File | Why not |
|------|---------|
| `firm_context.yaml` | Contains your real name, firm, team, investment focus |
| `firm_config.json` | Contains your real Google Doc IDs and deal keywords |
| `drive-docs.yaml` | Contains your personal Google Drive IDs |
| `~/credentials/*.pickle` | OAuth tokens — would give Drive/Gmail access |
| `~/credentials/*.json` | OAuth client credentials |
| `YONI_CONTEXT.md` | Personal context document |
| `logs/` | Runtime logs |
| `*.plist` | LaunchAgent files (machine-specific paths) |

---

## ⚠️ REVIEW BEFORE COMMITTING

These files are tracked but may contain firm-specific content. Check them before pushing to a public repo.

| File | What to check |
|------|--------------|
| `config/targets.json` | Named deal targets — anonymize or remove for public repo |
| `config/search-queries.json` | May reference deal-specific search terms |
| `SKILL_pass1_scanner.md` | May reference specific sectors or counterparties |
| `call_recorder.py` | Check for hardcoded meeting titles or contact names |
| `.claude/launch.json` | May contain personal Claude config |

---

## How to push a clean version to GitHub

```bash
cd ~/tomac-cove-pipeline

# Stage everything safe
git add _firm_context.py _entity_normalizer.py _envelope_writer.py \
        _usage.py _capture_query_builder.py _resolved_row_sweep.py
git add firm_context.template.yaml firm_config.template.json
git add config/drive-docs.template.yaml config/dashboard-tiles.yaml
git add cos_gmail_mini_v2.py cos_otter_backfill.py cos_transcript_hook.py
git add cos-dashboard-server.py cos-dashboard-fetch.py cos-dashboard-refresh.py
git add cos_batch_write.py cos_prefetch_drive.py deal-dashboard-refresh.py
git add podcast_transcribe.py deal-system-compile.py
git add call_recorder.py call_recorder_menu.py call_recording_system.py call_scheduler.py
git add setup.py README.md GITHUB_MANIFEST.md .gitignore
git add config/routing-rules.md SKILL_pass1_scanner.md SKILL_pass2_analyst.md SKILL_pass3_ic_memo.md

# Verify nothing sensitive is staged
git diff --cached --name-only

# Commit
git commit -m "Package B portability: firm_context.yaml schema, _firm_context.py loader, GitHub-ready"

# Push
git push origin main
```
