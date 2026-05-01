# Package A — Market Intelligence

> Podcast transcription, research PDF processing, and the 3-pass deal pipeline scanner. The "what's happening in the market" half of the system.

## What it does

Ingests external market intelligence and turns it into structured deal ideas:

- **Podcast feeds** (RSS) → AssemblyAI transcription + Claude Sonnet six-section memo per episode → per-show Google Doc with TOC, plus aggregate "Podcast Summaries" doc
- **Research PDFs** (Jefferies, GS, RBN, Substack feeds) → sector-tagged appended to master Drive Docs
- **Deal pipeline (3-pass)** — weekly scan over targets with score calibration, archetype routing, and IC memo production

The output flows to:
- **Per-show podcast docs** (one Doc per podcast, episodes appended with TOC + memos)
- **Sector research docs** (Jefferies / GS / RBN / FVR / a16z)
- **Daily Market Update** (NotebookLM-generated briefing)
- **Deal Pipeline data JSON** (`~/dashboards/data/compiled/deal-pipeline-data.json`)
- **IC memos** (one per qualified target, attached to the Pipeline doc)

## Scripts

| Script | Trigger | Model routing | Notes |
|--------|---------|---------------|-------|
| `podcast_transcribe.py` | LaunchAgent daily | Sonnet (memo) | RSS feeds + folder IDs from `firm_config.json["podcast_feeds"]` |
| `deal-dashboard-refresh.py` | LaunchAgent weekly | 3-pass: Pass 1 Sonnet (scanner) → Pass 2 Opus (analyst) → Pass 3 Sonnet (IC memo) | The Opus pass is where the system earns its cost — multi-hop deal inference |
| `deal-system-compile.py` | Triggered after Pass 3 | n/a (data compile) | Reads `Deals/*.md` + Excel profit models → `deal-system-data.json` |

## Configuration

Package A activates when `firm_config.json` includes `"market_intelligence"` in the `packages` array:

```json
{
  "packages": ["market_intelligence"]
}
```

The dashboard then lights up the **Deal Ideas** and **Briefing** tiles.

## Podcast feeds

Add or remove podcasts by editing `firm_config.json["podcast_feeds"]`:

```json
"podcast_feeds": {
  "Catalyst":                "https://feeds.megaphone.fm/catalyst",
  "Open Circuit":            "https://feeds.megaphone.fm/open-circuit",
  "Energy Capital":          "https://api.substack.com/feed/podcast/1180283.rss",
  "Infrastructure Investor": "https://feed.podbean.com/infrastructureinvestorpodcast/feed.xml",
  "Energy Gang":             "https://rss.art19.com/the-energy-gang"
}
```

Each show name → RSS URL. The pipeline:
1. Parses each feed
2. Skips already-transcribed episodes via `~/credentials/processed_podcasts.json`
3. Downloads the audio, sends to AssemblyAI
4. Generates a six-section memo via Sonnet
5. Appends to the per-show Drive Doc and the aggregate "Podcast Summaries" Doc

The per-show Doc is auto-created on first encounter inside the folder set by `firm_config.json["podcast_transcripts_folder_id"]`. The summary Doc lives in `firm_config.json["podcast_summary_folder_id"]`.

## Research PDF processing

The Jefferies / GS / RBN processors live in `~/dashboards/routines/process/` (paths externalized in `drive-docs.yaml`). Each one:
1. Watches a Drive folder for new PDFs
2. Extracts text + tables
3. Sends to Sonnet for sector classification + summary
4. Appends to the right master Doc (mapped via `drive-docs.yaml`)

For a new firm, set `package: market_intelligence` on each research-source doc in `drive-docs.yaml` and update the folder watch IDs to your own.

## The 3-pass deal pipeline (advanced)

This is the crown jewel — a weekly scan that turns market-wide news into ranked, de-risked deal ideas with full IC memos. It runs three Claude passes in sequence:

### Pass 1 — Source Scanner (Sonnet)
- Inputs: search results from `config/search-queries.json`
- Output: structured deal candidates with provenance
- Model: `claude-sonnet-4-6`, max_tokens 2048

### Pass 2 — Pipeline Analyst (Opus)
- Inputs: Pass 1 output + existing pipeline state + market context
- Output: deal ideation, new target identification, score calibration, 5-test actionability gate, archetype routing, right-to-win angle classification
- Model: `claude-opus-4-7`, max_tokens 4096
- This is the only place Opus is used in the system. The cost is justified because the analyst pass requires multi-hop inference: geopolitical event → ownership structure → entry path → returns logic.

### Pass 3 — IC Memo Production (Sonnet)
- Inputs: Pass 2 qualified targets
- Output: structured IC memo per target (format defined, data given)
- Model: `claude-sonnet-4-6`, max_tokens 4096
- Format-constrained — Sonnet is sufficient and faster.

## Local state

| File | Purpose |
|------|---------|
| `~/credentials/processed_podcasts.json` | Podcast episode dedup tracker (per-GUID) |
| `~/credentials/podcast_doc_index.json` | Show name → Google Doc ID map |
| `~/credentials/processed_files.json` | Jefferies PDF dedup |
| `~/credentials/gs_processed_files.json` | GS PDF dedup |
| `~/credentials/processed_substack_articles.json` | Substack article dedup |
| `~/dashboards/data/compiled/deal-pipeline-data.json` | Latest pipeline scan output |
| `~/dashboards/data/compiled/deal-system-data.json` | Compiled deal portfolio (post-Pass-3) |

## Required environment

```bash
export ANTHROPIC_API_KEY="sk-ant-..."     # required
export ASSEMBLYAI_API_KEY="..."           # required for podcast_transcribe.py only
```

AssemblyAI runs at ~$0.009 / minute of audio. A 60-min podcast episode costs ~$0.55 to transcribe + ~$0.05 for the Claude memo = ~$0.60 / episode total.

## Cost profile

| Volume | Weekly cost (estimate) |
|--------|----------------------|
| 5 podcasts × 4 episodes / week | ~$12 |
| 30 Jefferies / GS PDFs / week | ~$3 |
| 1 weekly 3-pass scan over 30 targets | ~$8–15 (Opus pass dominates) |

Total Package A: typically $25–35 / week running cost.

## What you can skip

If you don't want the 3-pass pipeline (it's heavyweight — designed for 25–30 named targets), Package A still functions as a podcast/research-PDF ingestion system. Just don't run `deal-dashboard-refresh.py` — the rest works standalone.
