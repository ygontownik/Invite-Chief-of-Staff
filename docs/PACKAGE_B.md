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
| **`cos_capture_pipeline.py`** | **LaunchAgent daily 7:22am** | Sonnet — full capture + reconciliation + auto-drafts in one call | ~$0.10–0.30 / run |
| `cos_otter_backfill.py` | LaunchAgent daily | Pass 1 Sonnet (memo) + Pass 2 Opus (deal/LP extraction, multi-hop) | ~$0.10–0.50 / transcript |
| `cos_transcript_hook.py` | Fired by `call_recorder.py` post-recording | Sonnet (single-pass) | ~$0.02–0.05 / call |

**`cos_capture_pipeline.py`** is the daily centerpiece — it replaced the old `cos-capture-pipeline` Claude Code SKILL on 2026-04-30 with a portable Python implementation. Same behavior, no Claude Code dependency, supports Gmail OR Outlook via the email provider abstraction. The legacy SKILL is preserved at `~/.claude/scheduled-tasks/cos-capture-pipeline/SKILL.md.archive-pre-python-migration`.

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

---

## Email Provider Abstraction

`cos_capture_pipeline.py` and other Package B scripts read inboxes through a provider abstraction (`_email_provider.py`) so the same code works against Gmail or Microsoft 365 / Outlook.

Selection is config-driven:

```json
// firm_config.json
{ "email_provider": "gmail" }     // or "outlook"
```

| Provider | OAuth | Token cache | API |
|---------|-------|-------------|-----|
| `gmail` | Google OAuth (existing flow) | `~/credentials/gmail_mini_token.pickle` | Gmail v1 REST |
| `outlook` | MS device-code flow | `~/credentials/ms_token.json` | Microsoft Graph v1.0 |

### Outlook one-time setup

You have two paths — pick whichever fits your situation. Neither requires a paid Azure subscription.

**Path A — Use Microsoft's public client (zero setup, works for personal Outlook accounts):**

1. Don't create any client config file.
2. Set `"email_provider": "outlook"` in `firm_config.json`.
3. First run prints a notice that it's using Microsoft's public Azure-CLI client and starts a device-code flow. Sign in with your Outlook account in the browser, paste the printed code, done.

This uses Microsoft's pre-registered Azure CLI client ID (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) — the same one `az login` uses. It's appropriate for personal / individual use. Enterprises typically prefer Path B for audit and policy reasons.

**Path B — Register your own app (recommended for enterprise / shared deployment):**

1. Go to **https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade** (this URL bypasses the Azure subscription prompt and goes straight to App registrations under Microsoft Entra ID).
2. Click **+ New registration**.
   - Type: "Public client" / native
   - For personal Outlook accounts: tenant_id = `consumers`
   - For Microsoft 365 work account: tenant_id = your org tenant ID, or `common`
3. Note the Application (client) ID.
4. API permissions tab → add Microsoft Graph delegated permissions:
   `Mail.Read`, `Mail.ReadWrite`, `User.Read`, `offline_access`. Grant admin consent if it's your org tenant.
5. Save at `~/credentials/ms_oauth_client.json`:
   ```json
   {"client_id": "00000000-0000-0000-0000-000000000000", "tenant_id": "consumers"}
   ```
6. Set `"email_provider": "outlook"` in `firm_config.json`.
7. First run triggers the device-code flow as in Path A.

After that, all pipeline scripts read from your Outlook mailbox the same way Gmail users access theirs.

---

## Future: Agentic Action Enrichment (Plan C — not yet built)

The current pipeline drafts email replies on your behalf. The natural next layer is enriching other action types so each follow-up on the dashboard has a click-through that takes you to a pre-prepared artifact:

| Action pattern | What Claude could pre-do | Click-through goes to |
|---------------|-------------------------|----------------------|
| "Register for [conference]" | Look up the registration page, populate name/email/firm | Pre-filled registration form |
| "Send X to Y by [date]" | Locate X in Drive, draft an email with X attached | Email draft (already done for emails) |
| "Pull [data] before call with Z" | Fetch the data, attach to the action item as an artifact | Inline data viewer |
| "Look into Y" | Web research → 1-page brief | Brief in Drive |
| "Schedule call with X" | Find mutually free slot, draft calendar invite | Pre-filled calendar event |

Architecturally this is a tool-use loop on top of `cos_capture_pipeline.py` — the foundation (provider abstraction, firm-context-driven prompts, JSON spec → batch writes) is already there. Ship as Plan C in a future iteration when there's a specific action type that's high-value enough to automate.
