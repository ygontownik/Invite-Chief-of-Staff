# INSTALL_SUBSCRIPTION.md — onboarding for a new tenant in subscription mode

> **⚠ STOP — principal authorization required.** Subscription mode bills
> against your Claude Pro/Max 5-hour rate window. If you are an operator
> setting this up at someone else's direction, **confirm with the
> principal that subscription mode is authorized for this tenant** before
> running anything in this guide. The principal alone decides which
> tenants run on subscription vs api. If you do not have explicit
> authorization, follow [INSTALL.md](INSTALL.md) (api mode) instead.

This guide walks a brand-new tenant through standing up the cos-pipeline
dashboard against **their own Claude Pro/Max account** instead of a
shared Anthropic API key. Subscription mode means:

- **Zero API spend** for the standard pipelines (briefing, capture,
  research, deals). Calls bill against your existing Claude Pro/Max
  5-hour window, not per-token.
- **Each tenant pays their own subscription.** Nothing routes through
  another tenant's API key.
- **Your prompts and data stay on your machine + your Claude.ai
  account.** No central operator handles them.

If you want pay-per-token API mode instead (predictable per-call cost,
no rate window), see [INSTALL.md](INSTALL.md) — pick `--auth-mode=api`
at install time.

---

## Prerequisites

| What | Why | How to check |
|---|---|---|
| Mac (Apple Silicon recommended) | The pipeline + dashboard run as LaunchAgents | `uname -m` returns `arm64` |
| Claude Pro or Max subscription | The dispatch path uses your OAuth, not an API key | Sign in to claude.ai; confirm Pro/Max in account settings |
| Claude Code CLI (`claude`) | The dispatch path uses `claude_agent_sdk` which talks to your local Claude OAuth state | `which claude` returns a path |
| Python ≥ 3.10 | `claude_agent_sdk` does not import on Python 3.9 | `python3 --version` (or `/opt/homebrew/bin/python3 --version`) |
| Homebrew Python recommended | macOS system Python is 3.9 — too old | `brew install python@3.13` if needed |
| Google account | OAuth into Drive + Gmail for content pipelines | n/a |

---

## 30-second mental model

```
  YOUR MACHINE                                 CLAUDE.AI (your account)
  ─────────────                                ────────────────────────
  cos-pipeline/                                claude.ai/projects/
   ├─ _model_router.py        ──── OAuth ───►   ┌────────────────────────┐
   │   (subscription dispatch)                  │ <slug> · Briefing      │ Sonnet 4.6
   ├─ data-<slug>/                              │ <slug> · Capture       │ Sonnet 4.6
   │   ├─ dispatch.jsonl       (call ledger)    │ <slug> · Research      │ Sonnet 4.6
   │   ├─ queue.jsonl          (rate-limit Q)   │ <slug> · Deals         │ Opus 4.7
   │   └─ subscription-health.json (snapshot)   └────────────────────────┘
   └─ ~/cos-pipeline-config-<slug>/             ▲
       ├─ firm_context.yaml :: auth_mode        │ system prompt
       └─ firm_config.json   :: claude_projects │ lives in the project
                                                 (v2; v1 inlines per call)
```

---

## Install — eight steps

### 1. Clone the repo

```bash
git clone https://github.com/<your-fork>/cos-pipeline ~/cos-pipeline
cd ~/cos-pipeline
```

### 2. Provision your config dir

```bash
# Pick a slug — lowercase, hyphenated, 2-16 chars (e.g. "acme-cap")
SLUG=acme-cap
mkdir -p ~/cos-pipeline-config-${SLUG}
cp firm_context.template.yaml ~/cos-pipeline-config-${SLUG}/firm_context.yaml
cp firm_config.template.json   ~/cos-pipeline-config-${SLUG}/firm_config.json

# Edit firm_context.yaml — fill in principal name, firm name,
# investment focus, owner whitelist, peer firms, etc.
```

### 3. Install Python deps

```bash
# claude_agent_sdk MUST go under Homebrew Python (3.10+), not the
# Mac system Python.
/opt/homebrew/bin/python3 -m pip install --break-system-packages \
   'claude-agent-sdk>=0.1.72' anthropic pyyaml google-auth-oauthlib
```

### 4. Verify Claude Code is installed and you're signed in

```bash
which claude          # should print a path
claude --version      # should print the version
# In another terminal: open `claude` interactively and confirm
# the welcome banner shows YOUR Pro/Max account name. If not:
claude login
```

### 5. Run the standard installer (api-mode prereqs)

```bash
./setup.sh --instance=${SLUG} --domain=infra-pe
# (or --domain=real-estate / generic-dealmaker)
```

This sets up the dashboard server, OAuth bootstrap, keychain entry
(left blank for subscription tenants — only used for optional API
fallback), and validates the basics.

### 6. Run the subscription add-on installer

```bash
./setup.sh.subscription.next --instance=${SLUG}
```

This walks you through five steps (S1–S5):

- **S1** verifies the Claude CLI + login.
- **S2** verifies Python ≥ 3.10 + the `claude_agent_sdk` import.
- **S3** prints **four copy-pasteable system prompts**, one per
  package, and asks you to paste each one into a new
  [Claude.ai project](https://claude.ai/projects). Per the table:

  | Project name (suggested) | Model | Prompt source |
  |---|---|---|
  | `${SLUG} · Briefing` | Sonnet 4.6 | `domains/<domain>/prompts/briefing-morning.txt` |
  | `${SLUG} · Capture` | Sonnet 4.6 | `domains/<domain>/prompts/email-triage.txt` |
  | `${SLUG} · Research` | Sonnet 4.6 | `domains/<domain>/prompts/research-summary.txt` |
  | `${SLUG} · Deals` | Opus 4.7 | `domains/<domain>/prompts/deal-summary.txt` |

  After creating each project, you copy the project ID from the URL
  (`claude.ai/project/<ID>`) and paste it back at the prompt.

- **S4** writes `auth_mode: subscription` to your `firm_context.yaml`
  and the four project IDs into `firm_config.json :: claude_projects`.
- **S4.5** stages two LaunchAgent plists (queue-drain +
  subscription-health) into `data-${SLUG}/staged-launchagents/` for
  you to review before installing.
- **S5** prints a validation summary.

### 7. Run preflight

```bash
./scripts/preflight_subscription.sh --instance=${SLUG}
```

This is **read-only** — it makes no Claude calls. It verifies
everything from steps 1–6: repo layout, config files, auth_mode field,
claude CLI, Python + SDK, claude_projects population, domain bundle
prompts, plist templates, and a `_model_router --dry-run` smoke test.

If preflight fails, the printed fix-list tells you exactly what to do
for each red ✗.

### 8. Install + load the LaunchAgents

```bash
# Review the staged plists.
ls -l ~/cos-pipeline/data-${SLUG}/staged-launchagents/

# Install + load.
cp ~/cos-pipeline/data-${SLUG}/staged-launchagents/*.plist \
   ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.cos.${SLUG}.queue-drain.plist
launchctl load ~/Library/LaunchAgents/com.cos.${SLUG}.subscription-health.plist
```

The dashboard server LaunchAgent is loaded by the standard installer
in step 5. After step 8, the queue-drain daemon polls every 30 min
and the subscription-health snapshot regenerates every hour.

---

## Verifying it works

### Preflight should be all-green

```bash
./scripts/preflight_subscription.sh --instance=${SLUG}
# expect: "Ready for cutover. Tenant '${SLUG}' passes all subscription-mode
# prerequisites."
```

### One real subscription call

```bash
/opt/homebrew/bin/python3 -c "
import _model_router as mr
out = mr.call_claude(
    'cos-personal-briefing',
    system='You are a test assistant.',
    messages=[{'role':'user','content':'Reply with: pong'}],
    mode='subscription', tenant='${SLUG}',
)
print(out['text'])
print('subscription_meta:', out['subscription_meta'])
"
# expect: pong
# subscription_meta should show rate_limit_status='allowed' and the
# expected resets_at timestamp.
```

### Cost report

```bash
python3 ~/cos-pipeline/costs.py --tenant=${SLUG} --days=1
# Subscription panel should show "Calls: 1 (ok=1 failed=0)" and
# the latest rate-limit status.
```

---

## Slash commands + auto-pipeline

`setup.sh` (run in step 5) installs all five subscriber-facing slash commands into `~/.claude/commands/` and registers a Claude Code Stop hook that orchestrates the auto-pipeline. **The full slash-command + cadence reference is in [INSTALL.md → Slash commands installed and Auto-pipeline (Stop hook cadence)](INSTALL.md#slash-commands-installed).** Quick summary:

| Slash command | Trigger |
|---|---|
| `/new-deal` | Manual — when you originate a new deal |
| `/deal-sync` | Auto every 2h (regenerates each deal's status + brief + dashboard entry from `log.json`) |
| `/deal-update` | Manual — push a `---DEAL-UPDATE---` JSON from a deal session |
| `/refresh-project-instructions` | Auto every 24h, gated by ref-doc change |
| `/capture-deal-chats` | Auto every 4h, block-only scrape (full chat content stays on claude.ai) |

These are subscription-mode-friendly: every auto-trigger that uses Claude (`/deal-sync`, `/refresh-project-instructions`, `/capture-deal-chats`) spawns a headless `claude -p ...` against your Pro/Max account — same rate-window economics as the scheduled SKILLs, no API spend.

`/dash` and `/dash-close` are **not** installed by `setup.sh` — those are dashboard-development tools used only by the maintainer of the codebase, not subscribers.

---

## What happens at runtime

- **Each scheduled SKILL** that has `mode: subscription` in
  `routines.yaml` calls `_model_router.call_claude(...)`. The router
  resolves the route, applies the `auth_mode` override, and dispatches
  via `claude_agent_sdk.query()` against your Claude Pro/Max account.
- **Each call appends a row** to `data-${SLUG}/dispatch.jsonl` with
  the model used, token counts, the `RateLimitEvent.status`, and the
  reset timestamp.
- **If the 5-hour window is exhausted**, the dispatcher branches by
  task type:
  - Time-insensitive (briefing, podcast, weekly deals, research) →
    enqueued to `data-${SLUG}/queue.jsonl` with the original prompt
    persisted; the queue-drain daemon re-fires after the window
    resets.
  - Time-sensitive (otter post-call hook, gmail-mini, capture) →
    raised; the next scheduled fire retries.
- **Hourly health snapshots** to `subscription-health.json` give the
  dashboard a 1-line tile: "47 calls today, 3 failures, 2 queued,
  next reset in 1h47m."

---

## When something goes wrong

### `claude_agent_sdk import failed`

You're running on Python 3.9 (system Python). Switch to Homebrew:

```bash
brew install python@3.13
/opt/homebrew/bin/python3 -m pip install --break-system-packages 'claude-agent-sdk>=0.1.72'
# Then re-run setup.sh.subscription.next so the LaunchAgent plist
# templates get re-stamped against /opt/homebrew/bin/python3.
```

### `claude --version` works but calls return "rate_limit_exceeded" immediately

You may be sharing a Pro/Max account between interactive Claude Code
work and the pipeline daemons. Check:

```bash
python3 ~/cos-pipeline/_subscription_health.py --tenant=${SLUG}
# look at the "Rate-limit (latest seen)" line. If status='exceeded',
# wait until resets_at and try again.
```

If this happens consistently, schedule the heavy pipelines overnight
per [SUBSCRIPTION_SCHEDULE.md](SUBSCRIPTION_SCHEDULE.md) and keep your
interactive Claude Code use on the morning side of the schedule.

### Queue is filling up

Look at `data-${SLUG}/queue.jsonl` — each row has the original
`task_type` and `error_msg`. If `attempts` is climbing toward 24, the
window is staying exhausted across fire cycles. The drain daemon
moves rows past 24 attempts to `queue.dead.jsonl` for human review.

### The dashboard says "subscription dispatch unhealthy"

```bash
python3 ~/cos-pipeline/_subscription_health.py --tenant=${SLUG}
# Look at the by_outcome breakdown. failure: counts > 0 = the
# dispatch is hitting errors. Check dispatch.jsonl :: error_msg
# for the underlying message.
```

---

## Switching between subscription and API mode

To go subscription → api:

```bash
# Edit firm_context.yaml :: auth_mode → "api"
# Add an ANTHROPIC_API_KEY to the keychain:
security add-generic-password \
    -s "cos-pipeline-${SLUG}/ANTHROPIC_API_KEY" \
    -a "$USER" \
    -w "<your-key>"
# Optionally unload the queue-drain + health LaunchAgents (api mode
# doesn't use them):
launchctl unload ~/Library/LaunchAgents/com.cos.${SLUG}.queue-drain.plist
launchctl unload ~/Library/LaunchAgents/com.cos.${SLUG}.subscription-health.plist
```

To go api → subscription: re-run step 6 above.

---

## Uninstalling

```bash
./setup.sh --instance=${SLUG} --uninstall \
   [--purge-data] [--purge-config] [--yes]
```

This removes all `com.cos.${SLUG}.*` LaunchAgents and (with the
opt-in flags) the data + config dirs. Your **Claude.ai Projects are
not touched** — they live on Anthropic's servers, not your machine.
Delete them at claude.ai/projects if you want a full wipe.

---

## What's next (v2)

- **Project targeting** — once `ClaudeAgentOptions` exposes a project
  field, `_model_router._dispatch_subscription` will pass the
  per-package project ID and your inlined-preamble overhead drops to
  zero. CSPIKE_PLAN.md Probe 5 tracks this.
- **Multi-tenant under one Pro account** — not supported. Each tenant
  needs its own Claude Pro/Max subscription.
- **Per-routine project override** — escape hatch for "this one
  routine needs its own project." Adds a `claude_project_override:`
  field per routine in `routines.yaml`.

---

## Cross-references

| Doc | Why read it |
|---|---|
| [CSPIKE_PLAN.md](CSPIKE_PLAN.md) | The decision rationale: why Path (b) `claude_agent_sdk.query()` won, what the bare-mode options finding was, what's deferred to v2. |
| [SUBSCRIPTION_INSTALL.md](SUBSCRIPTION_INSTALL.md) | Engineer-facing install spec; this doc is the tenant-facing version. |
| [SUBSCRIPTION_SCHEDULE.md](SUBSCRIPTION_SCHEDULE.md) | Default overnight schedule for subscription tenants — heavy work fires 2-6 AM in the W1 window. |
| [HANDOFF_RUN6.md](HANDOFF_RUN6.md) | Inventory of what shipped in the subscription cutover work. |
