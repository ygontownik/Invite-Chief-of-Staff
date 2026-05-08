# Security & Data Flow

> **Read this before deploying.** Plain-English description of what data leaves your machine, where credentials live, and how to disable each feature.

---

## TL;DR — what you're trusting

| Trust target | What you trust them with | Disable how |
|--------------|-------------------------|------------|
| **Anthropic** (api.anthropic.com) | Email + transcript content, your investment focus, owner names | Don't run pipelines |
| **Google** (Gmail/Drive/Docs/Calendar APIs) | Read inbox, write to your 4 Drive Docs, list calendar events | Set `email_provider` away from gmail; revoke OAuth tokens |
| **Microsoft** (Graph API, optional) | Read Outlook inbox, write drafts | Set `email_provider` to gmail or none |
| **AssemblyAI** (optional, Package A only) | Audio file content for transcription | Don't run `podcast_transcribe.py` |
| **GitHub** (where the code lives) | Code only — your `firm_context.yaml` and `firm_config.json` are gitignored | n/a |

**You do NOT trust:** any third-party SaaS dashboard, any analytics, any telemetry. Nothing this system runs sends data anywhere except the 5 services above. The dashboard is local (`localhost:7777`) — your data stays on your Mac except where explicitly noted below.

---

## Data flow diagram

```
                    YOUR MAC (everything below this line is local)
  ┌──────────────────────────────────────────────────────────────────────┐
  │                                                                      │
  │   ~/credentials/         ~/cos-pipeline/         ~/dashboards/        │
  │   (OAuth tokens)         (code + configs)        (cache + logs)      │
  │                                                                      │
  │   ↑                            ↑                       ↑             │
  │   └─── reads ────┬──── reads ──┘                       │             │
  │                  │                                     │             │
  │   Pipeline scripts (cos_capture_pipeline.py, etc.)     │             │
  │                  │                                     │             │
  │                  ↓ writes JSON                         │             │
  │            cos_batch_write.py ──────────────────► dashboard-data.json│
  │                  │                                                   │
  └──────────────────┼───────────────────────────────────────────────────┘
                     │
                     ▼ outbound HTTPS
                ┌────┴─────┐
                │ Anthropic │ ◄── prompts containing email/transcript text
                │  Google   │ ◄── Gmail search, Doc reads/writes, Calendar list
                │  MS Graph │ ◄── Outlook search, draft create  (optional)
                │ AssemblyAI│ ◄── audio file content  (Package A only)
                └───────────┘
                     │
                     ▼ inbound HTTPS responses (text only)
                JSON bodies → parsed by your scripts → written to local files
```

**No inbound connections** to your Mac. Nothing exposes a public port. The dashboard server binds to `localhost:7777` only.

---

## Per-service detail

### 1. Anthropic API (`api.anthropic.com`)

**When called:**
- `cos_gmail_mini_v2.py` — every 2h (email triage + DEAL/RECRUIT enrichment)
- `cos_capture_pipeline.py` — daily 7:22am (capture + reconciliation + drafts)
- `cos_otter_backfill.py` — daily (transcript memo + extraction)
- `cos_transcript_hook.py` — after each new recording
- `podcast_transcribe.py` — daily (Package A)
- `deal-dashboard-refresh.py` — weekly 3-pass (Package A)

**What's sent (per call):**
- The prompt header: principal name, firm name, team members, investment focus, peer firms, draft voice rules — read from `firm_context.yaml`
- The content being analyzed: email body excerpts, full transcript text, calendar events, existing follow-up doc content
- For drafts: the email thread context

**Volume estimate:** A typical day sends ~100k–300k tokens of input across all pipelines (~$0.30–$1.00).

**Disable:** Stop running the pipelines (`./setup_launchagents.sh --uninstall`). The dashboard server itself never calls Anthropic.

**Anthropic's data handling:** API requests are not used for training (per their commercial Terms of Service). Optional zero-data-retention can be requested via Anthropic enterprise contracts.

---

### 2. Google APIs

| Service | What's accessed | Scope requested |
|---------|----------------|-----------------|
| Gmail v1 | Inbox messages (read), draft creation | `gmail.readonly`, `gmail.compose` |
| Drive v3 | List folder contents, create blank Docs | `drive.file` (limited — only Docs your script created) |
| Docs v1 | Read + write your 4 designated Docs | `documents` |
| Calendar v3 | List events on primary calendar | `calendar.readonly` |

**Token storage:** OAuth tokens cached locally as pickle/json files in `~/credentials/`:
- `gmail_mini_token.pickle` — Gmail-only token (read+compose)
- `gdrive_token.pickle` — Drive + Docs token
- `token.json` — Calendar token (refreshable)

**To revoke access:** https://myaccount.google.com/permissions → find "COS Pipeline" → Remove. Then delete the pickle/json files in `~/credentials/`. Pipelines will fail until re-authorized.

**Restricted scope:** `drive.file` is intentional — the script CANNOT read Drive content it didn't create. Your other Drive files are off-limits to the script regardless of what's in your account.

---

### 3. Microsoft Graph API (`graph.microsoft.com`) — optional

**When called:** Only if `firm_config.json` has `"email_provider": "outlook"`. If you're on Gmail, Microsoft is never contacted.

**Scopes requested:**
- `Mail.Read`, `Mail.ReadWrite` — read inbox, create drafts
- `User.Read` — basic profile (required by Graph)
- `offline_access` — refresh tokens

**Token storage:** `~/credentials/ms_token.json` (access token + refresh token). 1-hour access token lifetime; refresh token typically 90 days.

**To revoke:** https://account.microsoft.com/privacy/app-access → revoke. Delete `ms_token.json`. Pipelines fall back to whatever `email_provider` you set, or fail clean if Outlook was the only configured option.

---

### 4. AssemblyAI (`api.assemblyai.com`) — Package A only

**When called:** Only by `podcast_transcribe.py` when transcribing podcast audio.

**What's sent:** The audio file content (downloaded from RSS feeds). No email content, no transcripts, no firm data.

**Disable:** Don't run `podcast_transcribe.py`. Or set `packages: ["operations"]` in `firm_config.json` to disable Package A entirely.

**Data retention:** AssemblyAI deletes audio after transcription per their default retention policy.

---

### 5. GitHub (where this code lives)

The repo at `https://github.com/ygontownik/Invite-Chief-of-Staff` contains **only code, templates, and docs.** It NEVER receives:
- Your `firm_context.yaml` (gitignored)
- Your `firm_config.json` (gitignored)
- Your OAuth tokens, API keys, or any credentials (in `~/credentials/`, gitignored)
- Your dashboard cache or runtime logs (gitignored)
- Your real Doc IDs (template values only)

You can verify this with: `git check-ignore firm_context.yaml firm_config.json` — both should be reported as ignored.

---

## Credential storage on your Mac

| Credential | Where | Format | Backup risk |
|------------|-------|--------|-------------|
| Anthropic API key | macOS Keychain (service: `cos-pipeline/ANTHROPIC_API_KEY`) | Encrypted by Keychain | Time Machine encrypts Keychain by default |
| Dashboard HTTP Basic Auth | macOS Keychain | Encrypted by Keychain | Same |
| Gmail OAuth tokens | `~/credentials/gmail_mini_token.pickle` | Pickled Python object (plaintext on disk) | **Backups WILL include this** |
| Drive OAuth tokens | `~/credentials/gdrive_token.pickle` | Pickled (plaintext on disk) | Same |
| Google Calendar token | `~/credentials/token.json` | JSON (plaintext on disk) | Same |
| Microsoft tokens | `~/credentials/ms_token.json` | JSON (plaintext on disk) | Same |
| OAuth client secrets | `~/credentials/gdrive_credentials.json`, `ms_oauth_client.json` | JSON (plaintext on disk) | Same |

**Implication:** if your Mac is compromised at the user-account level, the OAuth tokens grant access to your Gmail/Drive/Outlook content. Mitigations:
- Use FileVault disk encryption (default on modern Macs)
- Don't ssh into your Mac with keys you've shared
- Rotate Anthropic API key periodically (`./setup_keychain.sh` re-prompts)
- For Gmail/Outlook tokens: revoke at the provider when convenient

**Recovery:** if you lose the credentials directory, the pipelines fail until you re-run OAuth flows. No data is lost — your Drive Docs and emails are untouched.

---

## Per-feature toggles (what you can turn off)

Set these in `firm_config.json`:

```json
{
  "packages": ["operations"],            // omit "market_intelligence" to disable Package A
  "auto_drafts_enabled": false,          // disable email draft generation
  "max_emails_per_run": 10,              // cap how many emails Claude sees per scan
  "first_run_lookback_hours": 1          // first-run window
}
```

Set these in `firm_context.yaml`:

```yaml
draft_voice:
  never_include:                         # rules Claude must follow when drafting
    - "Never commit to specific deal terms"
    - "Never speculate about firm strategy"
```

---

## What this system CANNOT do (by design)

- ❌ Send email automatically. Drafts only — you click Send.
- ❌ Modify Drive files outside the 4 designated Docs (drive.file scope restriction).
- ❌ Read your Drive content that wasn't created by the script.
- ❌ Access financial systems, banking, payment portals.
- ❌ Make calls or take actions outside the scoped APIs above.

If a script ever tries to do something outside these boundaries, it would fail at the API authorization layer.

---

## Audit trail

Every Anthropic API call is logged to `~/dashboards/data/anthropic-usage.jsonl` with timestamp, site, model, and token counts. Use `python3 costs.py` (when available) to see aggregated spend.

Pipeline runs log to `~/dashboards/logs/claude-tasks/` with stdout and stderr per LaunchAgent. Logs rotate weekly.

---

## Reporting issues

Found a security issue in the code? Open an issue at https://github.com/ygontownik/Invite-Chief-of-Staff/issues with `[security]` in the title. Don't include credentials, tokens, or PII in the report.
