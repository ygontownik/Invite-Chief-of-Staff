# Package B — Operations

> Email triage, call transcript processing, and action-item extraction. The "what's happening today" half of the system.

## What it does

Watches your day-to-day workflow and turns it into structured intelligence:

- **Inbound emails** (Gmail) → triaged into DEAL / ACTION / RESEARCH / IGNORE; deal threads get enriched analysis appended to the right Google Docs. Optional RECRUIT category enabled per-tenant via `firm_context.yaml :: features.job_search`.
- **Call transcripts** (Otter AI + desktop recordings) → six-section investor memo + structured JSON extraction (action items, deal updates, LP intel, new contacts).
- **Real-time hook** → fires after each new recording lands in Drive; lightweight extraction so action items show up on the dashboard within ~30 seconds of the call ending.

All output flows to:
- **Follow-ups doc** — pending actions table
- **Pipeline doc** — deal intelligence prose
- **People doc** — contact rollup
- **Recruiting doc** — only created/written when `features.job_search: true` (off by default for new tenants; opt-in for principals running an active job search)

## Scripts

| Script | Trigger | Model routing | Cost / run |
|--------|---------|---------------|-----------|
| `cos_gmail_mini_v2.py` | LaunchAgent every 2h, Mon–Fri 8am–8pm | Haiku triage all → Sonnet enrich DEAL ≥0.7 (RECRUIT also when features.job_search on) | ~$0.02–0.08 |
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

The dashboard then lights up the **HQ** (formerly "Status"), **Deal Pipeline** (formerly "TC Pipeline"), and **Personal** tiles. Tiles requiring `market_intelligence` show empty-state placeholders. Tile labels can be customized per-tenant via `firm_context.yaml :: tile_labels`. The **Personal** tile is hidden unless `features.job_search` is on.

## Required Drive docs

These three must exist before Package B can run (recruiting is opt-in):

| Slug in `firm_config.json["docs"]` | Purpose | Required? |
|-----|---------|---|
| `followups` | Action item table | Always |
| `pipeline` | Deal intel narrative | Always |
| `people` | Contact rollup | Always |
| `recruiting` | Job-search tracker | Only when `features.job_search: true` |

The setup script (`python3 setup.py --create-docs`) can auto-create blank Google Docs and populate the IDs for you.

## Required keywords

Package B uses two keyword lists from `firm_config.json` for fast pre-classification:

- **`deal_keywords`** — terms like specific deal names, "term sheet", "loi", "diligence", "ic memo". Any email matching one of these auto-classifies as DEAL without LLM triage. Loaded from per-tenant `firm_config.json` first, then the active domain bundle's `config.yaml`, then a hardcoded fallback.
- **`recruit_keywords`** — only consulted when `features.job_search: true`. Search firm names, "interview", "offer", "comp", etc. Auto-classifies as RECRUIT.

Tune these to your firm. The defaults in your domain bundle (`~/cos-pipeline/domains/<your-domain>/config.yaml`) are illustrative — replace with your actual deal codenames.

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
- `COS › Recruiting › [firm name]` — recruiting actions (only when `features.job_search: true`)
- `COS › Follow-ups` — fallback for actions with no specific deal/LP

The `[DEAL_WORKSTREAM]` token is replaced at runtime with `firm_context.yaml.workstream_categories.deal` (e.g. "ExampleCo Deals" or "Meridian Deals").

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

---

## Troubleshooting

Common failures and fixes. Most issues fall into one of these patterns.

### Setup fails

**`./setup.sh: Permission denied`**
```bash
chmod +x ./setup.sh ./setup_keychain.sh ./setup_launchagents.sh
```

**`No firm context file found. Expected one of: firm_context.yaml, firm_context.json`**
You haven't filled in the templates yet. Run:
```bash
python3 setup.py --fix    # copies templates → real configs
# then edit firm_context.yaml + firm_config.json with your data
```

**`Missing dependency: No module named 'yaml'` (or 'google', 'anthropic')**
```bash
pip3 install pyyaml google-auth google-auth-oauthlib google-api-python-client anthropic
```

### OAuth fails

**Browser opens but page says "redirect_uri_mismatch" or "invalid_request"**
Your `gdrive_credentials.json` was created as a Web app, not Desktop. Re-download from Google Cloud Console:
- APIs & Services → Credentials → + CREATE CREDENTIALS → OAuth client ID
- **Application type: Desktop application** (this matters)
- Download JSON → save as `~/credentials/gdrive_credentials.json`

**`No OAuth client at ~/credentials/gdrive_credentials.json`**
You haven't downloaded the OAuth client config yet. See above. Free; takes 2 minutes.

**Browser hangs or "localhost refused to connect" after Google sign-in**
The script's local OAuth server died (usually a stale Python process). Kill any lingering Python processes and retry:
```bash
pkill -f cos_otter_backfill || pkill -f setup.py
python3 setup.py --create-docs
```

**`AuthError: invalid_grant`**
Your refresh token expired (typical after 6 months idle, or after Google password change). Delete and re-auth:
```bash
rm ~/credentials/gmail_mini_token.pickle
python3 cos_gmail_mini_v2.py --list --backfill 1h    # triggers fresh OAuth
```

### Pipelines silently doing nothing

**`cos-gmail-mini` runs but no follow-ups appear**
Check the log:
```bash
tail -50 ~/dashboards/logs/claude-tasks/cos-gmail-mini.stdout.log
```
Most common causes:
- `ANTHROPIC_API_KEY not set` — Keychain not loaded; run `./setup_keychain.sh` and ensure the LaunchAgent script sources it
- All emails got classified `IGNORE` — your `deal_keywords` (and `recruit_keywords` if job_search is on) in `firm_config.json` are too narrow
- `processed_emails.json` got corrupted — `rm ~/credentials/processed_emails.json` and re-run

**Dashboard shows "package inactive" badges everywhere**
Your `firm_config.json` is missing the `packages` field or has it set wrong. Should be:
```json
"packages": ["operations", "market_intelligence"]
```

**Dashboard data is stale (everything looks like yesterday)**
The auto-warmup is on a 10-min cycle. Force a refresh:
```bash
curl -s -X POST http://localhost:7777/warmup
```
If that doesn't help, the fetcher is failing — check:
```bash
python3 ~/cos-pipeline/cos-dashboard-fetch.py 2>&1 | tail -20
```

### LaunchAgent issues

**LaunchAgent installed but never fires**
Check it's loaded:
```bash
launchctl list | grep cos-pipeline
```
Check its logs:
```bash
tail -50 ~/dashboards/logs/claude-tasks/<task-name>.stderr.log
```
Common: the script path in the plist points to a moved file. Re-run `./setup_launchagents.sh` to regenerate plists with current paths.

**`launchctl: Bootstrap failed: 5: Input/output error`**
The LaunchAgent service is in a bad state. Force-reload:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.cos-pipeline.<name>.plist 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cos-pipeline.<name>.plist
```

### Drafts not appearing

**`cos_capture_pipeline` runs successfully but no drafts in Gmail/Outlook**
Two possibilities:
1. Claude decided no email needed a reply. Check the log for `drafts: 0` in the summary.
2. The provider's `create_draft` failed. Search the log for "Draft failed":
   ```bash
   grep "Draft failed\|create_draft" ~/cos-pipeline/logs/capture_pipeline.log | tail -10
   ```

**Drafts created but with wrong tone or content**
Edit `firm_context.yaml` → `draft_voice` block. Add or remove rules in `always_include` / `never_include`. Next run picks up the change automatically.

### Outlook-specific

**`Device code expired`**
You waited too long to enter the code in the browser. Re-run; the new code is valid for 15 minutes.

**`AADSTS50020: User account from identity provider does not exist in tenant`**
Your `tenant_id` in `~/credentials/ms_oauth_client.json` doesn't match your account type:
- Personal Outlook (outlook.com, hotmail.com) → use `"tenant_id": "consumers"`
- Work Microsoft 365 → use your org tenant ID OR `"tenant_id": "common"`

**Don't have an Azure subscription / can't register an app**
You don't need a paid Azure subscription. Either:
- Skip the registration entirely — the system falls back to Microsoft's public Azure-CLI client_id, OR
- Go directly to https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade (this URL bypasses the subscription paywall and goes straight to App registrations under Microsoft Entra ID, which is free)

### Cost concerns

**API spend higher than expected**
Run `python3 costs.py --by-script` to see which script is the cost driver. Tactics:
- Reduce `max_emails_per_run` in `firm_config.json` (default 50 → try 25)
- Disable Package A if you don't use it: `"packages": ["operations"]`
- The Pass-2 Opus call in `cos_otter_backfill.py` is expensive — only fires for deal/LP transcripts. If you're not running infra deals, Opus might be overkill; edit the model routing in that script.

**`projected_monthly_usd` is alarming**
Set a daily threshold in `firm_config.json`:
```json
"cost_alert_threshold_daily_usd": 5.00
```
The Health tile will surface a warning when daily spend exceeds the threshold.

### Dashboard / port conflicts

**`OSError: [Errno 48] Address already in use` on port 7777**
Another process is on 7777. Find and stop it:
```bash
lsof -i :7777
launchctl unload ~/Library/LaunchAgents/com.cos-pipeline.dashboard-server.plist
# wait 5 seconds
launchctl load ~/Library/LaunchAgents/com.cos-pipeline.dashboard-server.plist
```

**Dashboard says "401 Unauthorized" in browser**
You forgot the HTTP Basic Auth credentials. Username + password are what you set during `./setup_keychain.sh`. To reset:
```bash
./setup_keychain.sh    # re-prompts for DASHBOARD_USERNAME and DASHBOARD_PASSWORD
launchctl unload ~/Library/LaunchAgents/com.cos-pipeline.dashboard-server.plist
launchctl load ~/Library/LaunchAgents/com.cos-pipeline.dashboard-server.plist
```

### Still stuck?

1. Run `python3 setup.py` — the validator surfaces most config issues.
2. Tail all logs at once: `tail -f ~/dashboards/logs/claude-tasks/*.stderr.log`
3. Run a pipeline manually with `--dry-run` to see what it intends without writes:
   ```bash
   python3 cos_capture_pipeline.py --dry-run --since 24h
   ```
4. Open an issue at https://github.com/ygontownik/Invite-Chief-of-Staff/issues with:
   - The error message
   - Last 30 lines of the relevant log
   - Your `python3 --version` and `pip3 list | grep -E 'google|anthropic|yaml'` output
   - Don't include API keys, OAuth tokens, or PII
