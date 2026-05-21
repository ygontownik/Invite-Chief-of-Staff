---
description: Make a safe update to the ~/dashboards/ tree using the PREFLIGHT framework
argument-hint: "<describe the change you want to make>"
---

# /dash — Dashboard update workflow

You are being invoked to make a change inside `~/dashboards/`. The user's
request follows below as `$ARGUMENTS`. Before doing anything, walk the
framework below. It is designed so that dashboard updates never resurrect
deleted items, never drift the shared chrome, and never lose user
preferences across syncs.

---

## Step 0 — Read the framework (ALWAYS)

Read these four files first. They are authoritative:

1. `~/dashboards/docs/CLAUDE.md` — operating rules (Operating Principles
   section has the load-bearing principles, currently 8).
2. `~/dashboards/docs/PREFLIGHT.md` — pre-merge checklist. Step 6
   contains the seven permanent checks every change walks through.
3. `~/dashboards/docs/DECISIONS.md` — non-obvious judgment calls and
   why they were made. Do not re-litigate a logged decision without
   logging the reversal.
4. `~/dashboards/config/dash_corrections.md` — dated running
   corrections from prior `/dash` sessions. Patterned on the Tomac
   Cove `investment_principles.md` learning loop. If an entry here
   contradicts items 1-3, the more recent dated entry wins until the
   older doc is updated to match. At the end of this session
   (Step 4.5), you will append any generalizable lesson learned back
   into this file.

Also glance at `~/dashboards/docs/ARCHITECTURE.md` if the change touches
a data contract or a new route.

## Step 0.5 — Check for lingering resolved points

`corrections-queue.json` is pruned automatically by the server on every
warmup cycle (`_prune_corrections_queue()` in `cos-dashboard-server.py`),
so manual cleanup is rarely needed. But do a quick scan for:

- **Ghost TODO/FIXME comments** left by a prior `/dash` session that
  reference an issue now fixed — delete the comment, don't leave markers.
- **Temporary debug code** — test endpoints, `print()` / `logging.debug`
  statements added during a prior session's investigation — remove if the
  underlying issue was resolved.

Do NOT prune `~/dashboards/config/dash_corrections.md` — that file is
append-only by design. Superseded rules get a newer dated entry, not a
deletion.

## Step 1 — Plan

- State what's changing in one paragraph.
- Identify which of the seven PREFLIGHT checks apply. Most changes touch
  at least one: tombstone respect, user-state separation, stable IDs,
  server-auth preferences, idempotent POSTs, auth tier, chrome opt-out.
- Identify which data contract is affected (none / dashboard-data /
  deal-system-data / deal-pipeline-data / cos-run-state / user-state).
- If the change adds a new preference, it MUST: (a) live under
  `data/user-state/*.json`, (b) have a stable content-derived ID via
  `window.__itemId`, (c) hydrate from a `window.__*_INITIAL__` injected
  server-side, and (d) persist through an idempotent POST endpoint.
- If the change adds or modifies a user-visible chrome string (topnav,
  shared buttons, freshness badge, route label), the string lives in
  `config/strings.yaml` and templates reference it as `{{STR:dot.path}}`.
- If the change touches `_topnav.html` or `design-system.css`, remember
  those inject into every HTML route via `_inject_shared_chrome()`.

## Step 2 — Implement

- Edit the smallest set of files. Prefer editing over creating.
- Do not duplicate strings, doc IDs, or schedules — they live in
  `config/` (strings.yaml, drive-docs.yaml, schedule.yaml).
- User dismissals and reorders go through the tombstone/order endpoints;
  do not hand-roll another persistence scheme.
- Every new HTTP route declares its auth tier (owner-only,
  partner-allowed, or localhost-only). Default-deny.
- Pause between meaningful sub-steps if the change is substantial.

## Step 3 — Verify

Run these in order. Stop on any failure:

```bash
# Syntax check the server
python3 -c "import ast; ast.parse(open('/Users/ygontownik/dashboards/app/cos-dashboard-server.py').read())"

# Bounce the server to pick up changes
launchctl kickstart -k gui/$(id -u)/com.yoni.cosdashboard

# Smoke test the dashboard (owner auth)
source ~/dashboards/scripts/load-secrets.sh
curl -s -u "owner:$OWNER_PASSWORD" -o /tmp/cos.html -w "HTTP %{http_code} size %{size_download}\n" http://localhost:7777/

# Full system verification
bash ~/dashboards/scripts/verify-system.sh
```

Expected: HTTP 200, size > 100KB, verify-system reports 0 FAIL. The two
WARNs for `calendar.renew` (periodic, between runs) and `cloudflared`
(missing cert.pem — unrelated infra issue) are known and benign to
dashboard work.

If the change touches routes, also confirm:
- Partner auth returns 403 on owner-only routes.
- Localhost-only routes (`/warmup`, `/refresh`) return 403 from non-loopback.

If the change touches the UI, manually click through the affected flow
in a browser at least once. If you can't, say so explicitly rather than
claiming success.

## Step 4 — Document

Append to `~/dashboards/docs/CHANGELOG.md` under today's date (create
the date heading if it doesn't exist). Narrative bullets, not a diff
summary — explain what changed and why.

If the change involved a non-obvious judgment call (picked approach A
over B, deviated from an existing pattern, trade-off that future-you
might question), log it in `~/dashboards/docs/DECISIONS.md` with:

- **What** was decided
- **Why** — the constraint or incident behind it
- **How to apply** — when this binds future work

## Step 4.5 — Append to dash_corrections.md if there's a lesson

Before committing, ask: **is there a generalizable rule from this
session?** A bug pattern that could recur, a prompt weakness you
caught, a file that drifted out of sync, a principle from CLAUDE.md
you almost violated, a piece of context future-you will want.

If yes — append a dated bullet to
`~/dashboards/config/dash_corrections.md` under the relevant topic
(add a new `## TOPIC —` section if none fits). One bullet = one rule.
Specific and falsifiable. The rule applies to all future `/dash`
invocations the moment it lands in that file.

If no — skip this step.

Do NOT skip this step silently. If you decide no rule is warranted,
briefly say so in the session summary so Yoni can disagree.

## Step 5 — Commit

Commit in `~/dashboards/`. Stage narrowly (specific files, not `git add -A`).
One focused commit per logical change — prefer several small commits
over one sprawling one. Commit message format:

```
<short imperative title under 70 chars>

<1-3 paragraph body explaining the why and any caveats>

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

Do not push unless Yoni explicitly asks.

---

## User request

$ARGUMENTS
