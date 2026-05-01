# Package B — Operations

> Email triage, call transcript processing, and action-item extraction. The "what's happening today" half of the system.

## What it does

Watches your day-to-day workflow and turns it into structured intelligence:

- **Inbound emails** (Gmail) → triaged into DEAL / RECRUIT / ACTION / RESEARCH / IGNORE; deal and recruiting threads get enriched analysis appended to the right Google Docs.
- **Call transcripts** (Otter AI + desktop recordings) → six-section investor memo + structured JSON extraction (action items, deal updates, LP intel, new contacts).
- **Real-time hook** → fires after each new recording lands in Drive; lightweight extraction so action items show up on the dashboard within ~30 seconds of the call ending.

All output flows to:
- **Follow-ups doc** — pending actions table
- **Recruiting doc** — job-search pipeline (only if `job_search_active: true` in firm_context)
- **Pipeline doc** — deal intelligence prose
- **People doc** — contact rollup

## Scripts

| Script | Trigger | Model routing | Cost / run |
|--------|---------|---------------|-----------|
| `cos_gmail_mini_v2.py` | LaunchAgent every 2h, Mon–Fri 8am–8pm | Haiku triage all → Sonnet enrich DEAL/RECRUIT ≥0.7 | ~$0.02–0.08 |
| `cos_otter_backfill.py` | LaunchAgent daily | Pass 1 Sonnet (memo) + Pass 2 Opus (deal/LP extraction, multi-hop) | ~$0.10–0.50 / transcript |
| `cos_transcript_hook.py` | Fired by `call_recorder.py` post-recording | Sonnet (single-pass) | ~$0.02–0.05 / call |

All three load identity, owner whitelist, peer firms, and team behavior from `firm_context.yaml` at import time. Every prompt — including the 1.5 KB "who you are" header — is generated dynamically with prompt caching enabled, so the cached prefix delivers ~90% effective input cost reduction across runs.

## Configuration

Package B activates when `firm_config.json` includes `"operations"` in the `packages` array:

```json
{
  "packages": ["operations"]
}
```

The dashboard then lights up the **Status**, **TC Pipeline**, and **Personal** tiles. Tiles requiring `market_intelligence` show empty-state placeholders.

## Required Drive docs

These four must exist before Package B can run:

| Slug in `firm_config.json["docs"]` | Purpose |
|-----|---------|
| `followups` | Action item table |
| `pipeline` | Deal intel narrative |
| `people` | Contact rollup |
| `recruiting` | Job-search tracker |

The setup script (`python3 setup.py --create-docs`) can auto-create blank Google Docs and populate the IDs for you.

## Required keywords

Package B uses two keyword lists from `firm_config.json` for fast pre-classification:

- **`deal_keywords`** — terms like specific deal names, "term sheet", "loi", "diligence", "ic memo". Any email matching one of these auto-classifies as DEAL without LLM triage.
- **`recruit_keywords`** — search firm names, "interview", "offer", "headhunter", "comp". Auto-classifies as RECRUITING.

Tune these to your firm. The defaults in `firm_config.template.json` are illustrative — replace with your actual deal codenames and recruiting contacts.

## Required research senders

`firm_config.json["research_senders"]` maps domains to Google Doc IDs. Emails from these domains skip LLM triage entirely and append directly to the named doc:

```json
"research_senders": {
  "research-firm-1.com": "GOOGLE_DOC_ID_FOR_THEIR_ARCHIVE",
  "research-firm-2.com": "GOOGLE_DOC_ID_FOR_THEIR_ARCHIVE"
}
```

## Local state

| File | Purpose |
|------|---------|
| `~/credentials/processed_emails.json` | Gmail dedup tracker — IDs of already-processed messages |
| `~/credentials/processed_cos_transcripts.json` | Transcript dedup tracker |
| `~/credentials/gmail_mini_token.pickle` | Gmail OAuth token (read-only scope) |
| `~/credentials/gdrive_token.pickle` | Drive + Docs OAuth token |
| `~/credentials/token.json` | Calendar OAuth token (for attendee resolution) |

## Speaker identification on calls

Package B's transcript pipeline uses the team list in `firm_context.yaml` to attribute commitments correctly:

- The first non-principal team member with "deal" in their role is treated as the **deal lead** for internal-call attribution rules
- On internal calls, deal-driving statements default to the deal lead
- On external calls, the principal is the primary firm voice
- Each team member's `internal_call_role` field describes their typical behavior on internal calls

Owner attribution requires a clean speaker mapping. If the model can't confidently identify the principal's speaker tag, it emits items with `owner="unknown"` and the validator routes them to `routingExceptions[]` for manual review rather than guessing.

## Action item routing

Every extracted action gets a `dashboard_path` field that controls where it appears on the dashboard:

- `COS › [DEAL_WORKSTREAM] Deals › [deal name]` — deal-specific actions
- `COS › [DEAL_WORKSTREAM] Fundraising › [LP name]` — fundraising actions
- `Deal Pipeline › [theme] › [target name]` — deal pipeline actions (Package A integration)
- `COS › Recruiting › [firm name]` — recruiting actions
- `COS › Follow-ups` — fallback for actions with no specific deal/LP

The `[DEAL_WORKSTREAM]` token is replaced at runtime with `firm_context.yaml.workstream_categories.deal` (e.g. "Tomac Cove" or "Meridian Deals").

## Cost profile

| Volume | Daily cost (estimate) |
|--------|----------------------|
| 50 emails / day, 8 calls / week | ~$2–3 / day |
| 100 emails / day, 15 calls / week | ~$5–8 / day |
| Heavy use (200 emails, 25 calls) | ~$10–15 / day |

Prompt caching keeps marginal cost low — the firm identity header (~1.5 KB) caches across all items in a single run.
