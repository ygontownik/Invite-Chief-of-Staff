# INSTALL.md — set up the COS pipeline on your Mac

This walks you through getting your own private COS dashboard + email/briefing pipelines running on a Mac. Estimated time: **45–60 min**, mostly waiting on dependencies and OAuth flows.

## What you end up with

- A web dashboard at `http://localhost:<port>/` (default 7777) that aggregates your follow-ups, calendar, deals, and email queue.
- Daily AM briefing emailed to your distribution list (via NotebookLM + Anthropic).
- A capture pipeline that runs every morning at 7:22 — scans inbox + transcripts, drafts replies, reconciles follow-ups.
- Per-2h Gmail triage classifying inbound mail.
- Optional: podcast transcription, Otter call ingestion, Substack research sync.

All your data lives **on this Mac**. No third-party hosting. No data leaves the machine except (a) outbound API calls to Anthropic / Google / AssemblyAI, and (b) emails you explicitly send through the system.

## Prerequisites

You'll need to have ready:

- **macOS 13+** (Ventura or later).
- **Homebrew** — install from https://brew.sh if not present. Run `brew --version` to check.
- **Python 3.11+** — `brew install python@3.12` if missing.
- **git** — usually pre-installed with Xcode CLT; otherwise `xcode-select --install`.
- **Claude Code CLI** — install per https://docs.claude.com/claude-code. Required for the SKILL-based briefing flow; everything else works without it.
- **A Google Workspace account** with:
  - Drive enabled (folders + Docs).
  - Gmail (the system reads/drafts in this account).
  - Calendar (the system reads upcoming events).
- **An Anthropic API key** (https://console.anthropic.com). The pipelines use Claude Sonnet for triage and Claude Haiku for theme classification. Expect ~$5–20/month at typical usage; you control the cap via Anthropic's billing console.
- **An AssemblyAI API key** (https://www.assemblyai.com) — only if you want call/podcast transcription. Skippable on day 1.

## Install

```bash
# 1. Clone the public code repo
git clone <your-public-cos-pipeline-url> ~/cos-pipeline
cd ~/cos-pipeline

# 2. Run the bootstrap. Replace `<slug>` with a short ID for yourself
#    (lowercase, 2-16 chars, no spaces). Example: `acme-cap` or `re-dev`.
./setup.sh --instance=<slug> --domain=<domain>
```

The `--domain` argument picks a domain bundle of prompts and templates. Choose:
- `infra-pe` — infrastructure private equity (power, midstream, digital infra)
- `real-estate` — real-estate development / investment
- `generic-dealmaker` — fallback for other deal-driven workflows

The script walks you through 8 steps interactively. Each step prints what it's about to do and asks for confirmation. You can stop and resume with `--resume` later.

### Auth mode — pick at install time

> **⚠ Principal authorization required for `subscription` mode.** Subscription
> mode bills against the principal's Claude Pro/Max 5-hour rate window, which
> is shared with interactive Claude Code use. **Do not pass
> `--auth-mode=subscription` unless the principal has explicitly authorized
> this deployment for that path.** When in doubt, default to `api`.

Pass `--auth-mode=<subscription|api>` to `setup.sh` to skip the post-install prompt:

```bash
# api mode (default if omitted) — Anthropic per-token, billed to your API key
./setup.sh --instance=<slug> --domain=<domain> --auth-mode=api

# subscription mode — uses your Claude Pro/Max OAuth, $0 marginal per call
./setup.sh --instance=<slug> --domain=<domain> --auth-mode=subscription
```

When `--auth-mode=subscription` is set, the installer chains
`setup.sh.subscription.next` after Step 8 — it walks the four-project
Claude.ai provisioning, writes `auth_mode: subscription` to your
firm_context.yaml, stages the queue-drain + subscription-health
LaunchAgents for review, and points you at preflight. Full walkthrough
in **[INSTALL_SUBSCRIPTION.md](INSTALL_SUBSCRIPTION.md)**. Existing
api-mode tenants who want to flip later: see **[MIGRATE_TO_SUBSCRIPTION.md](MIGRATE_TO_SUBSCRIPTION.md)**.

Optional flag: `--add-fallback-api-key` (subscription mode only) — also
prompts for an Anthropic API key, used as a fallback when the 5-hour
subscription window is exhausted.

## What the script does

| Step | What | Time |
|---|---|---|
| 1 | Verifies prerequisites (macOS, Python, Homebrew, git, Claude Code) | <30s |
| 2 | Installs Python deps (pyyaml, google-auth, anthropic, etc.) | ~2 min |
| 3 | Creates your config dir at `~/cos-pipeline-config-<slug>/` (private git repo) and seeds `firm_context.yaml` from a template — name, role, firm, team | ~3 min |
| 3b | Copies the domain bundle (prompts + templates) into your config | <30s |
| 4 | Asks which transcription source you'll use (Otter / Beside / Fireflies / Zoom / none) | <30s |
| 5 | Stores your API keys in macOS Keychain under prefix `cos-pipeline-<slug>` | ~2 min |
| 6 | Runs the Google OAuth flow via `oauth_bootstrap.sh --scope=all` (Drive + Docs + Gmail + Calendar in one pass; idempotent — skips scopes whose token already exists) + creates your Drive folder tree (Follow-ups, Pipeline, People CRM, Briefing Log, Daily Market) | ~5 min |
| 7 | Installs LaunchAgents for the dashboard + capture + Gmail triage daemons | ~1 min |
| 8 | Validates the install (config gates, Drive accessible, keychain populated, port free) | <30s |

After Step 8, you'll see:
```
Dashboard    : http://localhost:<port>
Config dir   : ~/cos-pipeline-config-<slug>
Data dir     : ~/cos-pipeline/data-<slug>
Logs dir     : ~/cos-pipeline/logs-<slug>
```

## First run

1. Open `http://localhost:<port>/` in your browser. **Log in with the username and password you typed during Step 5** — `setup_keychain.sh` seeds them into `~/cos-pipeline-config-<slug>/config/users.json` so the dashboard recognizes you. Add more users any time from the Admin tile (Access Management tab).
2. Run a manual capture to seed your dashboard with current state:
   ```bash
   COS_CONFIG_DIR=~/cos-pipeline-config-<slug> python3 ~/cos-pipeline/cos_capture_pipeline.py --since 24h
   ```
3. The dashboard will populate within 5–10s.

## Daily/weekly cadence (after install)

| When | What runs | Where |
|---|---|---|
| Daily 5:00 AM | Podcast transcription | Drive: `Podcast Summaries` |
| Daily 7:22 AM | Capture pipeline (inbox scan, draft replies, reconcile follow-ups) | Dashboard `/` |
| Daily 7:51 AM | Personal briefing (Anthropic synthesis of overnight signals) | Daily Market Update doc |
| Daily 8:30 AM (Tue–Fri) | NotebookLM briefing email to your distribution list | Email |
| Every 2h M–F 8 AM–8 PM | Gmail mini triage | Dashboard email queue |
| Every 15 min always-on | Otter transcript ingestion (if enabled) | Drive transcripts folder |
| Sunday evening | Weekly market summary | Email |

All schedules are config — edit `~/cos-pipeline-config-<slug>/distributions/*.yaml` to change recipients, times, or disable a flow.

## Re-validating

```bash
~/cos-pipeline/setup.sh --instance=<slug> --validate
```

Runs the preflight without modifying anything. A clean install reports `═══ VALIDATE: PASS (<slug>) ═══` with these checks:

- Config dir is a git repo at `~/cos-pipeline-config-<slug>/`
- `firm_context.yaml` has required keys: principal, team, owner_whitelist, domain
- `firm_config.json :: keychain_service_prefix` is `cos-pipeline-<slug>`
- All required Google Docs accessible via OAuth token
- Keychain entries `cos-pipeline-<slug>/ANTHROPIC_API_KEY` + `cos-pipeline-<slug>/ASSEMBLYAI_API_KEY` present
- Dashboard env vars `OWNER_PASSWORD` + `PARTNER_PASSWORD` set in plist or shell env
- `users.json` has at least one user with username + password
- Port free (or in-use by your own dashboard)
- No legacy `com.cos-pipeline.*` LaunchAgent label collisions

Useful after manual edits to `firm_context.yaml` or `firm_config.json` to confirm everything still resolves.

## Uninstalling

The canonical one-shot uninstall (introduced session 4):

```bash
# Tear down everything for your slug — bootouts every com.cos.<slug>.* LaunchAgent,
# sweeps cos-pipeline-<slug>/* keychain entries, idempotent.
~/cos-pipeline/setup.sh --instance=<slug> --uninstall

# Add --purge-data to remove data-<slug>/ + logs-<slug>/ dirs (with confirm)
~/cos-pipeline/setup.sh --instance=<slug> --uninstall --purge-data

# Add --purge-config to remove the config repo at ~/cos-pipeline-config-<slug>/
~/cos-pipeline/setup.sh --instance=<slug> --uninstall --purge-config

# Skip all confirmation prompts (for unattended runs / scripts)
~/cos-pipeline/setup.sh --instance=<slug> --uninstall --yes --purge-data --purge-config
```

Slug-isolated: only touches `com.cos.<slug>.*` agents, `cos-pipeline-<slug>/*` keychain entries, and `*-<slug>/` directories. Other tenants on the same Mac are unaffected.

The `~/cos-pipeline/` code repo and your OAuth tokens at `~/credentials/` are NOT removed by `--uninstall`. To revoke OAuth: https://myaccount.google.com/permissions.

## Troubleshooting

- **Dashboard returns 502 / nothing on `:<port>`** — check `launchctl list | grep com.cos.<slug>.` for a running dashboard agent. If absent, run `setup.sh --instance=<slug> --resume` to reinstall.
- **API rate-limit errors in logs** — your Anthropic plan may need a higher tier. Check `http://localhost:<port>/admin/spend` for current usage.
- **OAuth token expired** (e.g. `invalid_grant: Token has been expired or revoked`) — re-consent for the affected scope:
  ```bash
  ~/cos-pipeline/oauth_bootstrap.sh --scope=full --force          # Drive+Docs+Gmail+Calendar
  ~/cos-pipeline/oauth_bootstrap.sh --scope=drive --force         # Drive+Docs only
  ~/cos-pipeline/oauth_bootstrap.sh --scope=gmail-read --force    # Gmail readonly
  ~/cos-pipeline/oauth_bootstrap.sh --scope=gmail-compose --force # Gmail readonly + compose
  ~/cos-pipeline/oauth_bootstrap.sh --scope=all --force           # Re-consent everything
  ```
  After re-consent: `launchctl kickstart -k gui/$(id -u)/com.cos.<slug>.dashboard` to restart the dashboard.
- **API key not picked up** (e.g. `[auth] WARNING: ANTHROPIC_API_KEY not set`) — verify with `python3 ~/cos-pipeline/_secrets.py probe ANTHROPIC_API_KEY`. If `<unset>`, write it: `security add-generic-password -s "cos-pipeline-<slug>/ANTHROPIC_API_KEY" -a "$USER" -w "<your-key>" -U`.
- **Failed install / want to start over** — run `setup.sh --instance=<slug> --uninstall --purge-data --purge-config --yes` for a clean teardown, then re-run setup.
- **Briefing missing Bank Street / Deal Ideas section** — likely a Drive sync stall. Check `cos-personal-briefing.run.log` for the "Sync confirmed" line.

## Where to get help

Questions about your specific tenant config: contact whoever installed this for you.

Code-level issues: file at the public `~/cos-pipeline` repo's issue tracker.

Data privacy concerns: your data stays on this Mac. Review `firm_context.yaml` and any file under `~/cos-pipeline-config-<slug>/` to see exactly what config is in scope. Revoke OAuth tokens at https://myaccount.google.com/permissions if you decide to stop using the system.
