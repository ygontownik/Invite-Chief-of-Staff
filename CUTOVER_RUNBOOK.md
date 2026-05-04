# CUTOVER_RUNBOOK.md — your task list (Run 6)

This is what **you** need to do to actually finish Run 6. Everything
else has been prepared as `.next` drafts, tests, runbooks, and plist
templates. The work below is the live-state changes I cannot do per
HARD RULES.

**Total active work for you:** ~10 minutes for Phase A + ~15 minutes
to start Phase B. Phase C is a 1-week observe period.

Phase A is fully reversible. Phase B is per-tenant; you can run it
once on a canary slug and stop. Phase C is observation only.

---

## Phase A — promote `.next` drafts to live (~5 min, all-or-nothing)

### A.1 Snapshot first (1 min)

```bash
cd ~/cos-pipeline
DATE=$(date +%Y%m%d-%H%M%S)
mkdir -p .run6-cutover-bak-${DATE}
cp _firm_context.py _model_router.py _model_router_test.py costs.py \
   .run6-cutover-bak-${DATE}/
# _subscription_queue.py and _subscription_health.py don't exist yet
# (they're new, no live file to back up).
echo "snapshot at .run6-cutover-bak-${DATE}/"
```

### A.2 Promote drafts (1 min)

```bash
cd ~/cos-pipeline
cp _firm_context.py.next         _firm_context.py
cp _model_router.py.next         _model_router.py
cp _model_router_test.py.next    _model_router_test.py
cp _subscription_queue.py.next   _subscription_queue.py
cp _subscription_health.py.next  _subscription_health.py
cp costs.py.next                 costs.py
```

### A.3 Verify cutover landed clean (~3 min)

```bash
cd ~/cos-pipeline

# All tests still pass post-cutover.
for t in _model_router_test.py tests/test_*.py; do
  python3 "$t" >/dev/null 2>&1 && echo "OK $t" || echo "FAIL $t"
done
# Expect: 24 OK, 0 FAIL.

# The new test_model_router_test.py.next assertions are now live:
python3 _model_router_test.py 2>&1 | grep "TestSubscriptionDispatch" | head -3
# Expect: 'test_subscription_dispatches_via_sdk ... ok' and
#         'test_subscription_returns_zero_usd ... ok'
# (No more NotImplementedError assertion.)

# One trivial subscription smoke call (uses your Pro/Max window once).
/opt/homebrew/bin/python3 -c "
import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions
async def go():
    async for c in query(prompt='Reply with: pong',
                         options=ClaudeAgentOptions(
                             model='claude-haiku-4-5-20251001',
                             tools=[], skills=None, mcp_servers={},
                             setting_sources=[], plugins=[],
                         )):
        if type(c).__name__ == 'AssistantMessage':
            for b in getattr(c, 'content', None) or []:
                t = getattr(b, 'text', None)
                if t: print(t)
asyncio.run(go())
"
# Expect: pong

# Live state.
curl -sf http://localhost:7777/ -o /dev/null && echo "HTTP 200 OK"
launchctl list | grep -c com.yoni.cosdashboard   # expect 1
```

### A.4 Rollback (only if A.3 fails)

```bash
cd ~/cos-pipeline
cp .run6-cutover-bak-${DATE}/* ./
rm _subscription_queue.py _subscription_health.py   # these were new in cutover
echo "rolled back"
```

---

## Phase B — onboard a canary tenant in subscription mode (~15 min + Claude.ai project clicks)

Pick a slug for your canary. Use `re-dev` (already reserved per
DECISIONS.md C5) or invent a new one. Don't pick `tomac` — soak the
canary first. **You'll need ~5 minutes of clicking through claude.ai
to create 4 projects.**

### B.1 Provision the basics (api-mode for now) — ~3 min

```bash
SLUG=re-dev   # or your canary slug
cd ~/cos-pipeline
./setup.sh --instance=${SLUG} --domain=infra-pe --auth-mode=subscription
```

This will:
1. Walk the existing 8-step setup (config dir, OAuth, etc.).
2. After Step 8, **automatically** chain
   `setup.sh.subscription.next` because of `--auth-mode=subscription`.
3. Step S1–S2 verify your Claude CLI + Python ≥ 3.10 + SDK.
4. **Step S3** prints copy-pasteable system prompts for four
   projects. You open claude.ai, create each project, paste the
   prompt, set the model (Sonnet for briefing/capture/research, Opus
   for deals), grab the project ID from the URL, and paste it back.
5. Step S4 writes `auth_mode: subscription` and `claude_projects:
   {...}` into your config files.
6. Step S4.5 stages two LaunchAgent plists for review.

If you want to skip the project provisioning for now and inline-
preamble per call (v1 fallback), hit Enter at each project prompt.

### B.2 Run preflight — ~30 sec

```bash
./scripts/preflight_subscription.sh --instance=${SLUG}
```

**Must come back PREFLIGHT: N passed, 0 failed.** Yellow `!` warnings
on `claude_projects.* is blank` are OK (means inlined-preamble fallback
will fire). Red `✗` items have a printed fix command — run it and re-
preflight before continuing.

### B.3 Load the staged LaunchAgents — ~30 sec

```bash
SLUG=re-dev   # match B.1
ls ~/cos-pipeline/data-${SLUG}/staged-launchagents/
# Should list:
#   com.cos.<slug>.queue-drain.plist
#   com.cos.<slug>.subscription-health.plist

cp ~/cos-pipeline/data-${SLUG}/staged-launchagents/*.plist \
   ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.cos.${SLUG}.queue-drain.plist
launchctl load ~/Library/LaunchAgents/com.cos.${SLUG}.subscription-health.plist

# Verify they loaded:
launchctl list | grep "com.cos.${SLUG}\."
```

### B.4 One real subscription call to confirm dispatch works — ~30 sec

```bash
SLUG=re-dev
/opt/homebrew/bin/python3 -c "
import _model_router as mr
out = mr.call_claude(
    'cos-personal-briefing',
    system='You are a test assistant.',
    messages=[{'role':'user','content':'Reply with: pong'}],
    mode='subscription', tenant='${SLUG}',
)
print('TEXT:', out['text'])
print('META:', out['subscription_meta'])
print('USD:',  out['est_usd'])
"
# Expect: TEXT: pong
#         META: {'rate_limit_status': 'allowed', 'rate_limit_resets_at': ..., 'project_id': '<id-or-None>'}
#         USD: 0.0
```

Then check the dispatch ledger captured the call:

```bash
tail -1 ~/cos-pipeline/data-${SLUG}/dispatch.jsonl | python3 -m json.tool
# Expect: outcome: "ok", model, ts, etc.
```

### B.5 Rollback (only if B.4 fails)

```bash
SLUG=re-dev
launchctl unload ~/Library/LaunchAgents/com.cos.${SLUG}.queue-drain.plist
launchctl unload ~/Library/LaunchAgents/com.cos.${SLUG}.subscription-health.plist
rm ~/Library/LaunchAgents/com.cos.${SLUG}.queue-drain.plist
rm ~/Library/LaunchAgents/com.cos.${SLUG}.subscription-health.plist

# Flip auth_mode back to api so any future fires don't try subscription.
sed -i.bak 's/^auth_mode: subscription/auth_mode: api/' \
   ~/cos-pipeline-config-${SLUG}/firm_context.yaml

# Or full uninstall (slug-isolated):
./setup.sh --instance=${SLUG} --uninstall
```

---

## Phase C — observe for 1 week, then decide next steps

### C.1 Daily check (~30 sec)

```bash
SLUG=re-dev
python3 ~/cos-pipeline/_subscription_health.py --tenant=${SLUG} --hours=24
python3 ~/cos-pipeline/costs.py --tenant=${SLUG} --days=1
```

**Healthy looks like:**
- `failed_calls / total_calls` < 5%.
- `queue.dead_letter_count` = 0.
- API spend (from `costs.py`) is zero or very low.
- `latest_rate_limit_status` is `allowed` (not `exceeded`) most of the time.

**Unhealthy looks like:**
- Failures climbing. Check `data-${SLUG}/dispatch.jsonl` for the
  `error_msg` field on failure rows.
- Queue depth growing day over day (rows arriving faster than
  draining). The subscription window is saturated.
- Dead-letter count > 0. Same task hit 24 retries — investigate root
  cause from `data-${SLUG}/queue.dead.jsonl` :: `last_error_msg`.

### C.2 At end of week 1 — go / no-go decision

**Go criteria** (proceed to Phase D — roll forward):
- Cumulative failure rate < 5% over the week.
- No dead-letter rows.
- API spend on the canary stayed near $0 (no unintended fallback).
- You're satisfied with the rate-limit pattern (no business-hours
  saturation that hurt your interactive Claude Code use).

**No-go criteria** (roll back to api-mode for canary, debug):
- Persistent dead-letter rows.
- API spend showed up unexpectedly (means subscription failed and
  fell through to api fallback unbeknownst to you).
- Window saturation hurt your interactive use.

To roll back the canary to api mode:

```bash
SLUG=re-dev
sed -i.bak 's/^auth_mode: subscription/auth_mode: api/' \
   ~/cos-pipeline-config-${SLUG}/firm_context.yaml
launchctl unload ~/Library/LaunchAgents/com.cos.${SLUG}.queue-drain.plist
launchctl unload ~/Library/LaunchAgents/com.cos.${SLUG}.subscription-health.plist
# Make sure ANTHROPIC_API_KEY is in the keychain for that tenant.
security find-generic-password -s "cos-pipeline-${SLUG}/ANTHROPIC_API_KEY" -a "$USER" -w >/dev/null
```

---

## Phase D (optional, only after Phase C goes green) — flip tomac

Tomac is your primary tenant — flip it last. Follow [MIGRATE_TO_SUBSCRIPTION.md](MIGRATE_TO_SUBSCRIPTION.md)
end-to-end. Same pattern as the canary, except the rollback insurance
matters more (your daily briefing depends on this working).

```bash
# At end of canary week, when ready:
SLUG=tomac
# Snapshot config first (already done in MIGRATE_TO_SUBSCRIPTION.md step 1).
./setup.sh.subscription.next --instance=${SLUG}
./scripts/preflight_subscription.sh --instance=${SLUG}
# Load LaunchAgents per B.3.
# Run one subscription call per B.4.
# Watch dispatch.jsonl for the next scheduled fire.
```

---

## Things you do NOT need to do (already shipped or out of scope)

- ✗ Edit any `_*.py.next` file — they're ready to `cp`.
- ✗ Edit any `setup.sh` file — `setup.sh.next` already wires
  `--auth-mode=` and chains the subscription installer.
- ✗ Provision Claude.ai projects via API — Anthropic doesn't expose
  one yet. Manual UI provisioning is the only path. Setup walks you
  through it.
- ✗ Probe 4 (recovery semantics) — surfaces during canary's first
  real rate-limit hit. You don't need to engineer it.
- ✗ Probe 5 (project targeting) — blocked on the SDK exposing a
  project field. Telemetry-only today.

---

## If you get stuck

| Symptom | Where to look | Fix |
|---|---|---|
| Cutover A.3 tests fail | output of failing test | rollback per A.4, then read the test failure carefully — a single regression is easier to debug than five at once |
| Preflight B.2 red ✗ | preflight prints suggested fix command | run that command; re-preflight |
| B.4 subscription call hangs | `claude` CLI auth state | run `claude` in another terminal, confirm Pro/Max banner shows; if not, `claude login` |
| `claude_agent_sdk import failed` | Python version | `which python3` returns 3.9; use `/opt/homebrew/bin/python3 -m pip install --break-system-packages claude-agent-sdk` |
| dispatch.jsonl never grows | LaunchAgents not loaded or routine not firing | `launchctl list \| grep com.cos.${SLUG}.` shows what's loaded; manually fire: `launchctl kickstart -k gui/$(id -u)/com.yoni.claude-task.cos-personal-briefing` |
| queue.jsonl growing | window saturated | check `_subscription_health.py --tenant=${SLUG}`; `latest_rate_limit_status=exceeded` confirms; either wait for reset or shift schedule overnight per [SUBSCRIPTION_SCHEDULE.md](SUBSCRIPTION_SCHEDULE.md) |
| Dashboard shows nothing about subscription | tile not implemented yet | use CLI: `python3 _subscription_health.py --tenant=${SLUG}` (dashboard tile is a v2 follow-on; CLI gives the same data) |

---

## Cross-references

- [HANDOFF_RUN6.md](HANDOFF_RUN6.md) — full inventory of what shipped (engineer-facing)
- [INSTALL_SUBSCRIPTION.md](INSTALL_SUBSCRIPTION.md) — tenant-facing 8-step install
- [MIGRATE_TO_SUBSCRIPTION.md](MIGRATE_TO_SUBSCRIPTION.md) — existing-tenant migration
- [CSPIKE_PLAN.md](CSPIKE_PLAN.md) §"Production cutover sequence" — the original plan; §1–8 now annotated SHIPPED/PENDING
- [SUBSCRIPTION_SCHEDULE.md](SUBSCRIPTION_SCHEDULE.md) — overnight schedule shifts if needed
