# MIGRATE_TO_SUBSCRIPTION.md — flipping an existing api-mode tenant

> **⚠ STOP — principal authorization required.** Subscription mode bills
> against the principal's Claude Pro/Max 5-hour rate window. **Confirm
> with the principal that this tenant is authorized to migrate to
> subscription** before running anything in this guide. If you are not
> the principal, do not proceed without explicit sign-off — the
> principal alone decides which tenants run on subscription vs api.

This guide is for a tenant who is **already running cos-pipeline in
api mode** (paying per Anthropic API call) and wants to switch to
subscription mode (their own Claude Pro/Max subscription, zero API
spend, 5-hour rate window).

Brand-new tenants should follow [INSTALL_SUBSCRIPTION.md](INSTALL_SUBSCRIPTION.md)
instead — this doc assumes the basic install (config dir, dashboard
server, OAuth, keychain) is already in place and working.

---

## What changes, what doesn't

| Surface | Before (api) | After (subscription) |
|---|---|---|
| Auth | `ANTHROPIC_API_KEY` in keychain | OAuth via `claude` CLI / Pro/Max subscription |
| Per-call billing | $X per 1M tokens (Anthropic invoice) | $0 (counted against 5-hour window) |
| Routine dispatch | `anthropic.Anthropic().messages.create(...)` | `claude_agent_sdk.query(...)` |
| Rate limits | 80 RPM-class API limits, no window | 5-hour window shared with interactive Claude Code use |
| Failure handling on rate-limit | API error surfaced | TIME_INSENSITIVE → queued + retried; TIME_SENSITIVE → re-raised |
| Daemons running | dashboard, podcast, otter, etc. | + queue-drain (every 30 min), + subscription-health (hourly) |
| Cost report | `costs.py` shows $ figure | `costs.py` shows $ for any api fallback + a separate Subscription panel (calls / tokens / rate-limit status) |
| Schedule | Whenever your existing routines.yaml says | Same today; consider shifting heavy work overnight (see [SUBSCRIPTION_SCHEDULE.md](SUBSCRIPTION_SCHEDULE.md)) |

What **doesn't** change:
- Your firm_context.yaml (other than the new `auth_mode` field).
- Your dashboard tiles, owner whitelist, peer firms, counterparty
  aliases, deal keywords, OAuth state, Drive doc IDs.
- AssemblyAI for podcast/call transcription (still $0.009/min — orthogonal to Claude).
- API mode is **not deleted** — it remains as an opt-in fallback when
  `--add-fallback-api-key` is set, and the same router code paths handle both.

---

## Prerequisites

Before starting, confirm:

```bash
which claude                     # must print a path
claude --version                 # must work without error
/opt/homebrew/bin/python3 -c 'import claude_agent_sdk'   # must not raise
```

If any of those fail, install per the prerequisites section of
[INSTALL_SUBSCRIPTION.md](INSTALL_SUBSCRIPTION.md) before continuing.

You also need the cutover to live `_model_router.py` etc. to be done
already. If your `~/cos-pipeline/_model_router.py` still raises
NotImplementedError on subscription, the principal hasn't done the
six-`cp` cutover yet — see [HANDOFF_RUN6.md](HANDOFF_RUN6.md). Don't
start migrating tenants until cutover is live.

---

## Migration — eight steps

The flow is: **provision projects → write fields → preflight → load
LaunchAgents → smoke test → soak → optional: shift schedule**. None
of this overwrites api-mode state — you can revert at any step.

### 1. Snapshot your current state

```bash
SLUG=<your-slug>
DATE=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/cos-pipeline-config-${SLUG}.bak-${DATE}
cp ~/cos-pipeline-config-${SLUG}/firm_context.yaml \
   ~/cos-pipeline-config-${SLUG}/firm_config.json \
   ~/cos-pipeline-config-${SLUG}.bak-${DATE}/

# Capture LaunchAgent state.
launchctl list | grep "com.cos.${SLUG}\." > ~/cos-pipeline-config-${SLUG}.bak-${DATE}/launchctl.snapshot
```

If anything goes wrong, you can restore by copying these back and
re-loading the original LaunchAgents.

### 2. Run the subscription installer add-on

```bash
cd ~/cos-pipeline
./setup.sh.subscription.next --instance=${SLUG}
```

This walks the same five-step flow as a new tenant (S1 CLI verify,
S2 Python+SDK, S3 four-project provisioning, S4 write `auth_mode`
+ `claude_projects`, S4.5 stage plists, S5 summary). At the end:

- `firm_context.yaml :: auth_mode: subscription` is written.
- `firm_config.json :: claude_projects` has your four project IDs.
- `data-${SLUG}/staged-launchagents/` has the queue-drain +
  subscription-health plists ready for review.

### 3. Run preflight

```bash
./scripts/preflight_subscription.sh --instance=${SLUG}
```

Must come back **PREFLIGHT: N passed, 0 failed**. Any red ✗ has a
suggested fix command — follow it before continuing. Common ones:

- `claude_projects.<package> is blank` — re-run setup.sh.subscription.next
  and paste a project ID. (Yellow warning — install proceeds with
  preamble inlined per call; v2 will use the project ID once Probe 5
  is wired.)
- `auth_mode field absent` — your firm_context.yaml didn't get
  written. Re-run setup.sh.subscription.next.

### 4. Load the new daemons (NOT replacing the old ones)

```bash
cp ~/cos-pipeline/data-${SLUG}/staged-launchagents/*.plist \
   ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.cos.${SLUG}.queue-drain.plist
launchctl load ~/Library/LaunchAgents/com.cos.${SLUG}.subscription-health.plist
```

**You do NOT unload your existing routine LaunchAgents** at this step.
The flip happens automatically on the next fire of any routine that
already has `mode: subscription` in routines.yaml — `auth_mode:
subscription` is a HARD override that forces the dispatcher down the
new path.

### 5. Smoke test — one trivial subscription call

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
```

Then check the dispatch ledger has one row:

```bash
tail -1 ~/cos-pipeline/data-${SLUG}/dispatch.jsonl | python3 -m json.tool
```

You should see `outcome: ok`, the model used, and a
`rate_limit_status` field. If `outcome` starts with `failure:`, the
ledger row will also have an `error_msg` — read it before continuing.

### 6. Watch the first scheduled fire

The next time any subscription-mode routine fires (e.g.
`cos-personal-briefing` at 7:51 AM), it'll route through the SDK
dispatch automatically. Watch:

```bash
# After the routine fires:
python3 ~/cos-pipeline/_subscription_health.py --tenant=${SLUG} --hours=1

# Should show:
#   calls: 1  (ok=1  failed=0)
#   rate-limit (latest): status=allowed  resets_at=...
```

If `failed` is non-zero, `data-${SLUG}/dispatch.jsonl` has the
error_msg.

### 7. Soak for one week

Don't shift schedules or unload api-fallback yet. Let the existing
routines run on subscription for a week. After 7 days, check:

```bash
python3 ~/cos-pipeline/_subscription_health.py --tenant=${SLUG} --hours=168
python3 ~/cos-pipeline/costs.py --tenant=${SLUG} --days=7
```

**Healthy soak looks like:**
- `failed_calls / total_calls` < 5%.
- `queue.dead_letter_count` = 0 (no permanently-stuck rows).
- `costs.py` shows zero or near-zero API spend (subscription is the
  primary path; any small spend is the optional API fallback if you
  enabled it).
- Anthropic console at console.anthropic.com/usage shows API spend
  drop sharply once subscription mode took over.

### 8. (Optional) Shift heavy work overnight

If during the soak week you saw rate-limit hits during business hours
(your interactive Claude Code use competing with pipeline fires),
consider shifting heavy routines (`tomac-cove-weekly-pipeline`,
`tomac-deal-compile`, podcast transcription, GS/Jefferies/RBN
research) to 2-6 AM per the schedule in [SUBSCRIPTION_SCHEDULE.md](SUBSCRIPTION_SCHEDULE.md).
The current schedules stay in routines.yaml unmodified; the shift is
a per-plist `StartCalendarInterval` change for each affected
LaunchAgent.

---

## Reverting to api mode

If subscription mode doesn't work for you (e.g. constant rate-limit
hits because your interactive Claude Code use saturates the window):

```bash
# 1. Edit firm_context.yaml — change "auth_mode: subscription" to "auth_mode: api"
# 2. Confirm ANTHROPIC_API_KEY is still in the keychain:
security find-generic-password -s "cos-pipeline-${SLUG}/ANTHROPIC_API_KEY" -a "$USER" -w >/dev/null && echo "key present"
# 3. (Optional) unload the subscription daemons — they'll be no-ops in api mode but still fire:
launchctl unload ~/Library/LaunchAgents/com.cos.${SLUG}.queue-drain.plist
launchctl unload ~/Library/LaunchAgents/com.cos.${SLUG}.subscription-health.plist
```

The next fire of any routine reads the new `auth_mode` and dispatches
via the anthropic SDK. No code change, no LaunchAgent reload for
the routine plists themselves.

---

## Common surprises during migration

### Day 1: dispatch.jsonl is empty

You ran preflight green and loaded the LaunchAgents, but no rows
appear in dispatch.jsonl. Most likely cause: no subscription-mode
routine has fired yet. Check the next scheduled fire from the dashboard
admin tab, or fire one manually:

```bash
launchctl kickstart -k gui/$(id -u)/com.yoni.claude-task.cos-personal-briefing
# or whichever routine you have wired
```

### Day 2: a TIME_INSENSITIVE routine returned "queued: True"

Expected behavior when the 5-hour window is exhausted. Check:

```bash
cat ~/cos-pipeline/data-${SLUG}/queue.jsonl
```

The queue-drain daemon (every 30 min) will re-fire after `queue_until`.

### Day 3: `costs.py` still shows API spend

If you have `--add-fallback-api-key` enabled, certain failure modes
fall through to api. This is by design. Check
`data-${SLUG}/dispatch.jsonl` for `outcome=failure:*` entries — every
failure that fell through to api is logged. If the failure rate is
low, that's fine; if it's high, investigate the root cause (likely
your subscription window is being saturated).

### Day 4: queue.dead.jsonl has rows

Rows landing in the dead-letter file means the same task hit 24
consecutive failures across drain attempts. Read the file:

```bash
cat ~/cos-pipeline/data-${SLUG}/queue.dead.jsonl
```

Most common reason: a routine genuinely fails for a non-rate-limit
reason (a stale auth state, a deleted Drive doc, etc.). The error_msg
field in each row tells you. After fixing the root cause, you can
manually re-fire by appending the row to queue.jsonl with `attempts: 1`
and a `queue_until` of `null`.

---

## Rollout cadence (for principals onboarding multiple tenants)

The recommended order:
1. **Canary tenant first** — pick one (non-tomac, low-stakes) and run
   the migration end-to-end. Soak 1 week.
2. **One package at a time** for subsequent tenants — flip briefing
   first, then capture, then research, then deals. Each gets a
   shorter soak (24-48h) once the canary has soaked clean.
3. **Tomac last** — the principal's primary tenant flips only after
   the canary + at least one production tenant have soaked clean,
   per the canary plan in [CSPIKE_PLAN.md](CSPIKE_PLAN.md) §
   "Production cutover sequence."

---

## Cross-references

- [INSTALL_SUBSCRIPTION.md](INSTALL_SUBSCRIPTION.md) — new-tenant install (this doc is the migration version)
- [SUBSCRIPTION_INSTALL.md](SUBSCRIPTION_INSTALL.md) — engineer-facing spec
- [SUBSCRIPTION_SCHEDULE.md](SUBSCRIPTION_SCHEDULE.md) — overnight schedule for heavy routines
- [CSPIKE_PLAN.md](CSPIKE_PLAN.md) — dispatch path decision + canary cutover sequence
- [HANDOFF_RUN6.md](HANDOFF_RUN6.md) — what shipped in the cutover work
