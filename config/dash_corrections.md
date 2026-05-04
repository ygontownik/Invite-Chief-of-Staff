# /dash — Running Corrections

Authoritative reference for lessons learned during `/dash` sessions.
**Read before planning any change inside `~/dashboards/`.**

Mirrors the pattern used by
`~/tomac-cove-pipeline/config/investment_principles.md` for the Tomac
Cove Pass-2 analyst skill: a dated, append-only log of specific,
falsifiable corrections that apply to all future `/dash` invocations.

---

## HOW TO USE THIS FILE

- **Step 0 of /dash reads it** alongside `CLAUDE.md`, `PREFLIGHT.md`,
  and `DECISIONS.md`. If an entry here contradicts those files, the
  more recent dated entry wins until the older doc is updated to match.
- **Append on every correction** — whenever Yoni pushes back, a filter
  reveals an upstream bug, or a session uncovers a repeatable pattern,
  write it down here before closing the session. Don't let the lesson
  live only in CHANGELOG prose.
- **Dated bullets, grouped by topic.** One bullet = one rule. Each
  bullet must be specific and falsifiable. "Be careful with X" is not
  a rule; "X requires Y because Z" is.
- **Never delete entries.** If a rule is superseded, add a new dated
  entry that explicitly supersedes the older one.

---

## TOPIC — LLM EXTRACTION PROMPTS

### 2026-04-21 — Fix the source, not just the symptom

When a post-hoc filter is added to the compile layer in
`app/cos-dashboard-fetch.py` (regex denylist, dedupe table, content
suppression rule), the upstream LLM extraction prompt that produced
the garbage must be updated in the same pass. The two places to edit:

- `routines/process/cos_otter_backfill.py` — `BACKFILL_PREAMBLE`
  (transcript extraction, Pass 2 Sonnet).
- `routines/process/cos_email_backfill.py` — extraction system prompt
  near the top of the file.

A filter without a corresponding prompt fix means the garbage keeps
coming and the filter list keeps growing. This is codified as
Operating Principle #8 in `docs/CLAUDE.md`.

### 2026-04-21 — Actions require mutual acceptance

One-sided offers are not actions. If one party on a call offers
something ("I could ping my ex-colleague on the Oncor board…") and
the counterparty does NOT affirmatively accept within the transcript,
do not emit it as an `action_item`. This rule lives in both extractor
preambles. Incident: the 2026-04-21 Cholla/Gideon call produced a
fabricated "Ping ex-colleague on Oncor board" action that Yoni had
offered but Gideon never accepted.

---

## TOPIC — DEAL IDENTITY / COUNTERPARTY ALIASES

### 2026-04-21 — `_CP_ALIASES` is the single source of truth

Different spellings of the same deal ("Chisholm / CHolla — Gideon
Powell", "Big South Dallas", "Cholla Petro") must collapse to one
canonical name so the auto-promoter doesn't create parallel deal rows
and the Awaiting External UI doesn't show the same deal twice.
The alias table lives in two mirrored places and MUST be kept in sync:

- `app/cos-dashboard-fetch.py` — `_CP_ALIASES` (tuple).
- `app/templates/cos-dashboard.html` — `__CP_ALIASES` (const).

When adding a new deal to the Tomac pipeline that has known alternate
spellings, add aliases to both tables in the same commit.

---

## TOPIC — FRESHNESS / SIGNAL RANKING

### 2026-04-21 — Recency beats due date for "next step" overlay

`_overlay_freshest_signal` in `app/cos-dashboard-fetch.py` must sort
candidate actions by (presence of `addedDate`, most-recent `addedDate`,
fundraising-keyword weight, earliest `due` as final tiebreak). Sorting
primarily by `due` lets an old overdue scheduling reminder (e.g. a
ranch-visit proposal) mask today's fundraising-focus action captured
from a fresh call. If the sort logic is ever simplified, that bug
returns.

Fundraising keywords that should score UP: FEA, LC, teaser, CIM, term
sheet, refund, milestone, Oncor, Phase 2, tcip, bridge, anchor, raise,
post, IRA, credit, structure, PCLR, capital.

Scheduling keywords that should score DOWN: ranch visit, schedule,
reschedule, meeting, in-person, dates, coordinate, confirm via.

---

## TOPIC — PROCESS / SESSION HYGIENE

### 2026-04-21 — Every session appends here if there's a lesson

If a `/dash` session surfaces any repeatable correction — a bug
pattern, a prompt weakness, a file that drifts out of sync, a
principle you caught yourself violating — write it here before
committing. Specifically: after Step 4 (CHANGELOG) and before Step 5
(commit), ask "is there a generalizable rule here?" — if yes, append
a dated bullet under the relevant topic. If no existing topic fits,
add a new `## TOPIC —` section at the bottom.

---

## TOPIC — BUTTON / ENDPOINT CONTRACTS

### 2026-04-27 — Tooltip text is a contract; verify it against code before shipping

`strings.yaml` button tooltips describe what a button actually does. Before
adding a feature to a button's code path, check whether the existing tooltip
already claims that feature — if it does and the code doesn't back it up, that
is a bug (misleading UI), not a feature gap. In this session: the Pull Fresh
Data button tooltip said "Otter + Gmail + Calendar" but the handler never
called `cos_otter_backfill.py`. The fix was both code (add Otter to the
handler) and strings (correct the tooltip). Always update both together.

### 2026-04-27 — Dead helper functions require a call-site fix, not a comment

`get_unprocessed_transcripts()` in `cos-dashboard-fetch.py` was defined,
documented, and never called — `unprocessed_transcripts` was hardcoded to `[]`.
The function appeared correct in isolation; the bug was invisible without
tracing the call graph. Before treating a helper as "working," verify it is
actually invoked in `main()` or equivalent. A function with no callers in
production is dead code regardless of how complete it looks.

---

## TOPIC — PYTHON RUNTIME COMPATIBILITY

### 2026-04-28 — All routines invoked by `/usr/bin/python3` (3.9) require `from __future__ import annotations`

The launchd runner scripts (`scripts/otter-backfill-runner.sh` and friends) explicitly use `/usr/bin/python3` which lands on macOS-shipped Python 3.9. Any `.py` file they invoke directly OR transitively import that uses PEP 604 union syntax (`X | None`, `list[dict] | None`, etc.) at module level OR in function signatures will crash at import time with `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`.

**Rule**: every file in `routines/` (especially `routines/process/`) that uses `X | None` or similar PEP 604 syntax MUST have `from __future__ import annotations` as its first import. The dashboard server itself runs on `/opt/homebrew/bin/python3` (3.14) so server-only code is safe — but the moment a `routines/` module imports a server-side helper, the whole transitive closure must be 3.9-compatible.

**Detection**: `grep -lE ': [A-Z][A-Za-z]* \| None| -> [A-Z][A-Za-z]* \| None' routines/**/*.py` then check each match for `from __future__ import annotations`. Audit performed 2026-04-28: only `cos_otter_backfill.py` was missing it (causing 4+ hours of silent pipeline failure 2026-04-27 17:12–17:58 ET). All other files in `routines/process/` already had the future import.

**Why this surfaced silently**: the runner script logged the TypeError to `~/dashboards/logs/otter-backfill.log` but no dashboard surface flagged the failed runs. The freshness chip added in this same commit closes that observability gap.

---

## TOPIC — AWAITING EXTERNAL / LOGIC CHECKS

### 2026-05-01 — Passed due dates are signals of completion, not persistence

When reviewing `awaitingExternal` items during a `/dash` session, treat a passed due date as evidence that the underlying event likely already happened — a call invite for Apr 18 should be dismissed by May 1, not left as an "overdue" reminder. Apply this logic before touching any item:

1. Is the due date > 2 weeks in the past? Default: dismiss unless there is explicit evidence the underlying action is still open.
2. Is the item a meeting invite / scheduling confirm? If the date passed, the meeting happened (or was missed). Dismiss.
3. Is the item a "propose times" or "confirm calendar" action? If the date passed, dismiss — this kind of prompt has zero value after the window closes.

**Do not leave past-due scheduling artifacts in the list** — they inflate the count, mask genuinely open items, and signal to the user that the pipeline is not thinking about context. When in doubt, dismiss and let the capture pipeline re-surface anything that is still live.

### 2026-05-01 — Read compiled data before asking the user for deal status

Before updating `config/tomac-config.yaml`, `config/recruit-config.yaml`, or any other manually-curated config, ALWAYS read:

1. `data/compiled/deal-system-data.json` — deal health, thesis scores, actions, milestones, activity log
2. `data/compiled/dashboard-data.json` → `followUps[]` and `awaitingExternal[]` filtered for deal names

These files are the output of the capture pipeline and contain extracted intelligence from call transcripts, emails, and Otter recordings. Status updates, deal sequencing decisions, resolved actions, and new counterparties will be in here before the user tells you. Asking the user verbally for information that is already in the compiled data is a workflow failure.

**How to apply:** In every `/dash` session that touches deal or recruiting config, run the Python read commands above before Step 1 (Plan). If a deal update is needed, derive it from the data first — only ask the user to fill in what the data cannot answer.

### 2026-05-01 — `/item/delete` requires both `id` and `source` fields

The `/item/delete` endpoint rejects requests missing `source`. Always POST `{"id": "<id>", "source": "awaitingExternal"}` (or the appropriate source for the section). The error message "id and source required" is the signal.

---

## TOPIC — DASHBOARD-DATA.JSON SCHEMA HYGIENE

### 2026-04-28 — Display-only fields must not be persisted; recompute at serve time

`generatedAt` was persisting as a human-formatted string ("Tue Apr 14 2026 · Updated 4:36p") in `dashboard-data.json` because `cos-dashboard-fetch.py` did `merged = {**state, **live_data}` and the prior `state[generatedAt]` survived every refresh. Server and refresh paths recompute `generatedAt` fresh from `fetchedAt` on every serve — but the persisted stale string sat in the file for 13 days, visible to anything reading `dashboard-data.json` directly.

**Rule**: any field whose value is computed for display (timestamp formatting, age labels, "X minutes ago" strings) belongs only in the served HTML, never in the persisted JSON. The producer must explicitly `merged.pop('display_field', None)` before write. Persist only the canonical underlying datum (e.g. `fetchedAt` ISO string), and let consumers format.

**Detection rule of thumb**: if two paths compute the same field and only one persists it, that's the smell. Audit producer code paths whenever a UI shows an unexpectedly stale timestamp.

---

## TOPIC — OTTER / TRANSCRIPT PIPELINE

### 2026-04-21 — One call = one Google Doc

Otter (via Zapier) routinely drops the same call into Drive 2-4 times:
Zapier double-fires, and Otter separately exports a `.txt` full-
transcript alongside the Google Doc. Downstream effects: the dashboard
processes duplicates, the user sees 3+ copies in the Tomac/Recruiting/
Other folder, and the `.txt` is sometimes kept as the canonical copy
when a Google Doc is preferred.

Rule for all future Otter ingestion:

- `routines/process/cos_otter_backfill.py` runs
  `consolidate_transcript_siblings(...)` at the top of every Otter
  folder scan, BEFORE the dedup-tracker check. Siblings are grouped by
  `_transcript_identity_key(filename)` (date + normalized title —
  strips `_Otter_Ai`, extensions, decorative dashes, collapses
  whitespace). For groups with >1 file: pick the richest body, ensure
  it is a Google Doc (copy-convert `.txt` to Doc if richest is .txt),
  `drive_trash()` the siblings, and record `consolidated_into` on the
  trashed tracker entries.
- Trash is always reversible (`{"trashed":true}` PATCH) — never
  `files.delete`. Consolidation must be recoverable.
- Date is part of the identity key — two calls on different dates with
  the same title are NOT merged.
- If a sibling had already been processed (actions extracted, follow-
  ups written), the survivor inherits that processed state so we don't
  double-extract.

---

## TOPIC — SECRETS / ENVIRONMENT LOADING

### 2026-04-27 — `load-secrets.sh` overwrites env vars; never source it when testing a running daemon's secret

`~/dashboards/scripts/load-secrets.sh` re-exports env vars by pulling from
macOS Keychain. If the Keychain entry for a secret (e.g. `WEBHOOK_SECRET`)
differs from the value in `.zshrc` or a launchd plist, sourcing
`load-secrets.sh` silently replaces the shell var. Testing a daemon endpoint
that validates a secret will fail if you sourced `load-secrets.sh` first.

Rule: when smoke-testing daemon endpoints with secrets, use the hardcoded
plist value directly (or `grep -A2` the running plist) rather than
`source load-secrets.sh`. The daemon reads its secret from the plist
`EnvironmentVariables` block — not from `.zshrc` or Keychain at runtime.

---

## TOPIC — EXTRACTION / ROUTING

### 2026-04-27 — action_items[].dashboard_path requires post-extract backfill

Sonnet reliably fills `envelope_items[].dashboard_path` but often returns `""`
for `action_items[].dashboard_path` even when the same call's envelope items
have correct paths. Two-layer defense required in `cos_otter_backfill.py`:

1. `_backfill_action_dashboard_paths()` — called before `write_followups()`.
   Fills empty paths by cross-referencing envelope_items (content match first,
   then best deal/LP path), then tomac_intel paths, then category-based default.
2. `write_followups()` fallback: use `get("dashboard_path") or default`, not
   `get("dashboard_path", default)` — the dict.get default only fires on missing
   keys, not empty strings.

Prompt reinforcement alone is insufficient — always pair with post-extract code.

---

## TOPIC — KEYWORD MATCHING / CLASSIFICATION

### 2026-04-27 — Theme tagging: curated keywords + dynamic tokens, never curated-only

`tag_origination_items()` uses three passes: (1) dashboard_path substring,
(2) curated keyword map, (3) dynamic tokens from live theme data. Pass 3 is
the critical one — it ensures new themes added to `deal-pipeline-data.json`
are matched without any code edit. Curated-only classifiers drift silently
when new categories appear: items accumulate in "unmatched" until someone
notices and updates the code. Always pair a curated map with a dynamic
fallback derived from the actual category metadata. The pattern is:
`_extract_theme_name_tokens(theme)` → significant words from name + proper
nouns from thesis text.

---

## TOPIC — HTML / JS EDITING IN DEAL DASHBOARD

### 2026-04-27 — Use Python line-list rewrite for multi-line JS insertions; never `sed -i '' Nc`

The deal dashboard JS is minified-style: long logical expressions split across
indented continuation lines. `sed -i '' '<N>a\...'` inserts after a *line*
boundary but cannot replace a logical block that spans multiple lines. If the
insertion point is inside a JS expression (e.g. between two `createElement`
arguments), `sed` splits the expression and produces a syntax error that is
invisible until the browser console is checked.

Rule: for any multi-line block replacement or insertion in `deal-dashboard.html`
or `cos-dashboard.html`, use a Python script that operates on `lines[]` by index.
Read the target line numbers with `sed -n '<N>p'` first, confirm the exact content,
then rewrite the slice. Never use `sed -i '' '<N>a\` for JS edits.

---

## TOPIC — INFORMATION ARCHITECTURE

### 2026-04-28 — Tab-context separation: Status = TC Cove ops, Personal = job/personal

Status tab (/) is now scoped to Tomac Cove operations: TC overview, fundraising, team
actions, TC awaiting external, TC todos. Personal tab (/personal/) is scoped to
recruiting and personal life: job search, personal actions, recruiting/TC awaiting external.

**Rule:** When adding a new card or section, assign it to Status if it's about TC Cove
operations (deals, fundraising, LP, team accountability); assign it to Personal if it's
about Yoni's recruiting or personal obligations. Do NOT put the same item on both tabs.
If an item spans both contexts, the "where does Yoni act on it?" test resolves placement.

### 2026-04-28 — `render()` is the right place for tab-conditional render-ORDER changes

To change what renders before what on a given tab (e.g. panels above the Topics watchlist
on Personal), inject the conditional block directly in the `render()` function's
`innerHTML` template string — not inside `buildDailyLayout`. This keeps layout order
decisions at one level of abstraction, avoids passing extra flags through the call chain,
and makes it obvious which tab gets which order by reading `render()` alone.

### 2026-04-27 — Synopsis cards must point to a canonical home, not re-render content

When two surfaces would render the same content (calls, calendar, themes, follow-ups, deal flash, market briefing), pick one canonical home and reduce the other to a synopsis card with a visible "→" link. Reject any new card/tab/section that renders content already rendered elsewhere at similar fidelity. The "where does the user act on it?" test resolves ambiguity: that's the home; everything else is a pointer. Codified in `docs/DECISIONS.md` (2026-04-27).

Pre-audit, 8 content blocks lived in 2–4 surfaces each. The Wave 1 + 2 cleanup eliminated 6 duplicates (Briefing > Day/Follow-ups/Conf Calls/portfolio-stats hero, Status > Themes Synopsis/Intel Card, Admin > Processed transcripts). Remaining: TC Pipeline > CoS Briefing duplicates `/briefing/` > Deal Flash across two codebases (React vs Python) — queued as Wave 3.

---

## TOPIC — WEBHOOK ARCHITECTURE

### 2026-04-27 — Use existing public tunnel; don't add a second one

The dashboard server (port 7777) is not public. When a GAS or Zapier webhook
needs to reach it, proxy through the existing ngrok URL (port 8765,
call_scheduler). Add a new route to `call_scheduler.py`'s `do_POST`, before
the HMAC block. This avoids a second ngrok process or cloudflared ingress.

The GAS secret uses a simpler `X-Otter-Secret` header check (not HMAC)
because the body is JSON from GAS (not a calendar push), and constant-time
comparison on the raw secret is sufficient. Don't use the HMAC path for new
endpoints unless the caller can compute HMAC signatures.

---

## TOPIC — LLM CLASSIFICATION / QUALITY GATES

### 2026-04-27 — Keyword matching requires a Haiku confirmation gate; never ship keyword-only signals

Keyword matching against free-text origination items (content, context, counterparty) produces
false positives at a high rate even with named-asset-specific terms. Company names and asset
names appear in unrelated transcripts (e.g. "ArcLight" in a fundraising call not about MISO
power). A Haiku confirmation pass — one call per item with any matches — is required at refresh
time to confirm each tag is substantive.

Pattern for all future signal-classification features in `deal-dashboard-refresh.py`:

1. Keyword pass runs first (fast, no API cost) to narrow the candidate set.
2. `_classify_signals_haiku()` confirmation pass removes false positives.
3. Both layers are required; neither alone is sufficient.
4. Gate behind a `CLASSIFY_SIGNALS = True/False` flag so it can be bypassed for local testing.
5. Graceful degradation on API failure: keep keyword-matched tags, print WARNING, continue.

Incident: first iteration shipped 61/110 tagged with broad keywords. After narrowing keywords
to named-asset terms only (49/110) then adding Haiku confirmation (25/110 final), 24 false
positives were eliminated. The user's explicit feedback: "you need to have someone read each
of these to actually confirm it makes sense."

### 2026-04-27 — ANTHROPIC_API_KEY is in .zshrc, not loaded by bare shell; source before running

`deal-dashboard-refresh.py --classify` calls `os.environ.get('ANTHROPIC_API_KEY')`. Running
the script from a bare bash subshell (as the Bash tool does) doesn't inherit `.zshrc` exports.
The launchd daemon has the key in its plist `EnvironmentVariables` block and will work correctly
at runtime. When testing the Haiku pass manually: `source ~/.zshrc && python3 app/deal-dashboard-refresh.py`.
Do not confuse auth failures in the Bash tool with production failures.

## TOPIC — SERVER-SIDE TOMBSTONE FILTERING

### 2026-04-28 — _load_personal_items() must check deletions.json, not just email-resolutions.json

`_load_personal_items()` in `cos-dashboard-server.py` previously only filtered items
against `email-resolutions.json`. Items dismissed client-side via the tombstone endpoint
(which writes to `deletions.json`) would return on every pipeline regen because the server
never loaded those tombstone IDs. The fix: after checking email-resolutions, also compute
`_djb2('recruit|personal_items|' + name)` and skip any item whose tombstone hash is in
`_deleted_ids()`. This matches the exact key format the client-side `__isDel()` uses.

Rule: whenever a new item source is added to `personal-items.json` or any other server-loaded
user-state file, verify that `_load_personal_items()` (or its equivalent) checks
`_deleted_ids()` with the same key the client would use for that item type.

## TOPIC — SERVER STARTUP / MODULE INTEGRITY

### 2026-05-01 — Every module-level name reference must have a corresponding import; no silent _fc stubs

`cos-dashboard-server.py` crashed on startup for ~2 days because `_fc.find_firm_config()` and
`_fc.load_active_packages()` were called at module scope with `_fc` never imported. LaunchAgent
restarted the server to failure repeatedly; the dashboard showed stale data (50h badge) with no
obvious error surface until `/tmp/cos-dashboard.log` was checked directly.

**Rule**: any helper module referenced as `_fc`, `_ctx`, or similar must have an explicit import
at the top of the file. Verify with `grep -n "^_.*=\|^from\|^import" app/cos-dashboard-server.py`
before committing. Module-scope calls (outside a function) are especially dangerous because they
crash before the server can bind to port 7777, leaving the port dark with no 5xx to observe.

**Detection**: if the dashboard shows stale data and the port is dark (curl returns `exit 7`),
check `/tmp/cos-dashboard.log` first — launchd logs startup crashes there.

---

## TOPIC — DATA RENDERING COMPLETENESS

### 2026-04-28 — Filter conditions on TOMAC_CONFIG rows must use combined (myAction || nextStep)

`buildTeamActionsCard()` originally filtered `TOMAC_CONFIG` items to `myAction` non-empty only.
Team members (Mark, Nik) who have meaningful `nextStep` but an empty `myAction` were silently
dropped. Rule: whenever filtering action-carrying items from TOMAC_CONFIG, use
`(t.myAction && t.myAction.trim()) || (t.nextStep && t.nextStep.trim())` as the non-empty test,
then render `t.myAction || t.nextStep` as the action text. Never filter on a single optional
field when two fields serve the same purpose for different workflows.

---

## TOPIC — TOMAC_CONFIG / FUNDRAISING ENTRIES

### 2026-05-01 — Set `group` and `sourcedBy` at entry creation, not retroactively

`capitalRaisingAdvisors` entries in `tomac-config.yaml` require two fields that are easy to omit on first write and then forgotten until a UI restructure forces the audit:

- `group`: classification (`placement` | `gp_seed`) — drives which sub-section the row appears in. Omitting defaults to `placement`, which is wrong for GP-stake relationships.
- `sourcedBy`: name of the person who made the intro (if any). Omitting loses the attribution and makes it impossible to know who to chase if the intro stalls.

**Rule**: when adding any new `capitalRaisingAdvisors` entry, set both fields before committing. If the entry was sourced through a third party (e.g. Tanmay Kumar introduced Wafra and Piper Sandler), `sourcedBy` is required — do not treat it as optional.

**How to detect missing values**: `grep -A20 'capitalRaisingAdvisors' config/tomac-config.yaml | grep -v 'group:\|sourcedBy:'` will show entries missing either field. Run this check whenever adding new capital advisor entries.

---

## TOPIC — PROVENANCE / TRACEABILITY

### 2026-05-01 — `source_ref` exists on all pipeline items; top-level `source`/`addedDate` must be promoted from it

Every envelope item written by `cos_otter_backfill.py` and `cos_email_backfill.py` carries a `source_ref` dict with `{type, title, doc_url, date}`. However, the top-level `source`, `addedDate`, and `workstream` fields on `awaitingExternal` items were never populated — leaving 101/101 items fully untraceable in compiled JSON.

**Rule**: any compile step that builds or merges `awaitingExternal[]` must call `_promote_source_ref()` (defined in `cos-dashboard-fetch.py`) to populate top-level fields from `source_ref`. Never leave `source` and `addedDate` as null when `source_ref` is present.

**Detection**: `python3 -c "import json; ae=json.load(open('data/compiled/dashboard-data.json'))['awaitingExternal']; print(sum(1 for i in ae if not i.get('source')), 'untraceable items')"` — should be 0 after every compile run.

### 2026-05-01 — Email extraction: counterparty is the deal contact, not the From: header party

`cos_email_backfill.py` was attributing awaiting_external items to whoever appeared in the From: header, even when that person was a facilitating banker CC'd on a thread about a different deal. Pattern: 26 Pacific Fleet items attributed to "Lee / Piper Sandler Syndication" because Lee was CC'd on the "Next Steps" thread.

**Rule**: counterparty = the firm/person whose commitment is being tracked, not the email sender. When the thread subject is about a named deal in pipeline context, counterparty must reference that deal's primary contact. LP names must never appear as counterparty on deal operational items.

**Rule**: per-thread deduplication — same commitment across multiple messages in one thread = one item (most recent/specific version). Prompt now enforces this explicitly. Without it, a 10-message thread generates 10 paraphrases of the same action.

### 2026-05-01 — `_CP_ALIASES` must include common misspellings at entry creation

`chola` (one-l spelling of Cholla) was missing from `_CP_ALIASES`, causing `Chola — Robert Wittmeyer` to appear as a separate cluster in the UI. Rule: when adding a deal alias, also add common misspellings and short-form variants (`pfs` for Pacific Fleet, `thunderhead` for any Thunderhead variant). Both `_CP_ALIASES` (Python) and `__CP_ALIASES` (JS) must be updated together in the same commit.

### 2026-05-01 — Filter `Sourcing / Auto` from the active-deals prompt block

`load_pipeline_context()` in `cos_otter_backfill.py` builds an "ACTIVE DEALS" block from `dashboard-data.json → tomac[]`. That array includes auto-promoted counterparty entries staged as `Sourcing / Auto` — counterparty names extracted from awaitingExternal items, not real deals. Without the filter, the model sees `assistant`, `jkechejian@gmail.com`, `anonymous author`, etc. as active TC deals and mis-routes transcript items against them.

**Rule**: any code path that reads `tomac[]` from `dashboard-data.json` for prompt injection MUST filter out `stage == "Sourcing / Auto"` entries. Real deal stages are explicit labels set in deal config (e.g. "Diligence", "Sourcing", "Active Diligence"). Auto-staged counterparties are noise.

**Detection**: if the active-deals prompt block has more than ~10 entries, check for `Sourcing / Auto` leakage: `python3 -c "import json; [print(i.get('stage'), i.get('name')) for i in json.load(open('data/compiled/dashboard-data.json'))['tomac'] if i.get('stage')=='Sourcing / Auto']"`

### 2026-05-01 — Strip Otter filename padding at the point of origin, not only at display time

`cos_otter_backfill.py` previously wrote raw Otter filenames (with `───` padding and `_otter_ai` suffixes) directly into `source_ref.title`. This propagated to every downstream consumer. **Rule**: normalize `source_ref.title` at write time with `re.sub(r'^─+\s*|\s*─+$', '', file_name).strip()` before storing. The JS `_cleanSrcTitle()` is a second defense for pre-existing items only — it should never be the primary fix.

### 2026-05-01 — Stale one-time-event items require a two-layer filter

The EXIM Bank conference registration stayed on the dashboard after the conference date passed because neither the extraction prompt nor the compile step rejected it. The fix requires BOTH layers:

**Layer 1 (extraction / write time)**: Add a `STALENESS FILTER` rule block to every extraction prompt (`EMAIL_PREAMBLE`, `_BACKFILL_BODY`). The LLM must not emit `awaiting_external` items for conferences/summits/registrations/RSVPs/scheduling proposals where the event date is already past as of TODAY. This prevents the item from ever entering the pipeline.

**Layer 2 (compile / read time)**: `_auto_expire_stale_events()` in `cos-dashboard-fetch.py` — applied after `_promote_source_ref()` in the `awaitingExternal` pipeline. Regex matches event/scheduling patterns; if `due` or `addedDate` is >7 days in the past, the item is dropped and logged to stderr. This catches items that pre-date the prompt rules or slipped through.

**Rule**: any item about a one-time event (not a recurring relationship action) MUST have temporal context to remain valid. An item with no date context survives; an item with a clearly past date and one-time-event content is automatically retired.

### 2026-05-01 — [RESOLVED] items must be filtered at every point in the pipeline

`[RESOLVED]` is a tag the Follow-ups doc uses to mark completed actions. It surfaced as deal card `nextStep` in three separate ways: (1) in `matched_fus` (followup rows), (2) in `aw_all` (awaiting_external items), (3) in `nextStepDoc` (doc-parsed static field). Each must be filtered independently.

**Rule**: Any code path that selects `best_action` or `best_await` for deal card display MUST filter `[RESOLVED]` from the candidate list before the sort/min step — not after. The static `nextStep` from doc parsing must also be cleared if it starts with `[RESOLVED]` before the overlay writes to it.

### 2026-05-01 — `best_await` must prefer open items over overdue items

`min(aw, key=due)` picks the most-overdue item when there are both past and future items. This caused Apr 23 items to win over May 7 items for Cholla — showing outdated scheduling actions instead of current deal progress.

**Rule**: Before selecting `best_await`, split `aw_all` into `aw_open` (due >= today) and use `aw_open` if non-empty. Fall back to `aw_all` only if no open items exist. This ensures a future-dated commitment beats an overdue one when both are candidates.

### 2026-05-01 — Staleness patterns must be explicit about scheduling surface forms

"Text Yoni schedule for next week to confirm in-person meeting" did not match the original `schedule\s+(a\s+)?(call|meeting|time|intro)` pattern because "schedule" is used as a noun here, not a verb followed by a meeting type. Ranch visits, site visits, and calendar invite sends are also scheduling one-time events. Pattern must be maintained as an exhaustive list of natural-language forms, not just the most obvious verb forms.

## TOPIC — CSS / LAYOUT

### 2026-05-04 — CSS Grid 1fr blowout requires min-width: 0 on children

`grid-template-columns: 1fr 1fr 1fr` does NOT guarantee equal-width columns
when child elements contain long unbreakable strings. CSS Grid cells default
to `min-width: auto`, which lets a cell expand beyond its `1fr` share to fit
content. To enforce equal columns, add `min-width: 0` to every direct child
of the grid container (or to the class applied to those children).
`overflow-wrap: break-word` pairs with this to wrap long strings gracefully.
Pattern: `.grid-container > * { min-width: 0; overflow-wrap: break-word; }`.

### 2026-05-04 — Freshness badge ≠ page-content freshness without a reload signal

The freshness heartbeat polls `/cache-status` and updates the badge to reflect
the server's current `fetchedAt`. But the page's `DATA.*` payload is baked in
at serve time — background warmups update the file on disk but don't push new
content to open tabs. Without a reload signal, the badge can say "Fresh 10:00"
while the content still reflects the 8:00 load. This is the root cause of "not
showing fully updated."

**Rule**: any page that embeds data at serve time must inject the served-data
timestamp as `window.__PAGE_FETCHED_AT__`. The freshness heartbeat must compare
this against the server's live `fetchedAt` and show a visible reload prompt when
they diverge. Never let the freshness badge imply content currency it cannot
guarantee.

### 2026-05-04 — Holistic duplication audit before serve

The dashboard renders the same deal in multiple sections when section assignment is computed from independent sources (e.g. Live Deals reads `data/deals/<TICKER>/deal.md`, Origination reads `config/deal-config.yaml > dealOrigination`). Symptom: Cholla appeared in Live Deals as "Cholla / Gideon Powell" with stale `last_activity: 2026-04-15` while Origination showed "Cholla Digital" with current `lastAction: 2026-04-16`. Two surfaces, same deal, divergent freshness — confusing to the reader and a sign of source drift.

**Rule**: a deal must appear in exactly one canonical section based on a single field — `stage` from `data/deals/<TICKER>/deal.md` is authoritative. `Sourcing`/`Sourcing / Auto` → Origination only. `Active Diligence`/`Diligence`/`Live` → Live Deals only. Compile-time renderer must dedupe by canonical key (`_CP_ALIASES`-normalized name) before splitting into sections; if a name resolves to two records, the section assignment derived from `stage` wins and the entry from the other section is dropped with a stderr warning.

**Companion rule**: when the same deal exists both as a `data/deals/<TICKER>/` directory AND as a hand-curated `dealOrigination[]` row in `deal-config.yaml`, the directory is the source of truth for `name`, `contact`, and `stage`; the YAML row supplies presentation fields (`task`, `myAction`, `nextStep`) only. A divergent `name` between the two is a bug — fix the YAML, not the directory.

### 2026-05-04 — Tomac Cove + Fundraising sections must auto-populate from transcript folder

User repeatedly observed that recent Thunderhead/Fit Ventures activity captured in transcripts (2026-05-01 Thunderhead call: "Fit will provide more details on equity, TC to deliver one-page structure diagram") was NOT promoted to the Fundraising > Direct LPs / Co-invest section. The capture pipeline reached `awaitingExternal[]` with the right items but never wrote a `prospectiveInvestors[]` row to `deal-config.yaml` or surfaced the activity on the fundraising card.

**Rule**: any compile step that builds the Tomac Cove or Fundraising surfaces MUST scan `awaitingExternal[]` and `tomac_intel[]` for new counterparties before serve. If a `counterparty` (firm) appears with ≥2 items and is not present in `deal-config.yaml > capitalRaisingAdvisors[]` or `prospectiveInvestors[]`, surface it as a "candidate to add" badge on the fundraising card with a one-click promote action — do not let it sit invisibly in the awaiting list. Equivalent rule for the Tomac Cove section against `liveDeals[]`/`dealOrigination[]`.

**Detection**: `python3 -c "import json,yaml; d=json.load(open('data/compiled/dashboard-data.json')); cfg=yaml.safe_load(open('config/deal-config.yaml')); known={(r.get('name') or '').lower() for k in ('liveDeals','dealOrigination','capitalRaisingAdvisors','prospectiveInvestors') for r in (cfg.get(k) or [])}; from collections import Counter; cps=Counter((i.get('counterparty') or '').split(' — ')[0].lower() for i in d.get('awaitingExternal',[]) if i.get('counterparty')); [print(c,n) for c,n in cps.most_common() if c and c not in known and n>=2]"`

### 2026-05-04 — Past-date awaiting-external items: extract takeaway, don't just dismiss

Strengthens the 2026-05-01 "Passed due dates are signals of completion" rule. When a scheduled call/meeting date has passed, the call almost certainly happened. The right action is not to dismiss the item silently — it is to (1) check the transcript folder for a transcript with a matching date and counterparty, (2) extract the takeaway from that transcript, (3) write it as the new `nextStep` / `takeaway` on the deal record, then (4) dismiss the original scheduling item.

Specifically flagged: "Awaiting external — call with Jeff Kechejian" items dated 2026-04-29 / 2026-04-30 / 2026-05-04 were still surfacing as open scheduling reminders on 2026-05-04 even though the call(s) happened. Multiple duplicates further inflate noise.

**Rule** for `_auto_expire_stale_events()` and any successor: when a scheduling item with a past `due` date matches an `awaitingExternal` cluster for a known counterparty, perform a transcript lookup before dismissing. If a transcript exists, log "completed via transcript: <doc_url>" to stderr and propagate the call's takeaway to the deal/LP record. Silent dismissal without takeaway extraction loses information.

### 2026-05-04 — Action item phrasing: lead with company, then individual

Action surface labels like "Lee — chase Tanmay for Piper Sandler intro" lead with the individual; the user wants `Piper Sandler (Lee) — chase Tanmay for intro`. Reading top-down, the firm anchors the action in the user's mental model; the individual is a secondary detail. Same rule for `nextStep`, `myAction`, and any deal-card action chip.

**Rule**: any code that composes an action label from `(counterparty, contact, action)` MUST format as `"<Firm> (<Person>) — <verb-first action>"` when both are known, and `"<Firm> — <action>"` when only firm is known. Never `"<Person> — <action>"` unless the person has no firm context (rare). Apply in `cos-dashboard-fetch.py` action label composition and in `buildTeamActionsCard()` rendering.

### 2026-05-04 — Per-call dashboard-touch overlay must be the canonical promotion path

Every transcript ingestion produces an overlay listing how the call touches the dashboard (deals, LPs, follow-ups, awaiting items). When that overlay omits valid touchpoints — e.g. the 2026-05-01 Thunderhead call generated awaiting items for Fit Ventures but did NOT promote Fit Ventures into the Fundraising > Prospective Investors section — the overlay is unreliable and downstream surfaces silently miss content.

**Rule**: the call-touch overlay is the canonical promotion path. If a call surfaces a counterparty firm with ≥1 awaiting item AND that firm is not yet in `deal-config.yaml`, the overlay MUST emit a "promote to fundraising" or "promote to liveDeal" suggestion. Audit the overlay generator (likely in `cos_otter_backfill.py` post-extract or a dedicated overlay routine) for this code path; if missing, that's the bug. The overlay is the surface that closes the loop between transcript and config — a gap there means rules above never fire.

### 2026-05-04 — Team action ownership default: deal lead when unknown

When a team action is tied to a specific deal and no explicit owner is set, default `owner` to the deal lead. Examples the user called out:
- Black Bayou follow-up on Mercuria proposal → Mark Saxe (deal lead).
- US Towers materials review → Nik (deal lead).

Both were previously `owner: "Yoni"` in `deal-config.yaml > liveDeals[]`. Corrected this session.

**Rule**: any code that composes Team Actions from deal records MUST resolve missing `owner` via `data/deals/<TICKER>/deal.md > owner` field as fallback. Hardcoding "Yoni" as a universal default is wrong and creates accountability confusion. If neither the action record nor the deal record specifies an owner, log a stderr warning naming the deal — do not silently default.

**Detection**: `python3 -c "import yaml; cfg=yaml.safe_load(open('config/deal-config.yaml')); [print(r.get('name'), '→', r.get('owner')) for k in ('liveDeals','dealOrigination') for r in (cfg.get(k) or [])]"` — every row should have an explicit owner that matches the deal lead in `data/deals/`.

### 2026-05-04 — Transcription error: Harvard ≠ Harbert (Harbert Management Corporation)

The Otter transcript LLM consistently mis-transcribed "Harbert" as "Harvard" in Thunderhead-related calls — Harbert Management Corporation is a Birmingham AL alternative asset manager (Fit Ventures' anchor investor). "Harvard" was propagating into `data/deals/thunderhead/deal.md`, `LPs.md`, `TERMS.md` (21 occurrences total, all corrected this session) and into deal taglines, key risks, and LP tables.

**Rule**: maintain a known-transcription-corrections block in the LLM extraction prompt (`BACKFILL_PREAMBLE` in `cos_otter_backfill.py` and the email extractor). At minimum, the block should normalize:
- "Harvard" → "Harbert" (in Thunderhead/Fit Ventures contexts)
- "Encore" → "Oncor" (per global memory `project_aliases_and_people.md`)
- Any future homophone collisions surfaced by the user.

This is Operating Principle #8 ("Fix the source, not just the symptom") applied to transcription. A post-hoc replace in `cos-dashboard-fetch.py` would be a filter; the prompt-level correction stops the bad name from ever entering the pipeline.

**Companion rule**: when a user reports a wrong firm name in dashboard output, do not just edit the visible record — search the entire `data/deals/` tree (`grep -rn 'Harvard' data/deals/`) for the same error and fix every occurrence in one pass. Otherwise the error reappears at next compile.

### 2026-05-04 — Holistic "broad-lens" audit pass before any /dash close

User explicitly asked for a reviewer who looks at the dashboard "with a broad lens" — checks for duplication, conflicting info, mistakes between sections — instead of only fixing the specific item raised. A narrow `/dash` session that ships a single fix without scanning adjacent surfaces lets symmetric bugs persist.

**Rule**: every `/dash` session MUST perform a holistic audit before commit. Minimum checklist:
1. Same deal name appearing in ≥2 sections of `deal-config.yaml` or across a `data/deals/` directory + a YAML row → flag.
2. `lastAction` date in YAML stale by >7 days vs. `last_activity` in the deal directory → flag.
3. `awaitingExternal[]` items past `due` by >2 weeks → flag for transcript lookup, not silent skip.
4. Counterparty firms surfaced in awaiting items with ≥2 hits but absent from `deal-config.yaml` → flag.
5. Action labels leading with a person rather than a firm → flag.
6. Team actions with `owner: "Yoni"` on a deal whose lead is Mark or Nik → flag.

If any flag fires, fix at the source (deal directory or YAML) in the same session. The audit is the antidote to whack-a-mole correction loops where the user has to keep flagging the same class of error.

## TOPIC — TILE / DRILLDOWN COHERENCE

### 2026-05-04 — Tile clicks must scroll-highlight existing sections, never reveal hidden ones

User flagged that the HQ tab's "Tomac Cove" tile (5 active deals) navigates to a drilldown panel containing sections — notably "Dealflow" and "Portfolio" — that are NOT visible anywhere else on the HQ tab. This violates the principle of progressive disclosure: a tile should let the user dive deeper into what they already see, not surface hidden content that bypasses the page's normal information architecture.

**Rule**: a tile click on the HQ tab MUST resolve to a scroll-highlight of existing visible sections on that tab (Live Deals, Origination, Fundraising, Awaiting Counterparties, Team Actions). Never to a hidden panel that contains alternate sections. If a tile's content has no corresponding visible section on the tab, the tile is over-reaching — either add the section to the page properly, or remove the tile drilldown.

**Specific actions for parallel UI session**:
- Remove the "Dealflow" sub-section from the Tomac Cove tile drilldown — duplicates / replaces the visible Live Deals + Origination sections without adding value.
- Remove the "Portfolio" sub-section from the Tomac Cove tile drilldown — no portfolio surface exists elsewhere on HQ; if portfolio is a real concept, surface it properly as a top-level section.
- KEEP the Fundraising sub-section in the Tomac Cove drilldown — the user explicitly said this is appropriate. Note that the drilldown Fundraising shows counterparty *views* (named LPs/advisors and what's open with each), while the higher Fundraising section on HQ shows *who we have spoken to* (relationship inventory). Different lenses on related data — keep both.

**Rule (general)**: every tile must declare a `scrollTarget` (CSS selector for the section to highlight) rather than a `panelTemplate`. If a tile cannot resolve to an existing visible section, the tile should not exist on that tab.

---

## TOPIC — AWAITING-EXTERNAL HYGIENE

### 2026-05-04 — Awaiting Counterparties: organize by firm, dedup paraphrases, drop already-happened

User opened HQ on 2026-05-04 and saw 24 awaiting-external items, many of which were duplicates and many for events that had already happened. Specific patterns discovered in this session's audit:

1. **Per-thread paraphrase explosion**: Lee/Piper Sandler had 26 items, all paraphrases of 4 distinct asks (Uber lease, site pipeline, promote/OpCo-PropCo proposal, Friday call confirm). Each email in a 10-message thread produced its own paraphrased extraction. Per the 2026-05-01 rule, per-thread dedup must run at extraction; absent that, this user-facing list balloons by 5-10× per active thread.
2. **Past-date scheduling clusters**: Garden Investments (9 items for an Apr 28-30 meeting), Apogee Comply / Jeff Kechejian intros (7 items for Apr 29/30 calls), ArcLight (Apr 18 / Apr 30), Heidrick & Struggles, iSquared, Active Infra, Black Mountain, EXIM Bank conference reg — all events that happened or expired. The `_auto_expire_stale_events()` regex misses common natural-language forms ("Confirm meeting day/time", "Confirm meeting logistics", "Confirm Wednesday 4/29 meeting"). The verb pattern `confirm\s+(call\s+time|meeting\s+time|in.person|the\s+meeting)` catches "meeting time" but NOT "meeting day/time" — slash-separated alternations need to be in the pattern.
3. **Counterparty alias fragmentation**: Cholla appears under 5 firm-name spellings (Cholla / Gideon Powell, Cholla, Chola, Cholla Petro, Chisholm / CHolla); Thunderhead under 2 (Thunderhead, Thunderhead Energy Solutions); PNGTS under 2; Active Infra/Active Infrastructure as separate clusters. Each name fragment fragments the by-firm grouping the user wants.
4. **Workflow stage supersession**: Martin Legal Group had 8 items about *drafting* the Cholla NDA, while Cholla / Gideon Powell had 5 items about *reviewing Yoni's redline* — meaning the NDA was already drafted and the workflow advanced. The drafting items should auto-retire when supersession is detected.
5. **Phantom firms from extraction noise**: "Pacific Industrial" (LLM mis-firmed Pacific Fleet), "assistant" (extracted "via Michelle" as an action owner), "Unknown" (LinkedIn outreach with no firm tag), "attorneys" (generic counterparty) — all should be either rerouted to the right firm or auto-dropped.

**Rule for the awaiting-counterparties UI surface**:
- Group by canonical firm (alias-resolved via `_CP_ALIASES`), not raw `counterparty` string.
- Within each firm group, dedup by content similarity (normalize whitespace + lowercase + strip dates; collapse items where the first 60 chars match).
- Retire items whose `due` is >0 days past (not 7) when the content matches a scheduling pattern AND a transcript exists for the same counterparty + date — meaning the call happened.
- Auto-supersede items in upstream workflow stages: if an "Awaiting NDA draft" item exists for firm X and an "Awaiting NDA review" item also exists for firm X with `addedDate` newer, the draft item is retired automatically.
- Drop items with `counterparty` matching extraction-noise patterns: `^assistant$`, `^attorneys?$`, `^[Uu]nknown$`, bare email addresses (`@gmail.com`-suffixed counterparties).

**Cleanup performed in this session**: 78 of 102 raw awaiting items tombstoned (75 from the audit batch + 3 final dups on second pass); visible count down from ~24 to **14 distinct items** across 9 firms. Tombstones written to `data/user-state/deletions.json` with category context. The cleanup is not a fix — the patterns above must be enforced upstream or the same garbage will re-extract on the next pass.

### 2026-05-04 — Counterparty extraction: drop firms that look like extraction noise

`cos_otter_backfill.py` and `cos_email_backfill.py` produced counterparty values like `assistant`, `attorneys`, `Unknown`, `jkechejian@gmail.com`, `— Mark Saxe` (orphaned em-dash). These are all signals that the LLM failed to identify a real firm and emitted a fallback or a misparsed token. They pollute the by-firm grouping.

**Rule**: in the extraction prompts, add an explicit rule: "If the firm cannot be identified from the email/transcript, emit `counterparty: ''` and tag `intel_type: 'unattributed'` — DO NOT emit a generic placeholder like 'assistant', 'attorneys', 'Unknown', 'team', or a bare email address." At compile time, items with `counterparty == ''` are routed to a separate "Unattributed Items" review queue rather than appearing in by-firm awaiting lists. This is preferable to silently dropping them — review surfaces the extraction failure for prompt tuning.

### 2026-05-04 — Stale-event regex must include slash-separated and noun-form variants

`_STALE_EVENT_PATTERNS` misses "Confirm meeting day/time", "Confirm meeting logistics", "Confirm Wednesday <date> meeting", "intro meeting", and "live call". Add to the regex:

- `meeting\s+(day|date)?[\s/]*(and|/)\s*time` — "meeting day/time", "meeting day and time"
- `meeting\s+(logistics|day\s+and\s+location|location)` — "meeting logistics"
- `confirm\s+\w+\s+\d+/\d+\s+(meeting|call|intro)` — "confirm Wednesday 4/29 meeting"
- `intro\s+(call|meeting)` — generic intro events
- `live\s+call|connect\s+live` — "Friday 5/1 live call"
- `catch.?up\s+(call|meeting|chat)` — catchup events

Update the pattern in `cos-dashboard-fetch.py` and add unit-test fixtures for each new form. Without the broadened patterns, stale-event auto-expire silently misses 50%+ of past-date scheduling items the user has to manually dismiss every week.

### 2026-05-04 — Workflow-stage supersession rule

When the same counterparty has both an upstream-stage and a downstream-stage awaiting item for what is clearly the same workflow (NDA draft vs. NDA review; calendar invite vs. meeting confirmation; teaser request vs. teaser delivery), the upstream item is retired automatically with a "superseded by <id>" tombstone reason.

**Detection heuristic**: for each `(canonical_firm, content_topic)` cluster, sort items by `addedDate`. If item N's content uses an upstream verb ("draft", "send invite", "propose times") and item N+1's content uses a downstream verb ("review", "execute", "confirm <past tense>", "respond to redline"), item N is superseded.

**Rule**: implement in `cos-dashboard-fetch.py` after `_promote_source_ref()` and before `_auto_expire_stale_events()`. Log each supersession to stderr so prompt drift can be audited.

---

## TOPIC — TIME-REFERENCE NORMALIZATION

### 2026-05-04 — "next week" / "later this week" must materialize to "week of YYYY-MM-DD"

Floating time references go silently stale. A 2026-04-23 email saying "let's catch up next week" still reads as "next week" on 2026-05-04 — the user's eye glosses past the stale qualifier and reads it as current. Two-layer defense added 2026-05-04:

1. **Compile-time materialization** — `_materialize_next_week()` in `cos-dashboard-fetch.py` (runs in the awaiting-external pipeline before staleness checks). Replaces the floating phrase with `week of <Monday-of-target-week>` computed from the item's `addedDate`. Idempotent.
2. **Extraction-time rule** — extraction prompts in `cos_otter_backfill.py` and `cos_email_backfill.py` should normalize the same phrases at write time (not yet implemented; rule stands as defense-in-depth).

Phrases handled: "next week" (+7d), "early next week" (+7d), "late next week" (+10d), "later this week" (+3d), "end of the week" (+3d), "end of next week" (+10d). All anchored on `addedDate`, snapped to the Monday of the target week.

---

## TOPIC — TILE / DRILLDOWN UX

### 2026-05-04 — Personal tab task icon is the action, not a popup launcher

The Personal panel `Task` column buttons should DO the action, not open a popup that explains the action. When `taskUrl` is populated the button is an anchor (mailto, calendar URL, LinkedIn search) — that's correct. When it's empty, the legacy fallback opened a modal. The user's explicit feedback: that's wrong.

**Rule** (now enforced in `_ptHelpers.taskBtn`):
- For `claude_code` rows OR `personal_items` rows whose `name` starts with "Dashboard Update — ", the task button MUST link to `https://claude.ai/new?q=<encoded prompt>` (Claude session pre-loaded with the task).
- Other rows lacking `taskUrl` get the modal fallback as a degraded path, but populating `taskUrl` in the source config is the right fix.
- The popup is for review/context, not task execution.

**Composition rule**: Claude-prompt encoded body = `name + myAction + what` joined with blank lines, skipping empty fields.

### 2026-05-04 — Universal text-fit rule: succinct, fits the box

Every text in a fixed-size component (table cell, tile, badge, modal label) must fit. When source data has long text, the layout MUST either:
- Use a `minmax(<min>, <fr>)` grid column with `min-width: 0; overflow-wrap: break-word` on direct children for graceful wrapping, OR
- Truncate with ellipsis and surface full text on hover/click.

Banned: fixed-px columns whose content blows them out; nowrap content that overflows horizontally; long single-line strings without word-break.

**This session's fix**: `.pt-row` and `.pt-col-hdr` `140px 1fr 110px 68px` → `minmax(160px, 1.2fr) minmax(0, 1.6fr) 110px 80px` plus `.pt-row > div { min-width: 0; overflow-wrap: break-word }`.

---

## TOPIC — VISUAL CONSISTENCY ACROSS ROUTES

### 2026-05-04 — All HTML routes use the cream-paper / serif chrome — no per-route navbar

Pre-2026-05-04, the React `/portfolio/` route (`app/tomac-cove-src/src/App.js`) had its own `GlobalNav` with `T.navy` (#1B2D45) background and monospace courier tabs — diverged from the Python-Jinja `_topnav.html`. Tabs also used different labels ("Status" vs. "HQ", "TC Pipeline" vs. "Deal Pipeline").

**Rule**: `_topnav.html` is the single source of truth. Any route that renders a top-level navbar MUST either use the shared partial via `_inject_shared_chrome()`, OR replicate its design tokens AND label set EXACTLY.

**React parity**: when `App.js GlobalNav` is updated, mirror with `_topnav.html` first; React follows. Label/route divergence is a bug — fix in both files in the same commit.

**Build pipeline**: `app/tomac-cove-src/` is a CRA project; `npm run build` outputs to `tomac-cove-src/build/` and must be `rsync -a --delete`-ed to `app/tomac-cove-build/` (where the server reads). After updating App.js, rebuild AND deploy.

### 2026-05-04 — Modal text contrast: never use slate-400 on white

`.modal-section-label { color: #94a3b8 }` (slate-400) on white failed WCAG AA. User flagged as "too light, hard to read."

**Rule**: modal/popover labels and subdued text use slate-600 (#475569) or darker. Slate-400 is acceptable only as a fourth-level subdued token (overlay legends, ghost icons), never primary label text.

---

## TOPIC — BRIEFING CONTENT FORMATS

### 2026-05-04 — Briefing parser must accept markdown AND fall back to a card on empty parse

`_parseBriefingFullText` (in `briefing-dashboard.html`) was tuned for the legacy `KEY TAKEAWAY:` / numbered-section format. The new personal-briefing fullText uses `## H2`, `### H3`, `**bold**`, `---` rule. The structured parser returned 0 cards → Daily Briefing tab empty.

**Rule**: when `_parseBriefingFullText(fullText)` returns 0 AND `fullText` is non-empty, render via `_markdownCard(fullText)` (lightweight md → HTML helper added 2026-05-04). Never let the briefing tab show "no items" while data exists.

**Future-proofing**: when a new briefing format ships, prefer extending `_parseBriefingFullText` over relying on the markdown fallback. The fallback is graceful degradation, not the canonical render path.

---

## TOPIC — AUTH / LOCALHOST TRUST

### 2026-05-04 — Trust the host's own LAN IPs as loopback for owner auth

Previously `_is_localhost()` returned True only for `127.0.0.1`/`::1`/`localhost`. Users opening the dashboard at `http://192.168.4.21:7777` from their own Mac saw the LAN IP as the connecting source — `_is_localhost()` returned False, `/admin` re-prompted for login despite being on the same machine.

**Rule** (codified 2026-05-04): `_is_localhost()` consults `_OWN_HOST_IPS`, computed at process start via `socket.gethostname()` + a UDP socket trick to find the bound LAN IP. Connections from any IP in this set are treated as loopback for auth. Safe in single-tenant deployments because (a) the dashboard binds to specific interfaces, (b) only same-host or same-LAN traffic can present those source IPs.

**General rule**: never assume `127.0.0.1` is the only loopback case for a host that listens on multiple interfaces.

---

## TOPIC — ROUTINES OBSERVABILITY

### 2026-05-04 — `/routines` log-stem must come from the plist's `StandardOutPath`, not the task name [SUPERSEDES the same-date "investigate" entry below]

**Resolved this session.** The previous "Routines page reports never_run for everything" investigation produced a fix: `_routines_data` and `_routines_health` now read log files at the path the plist actually writes to, rather than assuming `<task>.run.log` matches the task name.

**Bug pattern**: 14 of 15 plists historically routed `StandardOutPath` to a filename different from their plist label — e.g. label `morning-briefing` writes to `cos-personal-briefing.stdout.log`; label `weekly-summary-email` writes to `sunday-weekly-email.stdout.log`. Renaming the registry without renaming the plist log paths left the dashboard's routines surface blind to every successful run, reporting uniform `never_run`.

**Fix shipped**:
- `_routines_parse_plist` now extracts `StandardOutPath`, derives `log_stem` (basename minus `.stdout.log` / `.run.log` / `.stderr.log` / `.log` suffix), and returns it in the meta dict.
- `_routines_data` reads logs at `<log_stem>.{run,stdout,stderr}.log` and exposes those exact paths in `log_paths`.
- `_routines_health` reuses `log_paths` from `_routines_data` instead of rebuilding from task names.
- `_handle_routines_log` (the per-task log tail endpoint) also resolves through the stem.

**Rule**: any new plist that lands in `~/Library/LaunchAgents/com.yoni.claude-task.<task>.plist` SHOULD use `StandardOutPath = ~/dashboards/logs/claude-tasks/<task>.stdout.log` — the task-name-matched canonical path. Existing plists with mismatched stems are tolerated via the stem lookup. If you create a new plist, follow the canonical convention so the stem table can eventually be retired.

**Sub-finding (unfixed)**: only 12 of 15 plists were loaded in launchctl at session start — `inbox-capture`, `morning-briefing`, `podcast-processing` were missing. Loaded all three via `launchctl bootstrap gui/$UID <plist>`. Going forward, a routines health-check should fail loudly when a plist exists on disk but is not loaded — silent missing = silent broken pipeline.

**Sub-finding (unfixed, separate)**: most last-runs were May 1; today is May 4. Weekday-scheduled tasks did not catch up after Mac wake. macOS launchd doesn't run missed `StartCalendarInterval` events on wake. If routines must catch up after sleep, switch to `StartInterval` or add a wake-time `RunAtLoad` reconciliation script. Captured here for the next session.

### 2026-05-04 [SUPERSEDED] — Routines page reports `never_run` for everything; observability gap to investigate

`GET /routines` returned 15 routines all with `status: "never_run"`, `last_run: None`. `launchctl list | grep com.yoni.claude-task` shows 12 agents loaded (never executed); 3 are missing from launchd entirely (`inbox-capture`, `morning-briefing`, `podcast-processing`). Inconsistent with observed reality (`dashboard-data.json` IS regenerating; daily briefing has fresh content).

**Hypotheses for next session**:
1. The routines registry reads run history from `~/dashboards/logs/claude-tasks/<task>.run.log`. If logs write elsewhere (e.g., `/tmp/` or `~/cos-pipeline/logs/`), the registry never sees them.
2. The 3 missing-from-launchd routines need plists loaded: `launchctl load ~/Library/LaunchAgents/com.yoni.claude-task.<task>.plist`.
3. The `/routines` endpoint may be reading the canonical-plist registry but plists fire under different identifiers.

**Rule**: when `/routines` shows uniform `never_run`, treat as a critical observability gap. The routines surface is a health monitor — silent uniformity = a broken monitor, not a quiet day.

---

### 2026-05-04 — After a git push, restart the server to activate code changes

The dashboard server is a long-running LaunchAgent (`com.yoni.cosdashboard`).
It does NOT hot-reload Python source files. After any commit that touches
`cos-dashboard-server.py`, the server must be restarted via
`launchctl kickstart -k gui/$(id -u)/com.yoni.cosdashboard` for changes to
take effect. If a feature "isn't working" after a git push, check whether the
server is still running pre-push code before assuming the code is wrong.
