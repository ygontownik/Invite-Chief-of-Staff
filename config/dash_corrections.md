# /dash — Running Corrections

Authoritative reference for lessons learned during `/dash` sessions.
**Read before planning any change inside `~/dashboards/`.**

A dated, append-only log of specific, falsifiable corrections that apply
to all future `/dash` invocations.

---

## HOW TO USE THIS FILE

- **Step 0 of /dash reads it** alongside `CLAUDE.md`, `PREFLIGHT.md`,
  and `DECISIONS.md`. If an entry here contradicts those files, the
  more recent dated entry wins until the older doc is updated to match.
- **Append on every correction** — whenever the user pushes back, a
  filter reveals an upstream bug, or a session uncovers a repeatable
  pattern, write it down here before closing the session. Don't let the
  lesson live only in CHANGELOG prose.
- **Dated bullets, grouped by topic.** One bullet = one rule. Each
  bullet must be specific and falsifiable. "Be careful with X" is not
  a rule; "X requires Y because Z" is.
- **Never delete entries.** If a rule is superseded, add a new dated
  entry that explicitly supersedes the older one.
- **Generic rules only in this file.** Tenant-specific incidents, named
  deals, named people, and one-off events belong in the private
  `~/dashboards/config/dash_corrections_log.md`.

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
something and the counterparty does NOT affirmatively accept within
the transcript, do not emit it as an `action_item`. This rule lives
in both extractor preambles.

---

## TOPIC — DEAL IDENTITY / COUNTERPARTY ALIASES

### 2026-04-21 — `_CP_ALIASES` is the single source of truth

Different spellings of the same deal must collapse to one canonical
name so the auto-promoter doesn't create parallel deal rows and the
Awaiting External UI doesn't show the same deal twice. The alias
table lives in two mirrored places and MUST be kept in sync:

- `app/cos-dashboard-fetch.py` — `_CP_ALIASES` (tuple).
- `app/templates/cos-dashboard.html` — `__CP_ALIASES` (const).

When adding a new deal to the pipeline that has known alternate
spellings, add aliases to both tables in the same commit.

---

## TOPIC — FRESHNESS / SIGNAL RANKING

### 2026-04-21 — Recency beats due date for "next step" overlay

`_overlay_freshest_signal` in `app/cos-dashboard-fetch.py` must sort
candidate actions by (presence of `addedDate`, most-recent `addedDate`,
fundraising-keyword weight, earliest `due` as final tiebreak). Sorting
primarily by `due` lets an old overdue scheduling reminder mask
today's fundraising-focus action captured from a fresh call. If the
sort logic is ever simplified, that bug returns.

The keyword-weighting layer should up-rank fundraising terms (FEA, LC,
teaser, CIM, term sheet, milestone, Phase 2, bridge, anchor, raise,
IRA, credit, structure, capital, plus deal-specific tokens) and
down-rank scheduling terms (ranch visit, schedule, reschedule, meeting,
in-person, dates, coordinate, confirm via). Maintain the deal-specific
token list in the private log, not here.

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

`strings.yaml` button tooltips describe what a button actually does.
Before adding a feature to a button's code path, check whether the
existing tooltip already claims that feature — if it does and the code
doesn't back it up, that is a bug (misleading UI), not a feature gap.
Always update both code and `strings.yaml` together.

### 2026-04-27 — Dead helper functions require a call-site fix, not a comment

A helper function defined, documented, and never called is dead code
regardless of how complete it looks. Before treating a helper as
"working," verify it is actually invoked in `main()` or equivalent.
A function with no callers in production is dead code. Trace the call
graph; do not trust a function that appears correct in isolation.

---

## TOPIC — PYTHON RUNTIME COMPATIBILITY

### 2026-04-28 — All routines invoked by `/usr/bin/python3` (3.9) require `from __future__ import annotations`

The launchd runner scripts (`scripts/otter-backfill-runner.sh` and
friends) explicitly use `/usr/bin/python3` which lands on macOS-shipped
Python 3.9. Any `.py` file they invoke directly OR transitively import
that uses PEP 604 union syntax (`X | None`, `list[dict] | None`, etc.)
at module level OR in function signatures will crash at import time
with `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`.

**Rule**: every file in `routines/` (especially `routines/process/`)
that uses `X | None` or similar PEP 604 syntax MUST have
`from __future__ import annotations` as its first import. The
dashboard server itself runs on `/opt/homebrew/bin/python3` (3.14) so
server-only code is safe — but the moment a `routines/` module imports
a server-side helper, the whole transitive closure must be 3.9-compatible.

**Detection**: `grep -lE ': [A-Z][A-Za-z]* \| None| -> [A-Z][A-Za-z]* \| None' routines/**/*.py`
then check each match for `from __future__ import annotations`.

**Why this can surface silently**: the runner script logs the TypeError
to `~/dashboards/logs/<pipeline>.log` but no dashboard surface flags
the failed runs unless a freshness chip / observability widget closes
that gap.

---

## TOPIC — AWAITING EXTERNAL / LOGIC CHECKS

### 2026-05-01 — Passed due dates are signals of completion, not persistence

When reviewing `awaitingExternal` items during a `/dash` session,
treat a passed due date as evidence that the underlying event likely
already happened — a call invite for a date >2 weeks ago should be
dismissed, not left as an "overdue" reminder. Apply this logic before
touching any item:

1. Is the due date > 2 weeks in the past? Default: dismiss unless
   there is explicit evidence the underlying action is still open.
2. Is the item a meeting invite / scheduling confirm? If the date
   passed, the meeting happened (or was missed). Dismiss.
3. Is the item a "propose times" or "confirm calendar" action? If the
   date passed, dismiss — this kind of prompt has zero value after
   the window closes.

**Do not leave past-due scheduling artifacts in the list** — they
inflate the count, mask genuinely open items, and signal to the user
that the pipeline is not thinking about context. When in doubt,
dismiss and let the capture pipeline re-surface anything still live.

### 2026-05-01 — Read compiled data before asking the user for deal status

Before updating any manually-curated config (`config/*-config.yaml`),
ALWAYS read:

1. `data/compiled/deal-system-data.json` — deal health, thesis scores,
   actions, milestones, activity log
2. `data/compiled/dashboard-data.json` → `followUps[]` and
   `awaitingExternal[]` filtered for deal names

These files are the output of the capture pipeline and contain
extracted intelligence from call transcripts, emails, and Otter
recordings. Status updates, deal sequencing decisions, resolved
actions, and new counterparties will be in here before the user tells
you. Asking the user verbally for information already in the compiled
data is a workflow failure.

**How to apply:** In every `/dash` session that touches deal or
recruiting config, run the Python read commands above before Step 1
(Plan). If a deal update is needed, derive it from the data first —
only ask the user to fill in what the data cannot answer.

### 2026-05-01 — `/item/delete` requires both `id` and `source` fields

The `/item/delete` endpoint rejects requests missing `source`. Always
POST `{"id": "<id>", "source": "awaitingExternal"}` (or the appropriate
source for the section). The error message "id and source required"
is the signal.

---

## TOPIC — DASHBOARD-DATA.JSON SCHEMA HYGIENE

### 2026-04-28 — Display-only fields must not be persisted; recompute at serve time

`generatedAt` and similar display strings can persist as
human-formatted strings in `dashboard-data.json` if `cos-dashboard-fetch.py`
does `merged = {**state, **live_data}` and a prior `state[generatedAt]`
survives every refresh. Server and refresh paths recompute these fresh
from `fetchedAt` on every serve — but the persisted stale string can
sit in the file for days, visible to anything reading the JSON
directly.

**Rule**: any field whose value is computed for display (timestamp
formatting, age labels, "X minutes ago" strings) belongs only in the
served HTML, never in the persisted JSON. The producer must
explicitly `merged.pop('display_field', None)` before write. Persist
only the canonical underlying datum (e.g. `fetchedAt` ISO string), and
let consumers format.

**Detection rule of thumb**: if two paths compute the same field and
only one persists it, that's the smell. Audit producer code paths
whenever a UI shows an unexpectedly stale timestamp.

---

## TOPIC — OTTER / TRANSCRIPT PIPELINE

### 2026-04-21 — One call = one Google Doc

Otter (via Zapier) routinely drops the same call into Drive 2-4 times:
Zapier double-fires, and Otter separately exports a `.txt` full-
transcript alongside the Google Doc. Downstream effects: the dashboard
processes duplicates, the user sees 3+ copies in the destination
folder, and the `.txt` is sometimes kept as the canonical copy when a
Google Doc is preferred.

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

`~/dashboards/scripts/load-secrets.sh` re-exports env vars by pulling
from macOS Keychain. If the Keychain entry for a secret (e.g.
`WEBHOOK_SECRET`) differs from the value in `.zshrc` or a launchd
plist, sourcing `load-secrets.sh` silently replaces the shell var.
Testing a daemon endpoint that validates a secret will fail if you
sourced `load-secrets.sh` first.

Rule: when smoke-testing daemon endpoints with secrets, use the
hardcoded plist value directly (or `grep -A2` the running plist)
rather than `source load-secrets.sh`. The daemon reads its secret
from the plist `EnvironmentVariables` block — not from `.zshrc` or
Keychain at runtime.

---

## TOPIC — EXTRACTION / ROUTING

### 2026-04-27 — `action_items[].dashboard_path` requires post-extract backfill

Sonnet reliably fills `envelope_items[].dashboard_path` but often
returns `""` for `action_items[].dashboard_path` even when the same
call's envelope items have correct paths. Two-layer defense required
in `cos_otter_backfill.py`:

1. `_backfill_action_dashboard_paths()` — called before
   `write_followups()`. Fills empty paths by cross-referencing
   envelope_items (content match first, then best deal/LP path), then
   intel-section paths, then category-based default.
2. `write_followups()` fallback: use `get("dashboard_path") or default`,
   not `get("dashboard_path", default)` — the dict.get default only
   fires on missing keys, not empty strings.

Prompt reinforcement alone is insufficient — always pair with
post-extract code.

---

## TOPIC — KEYWORD MATCHING / CLASSIFICATION

### 2026-04-27 — Theme tagging: curated keywords + dynamic tokens, never curated-only

`tag_origination_items()` uses three passes: (1) dashboard_path
substring, (2) curated keyword map, (3) dynamic tokens from live
theme data. Pass 3 is the critical one — it ensures new themes added
to `deal-pipeline-data.json` are matched without any code edit.
Curated-only classifiers drift silently when new categories appear:
items accumulate in "unmatched" until someone notices and updates the
code. Always pair a curated map with a dynamic fallback derived from
the actual category metadata. The pattern is:
`_extract_theme_name_tokens(theme)` → significant words from name +
proper nouns from thesis text.

---

## TOPIC — HTML / JS EDITING IN DEAL DASHBOARD

### 2026-04-27 — Use Python line-list rewrite for multi-line JS insertions; never `sed -i '' Nc`

The deal dashboard JS is minified-style: long logical expressions
split across indented continuation lines. `sed -i '' '<N>a\...'`
inserts after a *line* boundary but cannot replace a logical block
that spans multiple lines. If the insertion point is inside a JS
expression (e.g. between two `createElement` arguments), `sed` splits
the expression and produces a syntax error that is invisible until the
browser console is checked.

Rule: for any multi-line block replacement or insertion in
`deal-dashboard.html` or `cos-dashboard.html`, use a Python script
that operates on `lines[]` by index. Read the target line numbers
with `sed -n '<N>p'` first, confirm the exact content, then rewrite
the slice. Never use `sed -i '' '<N>a\` for JS edits.

---

## TOPIC — INFORMATION ARCHITECTURE

### 2026-04-28 — Tab-context separation: one canonical tab per item

The Status tab and Personal tab address different operational
contexts. When adding a new card or section, assign it to exactly one
tab based on "where does the user act on it?". Do NOT put the same
item on both tabs. If an item spans both contexts, the action-locus
test resolves placement.

### 2026-04-28 — `render()` is the right place for tab-conditional render-ORDER changes

To change what renders before what on a given tab, inject the
conditional block directly in the `render()` function's `innerHTML`
template string — not inside layout-builder helpers. This keeps
layout-order decisions at one level of abstraction, avoids passing
extra flags through the call chain, and makes it obvious which tab
gets which order by reading `render()` alone.

### 2026-04-27 — Synopsis cards must point to a canonical home, not re-render content

When two surfaces would render the same content (calls, calendar,
themes, follow-ups, deal flash, market briefing), pick one canonical
home and reduce the other to a synopsis card with a visible "→" link.
Reject any new card/tab/section that renders content already rendered
elsewhere at similar fidelity. The "where does the user act on it?"
test resolves ambiguity: that's the home; everything else is a
pointer. Codified in `docs/DECISIONS.md` (2026-04-27).

---

## TOPIC — WEBHOOK ARCHITECTURE

### 2026-04-27 — Use existing public tunnel; don't add a second one

The dashboard server (port 7777) is not public. When a GAS or Zapier
webhook needs to reach it, proxy through the existing ngrok URL
(port 8765, call_scheduler). Add a new route to `call_scheduler.py`'s
`do_POST`, before the HMAC block. This avoids a second ngrok process
or cloudflared ingress.

The GAS secret uses a simpler `X-Otter-Secret` header check (not
HMAC) because the body is JSON from GAS (not a calendar push), and
constant-time comparison on the raw secret is sufficient. Don't use
the HMAC path for new endpoints unless the caller can compute HMAC
signatures.

---

## TOPIC — LLM CLASSIFICATION / QUALITY GATES

### 2026-04-27 — Keyword matching requires a Haiku confirmation gate; never ship keyword-only signals

Keyword matching against free-text origination items (content,
context, counterparty) produces false positives at a high rate even
with named-asset-specific terms. Company names and asset names appear
in unrelated transcripts. A Haiku confirmation pass — one call per
item with any matches — is required at refresh time to confirm each
tag is substantive.

Pattern for all future signal-classification features in
`deal-dashboard-refresh.py`:

1. Keyword pass runs first (fast, no API cost) to narrow the
   candidate set.
2. `_classify_signals_haiku()` confirmation pass removes false
   positives.
3. Both layers are required; neither alone is sufficient.
4. Gate behind a `CLASSIFY_SIGNALS = True/False` flag so it can be
   bypassed for local testing.
5. Graceful degradation on API failure: keep keyword-matched tags,
   print WARNING, continue.

### 2026-04-27 — `ANTHROPIC_API_KEY` is in `.zshrc`, not loaded by bare shell; source before running

`deal-dashboard-refresh.py --classify` calls
`os.environ.get('ANTHROPIC_API_KEY')`. Running the script from a bare
bash subshell (as the Bash tool does) doesn't inherit `.zshrc`
exports. The launchd daemon has the key in its plist
`EnvironmentVariables` block and will work correctly at runtime.
When testing the Haiku pass manually:
`source ~/.zshrc && python3 app/deal-dashboard-refresh.py`. Do not
confuse auth failures in the Bash tool with production failures.

---

## TOPIC — SERVER-SIDE TOMBSTONE FILTERING

### 2026-04-28 — `_load_personal_items()` must check `deletions.json`, not just `email-resolutions.json`

`_load_personal_items()` in `cos-dashboard-server.py` previously only
filtered items against `email-resolutions.json`. Items dismissed
client-side via the tombstone endpoint (which writes to
`deletions.json`) would return on every pipeline regen because the
server never loaded those tombstone IDs. The fix: after checking
email-resolutions, also compute
`_djb2('recruit|personal_items|' + name)` and skip any item whose
tombstone hash is in `_deleted_ids()`. This matches the exact key
format the client-side `__isDel()` uses.

Rule: whenever a new item source is added to `personal-items.json`
or any other server-loaded user-state file, verify that
`_load_personal_items()` (or its equivalent) checks `_deleted_ids()`
with the same key the client would use for that item type.

---

## TOPIC — SERVER STARTUP / MODULE INTEGRITY

### 2026-05-01 — Every module-level name reference must have a corresponding import; no silent stubs

`cos-dashboard-server.py` can crash on startup for days if a helper
module referenced as `_fc`, `_ctx`, or similar is called at module
scope without an explicit import at the top of the file. LaunchAgent
restarts the server to failure repeatedly; the dashboard shows stale
data with no obvious error surface until `/tmp/cos-dashboard.log` is
checked directly.

**Rule**: any helper module referenced as `_fc`, `_ctx`, or similar
must have an explicit import at the top of the file. Verify with
`grep -n "^_.*=\|^from\|^import" app/cos-dashboard-server.py` before
committing. Module-scope calls (outside a function) are especially
dangerous because they crash before the server can bind to port 7777,
leaving the port dark with no 5xx to observe.

**Detection**: if the dashboard shows stale data and the port is dark
(`curl` returns `exit 7`), check `/tmp/cos-dashboard.log` first —
launchd logs startup crashes there.

---

## TOPIC — DATA RENDERING COMPLETENESS

### 2026-04-28 — Filter conditions must use combined fields when two fields serve the same purpose

When filtering action-carrying items from a config array, never filter
on a single optional field when two fields serve the same purpose for
different workflows. Example: a team-actions card that filters on
`myAction` non-empty alone will silently drop rows whose meaningful
content sits in `nextStep`. Use
`(t.myAction && t.myAction.trim()) || (t.nextStep && t.nextStep.trim())`
as the non-empty test, then render `t.myAction || t.nextStep` as the
action text.

---

## TOPIC — CONFIG ENTRY HYGIENE

### 2026-05-01 — Set classification and attribution fields at entry creation, not retroactively

Config entries in `*-config.yaml` files often require classification
fields (e.g. `group`: `placement` vs. `gp_seed`) and attribution
fields (e.g. `sourcedBy`: name of the person who made the intro) that
are easy to omit on first write and then forgotten until a UI
restructure forces an audit.

**Rule**: when adding any new entry to a typed config array, set both
classification and attribution fields before committing. Omitting a
classification defaults to one bucket, which is often wrong. Omitting
attribution loses the chain of custody and makes it impossible to
know who to chase if the relationship stalls.

**How to detect missing values**: grep the config for the array key,
then for the required field names, and report rows missing either.
Run this check whenever adding new entries.

---

## TOPIC — PROVENANCE / TRACEABILITY

### 2026-05-01 — `source_ref` exists on all pipeline items; top-level `source`/`addedDate` must be promoted from it

Every envelope item written by `cos_otter_backfill.py` and
`cos_email_backfill.py` carries a `source_ref` dict with
`{type, title, doc_url, date}`. However, top-level `source`,
`addedDate`, and `workstream` fields on `awaitingExternal` items can
be left unpopulated — leaving items fully untraceable in compiled
JSON.

**Rule**: any compile step that builds or merges `awaitingExternal[]`
must call `_promote_source_ref()` (defined in
`cos-dashboard-fetch.py`) to populate top-level fields from
`source_ref`. Never leave `source` and `addedDate` as null when
`source_ref` is present.

**Detection**: count `awaitingExternal[]` items missing `source` —
should be 0 after every compile run.

### 2026-05-01 — Email extraction: counterparty is the deal contact, not the From: header party

`cos_email_backfill.py` can attribute awaiting_external items to
whoever appeared in the From: header, even when that person is a
facilitating banker CC'd on a thread about a different deal.

**Rule**: counterparty = the firm/person whose commitment is being
tracked, not the email sender. When the thread subject is about a
named deal in pipeline context, counterparty must reference that
deal's primary contact. LP names must never appear as counterparty
on deal operational items.

**Rule**: per-thread deduplication — same commitment across multiple
messages in one thread = one item (most recent/specific version).
Prompt must enforce this explicitly. Without it, a 10-message thread
generates many paraphrases of the same action.

### 2026-05-01 — `_CP_ALIASES` must include common misspellings at entry creation

Common one-letter misspellings and short-form variants must be added
to the alias table at entry-creation time, not retroactively when a
fragmented cluster appears in the UI. Both `_CP_ALIASES` (Python) and
`__CP_ALIASES` (JS) must be updated together in the same commit.

### 2026-05-01 — Filter `Sourcing / Auto` from active-deals prompt block

`load_pipeline_context()` in `cos_otter_backfill.py` builds an
"ACTIVE DEALS" block from compiled dashboard data. That array
includes auto-promoted counterparty entries staged as
`Sourcing / Auto` — counterparty names extracted from
awaitingExternal items, not real deals. Without filtering, the model
sees noise tokens (role placeholders, bare email addresses,
anonymous-author placeholders) as active deals and mis-routes
transcript items against them.

**Rule**: any code path that reads the deals array from
`dashboard-data.json` for prompt injection MUST filter out
`stage == "Sourcing / Auto"` entries. Real deal stages are explicit
labels set in deal config (e.g. "Diligence", "Sourcing", "Active
Diligence"). Auto-staged counterparties are noise.

**Detection**: if the active-deals prompt block has more than ~10
entries, check for `Sourcing / Auto` leakage.

### 2026-05-01 — Strip Otter filename padding at the point of origin, not only at display time

`cos_otter_backfill.py` previously wrote raw Otter filenames (with
`───` padding and `_otter_ai` suffixes) directly into
`source_ref.title`. This propagated to every downstream consumer.
**Rule**: normalize `source_ref.title` at write time with
`re.sub(r'^─+\s*|\s*─+$', '', file_name).strip()` before storing. The
JS `_cleanSrcTitle()` is a second defense for pre-existing items
only — it should never be the primary fix.

### 2026-05-01 — Stale one-time-event items require a two-layer filter

A one-time event item (conference registration, RSVP, scheduling
proposal) can stay on the dashboard after the event date passes if
neither the extraction prompt nor the compile step rejects it. The
fix requires BOTH layers:

**Layer 1 (extraction / write time)**: Add a `STALENESS FILTER` rule
block to every extraction prompt (`EMAIL_PREAMBLE`, `_BACKFILL_BODY`).
The LLM must not emit `awaiting_external` items for
conferences/summits/registrations/RSVPs/scheduling proposals where
the event date is already past as of TODAY. This prevents the item
from ever entering the pipeline.

**Layer 2 (compile / read time)**: `_auto_expire_stale_events()` in
`cos-dashboard-fetch.py` — applied after `_promote_source_ref()` in
the `awaitingExternal` pipeline. Regex matches event/scheduling
patterns; if `due` or `addedDate` is >7 days in the past, the item is
dropped and logged to stderr. This catches items that pre-date the
prompt rules or slipped through.

**Rule**: any item about a one-time event (not a recurring
relationship action) MUST have temporal context to remain valid. An
item with no date context survives; an item with a clearly past date
and one-time-event content is automatically retired.

### 2026-05-01 — `[RESOLVED]` items must be filtered at every point in the pipeline

`[RESOLVED]` is a tag the Follow-ups doc uses to mark completed
actions. It can surface as deal card `nextStep` in three separate
ways: (1) in `matched_fus` (followup rows), (2) in `aw_all`
(awaiting_external items), (3) in `nextStepDoc` (doc-parsed static
field). Each must be filtered independently.

**Rule**: Any code path that selects `best_action` or `best_await`
for deal card display MUST filter `[RESOLVED]` from the candidate
list before the sort/min step — not after. The static `nextStep`
from doc parsing must also be cleared if it starts with `[RESOLVED]`
before the overlay writes to it.

### 2026-05-01 — `best_await` must prefer open items over overdue items

`min(aw, key=due)` picks the most-overdue item when there are both
past and future items. This causes overdue scheduling actions to win
over current deal progress.

**Rule**: Before selecting `best_await`, split `aw_all` into
`aw_open` (due >= today) and use `aw_open` if non-empty. Fall back
to `aw_all` only if no open items exist. This ensures a future-dated
commitment beats an overdue one when both are candidates.

### 2026-05-01 — Staleness patterns must be explicit about scheduling surface forms

A pattern like `schedule\s+(a\s+)?(call|meeting|time|intro)` misses
phrases where "schedule" is used as a noun, or where the verb is
"text someone schedule for next week". Site visits, ranch visits,
and calendar invite sends are also scheduling one-time events.
Pattern must be maintained as an exhaustive list of natural-language
forms, not just the most obvious verb forms.

---

## TOPIC — CSS / LAYOUT

### 2026-05-04 — CSS Grid 1fr blowout requires `min-width: 0` on children

`grid-template-columns: 1fr 1fr 1fr` does NOT guarantee equal-width
columns when child elements contain long unbreakable strings. CSS
Grid cells default to `min-width: auto`, which lets a cell expand
beyond its `1fr` share to fit content. To enforce equal columns, add
`min-width: 0` to every direct child of the grid container (or to the
class applied to those children). `overflow-wrap: break-word` pairs
with this to wrap long strings gracefully.
Pattern: `.grid-container > * { min-width: 0; overflow-wrap: break-word; }`.

### 2026-05-04 — Freshness badge ≠ page-content freshness without a reload signal

The freshness heartbeat polls `/cache-status` and updates the badge
to reflect the server's current `fetchedAt`. But the page's `DATA.*`
payload is baked in at serve time — background warmups update the
file on disk but don't push new content to open tabs. Without a
reload signal, the badge can say "Fresh 10:00" while the content
still reflects the 8:00 load.

**Rule**: any page that embeds data at serve time must inject the
served-data timestamp as `window.__PAGE_FETCHED_AT__`. The freshness
heartbeat must compare this against the server's live `fetchedAt`
and show a visible reload prompt when they diverge. Never let the
freshness badge imply content currency it cannot guarantee.

### 2026-05-04 — Holistic duplication audit before serve

The dashboard renders the same deal in multiple sections when section
assignment is computed from independent sources (e.g. Live Deals
reads `data/deals/<TICKER>/deal.md`, Origination reads
`config/deal-config.yaml > dealOrigination`). Symptom: the same deal
appearing in two surfaces with divergent freshness — confusing to
the reader and a sign of source drift.

**Rule**: a deal must appear in exactly one canonical section based
on a single field — `stage` from `data/deals/<TICKER>/deal.md` is
authoritative. `Sourcing`/`Sourcing / Auto` → Origination only.
`Active Diligence`/`Diligence`/`Live` → Live Deals only. Compile-time
renderer must dedupe by canonical key (`_CP_ALIASES`-normalized name)
before splitting into sections; if a name resolves to two records,
the section assignment derived from `stage` wins and the entry from
the other section is dropped with a stderr warning.

**Companion rule**: when the same deal exists both as a
`data/deals/<TICKER>/` directory AND as a hand-curated
`dealOrigination[]` row in `deal-config.yaml`, the directory is the
source of truth for `name`, `contact`, and `stage`; the YAML row
supplies presentation fields (`task`, `myAction`, `nextStep`) only.
A divergent `name` between the two is a bug — fix the YAML, not the
directory.

### 2026-05-04 — Fundraising / pipeline sections must auto-populate from transcript-derived counterparties

Capture pipeline activity can reach `awaitingExternal[]` with the
right items but never write a `prospectiveInvestors[]` (or
equivalent) row to `deal-config.yaml` or surface the activity on the
fundraising card. Result: real activity sits invisibly in the
awaiting list.

**Rule**: any compile step that builds the fundraising or pipeline
surfaces MUST scan `awaitingExternal[]` and intel arrays for new
counterparties before serve. If a `counterparty` (firm) appears with
≥2 items and is not present in the relevant config array, surface it
as a "candidate to add" badge with a one-click promote action — do
not let it sit invisibly in the awaiting list.

**Detection**: enumerate counterparties in awaitingExternal that
appear ≥2 times and are not present in any `*-config.yaml` typed
array. The output should be empty after every compile.

### 2026-05-04 — Past-date awaiting items: extract takeaway, don't just dismiss

Strengthens the 2026-05-01 "Passed due dates are signals of
completion" rule. When a scheduled call/meeting date has passed, the
call almost certainly happened. The right action is not to dismiss
the item silently — it is to (1) check the transcript folder for a
transcript with a matching date and counterparty, (2) extract the
takeaway from that transcript, (3) write it as the new
`nextStep` / `takeaway` on the deal record, then (4) dismiss the
original scheduling item.

**Rule** for `_auto_expire_stale_events()` and any successor: when a
scheduling item with a past `due` date matches an `awaitingExternal`
cluster for a known counterparty, perform a transcript lookup before
dismissing. If a transcript exists, log
"completed via transcript: <doc_url>" to stderr and propagate the
call's takeaway to the deal/LP record. Silent dismissal without
takeaway extraction loses information.

### 2026-05-04 — Action item phrasing: lead with firm, then individual

Action surface labels that lead with the individual fail the
top-down reading model. The firm anchors the action in the user's
mental model; the individual is a secondary detail. Same rule for
`nextStep`, `myAction`, and any deal-card action chip.

**Rule**: any code that composes an action label from
`(counterparty, contact, action)` MUST format as
`"<Firm> (<Person>) — <verb-first action>"` when both are known, and
`"<Firm> — <action>"` when only firm is known. Never
`"<Person> — <action>"` unless the person has no firm context (rare).
Apply in `cos-dashboard-fetch.py` action-label composition and in
team-actions rendering.

### 2026-05-04 — Per-call dashboard-touch overlay must be the canonical promotion path

Every transcript ingestion produces an overlay listing how the call
touches the dashboard (deals, LPs, follow-ups, awaiting items). When
that overlay omits valid touchpoints — e.g. awaiting items generated
for a counterparty firm but no promotion suggestion into the
fundraising or pipeline section — the overlay is unreliable and
downstream surfaces silently miss content.

**Rule**: the call-touch overlay is the canonical promotion path.
If a call surfaces a counterparty firm with ≥1 awaiting item AND
that firm is not yet in `deal-config.yaml`, the overlay MUST emit a
"promote to fundraising" or "promote to liveDeal" suggestion. Audit
the overlay generator (likely in `cos_otter_backfill.py` post-extract
or a dedicated overlay routine) for this code path; if missing, that's
the bug. The overlay is the surface that closes the loop between
transcript and config — a gap there means the rules above never fire.

### 2026-05-04 — Team action ownership default: deal lead, never a hardcoded principal

When a team action is tied to a specific deal and no explicit owner
is set, default `owner` to the deal lead. Hardcoding any single
person as a universal default creates accountability confusion and
mis-attributes work.

**Rule**: any code that composes Team Actions from deal records MUST
resolve missing `owner` via `data/deals/<TICKER>/deal.md > owner`
field as fallback. If neither the action record nor the deal record
specifies an owner, log a stderr warning naming the deal — do not
silently default to a principal.

**Detection**: enumerate `liveDeals[]` and `dealOrigination[]` rows
and confirm every row has an explicit owner that matches the deal
lead in `data/deals/`.

### 2026-05-04 — Transcription error registry: codify aliases in `BACKFILL_PREAMBLE`, not just at compile time

The Otter transcript LLM mis-transcribes some firm names
consistently (homophones, near-homophones, common-word collisions).
A post-hoc replace in `cos-dashboard-fetch.py` would be a filter; the
prompt-level correction stops the bad name from ever entering the
pipeline.

**Rule**: maintain a known-transcription-corrections block in the
LLM extraction prompt (`BACKFILL_PREAMBLE` in `cos_otter_backfill.py`
and the email extractor). Each entry: `"<wrong>" → "<right>" (in <context>)`.
Add new entries the moment a homophone collision is observed.
Specific incident-driven aliases live in the private incident log.

This is Operating Principle #8 ("Fix the source, not just the
symptom") applied to transcription.

**Companion rule**: when a user reports a wrong firm name in
dashboard output, do not just edit the visible record — search the
entire `data/deals/` tree (`grep -rn '<WrongName>' data/deals/`) for
the same error and fix every occurrence in one pass. Otherwise the
error reappears at next compile.

### 2026-05-04 — Holistic broad-lens audit pass before any /dash close

Every `/dash` session MUST perform a holistic audit before commit. A
narrow session that ships a single fix without scanning adjacent
surfaces lets symmetric bugs persist.

Minimum checklist:
1. Same deal name appearing in ≥2 sections of `deal-config.yaml` or
   across a `data/deals/` directory + a YAML row → flag.
2. `lastAction` date in YAML stale by >7 days vs. `last_activity` in
   the deal directory → flag.
3. `awaitingExternal[]` items past `due` by >2 weeks → flag for
   transcript lookup, not silent skip.
4. Counterparty firms surfaced in awaiting items with ≥2 hits but
   absent from `deal-config.yaml` → flag.
5. Action labels leading with a person rather than a firm → flag.
6. Team actions with a hardcoded principal owner on a deal whose
   lead is someone else → flag.

If any flag fires, fix at the source (deal directory or YAML) in the
same session. The audit is the antidote to whack-a-mole correction
loops where the user has to keep flagging the same class of error.

---

## TOPIC — TILE / DRILLDOWN COHERENCE

### 2026-05-04 — Tile clicks must scroll-highlight existing sections, never reveal hidden ones

A tile should let the user dive deeper into what they already see,
not surface hidden content that bypasses the page's normal
information architecture. A tile drilldown to a panel containing
sections that are NOT visible anywhere else on the parent tab
violates progressive disclosure.

**Rule**: a tile click MUST resolve to a scroll-highlight of existing
visible sections on that tab. Never to a hidden panel that contains
alternate sections. If a tile's content has no corresponding visible
section on the tab, the tile is over-reaching — either add the
section to the page properly, or remove the tile drilldown.

**Rule (general)**: every tile must declare a `scrollTarget` (CSS
selector for the section to highlight) rather than a `panelTemplate`.
If a tile cannot resolve to an existing visible section, the tile
should not exist on that tab.

---

## TOPIC — AWAITING-EXTERNAL HYGIENE

### 2026-05-04 — Awaiting Counterparties: organize by firm, dedup paraphrases, drop already-happened

The awaiting-external surface bloats quickly when extraction emits
per-message paraphrases, past-date scheduling items, fragmented
aliases, and phantom firms.

**Rule for the awaiting-counterparties UI surface**:
- Group by canonical firm (alias-resolved via `_CP_ALIASES`), not
  raw `counterparty` string.
- Within each firm group, dedup by content similarity (normalize
  whitespace + lowercase + strip dates; collapse items where the
  first 60 chars match).
- Retire items whose `due` is >0 days past (not 7) when the content
  matches a scheduling pattern AND a transcript exists for the same
  counterparty + date — meaning the call happened.
- Auto-supersede items in upstream workflow stages: if an "Awaiting
  NDA draft" item exists for firm X and an "Awaiting NDA review"
  item also exists for firm X with `addedDate` newer, the draft item
  is retired automatically.
- Drop items with `counterparty` matching extraction-noise patterns:
  role placeholders (`^assistant$`, `^attorneys?$`, `^[Uu]nknown$`,
  `^team$`), bare email addresses (`@gmail.com`-suffixed
  counterparties), and orphaned em-dash fragments.

Without these enforced upstream, manual cleanup repeats every week.

### 2026-05-04 — Counterparty extraction: drop firms that look like extraction noise

Extractors can produce counterparty values like generic role tokens,
bare email addresses, or orphaned em-dash fragments. These all
signal that the LLM failed to identify a real firm and emitted a
fallback or a misparsed token. They pollute the by-firm grouping.

**Rule**: in the extraction prompts, add an explicit rule: "If the
firm cannot be identified from the email/transcript, emit
`counterparty: ''` and tag `intel_type: 'unattributed'` — DO NOT emit
a generic placeholder ('assistant', 'attorneys', 'Unknown', 'team',
or a bare email address)." At compile time, items with
`counterparty == ''` are routed to a separate "Unattributed Items"
review queue rather than appearing in by-firm awaiting lists. This
is preferable to silently dropping them — review surfaces the
extraction failure for prompt tuning.

### 2026-05-04 — Stale-event regex must include slash-separated and noun-form variants

`_STALE_EVENT_PATTERNS` commonly misses natural-language forms with
slash-separated alternations or noun-positioned words. Add to the
regex:

- `meeting\s+(day|date)?[\s/]*(and|/)\s*time` — "meeting day/time",
  "meeting day and time"
- `meeting\s+(logistics|day\s+and\s+location|location)` —
  "meeting logistics"
- `confirm\s+\w+\s+\d+/\d+\s+(meeting|call|intro)` — "confirm
  Wednesday 4/29 meeting"
- `intro\s+(call|meeting)` — generic intro events
- `live\s+call|connect\s+live` — "Friday 5/1 live call"
- `catch.?up\s+(call|meeting|chat)` — catchup events

Update the pattern in `cos-dashboard-fetch.py` and add unit-test
fixtures for each new form. Without the broadened patterns,
stale-event auto-expire silently misses 50%+ of past-date scheduling
items the user has to manually dismiss every week.

### 2026-05-04 — Workflow-stage supersession rule

When the same counterparty has both an upstream-stage and a
downstream-stage awaiting item for what is clearly the same workflow
(NDA draft vs. NDA review; calendar invite vs. meeting confirmation;
teaser request vs. teaser delivery), the upstream item is retired
automatically with a "superseded by <id>" tombstone reason.

**Detection heuristic**: for each `(canonical_firm, content_topic)`
cluster, sort items by `addedDate`. If item N's content uses an
upstream verb ("draft", "send invite", "propose times") and item
N+1's content uses a downstream verb ("review", "execute", "confirm
<past tense>", "respond to redline"), item N is superseded.

**Rule**: implement in `cos-dashboard-fetch.py` after
`_promote_source_ref()` and before `_auto_expire_stale_events()`.
Log each supersession to stderr so prompt drift can be audited.

---

## TOPIC — DELETIONS / TOMBSTONE LOOKUP CONSISTENCY

### AA1 — Tombstone-id format MUST match the schema's stable id; never hash-an-id-that's-already-a-hash *(silent auto-correct)* [ENFORCED via tools/checks/check_aa1.py]

**Real-world failure (2026-05-05, with screenshots from desktop)**: 80+ tombstoned awaiting-external items kept rendering on the dashboard for weeks despite being in `data/user-state/deletions.json` with `source: "awaitingExternal"`. Root cause: the client-side render filter for awaiting items called `__isDel('followup', k)` — which computes `djb2('followup|<item-id>')` and looks for THAT hash in the `__DELETIONS__` Set. But the Set contains the RAW 8-char hex ids (because the server's `/item/delete` handler stores the raw `id` field directly when source=`awaitingExternal`). The hash never matched the raw id → ZERO awaiting items got filtered.

This bug existed for ~2 months and is the actual reason the user kept seeing past-event scheduling items, completed-meeting confirmations, and superseded NDA-draft items long after the underlying events resolved. All rules + tombstones from prior sessions WORKED — they wrote to `deletions.json` correctly. The render layer just never read them correctly for awaitingExternal. Tenant-specific incident details in private log.

**Rule**: when an item type already has a STABLE id field on it (extracted hash, UUID, db key), the deletion lookup is direct Set membership: `window.__DELETIONS__.has(item.id)`. Do NOT call `__isDel(source, item.id)` — that's for items whose id is computed from `djb2(source + '|' + content[:60])` (followups, recruit rows, relationship rows where the "id" is itself a content-hash key).

**Two distinct deletion-id schemas in this dashboard**:
1. **Content-hash schema** (followups, recruit, rel): no stable item id; the "id" used for tombstoning is `djb2(source + '|' + content[:60])` computed at delete time + filter time. Use `__isDel(source, content)`.
2. **Stable-id schema** (awaitingExternal, deal actions, build-backlog): items carry a stable `id` field (8-char hex from extraction). Tombstone stores `id: <raw>`. Filter uses `dels.has(item.id)` directly.

Mixing them silently fails: the filter never matches, items render forever.

**Detection**: write a test that tombstones one awaiting item via the API, reloads the page, asserts the item is not in the rendered DOM. Run after any change to a render filter.

**Fix shipped 2026-05-05**: `buildAwaitingExternal()` and `awaitingCount` calculator in `cos-dashboard.template.html` switched from `__isDel('followup', k)` to direct `dels.has(k)` against `window.__DELETIONS__`.

**Companion rule for new render filters**: every time you write a new `.filter(i => ...)` against an item array, audit which deletion-id schema applies. If items have a stable `id` field, use direct Set membership. If they don't, use `__isDel(source, content)`. Never use `__isDel('<some-source>', i.id)` — that's almost always wrong (you're double-hashing or hashing-an-already-hash).

---

## TOPIC — RULES-IN-PLACE vs RULES-ENFORCED-IN-CODE

### AA2 — Documented rules without code enforcement are not actually rules *(meta-rule)*

**Diagnosis (2026-05-05)**: `dash_corrections.md` accumulated ~30 rules over multiple sessions. Many were "documentation only" — capturing the IDEAL behavior with no compile-step or render-layer code actually enforcing them. Examples that bit:
- D3 (curated config wins over awaitingExternal): documented as auto-tombstone rule; no code ever auto-tombstoned overlapping items.
- M3 (followUps doc ranked above briefing prose): documented as analyst-pass discipline; no code reconciles when sources conflict.
- Y1 (relationship direction from email-traffic): documented as analyst-pass check; no code surfaces source-line attribution.

The user reasonably asks "why didn't the rules catch this?" — because most rules ARE the analyst pass, not the code. When the analyst pass is me, the rules apply. When the dashboard runs unattended for a week, only the code-enforced subset applies.

**Rule (codified 2026-05-05)**: every rule in `dash_corrections.md` MUST declare its enforcement mode in the section header:
- `*(silent auto-correct)*` — code in compile/render enforces; no user action.
- `*(extraction-prompt enrichment)*` — extraction prompt emits the right field at write time.
- `*(analyst-pass discipline)*` — applied during a `/dash` review only; not enforced live.
- `*(documentation — UI rule)*` — UI implementation lives in another session; not enforced live.

**Companion rule**: when a "documentation" or "analyst-pass" rule keeps getting violated by the unattended pipeline, promote it to "silent auto-correct" by writing the code. The rules log is for capturing the standard; the code is what enforces it.

**Action**: audit existing rules quarterly (or after a recurring failure like the awaiting tombstone bug). Each rule should have a clear answer to "if I close my eyes for a week, will the dashboard still uphold this rule?"

---

## TOPIC — RELATIONSHIP DIRECTION & OWNERSHIP

### Y1 — Counterparty relationship direction must come from source-traffic, not analyst guess *(documentation)*

When an analyst pass populates a `prospectiveInvestors` / `capitalRaisingAdvisors` row, the `owner:` and `myAction:` fields define who-does-what. Getting the direction wrong is high-cost: the user looks at the dashboard and sees an action attributed to themself when it actually belongs to a counterparty (or to another team member who was the one with the relationship).

**Rule (codified 2026-05-04 after a real failure)**: before writing `owner:` for a new or updated counterparty row, confirm the direction by checking:

1. **Email traffic**: who introduced whom? Search the email pipeline for the firm name. If the originating thread `from:` is a team member (not the principal), the relationship was sourced/owned by them — set `owner:` accordingly + `sourcedBy: "<introducer name / firm>"` capturing the upstream connector.
2. **Call-attendee context**: who attended the kickoff call? If the principal was on the call but didn't initiate the relationship, the owner is still whoever drove the intro.
3. **Action direction**: who is sending materials TO whom? If the counterparty is the originator (e.g., an investment bank pitching deal flow), the `myAction:` is "wait to receive" not "send" — and may even be empty if the counterparty is the active party.

**Failure pattern that drove the rule**: An advisory-bank entry was logged with `owner: <principal>` and `myAction: "Send teaser + data room for <deal>"` — implying the principal was sending materials to the bank. Actual: the bank was pitching deal flow to the firm; another team member was the call originator (per email-thread headers); the bank sends teasers TO the firm, not the other way. Correct config: owner = team member who originated the relationship, `sourcedBy:` capturing the upstream connector, `myAction: ""` (wait to receive). Tenant-specific incident details in private log.

### Y2 — "Action direction inversion" check at extraction time *(extraction-prompt enrichment)* [ENFORCED via tools/checks/check_y2.py]

The extraction prompts in `cos_otter_backfill.py` and `cos_email_backfill.py` should explicitly disambiguate **which side of the conversation owes the next action**. When a call/email surfaces "send teaser/CIM/data room", the extraction MUST identify which party is sending — by inspecting the role context (advisor pitching → counterparty sends; principal pitching → principal sends).

**Rule extension (2026-05-04)**: every `awaiting_external` envelope item already requires `owner: "external"` + `counterparty:` so that semantically the action is owed BY the counterparty TO the principal. The corresponding deal-config curated `myAction:` field must reflect THE PRINCIPAL'S ACTION (which can legitimately be "wait" / empty when the counterparty owes the next move). When in doubt, mark `myAction: ""` and `task: ""` rather than fabricate a verb.

---

## TOPIC — DEAL NARRATIVE TRACKING

### V1 — Per-deal activity log (auto-appended, no manual maintenance) *(silent auto-correct)* [ENFORCED via tools/checks/check_v1.py]

Every active deal needs a chronological narrative — "what happened over the last 14 days, how did the situation evolve, where do things stand now" — that the user can read in 30 seconds without reconstructing from raw followups. **NO manual log maintenance** (rejected as overhead). Auto-derived from extraction signal.

**Implementation (codified 2026-05-04)**:
- `deal-system-compile.py > _compute_deal_logs()` runs at every compile.
- For each deal in `data/deals/<TICKER>/`:
  1. Compute deal-tokens: canonical name + ticker + id + alias needles (via `firm_context.yaml > counterparty_aliases`).
  2. Scan `dashboard-data.json > followUps[]`, `awaitingExternal[]`, `dealIntel[]`, `originationInbox[]` for items whose `who`/`counterparty`/`content` matches any token (substring, case-insensitive).
  3. For each match, compute a stable `id = djb2(source|who|what|date)`. If the id is not already in `data/deals/<TICKER>/log.json > entries[]`, append.
  4. Cap at 200 entries per deal (rolling window, newest first).
- Output: per-deal `log.json` files + `recent_log[]` array (top 5 newest) on each deal in `deal-system-data.json`.
- The briefing handler renders a "Deal Activity Log" section with last 3 entries per deal.

**Idempotency**: stable djb2 id prevents re-appending the same item across compiles. Manual edits to `log.json` are preserved (the auto-append only adds, never deletes).

**Rule for analyst passes**: when curating a deal's takeaway/nextStep/myAction, READ THE LOG FIRST (M3 rule extension). The log is where the chronological narrative lives; the curated config is the synthesis.

**Rule for new deals**: when adding a new `data/deals/<TICKER>/` directory, the log.json file is auto-created on first compile that finds matching signal — no manual seeding needed.

### V2 — Auto-extracted entries beat manual narrative *(documentation)*

If the user manually appends to `log.json > entries[]` (or any future structured log), the auto-extract MUST NOT overwrite manual entries. The append-only invariant: extraction adds entries with new ids; manual entries (different id format or marked `manual: true`) are preserved indefinitely. The mechanism currently relies on djb2 id uniqueness and rolling window cap — manual entries get the same treatment as auto entries (rolled out at 200-entry cap, newest first).

If manual narrative becomes a workflow, add `pinned: true` flag that excludes from rolling-window eviction.

---

## TOPIC — DEAL CARD ENRICHMENT

### Z1 — Capital-need estimates required even when imprecise *(analyst discipline)*

When a transcript mentions concrete capital sizing (per-project equity gap, equipment capex, deposit equity, etc.), the deal's `phase_capital[]` array MUST capture it — even as an illustrative scenario with `notes:` explaining the assumption.

**Rule**: the deal-card render shows `capital_in_play` derived from `phase_capital[]`. If the array is empty/abstract, the dashboard says "$0 in play" and the deal looks dormant when it isn't. When the principal mentions "buy first site at ~$100M, 50/50 debt/equity = $50M equity" or a transcript captures "5 sites / 3-4 GW; equipment capex $1,200-1,500/kW; 500 MW initial ramp ≈ $750M equipment; 40% gap = $150-300M LC/PG" — those numbers go directly into `phase_capital[]` with `notes:` field citing the source.

**Companion rule**: when a deal's capital structure has multiple legitimate scenarios (e.g., a sourcing-fee model AND an outright site-purchase model for the same deal), capture EACH as a separate `phase_capital[]` entry with explicit `phase:` labels. Don't pick one and bury the other.

### Z2 — `expecting_from_counterparty` block in actions.md *(analyst discipline)*

When a counterparty promises to share specific information (purchase price, energization cost, term-sheet specifics, equipment package details), it lives as a tracked-expectation row — NOT as a generic "follow up" action. Different shape: the principal is waiting to receive, not waiting to act.

**Rule** (codified 2026-05-04): every `data/deals/<TICKER>/actions.md` SHOULD have an "Awaiting from counterparty" sub-section listing each promised deliverable with: counterparty name, what was promised, when promised (call/email date), and tracking status. Surfaces the "what's the counterparty going to send next" view distinct from the "what does TC owe next" view.

**Why this matters**: dashboards naturally bias toward "what do I owe?" (myAction). The reverse view — "what is OWED TO me?" — is the predictor of when the deal advances next. Without it, the user re-asks "what was the counterparty supposed to send me?" each call.

---

## TOPIC — PERSONAL-ITEMS PROSE LENGTH

### Z5 — Brief layman text on the Personal tab; no inline terminal commands *(documentation — content discipline)*

User feedback (2026-05-04): Personal tab items rendered with multi-step terminal-command prose ("1) Check Keychain: `security find-generic-password...` 2) Verify ~/dashboards/scripts/load-secrets.sh...") in the visible action column. The Status column is narrow; long technical text wraps awfully and is unreadable.

**Rule** for `data/user-state/personal-items.json` (and any analogous personal-action surface):

- `name:` is a 5–8 word imperative summary in plain language ("Confirm AI key reaches briefing routine"), NOT a multi-clause technical title.
- `nextStep:` is ONE plain-English sentence (under 25 words) describing what will change in laymans terms. NEVER includes terminal commands, file paths with backticks, or step-numbered procedures.
- `myAction:` is a verb-first 5–10 word imperative ("Trigger manual run + watch for run.log"), NOT prose.
- Multi-step technical implementation detail goes in a separate `implementation_notes:` field — hidden by default in the rendered view; expandable for the engineer who will execute.

**Rationale**: the Personal tab is the principal's morning-glance surface. He reads it in 30 seconds. Technical step-by-step procedures belong in a runbook, not on the dashboard. When the dashboard surfaces a sysadmin task, the principal needs to know WHAT will change — not the commands to make it happen.

**Detection**: `python3 -c "import json; pi=json.load(open('data/user-state/personal-items.json')); [print(r['name'],'->',len(r.get('nextStep') or '')) for r in pi['items'] if len(r.get('nextStep') or '') > 200]"` — any nextStep over 200 chars probably violates the rule.

---

## TOPIC — PERSONAL TAB LAYOUT (UI BACKLOG)

### Z6 — Personal tab grid columns broken on narrow viewports *(documentation — UI rule)*

**Trigger**: User feedback 2026-05-04 with screenshot — the Personal tab three-column outer grid (Job Search | Personal | Deal Pipeline) renders catastrophically below ~1100px wide. The "Status" header wraps to one character per line ("S/T/A/T/U/S"), action text wraps to one character per line, and the page is unreadable on a 13" laptop with the dock visible.

**Surface**:
- File: `/Users/ygontownik/cos-pipeline/templates/cos-dashboard.template.html`
- CSS: `.grid-3` (line 65) — outer three-column container.
- CSS: `.pt-row` (line 385) — inner 4-column grid `minmax(160px, 1.2fr) minmax(0, 1.6fr) 110px 80px`.
- Existing mobile fallback at `@media (max-width: 780px)` (lines 838–846) collapses the inner grid but does NOT cover the 780–1100px middle band, which is where the breakage shows up.
- Affected panels rendered at lines ~2560 (Job Search), ~2835 (Personal), and the Deal Pipeline column on the Personal route.

**Data source**: pure CSS — no data shape change. The misrender is a layout issue, not a data issue.

**Behavior spec**:
1. Add a NEW breakpoint `@media (max-width: 1100px)` that collapses `.grid-3 { grid-template-columns: 1fr !important; }` for the Personal route. Don't widen the existing 780px breakpoint — keep its current effect for true mobile.
2. Within each panel, `.pt-row` should ALSO collapse to two columns (Item | Action+Status+Task stacked) when the panel itself is narrower than 280px. Implement via container query `@container (max-width: 280px)` with `container-type: inline-size` on the panel wrapper, OR a second media-query band at `(max-width: 880px)` that stacks the inner grid even before the outer collapses.
3. On the firm-name cell (first child of `.pt-row`) add `text-overflow: ellipsis; white-space: nowrap; overflow: hidden;` plus `title="${esc(t.name)}"` for hover reveal of the full string. This is in addition to the existing `min-width: 0; overflow-wrap: break-word` (which alone is insufficient).
4. The Status pill column must never render narrower than 80px or hidden — if there isn't room, stack the row instead.

**Companion rule (general)**: any grid layout rendered on the dashboard must have a single-column-collapse breakpoint AND a per-cell ellipsis fallback. Manual smoke test: resize to 800px wide and to 1024px wide; no text should wrap to one character per line.

**Acceptance**:
- At 1024px viewport, the Personal route renders three columns stacked vertically (or two-up if you prefer that intermediate band — call it out in the PR).
- At 800px viewport, no text in any `.pt-row` wraps to one-character-per-line.
- At 1280px+ viewport, the existing three-column layout is unchanged.
- Hovering a truncated firm name shows the full string via `title`.

---

## TOPIC — DASHBOARD UX (FUNDRAISING ALWAYS-VISIBLE + CLICK-TO-HISTORY)

### Z3 — Fundraising panel must be visible on HQ without drilldown *(documentation — UI rule)*

**Trigger**: User feedback 2026-05-04 — fundraising activity is core to the principal's day; it should NOT require a tile drilldown to surface. The HQ (Status route, `/`) top row currently renders Fundraising as one of three cards via `buildStatusTopRow()`, but the column is conditionally hidden when a tile-drilldown overlay is active, and is missing entirely on the briefing/post-capture re-render path.

**Surface**:
- File: `/Users/ygontownik/cos-pipeline/templates/cos-dashboard.template.html`
- Function: `buildStatusTopRow()` (line 1940) — the `[TC Overview | Fundraising | Team Actions]` three-column wrapper.
- Function: `buildFundraisingCard()` (line 1952) — the actual card body.
- The grid is hard-coded: `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;...">` (line 1942). Any code path that replaces the HQ top row's innerHTML with a tile drilldown's content must keep this row above the drilldown, not replace it.

**Data source**: `TOMAC_CONFIG.capitalRaisingAdvisors[]` and `TOMAC_CONFIG.prospectiveInvestors[]` (line 933, sourced from `__TOMAC_CONFIG__` server-injected blob). Plus `DATA.lpData[]` for the count chip. No new server endpoint required — the data is already on the page.

**Behavior spec**:
1. The HQ Status route (`/`) top row MUST always render `[TC Overview | Fundraising | Team Actions]` — three columns, no conditional rendering, no replacement on tile click. Tile drilldowns render BELOW this row, not in place of it.
2. When `capitalRaisingAdvisors[]` and `prospectiveInvestors[]` are both empty, render the Fundraising column as an empty-state card per H4 ("No active fundraising activity") rather than removing the column — preserving the three-column visual rhythm.
3. The deal-pipeline tile drilldown remains a counterparty-lens deep-dive (per prior tile-drilldown rule) but renders as a section below the always-visible top row.
4. On viewport collapse (< 1100px) the three columns stack vertically (per Z6) but order is preserved: TC Overview, then Fundraising, then Team Actions.

**Acceptance**:
- Loading `/` with any combination of TOMAC_CONFIG content shows three top-row columns; Fundraising is one of them.
- Clicking any deal-pipeline tile reveals a drilldown BELOW the top row; the Fundraising column remains visible above.
- A page state with zero advisors and zero investors still renders a "No active fundraising activity" card in the middle column.
- No JS path can `display:none` or `replaceChild` the Fundraising column without also replacing the entire top row.

### Z4 — Click-on-name → history drill *(documentation — UI rule)*

**Trigger**: User feedback 2026-05-04 — clicking a counterparty / advisor / deal / contact name today opens `showPriorityTarget()` (template line 3350) which only shows the curated config row from `recruit-config.yaml` / `deal-config.yaml`. The user wants the underlying chronological history (calls + emails + follow-ups + decisions) on the same modal.

**Surface**:
- File: `/Users/ygontownik/cos-pipeline/templates/cos-dashboard.template.html`
- Function: `showPriorityTarget(rowKey)` (line 3350) — the existing modal entry point. Extend, don't replace.
- Click sites that already route here: `.pt-row` (line 2480), `.tc-orig-row` (lines 2760, 3704), and the recruit-row variant (line 2553) which uses `showRecruiterModal` — apply the same extension there.
- Modal container: `<div id="modal" class="modal-overlay hidden">` (line 874).

**Data source**: all data is already shipped to the client; no new endpoint. Sources, in priority order:
1. `data/deals/<TICKER>/log.json` — V1 auto-log of decisions / activity per deal. Loaded via `DATA.dealLogs[ticker]` if present, else fetched lazily via `GET /deal-log?ticker=<TICKER>`. Match the click target's `rowKey` → ticker via the alias map (`__cpClusterKey`).
2. `DATA.followUps[]` — filter where `__cpClusterKey(item.counterparty) === __cpClusterKey(targetName)` OR `item.target_name === targetName` OR any alias from `_CP_ALIASES` matches.
3. `DATA.awaitingExternal[]` — same cluster-key filter as (2).
4. `source_ref.doc_url` on each above record — already populated, used as the per-row "open original" link.

**Behavior spec**:
1. Modal layout: existing curated-config row stays at top (header). Below it, a new "History" section renders a chronological timeline (newest first) merging sources 1–4.
2. Each timeline entry: `[date]  [source-type pill: call|email|followup|decision]  [one-line summary]  [→ link to source_ref.doc_url]`.
3. Decision marker glyph on entries flagged as decisions (presence of `decision: true` or non-empty `result` field): `✓` resolved, `⚠` blocked, `⏳` pending. Render in the source-type pill color slot.
4. Empty-history fallback: render `<div class="empty-state">No logged history yet — first activity will appear here.</div>` rather than collapsing the section.
5. Performance: dedup by `(date, source_ref.doc_url, summary[:60])` to avoid the same email surfacing as both a follow-up and an awaiting-external item.
6. Accessibility: timeline entries are list items, keyboard-navigable; pressing Enter on an entry opens `source_ref.doc_url` in a new tab.

**Acceptance**:
- Click a row whose name has at least one matching log.json entry, one followUp, and one awaitingExternal — modal shows all three in chronological order, deduped where they overlap.
- Click a row with no matches — modal shows curated config header plus the empty-history fallback (no JS error, no blank panel).
- Click a recruiter row — same modal extension applies via `showRecruiterModal`.
- Existing modal close behavior (overlay click, Esc key) still works.

---

## TOPIC — CONFIG PATH PRECEDENCE & SYMLINK DISCIPLINE

### W1 — Tenant configs: cos-pipeline-config-<slug> wins; dashboards/config is a symlink *(silent auto-correct + analyst discipline)*

**Real-world failure pattern (incident: 2026-05-04)**: `_resolve_deal_config_path()` in `cos-dashboard-server.py` checks paths in priority order:

1. `$COS_CONFIG_DIR/config/deal-config.yaml` (env-var override)
2. `~/cos-pipeline-config-<slug>/config/deal-config.yaml` (tenant repo)
3. `~/dashboards/config/deal-config.yaml` (legacy path)

When the tenant repo path exists, the dashboards-path file is silently ignored. Multiple analyst passes edited `~/dashboards/config/deal-config.yaml` thinking they were editing the canonical file; the server kept reading the older tenant-repo copy. Result: every "fix" appeared to apply but the live dashboard rendered stale state.

**Rule (codified 2026-05-04)**: any config file that has a tenant-repo equivalent (`~/cos-pipeline-config-<slug>/config/<name>.yaml` or similar) MUST be symlinked from the dashboards path to the tenant-repo path. This makes the dashboards-path edit-target visibly the SAME file the server reads. No silent precedence inversion possible.

**Detection**: `for f in ~/dashboards/config/*.yaml ~/dashboards/config/*.json; do tenant=~/cos-pipeline-config-<slug>/config/$(basename "$f"); if [ -f "$tenant" ] && [ ! -L "$f" ]; then echo "DRIFT: $f is a regular file but tenant copy exists at $tenant"; fi; done`

**This session's fix**: `~/dashboards/config/deal-config.yaml` symlinked to `~/cos-pipeline-config-<slug>/config/deal-config.yaml`. Same fix recommended for `recruit-config.yaml`, `email-capture.yaml`, `strings.yaml`, `user-tasks.yaml`, `users.json`, `deal_buckets.json` if/when the tenant-repo migration completes for those files.

---

## TOPIC — BROWSER CACHE / RESPONSE HEADERS

### X1 — HTML responses must set `Cache-Control: no-store` *(silent auto-correct)*

Server-side data (deal-config, recruit-config, fundraising user-state, deletions, etc.) is freshly injected into the HTML on every page-serve via `_load_*` calls. But if the browser caches the HTML response, a reload-after-sync serves the stale cached page — config edits the user just made appear to not take effect.

**iOS Safari** is the primary offender: it aggressively bfcaches HTML responses across navigation, even with same-URL reloads.

**Rule (codified 2026-05-04 in `_serve_html()`)**: every HTML response sets `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` + `Pragma: no-cache` + `Expires: 0`. Page weight is small (~3MB gzipped); the bandwidth cost of always-fresh is negligible vs. the correctness cost of stale.

**Companion rule for the sync button**: `tcSyncAll()` in `_topnav.html` triggers `window.location.replace(url + '?_t=' + Date.now())` after the `/refresh-all` POST completes. The query-param cache-buster forces a new URL → bypasses bfcache even on browsers that ignore Cache-Control. Without the cache-buster, iOS Safari can still serve the bfcached page.

---

## TOPIC — UNIFIED EXTRACTION + READTHROUGH STANDARD

Codified 2026-05-04. Extension to the lifecycle standard below — addresses the question "is the same overlay applied across all transcript sources, and how do we tie market intel back to active deals?"

### U1 — Extraction-pipeline parity *(silent + extraction-prompt enrichment)*

Every extraction prompt that produces narrative content (call transcripts via Otter, conference-call transcripts via the same pipeline, email threads, podcast memos, market-fetch summaries) MUST emit a `mentioned_firms[]` array containing every firm/organization name surfaced — actionable AND passing references. Powers the G5 inverse-audit sweep. Without this, we get the "X was in the briefing for weeks but had zero dashboard presence" failure mode.

**Status of each pipeline (2026-05-04)**:
- `cos_otter_backfill.py` (calls + conference calls): ✅ updated — emits `mentioned_firms[]`, `state`, `confidence`, `resolution_source`.
- `cos_email_backfill.py` (Gmail threads): ✅ updated — same fields.
- `podcast_transcribe.py` (industry podcasts): ❌ deferred — outputs prose memos to Google Docs, not JSON. Needs a structured machine-readable tail OR a separate JSON extraction record. Captured as deferred TODO.
- `cos_market_fetch.py` (RBN/Jefferies/etc. market briefs): ❌ deferred — same shape issue.
- Other pipelines (`cos_personal_briefing.py`, `cos_gmail_mini_v2.py`): not extractors; aggregators or pollers — no enrichment needed.

When a new extraction pipeline is added, U1 compliance is part of the acceptance criteria.

### U2 — Market intel ↔ deal readthrough *(silent auto-correct, partial)* [ENFORCED via tools/checks/check_u2.py]

Market briefings, podcast intel, and research feeds frequently surface signal relevant to specific active deals (e.g., a Jefferies note on ERCOT pricing matters for a Texas land deal; a podcast on hyperscaler power matters for a BTM gas play). The dashboard MUST connect intel to deals — silently — so deal cards and the daily briefing can call out readthroughs.

**Implementation (2026-05-04)**:
- `deal-system-compile.py > _compute_deal_readthroughs()` joins `dashboard-data.json > marketCommentary[].sections[].items[]` against per-deal tokens (canonical name, alias needles, sector words, geography words, tagline keywords). Filters generic words via deny list. Two confidence tiers: high (firm/alias hit) and medium (sector/geo/tagline hit ≥6 chars OR ≥2 hits).
- Output: `recent_readthroughs[]` array on each deal in `deal-system-data.json`. Capped at 5 per deal.
- The server's `/briefing/intel.json` handler appends a "Deal Readthrough" section to the briefing fullText, listing matched intel per deal.

**Future enhancement** (deferred TODO):
- Extraction-time hints: when the LLM extracts a market brief or podcast memo, emit `deal_readthrough[]: [{deal_id, relevance, evidence_quote}, ...]` based on its understanding. Compile takes the LLM's hints (high precision) and combines with keyword-join (high recall).
- Until then: keyword-join alone catches the obvious cases (a sector keyword matches a deal whose tagline carries the same word) but misses conceptually-relevant but vocabularly-distant cases (a counterparty-related news piece may not name the deal directly).

### U3 — Generic extraction prompts live in cos-pipeline (public repo)

Extraction prompts that operate on tenant-agnostic logic MUST live in the public `cos-pipeline/` repository, parameterized by `firm_context.yaml`. Tenant-specific data (deals, configs, transcripts) lives in the tenant repo (`dashboards/`).

**Status (2026-05-04)**:
- `cos_otter_backfill.py` ✅ public, parameterized via `_fc.load_firm_context()` and tokens like `_PRINCIPAL_FIRST`, `_DEAL_WS`, `_OWNERS`, `_PEER_FIRMS`.
- `cos_email_backfill.py` ❌ in tenant repo. EMAIL_PREAMBLE has hardcoded principal name, firm name, deal names. Needs `firm_context` parameterization before move. **Deferred — see U4.**

### U4 — Architectural debt: parameterize before moving *(documentation)*

When a tenant-specific extraction prompt is identified that should be in the public repo, the move requires a parameterization pass:

1. Audit hardcoded references (principal name, firm name, deal names, contact names, email addresses).
2. Replace with placeholders sourced from `firm_context.yaml` via `_firm_context.py` helpers (mirror `cos_otter_backfill.py`).
3. Add tenant-data context to the dynamic-block portion of the prompt (per-call data already does this).
4. Test with the live tenant context AND a synthetic empty-tenant fixture.
5. Move file to public repo + create symlink in tenant repo.

**Outstanding items**:
- `cos_email_backfill.py` — needs steps 1–5. Estimated 2-3 hours of careful refactor.
- `dash_corrections_proposer.py` — same parameterization need; lower priority since it's mostly tenant-coupled by design.

Don't block other work on this. Adopt as a milestone target (within 2 weeks of identification) but not a sprint blocker.

---

## TOPIC — ITEM LIFECYCLE STANDARD (Genesis / Maturation / Resolution)

Codified 2026-05-04 after a holistic pass on what makes items appear on/off the dashboard correctly. The 22 rules below extend the earlier topic-specific rules with a unified lifecycle model.

**Applied via** (one of three modes per rule):
- **Silent auto-correct** — compile/render code enforces; no user-facing alert.
- **Extraction-prompt enrichment** — `BACKFILL_PREAMBLE`/`EMAIL_PREAMBLE` emit richer fields so compile doesn't have to infer.
- **Documentation-only** — analyst-pass discipline; no live enforcement.

The user has explicitly chosen NO warn-only validators — the dashboard auto-corrects or stays silent; rules are the standard for analyst passes.

---

### GENESIS — how items appear on the dashboard

#### G1 — Confidence threshold for promotion *(extraction-prompt enrichment)*

A firm mentioned once in passing is not a new entry. Auto-promotion to `deal-config.yaml > capitalRaisingAdvisors[]` / `prospectiveInvestors[]` requires either ≥3 mentions across ≥2 source types within 14 days OR co-occurrence with an action verb (commit, send, schedule, intro, decided) on a single mention.

Extraction prompts emit `confidence: high|medium|low` on `new_contacts[]`. `low` mentions stay in a triage queue; only `high` (or repeated `medium`) candidates surface for promotion.

#### G2 — Schema validation at config load *(silent auto-correct)* [ENFORCED via tools/checks/check_g2.py]

Every row in `deal-config.yaml` and `recruit-config.yaml` requires: `name`, `lastAction`, `nextTouchBase` (or `movedToDormant`), `owner`, AND either non-empty `myAction` OR explicit dormant flag. Rows violating the schema are silently dropped from render with a single line to `/tmp/cos-dashboard.log` — no admin-tab alert.

#### G3 — Owner whitelist on curated config *(silent auto-correct)* [ENFORCED via tools/checks/check_g3.py]

Curated config `owner:` field accepts only values in `firm_context.yaml > team[]` (case-insensitive normalization for nicknames). Out-of-whitelist owners are treated as `owner: ""` at render and logged. Same enforcement that already applies to extraction-emitted owner field, now extended to curated configs.

#### G4 — No orphan deal directories *(silent auto-correct)* [ENFORCED via tools/checks/check_g4.py]

Every `data/deals/<TICKER>/` MUST contain `deal.md`, `actions.md`, `LPs.md`, `TERMS.md`. Missing files signal a half-created deal. `deal-system-compile.py > _assert_no_orphan_deal_dirs()` runs at every compile, logs the violation to stderr, and continues — no block, since compile may itself be the recovery path.

#### G5 — "What's missing?" inverse audit *(silent auto-correct + sidecar)*

Firms surfaced in `dashboard-data.json > followUps[]/awaitingExternal[]` ≥3 times in the last 14 days that do NOT appear in any curated config OR `data/deals/<TICKER>/` directory land in `data/compiled/g5-candidates.json` for analyst-pass triage. Catches the "ATT FTTH was in the briefing for weeks before anyone added it to config" failure mode.

---

### MATURATION — how items change while live

#### M1 — Single source of truth per fact *(documentation-only)*

Each discrete fact (commitment amount, owner, last meeting date, gating decision, milestone date) lives in exactly ONE field on the dashboard. Avoid duplicating between `takeaway` and `standingCommitments`, between `nextStep` and `myAction`. When the same fact appears in two fields, decide which field owns it and remove from the other. Auditor discipline; no validator.

#### M2 — `last_reviewed` distinct from `last_activity` *(documentation-only schema add)*

`last_activity` = something happened to the deal externally (call, email, transcript, action closure). `last_reviewed` = a human last opened the curated config row and confirmed the prose still reads true. A deal can have very recent activity AND a stale curated row simultaneously. Schema add (optional field); compile picks max of either for "freshness" surfaces. Adopt when the curated config edit volume justifies the distinction.

#### M3 — Fact-reconciliation hierarchy *(documentation-only)* [ENFORCED via tools/checks/check_m3.py]

When sources disagree on a fact (e.g., `deal.md` says `stage: Sourcing`, briefing fullText calls it "Active Bid"), the canonical hierarchy is:

1. **Calendar-confirmed event** (call took place, doc was signed) — overrides everything
2. **Followups doc + raw `awaitingExternal[]`** — high-fidelity ground-truth signal of who-owes-whom-what (each row is a primary observation, not an analyst's distillation)
3. **Curated config** (manually-edited `deal-config.yaml`)
4. **Compiled artifacts** (`deal-system-data.json`)
5. **Briefing prose** (LLM-summarized — DOWN-RANKED because it compresses many followUps into a few sentences and can lose nuance, mis-attribute, or speculate)

**Rule (codified 2026-05-04 after a real failure)**: when curating a deal's `takeaway` / `nextStep` / `myAction` during an analyst pass, READ THE FOLLOWUPS first. Filter `dashboard-data.json > followUps[]` for entries whose `who` or `what` mentions the deal/counterparty/asset. Synthesize curated prose from the FollowUps + transcripts (#2/#1), not from briefing prose (#5). Briefing fullText is useful for cross-reference but its "X proposal received" framing can be a paraphrase of "X proposal expected" — easy to mis-read as past tense when it's future.

**Failure pattern that drove the rule**: An analyst pass updated a deal's `next_milestone` to "<counterparty> proposal received" based on briefing summary; actual followUps showed the principal team was DRAFTING the term sheet itself (not waiting for a counterparty proposal) AND was actively building an alternative-financing counterparty list as the live workstream. The briefing summary had compressed many followUps into "awaiting <counterparty>" — easy to mis-read as past tense or as the only active workstream. Tenant-specific incident details in the private log.

**Detection one-liner**: `python3 -c "import json; d=json.load(open('data/compiled/dashboard-data.json')); [print(fu['who'],'|',fu.get('what','')[:140]) for fu in d['followUps'] if not (fu.get('what','') or '').startswith('[RESOLVED]') and any(k in (fu.get('who','')+fu.get('what','')).lower() for k in ['<deal_token>','<counterparty>'])]"` — replace tokens with the deal name + key counterparties.

#### M4 — Recency × relevance, not just recency *(documentation-only)*

Action sort prefers most-recent `addedDate` today. Add a `criticality` weight (high = bid/term sheet/IC; medium = diligence/intro/structure; low = scheduling/admin) so a 7-day-old "counterparty proposal received" doesn't get buried under a 1-hour-old "confirm Tuesday meeting." Implementation: `_overlay_freshest_signal()` already supports keyword weighting; extend with a `criticality` token table when needed.

#### M5 — Owner-change requires explicit confirmation *(documentation-only)*

When `owner:` field changes on an active config row, the change deserves a moment of human review (typo? extraction error? real reassignment?). Today changes are silent. Captured as discipline: when editing a config, double-check owner changes are intentional. Future enhancement: an owner-change diff lint at commit time.

#### M6 — Health-score-drop alert *(documentation-only)*

When a deal's `health` drops ≥10 points compile-to-compile, the change is meaningful. Future enhancement: a small chip on the deal card; not a banner. Today: rely on analyst-pass review of the deal-system-compile output line that prints scores.

---

### RESOLUTION — how items leave the dashboard

#### R1 — `Result` field required at close *(documentation-only)*

Every action moved to `## Closed items` in `actions.md` must have a `Result` field — what happened, what was the outcome, was it superseded. Today's closures already follow this; codify so future analyst passes don't lose the discipline.

#### R2 — Stale-tombstone garbage collection *(silent auto-correct)*

`data/user-state/deletions.json` grows monotonically as items get tombstoned. `deal-system-compile.py > _gc_stale_tombstones()` archives entries whose `id` no longer matches any current `awaitingExternal[]/followUps[]/personal-items[]` AND whose `deleted_at` is ≥90 days old. Archive lands in `data/user-state/deletions-archive.json` (restorable). Keeps the live tombstones file lean.

#### R3 — Auto-archive long-dormant *(documentation-only)*

Entries in `dormantInvestors:` / `priorityTargets.dormant:` / dormant recruiters with `movedToDormant` ≥6 months old should move to `archive/<year>/` directory. Preserves history; keeps active surfaces clean. Adopt when first 6-month dormancy threshold is hit.

#### R4 — Closure traceability — evidence reference *(extraction-prompt enrichment)*

When marking an action closed, the `Result` field SHOULD reference specific evidence: a transcript date, a follow-up `[RESOLVED]` tag, an executed document, a calendar event with attendees. Extraction prompts emit `resolution_source` on action_items where `state: closed`. Compile uses to populate Result automatically when closing actions inferred from extracted intel.

---

### HOLISTIC — cross-cutting standards

#### H1 — Explicit `state` field on every action item *(extraction-prompt enrichment)*

The dashboard infers item state today by parsing `myAction`/`nextStep` prose. That's brittle and produces the empty-myAction-wait-state acrobatics. Extraction prompts now emit `state: active|waiting|watching|blocked|dormant|closed` on every action item. Curated configs get the same field as a documented schema add — apply at the next batch edit, not as forced migration.

#### H2 — Action surface vs. intel surface separation *(documentation-only)*

The dashboard is for things the user **acts on**. Industry research, peer firm intel, market commentary — those are intel and belong in the briefing tab, NOT on HQ. Today's `peer_firms` denylist enforces this for one type. The general rule: items without `myAction` AND not blocking another action belong in a "context" surface, not on action surfaces.

#### H3 — Briefing cannot contradict the dashboard *(documentation-only)*

Briefing fullText and curated `deal-config.yaml > takeaway` for the same deal must agree on stage, owner, capital amount, named counterparty, deadlines. When they diverge, that's a bug in either the curated config (stale) or the briefing source (wrong). Future enhancement: a daily lint that diffs briefing prose against config takeaway and flags substantive divergence.

#### H4 — Empty-state rendering *(documentation-only — UI rule)*

**Trigger**: When all items in a section are dormant or filtered out, today's templates render an empty card with a header and zero rows — visually identical to a loading state, and indistinguishable from a render bug. Multiple analyst passes have flagged "is this section broken?" when it was simply empty.

**Surface**:
- File: `/Users/ygontownik/cos-pipeline/templates/cos-dashboard.template.html`
- Apply to all section renderers that loop over a list and emit rows: `buildFundraisingCard()` (line 1952), `buildTeamActionsCard()` (line 2007), `buildAwaitingExternal()` (referenced ~line 1874), `buildFollowUps()`, the priority-target panel renderers (~lines 2440, 2560, 2835), and the deal-pipeline tile renderer (line ~3556).
- Add a shared helper: `function emptyStateCard(message)` that returns a uniform grayed-out card body — re-use across all sections.

**Data source**: pure UI — triggered when the input array (after all filters: dormancy, tombstones, `[RESOLVED]` strip) is `length === 0`.

**Behavior spec**:
1. Each section renderer must check: after filters, if rows.length === 0, render `emptyStateCard(message)` instead of an empty body. Do NOT remove the card header — the user still needs to see what section is empty.
2. Standard empty-state copy by section (use these exact strings):
   - Fundraising: `"No active fundraising activity."`
   - Team Actions: `"No team actions queued."`
   - Awaiting External: `"Nothing waiting on counterparties right now."`
   - Follow-ups: `"No follow-ups queued."`
   - Live Deals: `"No active deals in this view."`
   - Priority Targets / Job Search: `"No priority targets in this bucket."`
3. Empty-state styling: `color: var(--ink-mid); font-style: italic; padding: 18px 14px; font-size: 12px;` — visually clearly an intentional blank, not a loading state.
4. Section header chrome (title, refresh button, count badge showing `(0)`) MUST still render. The count badge in particular signals "we checked, there are zero" vs. "we haven't loaded yet."
5. If a parent grid (e.g. the HQ three-column top row, Z3) requires a section to be present for layout reasons, empty-state honors that — never `display:none` the entire card.

**Acceptance**:
- Force-empty each section (e.g. by filtering all items out via the deal-config dormancy flag) — every section shows its specific empty-state message with the count badge `(0)` in the header.
- No section renders as a header with zero body content (the bug we're fixing).
- The HQ three-column top row (Z3) remains visually balanced when one column is empty.

#### H5 — Past-due staleness escalation visibility *(documentation-only — UI rule)*

**Trigger**: Past-due items today blend visually with future-dated items in the same list. The principal's eye doesn't catch them. Multiple sessions found "this counterparty has been waiting 23 days" buried mid-list with no visual escalation.

**Surface**:
- File: `/Users/ygontownik/cos-pipeline/templates/cos-dashboard.template.html`
- Apply to: `.pt-row` rendering (line 2480), `.fu-item` follow-up rows, `.dp-row` deal-pipeline rows, awaiting-external rows in `buildAwaitingExternal()`.
- Existing precedent: `.pt-row.pt-row-today` (line 442) already adds an amber left border for today-dated items. Extend this pattern — don't reinvent.

**Data source**: each row item's `due` / `nextTouchBase` / `expectedDate` field. The existing `urgencyLabel(t.nextTouchBase)` helper (line 1959) already classifies into `'overdue' | 'soon' | null`. Reuse — do not re-derive from raw dates.

**Behavior spec**:
1. Add a CSS class `.pt-row-overdue` (and analogous `.fu-item-overdue`, `.dp-row-overdue`) with:
   - `border-left: 3px solid #dc2626 !important;`
   - `background: rgba(220, 38, 38, 0.04);`
   - `padding-left: 5px;` (match existing tier-class padding)
2. Apply the class when `urgencyLabel(...).cls === 'overdue'`. The class wins over `pt-row-tier-1/2` and `pt-row-today` (use `!important` and class-order).
3. **Sort order**: overdue items render at the TOP of their containing section, before all other items, descending by `daysOverdue`. Within the overdue block, preserve any existing tier sub-ordering. Implement in the section renderer's sort comparator — not via DOM reordering.
4. Add an inline prompt label on each overdue row: `<span class="overdue-prompt" title="Click to classify">URGENT — classify or resolve</span>` rendered in the right-hand metadata column (next to the `urgLbl` pill, line 1974). Style: red text, uppercase, 9px, letter-spacing 0.06em.
5. Clicking the prompt opens the same `showPriorityTarget()` modal as a row click, but pre-scrolled to a "Classify this past-due item" CTA region (resolved / superseded / stage-graduated / re-propose / drop, per the Past-Due Item Resolution rules at line 1547).
6. Mobile fallback (≤780px): the prompt label collapses to a red dot indicator; full prompt visible only via the modal.

**Acceptance**:
- A row with `due = today - 5` renders with red left border, light-red background, and the "URGENT" prompt visible.
- All overdue rows in a section appear above all non-overdue rows regardless of original list order.
- Overdue rows on mobile still show the red border and dot indicator.
- Clicking the URGENT prompt opens the priority-target modal.
- Re-resolving an overdue item (advancing its date or marking resolved) immediately removes the overdue styling on next render.

#### H6 — Promotion / demotion as explicit transitions *(documentation-only)*

Movement of an item between buckets (awaiting → curated → dormant → archive) deserves an audit-logged transition. Future enhancement: `data/user-state/transitions.json` recording each transition with timestamp + reason. Today these are ad-hoc edits.

#### H7 — Rules-log itself needs maintenance *(documentation-only)*

`dash_corrections.md` is now substantial. Quarterly: audit each rule — still valid? still applied? Mark obsolete with `[SUPERSEDED-YYYY-MM-DD]` tag inline. Don't let the rules library become its own form of dashboard rot.

#### H8 — Cross-tab redundancy check *(silent auto-correct)*

Already implemented as `_assert_cross_config_dedup()` for deal-config × recruit-config. Extend to all visible surfaces if more bucket-types are added. Documented exception: name containing `(CURRENT ROLE)` in recruit-config — the principal's career anchor.

---

### Inverse audit summary

The genesis-to-resolution lifecycle plus 8 holistic rules — 22 in total — define the standard the dashboard must hold itself to. **Implementation summary**:

| Mode | Rules | Effort |
|---|---|---|
| Silent auto-correct | G2, G3, G4, G5, R2, H8 | Implemented in this session |
| Extraction-prompt enrichment | G1, H1, R4 | Layered into `BACKFILL_PREAMBLE` + `EMAIL_PREAMBLE` |
| Documentation only (analyst-pass discipline) | M1, M2, M3, M4, M5, M6, R1, R3, H2, H3, H4, H5, H6, H7 | No live enforcement; checked during analyst passes |

No warn-only validators. No admin-tab alerts. No real-time nags. The dashboard either auto-corrects, or quietly captures findings in stderr / sidecar files for the next analyst pass to review.

---

## TOPIC — DEAL STAGE & FRESHNESS

### 2026-05-04 — Canonical deal-stage ladder (ordered)

Every `data/deals/<TICKER>/deal.md > stage` value MUST be one of the following canonical labels (case-sensitive). `stage_index` mirrors the ordinal position so compile can sort:

| stage_index | stage | Meaning |
|---|---|---|
| 0 | `Watch` | Passive intel only; no engagement |
| 1 | `Sourcing` | Active research / dialogue; no commitment yet |
| 2 | `Active Bid` | The firm has put forward a position (bid, indication, term sheet) |
| 3 | `Diligence` | Engaged commercial diligence |
| 4 | `Advisory` | The firm holds a paid/structured advisor role |
| 5 | `Memo` | IC-memo stage |
| 6 | `IC` | At investment committee |
| 7 | `Live` | Closed / portfolio company |
| — | `Dormant` | Relationship paused; off active surfaces (separate sidecar) |

When a stage-graduating action closes (bid submitted, term sheet drafted, diligence opened, IC date booked), the `stage` AND `stage_index` MUST update in the same commit as the action's closure. **Banned**: leaving `stage: Sourcing` on a deal whose actions or briefing prose describe it as bid-active.

### 2026-05-04 — Stage-progression discipline

Companion to the ladder above. Acceptance criteria for stage advancement:

- `Sourcing → Active Bid`: a firm-side commitment has been formally communicated to the counterparty (bid, term sheet, equity indication).
- `Active Bid → Diligence`: counterparty has accepted/engaged on the bid; commercial diligence is open.
- `Diligence → Advisory`: firm role has been formalized in writing (paid advisory, observer, structured engagement).
- `* → Memo / IC`: an IC date is on the calendar.
- `* → Live`: closing documents are signed.
- `* → Dormant`: lastAction >30 days, no concrete next step, and the relationship has not been formally killed. Move to a `dormant:` sidecar.

A `/dash` audit that finds the stage label inconsistent with the deal's actions.md or recent briefing prose MUST update the stage in the same pass — don't silently note the discrepancy.

### 2026-05-04 — `last_activity` auto-derived from signals, not hand-edited

`data/deals/<TICKER>/deal.md > last_activity` is treated as a fallback only. The compile step (`deal-system-compile.py > overlay_fresh_signals()`) computes:

```
last_activity = max(
    dashboard-data.json > <slug>[].latestUpdate.date,   # tenant doc parse
    max(addedDate of followUps[] mentioning the deal),
    max(addedDate of awaitingExternal[] mentioning the deal),
    deal.md hand-edited last_activity
)
```

Tokens used to "mention the deal": canonical name, ticker, id (lowercased substring match against followUp `who`/`what` and awaiting `counterparty`/`content`).

### 2026-05-04 — `next_milestone` must always reference a future date [ENFORCED via tools/checks/check_next_milestone.py]

`data/deals/<TICKER>/deal.md > next_milestone_due` MUST be ≥ today. A past `next_milestone_due` means the milestone happened (close it; queue the next one) or slipped (roll forward with explicit reasoning). **Banned**: leaving a past `next_milestone_due` to render on the deal-pipeline mini-card.

**Detection**: `python3 -c "import json,datetime; t=datetime.date.today().isoformat(); ds=json.load(open('data/compiled/deal-system-data.json')); [print(d['name'], d.get('next_milestone_due')) for d in ds['deals'] if (d.get('next_milestone_due') or '') < t]"` — should be empty.

---

## TOPIC — PROSE / TIME-REFERENCE STALENESS (companion to next-week → week-of rule)

### 2026-05-04 — Curated config prose must not reference closed time windows

`takeaway` / `nextStep` strings in `deal-config.yaml` and `recruit-config.yaml` MUST NOT reference calendar-bounded events that have passed (e.g. "site visit Apr 28–May 2", "before the Dallas trip", "ranch visit window"). When the window closes, the prose goes silently stale and misleads any reader.

**Rule**: extend the 2026-05-04 next-week → week-of rule from extraction prompts to curated config files. When a `/dash` review finds a takeaway/nextStep referencing a closed window, it MUST be rewritten in the same pass.

### 2026-05-04 — Briefing/dashboard takeaway sync

When the daily briefing fullText surfaces deal context absent from the curated `deal-config.yaml > takeaway`, that context is fresh intel and must be reflected in the curated source. The briefing should be the highest-fidelity restatement; the dashboard takeaway should not lag the briefing's mention of the same deal.

**Detection**: `grep -i '<deal name>' /tmp/daily_briefing_*.txt` — diff the bullets against the curated takeaway for material new facts (named counterparties, committee meetings, geology/structural validation, deadline).

---

## TOPIC — ACTION-LIST INTEGRITY

### 2026-05-04 — `awaitingExternal ↔ myAction` mirror rule

When `awaitingExternal[]` contains a specific deliverable owed by the principal/team to a counterparty (data send, redline counter, Zoom set-up), the corresponding `deal-config.yaml > myAction` for that counterparty MUST cite the specific deliverable, not paraphrase as "follow up."

**Rule**: at compile time (or at the analyst review step), for each curated entry that has matching awaitingExternal items, surface a warning if the myAction string does not contain ≥1 noun from the awaiting content. Cheap signal; high-value.

### 2026-05-04 — Workflow-gate explicitness

When a myAction is gated on a sub-decision ("after aligning with X on Y", "once Z resolves"), the gate is itself an open team-action and MUST surface as one — assigned to the gate-holder. Do not bury blocking decisions inside dependent action text.

**Pattern**: split a gated myAction `"Send X to Y after aligning with Z on W"` into:
- A blocker action: `"Decide W with Z — <downstream> blocked until resolved"` (owner = Z)
- A dependent action: `"Send X to Y (post W decision)"` (owner = original)

### 2026-05-04 — Empty-myAction → wait-state explicit

If a curated config row has `myAction: ""` AND nextStep names a dependency, set myAction explicitly to `"Wait — gated on <X>"`. Empty myAction with a dependency dependency is silent ambiguity. Alternative: move the row to a dormant sidecar.

### 2026-05-04 — Counterparty-commitment categorization

`awaitingExternal[]` is for items pending a counterparty's response that affects YOUR pipeline. The counterparty's own commitments-to-you (e.g., "X pledged $Y when you bring a deal") belong in a `standingCommitments:` array on the prospectiveInvestor / advisor record. Do NOT route counterparty pledges into the awaiting queue — they create false noise.

### 2026-05-04 — Activity-volume vs. nextStep freshness

When a recruiting target or deal has ≥3 follow-ups in the last 7 days, the curated `nextStep` MUST reflect the most recent activity. A static nextStep that is older than the most recent burst of follow-ups is stale and misleading.

**Detection** (per `/dash` review): for each recruit-config row, count `followUps[]` whose `who` matches in the last 7 days; if count ≥3 and the row's `lastAction` < oldest-of-the-three, flag for refresh.

---

## TOPIC — DORMANCY & SCOPE

### 2026-05-04 — Dormancy classification rule

Counterparties (LP advisors, prospective investors, recruiting targets, recruiters) with `myAction: ""` AND `lastAction` >30 days old MUST be moved to a dormant sidecar — NOT left in the active list. Active surfaces are for items the user touches; dormant is a separate watchlist.

**Implementation**:
- `deal-config.yaml > dormantInvestors:` array (sibling of `prospectiveInvestors`).
- `recruit-config.yaml > priorityTargets.dormant:` array (sibling of `inDiscussion / waitingToHear / doIChase`).
- Recruiter rows: add `status: dormant` field on the row itself; UI filters out unless dormant view is opted in.

Each dormant entry carries `movedToDormant: YYYY-MM-DD` and a `nextStep` that names the reactivation trigger (e.g., "Reactivate if a concrete role surfaces via the introducing firm").

**Reactivation**: when a fresh signal arrives (call, email, intro), move the row back to the active bucket AND update lastAction.

### 2026-05-04 — Recruiting-bucket scope rule

`recruit-config.yaml > priorityTargets / recruiters` are for ACTIVE job-search interactions. Items where the underlying relationship is operational (deal counterparty, M&A history, prior-employer commodity overlap, vendor) MUST NOT be in recruit-config — they belong in deal-config or a dedicated relationship tracker.

**Documented exception**: an entry in `priorityTargets.inDiscussion` whose name contains `(CURRENT ROLE)` is the principal's career anchor (e.g., the firm they currently work at / are co-founding) and is intentionally tracked here as the career-arc reference. The cross-config dedup assertion treats this as a special case.

### 2026-05-04 — Time-bounded one-time-event auto-removal

Personal items / awaiting items whose trigger has a known natural expiry (LinkedIn device-verification email, expiring offer codes, scheduled-event confirmations, calendar-invite RSVPs past the event date) MUST be auto-removed once the expiry passes. Extends the 2026-05-04 stale-event auto-expire patterns to the personal-items.json schema.

### 2026-05-04 — `pending-verification` status for items the user can't quickly confirm

Personal items where the close criteria is "did this run/work as expected?" and the user can't confirm at audit time get a `status: pending-verification` tag and roll forward 7 days. They are NOT silently closed and NOT left in the active list as if open.

---

## TOPIC — DEDUP / CONSISTENCY

### 2026-05-04 — Cross-config dedup assertion (deal-config × recruit-config)

An entity (firm name) in `deal-config.yaml` (any section) MUST NOT also exist in `recruit-config.yaml` (any section). The dashboard server runs `_assert_cross_config_dedup()` at module import (startup) and logs a warning per overlap to the server log. Documented exception: `(CURRENT ROLE)` suffix in recruit-config.

**Rule**: when adding a new entry to either file, grep the other for the firm name. If it exists in both, decide which surface owns it and remove from the other.

### 2026-05-04 — Alias-before-supersession order in workflow-stage matching

`_supersede_workflow_stages()` MUST canonicalize counterparty via `__cpClusterKey` BEFORE grouping items. Verified working in the current implementation — the bug was the upstream regex, not the grouping.

**Companion fix this session**: broadened the NDA upstream regex from `(draft|deliver|send\s+draft)\s+(mutual\s+)?nda` to `(draft|deliver|send|finaliz[ae]|issue|stand[\s-]?up|prepar(e|ing))\s+...\s+nda` so "Finalize and send NDA" matches and gets superseded by "Review NDA redline" on the same canonical counterparty. **Rule for future regex tuning**: when a supersession fails to fire on a known alias-grouped pair, the bug is the verb pattern, not the grouping.

### 2026-05-04 — Curated-config-wins over awaitingExternal (auto-tombstone duplicates)

When the same operational action appears both as a curated `deal-config.yaml > myAction` AND an `awaitingExternal[]` item, the awaiting copy is auto-tombstoned. Curated wins; awaitingExternal is the queue for not-yet-curated items. Implemented at compile time by matching canonical counterparty + content-token overlap.

### 2026-05-04 — Briefing must reference deal-config-known deals

Any deal mentioned in the daily-briefing fullText must correspond to a deal in `deal-config.yaml > liveDeals|dealOrigination` OR `data/deals/<TICKER>/`. Briefing-only deals (no curated config and no deal directory) MUST be auto-suppressed by the briefing pipeline before output. Either add the deal to config or drop from briefing.

**Implementation**: the briefing SKILL reads `deal-config.yaml` and filters its candidate list against the union of `liveDeals + dealOrigination + data/deals/*/` directories. A `briefingExclude:` array in deal-config.yaml provides explicit suppression for borderline cases.

---

## TOPIC — OUTCOME CAPTURE & OBSERVABILITY

### 2026-05-04 — In-person-meeting outcome capture rule

When a recruiting/deal target has a calendared in-person meeting and the date passes, the dashboard MUST flag for retro-takeaway capture. The meeting either happened (capture takeaway, advance the relationship) or was missed (re-propose). Either way it is NOT acceptable to leave the row showing "meeting next week" five days after the date.

**Detection**: any recruit-config / deal-config row whose `nextStep` mentions a date within the past 14 days AND `lastAction` < that date — flag for capture.

### 2026-05-04 — `briefingSynopsis.captureSummary` freshness assertion [ENFORCED via tools/checks/check_capture_freshness.py]

`/briefing/intel.json` returns `synopsis.captureSummary` with a `date` field. If `captureSummary.date < today - 1`, the capture pipeline didn't run today. Surface a warning chip on the briefing tab + log to stderr. With the routines catch-up agent live, the underlying capture should run at every wake — a stale captureSummary is now an actionable signal of pipeline failure, not an ambient background condition.

### I4 — Capture-staleness chip on briefing tab *(documentation — UI rule)*

**Trigger**: Capture pipeline failures (API quota, OAuth drift, plist not loaded) silently degrade the briefing — yesterday's content is shown today with no warning. The principal needs an unmissable chip on the briefing tab. Server-side comparison was added 2026-05-04 EOD so the frontend stops re-deriving and they don't drift.

**Surface**:
- File: `/Users/ygontownik/cos-pipeline/templates/briefing-dashboard.html` (and any HQ-tab briefing summary card if it appears on `/`).
- Render location: top of the briefing tab body, immediately above `synopsis.captureSummary` content. A horizontal pill chip, full-width or right-aligned in the section header.
- File for reference (server side, do NOT modify): `/Users/ygontownik/cos-pipeline/cos-dashboard-server.py` — `_handle_briefing_intel` (line 3176), payload assembly at line 3408, captureStaleness construction at lines 3380–3406.

**Data source**: `GET /briefing/intel.json` response field `captureStaleness`:
```
captureStaleness: {
  date: "YYYY-MM-DD" | "",       // last successful capture-summary date
  daysStale: number | null,       // integer days, null when severity=unknown
  severity: "fresh" | "warn" | "stale" | "unknown",
  message: "<human string>",      // pre-rendered, render verbatim
  blocker: "<diagnostic>" | null, // optional cause hint (e.g., "API quota")
}
```
Server rules already encoded: `severity = "warn"` for 2–3 days stale, `"stale"` for >3 days, `"unknown"` when no date is present, `"fresh"` for <2 days. **Do NOT re-derive the comparison client-side** — render the chip directly off `severity` and `message`.

**Behavior spec**:
1. When `severity === "fresh"`: render NO chip. Section is unchanged.
2. When `severity === "warn"`: render a yellow chip.
   - Style: `border: 1px solid #d4a017; background: #fef9e7; color: #7a5b00; padding: 6px 12px; border-radius: 4px; font-size: 12px;`
   - Icon: `⚠` prepended.
   - Body text: `captureStaleness.message` rendered verbatim (already user-facing).
3. When `severity === "stale"`: render a red chip.
   - Style: `border: 1px solid #dc2626; background: #fef2f2; color: #7a1a1a; padding: 6px 12px; border-radius: 4px; font-size: 12px; font-weight: 600;`
   - Icon: `⛔` prepended.
   - Body text: `captureStaleness.message`.
4. When `severity === "unknown"`: render a gray chip with the message; same dimensions, `border: 1px solid #94a3b8; background: #f8fafc; color: #475569;`.
5. If `captureStaleness.blocker` is non-null, append it after the message in parentheses, smaller font: ` <span style="font-size:11px;opacity:0.8">(${blocker})</span>`.
6. Chip is clickable → opens `/admin/#tab-routines` so the user can inspect the failing routine.
7. Refresh cadence: chip re-renders when `/briefing/intel.json` is re-fetched. Don't poll independently.

**Acceptance**:
- Force `severity:"warn"` (e.g., touch `synopsis.captureSummary` to 2 days old via test harness) — yellow chip renders with the server's exact message.
- Force `severity:"stale"` — red chip renders.
- `severity:"fresh"` — no chip.
- Click the chip → navigates to `/admin/#tab-routines`.
- Frontend code does NOT contain its own `today - cs.date` comparison; it only branches on `severity`.

---

## TOPIC — PAST-DUE ITEM RESOLUTION

### 2026-05-04 — Past-due deal action sweep: every passed date must be classified, not left [ENFORCED via tools/checks/check_past_due_actions.py]

When a `/dash` audit finds open or in-progress actions in `data/deals/<TICKER>/actions.md` (or any equivalent tracker) whose `due` date is in the past, the session MUST classify every one before closing. Acceptable resolutions:

1. **Resolved** — clear evidence the action completed (later follow-up tagged `[RESOLVED]`, a closed item in the same file, a transcript / awaiting-external item demonstrating the deliverable was received). Move the row to **Closed items** with a `Result` field.
2. **Superseded** — overtaken by a newer action that subsumes it. Move to closed; reference the superseding row.
3. **Stage-graduated** — the underlying workstream advanced (e.g. an "intro call" became an "active diligence" workstream). Either close the parent and add the new child action, or update both `Action` and `due` in place with a comment.
4. **Rolled forward** — still genuinely open but the date is stale. Update `due` to a sensible future date (typically 1–2 weeks out) and update `status` to `in-progress` if there's evidence of active work.
5. **Blocked** — pending an external party. Update `status` to `blocked` and the `Action` text to name what's blocking (e.g. "gated on counterparty deal-structure clarity").

**Banned**: leaving a past-due open action in the tracker without classifying it. A passed date with no resolution is silent rot — it makes the dashboard look stale and trains the user to ignore the dates.

**Detection**: `python3 -c "import json; ds=json.load(open('data/compiled/deal-system-data.json')); from datetime import date; t=date.today().isoformat(); [print(d['name'], a) for d in ds['deals'] for a in (d.get('actions') or []) if a.get('status') in ('open','in-progress') and (a.get('due') or '') < t]"`

**Rule for stale-date sweeps in any other tracker** (`recruit-config.yaml > priorityTargets / recruiters`, `deal-config.yaml > nextTouchBase` fields, etc.): same classification options apply. If a `nextTouchBase` is past today and the underlying touch hasn't happened, roll forward; if it has happened, update `lastAction` and roll `nextTouchBase` forward; if the relationship is dormant, mark it so explicitly rather than leaving the date.

---

## TOPIC — DEAL-SECTION DEDUPLICATION

### 2026-05-04 — A deal must appear in exactly one section; canonical-name dedup runs at render

The deal-pipeline panel renders two adjacent sections:
- `LIVE DEALS` — from `DATA.dealPortfolio.deals` (compiled from `data/deals/<TICKER>/deal.md`)
- `DEAL ORIGINATION` — from `DEAL_CONFIG.dealOrigination` (curated in `deal-config.yaml`)

Without a dedup pass between the two, a deal that has both a `data/deals/<TICKER>/` directory AND a curated origination row renders twice. The user manages bucketing via `deal-config.yaml`, but the Live Deals column was reading the compiled portfolio without checking for overlap.

**Rule** (codified 2026-05-04 in the deal-pipeline panel function): build a Set of `__cpClusterKey(name)` for every row in `dealOrigination[]`; filter the Live Deals candidate list to exclude any deal whose canonical key is in that set. The user's manual bucketing wins — if a deal is curated as origination, it does NOT also appear in Live Deals regardless of its compiled `stage` field.

**Why this is alias-aware**: the same deal can be named differently in `deal-config.yaml` (curated short name) and `data/deals/<TICKER>/deal.md` (full deal-source name with the contact appended). Comparing raw strings would miss the duplicate; canonicalizing through `__cpClusterKey` (which applies `__CP_ALIASES` then the smart firm-keyword fallback) collapses both to one canonical name.

**Companion rule**: when a deal moves from Origination → Live Deals, REMOVE the `dealOrigination[]` entry from `deal-config.yaml` in the same commit that updates the deal's `stage` in `data/deals/<TICKER>/deal.md`. Otherwise the dedup filter will hide it from Live Deals (because it's still in dealOrigination[]). The user's manual bucketing is the source of truth.

---

## TOPIC — TILE DRILLDOWN SCOPE

### 2026-05-04 — Tile drilldowns expose ONE complementary lens, never parallel views

When a top-of-page tile (e.g. a deal-pipeline tile, a "5 active deals" stat card) is clicked, the resulting drilldown card MUST expose information that adds to what's already visible on the page — not a parallel set of sub-tabs that re-render the same content at lower fidelity.

**Failure pattern**: a tile click opens a 3-tab card whose tabs are
(a) a duplicate of an already-visible top-row section,
(b) a hidden complementary view that adds value, and
(c) a drill-through pointer to another route. The user gets confused: "why does this tile show me Dealflow when Dealflow is already at the top of the page?"

**Rule**: a tile drilldown should contain at most one card, and that card should be a complementary lens — typically a counterparty-by-counterparty view when the parent surface is a relationship inventory, or vice versa. If a sub-tab in a drilldown duplicates a top-row section, delete the sub-tab; if a sub-tab is a navigational pointer, replace the drilldown click with a direct link.

**Companion rule**: when the tile's content corresponds to an already-visible section of the page, the click should scroll-highlight that section rather than render a parallel card. See the 2026-05-04 "tile click must scroll-highlight" entry above.

Tenant-specific pre/post details for this session's surgery live in the private log.

---

## TOPIC — COUNTERPARTY ALIAS COVERAGE

### 2026-05-04 — Smart fallback in `__cpClusterKey` + alias entries for any person-led raw counterparty

`__cpClusterKey` previously fell back to splitting on `/` / `—` and taking the first part. When the raw counterparty was extracted in `Person / Firm` order, that fallback clustered the items under the person's name instead of the firm. Two-layer fix:

1. **Smart fallback** — when no `__CP_ALIASES` entry matches, scan the `/`-`—`-`,`-`|`-separated parts for one that contains a firm-shape keyword (`capital`, `partners`, `management`, `investments`, `bank`, `corp`, `llc`, `holdings`, `advisors`, `ventures`, `fund`, `equity`, `industries`, `infrastructure`, `securities`, `group`, `properties`, `finance`, `strategies`, `asset(s)`, `trust`, plus lower-case forms of placement-agent and PE-firm surnames maintained in the keyword list). Use that firm half as the canonical key. Falls through to the legacy first-part-only behavior when no firm keyword matches.

2. **Explicit aliases** — for firms with quirky raw extractions (the LLM consistently inverts the order, includes a desk name, or splits unusually), add an explicit entry to `firm_context.yaml > counterparty_aliases` with all observed needles → canonical name.

**Rule (companion to the 2026-04-21 _CP_ALIASES single-source rule)**: when a `/dash` session surfaces a cluster that headlines a person rather than a firm, the firm needs an alias entry in `firm_context.yaml`. The smart fallback handles the long tail; explicit aliases give canonical display strings and stable keys.

The list of firms added this session lives in the private log (tenant-specific).

---

## TOPIC — TIME-REFERENCE NORMALIZATION

### 2026-05-04 — "next week" / "later this week" must materialize to "week of YYYY-MM-DD"

Floating time references go silently stale. An email from weeks ago
saying "let's catch up next week" still reads as "next week" today —
the user's eye glosses past the stale qualifier and reads it as
current. Two-layer defense:

1. **Compile-time materialization** — `_materialize_next_week()` in
   `cos-dashboard-fetch.py` (runs in the awaiting-external pipeline
   before staleness checks). Replaces the floating phrase with
   `week of <Monday-of-target-week>` computed from the item's
   `addedDate`. Idempotent.
2. **Extraction-time rule** — extraction prompts in
   `cos_otter_backfill.py` and `cos_email_backfill.py` should
   normalize the same phrases at write time (defense-in-depth).

Phrases handled: "next week" (+7d), "early next week" (+7d), "late
next week" (+10d), "later this week" (+3d), "end of the week" (+3d),
"end of next week" (+10d). All anchored on `addedDate`, snapped to
the Monday of the target week.

---

## TOPIC — TILE / DRILLDOWN UX

### 2026-05-04 — Personal tab task icon is the action, not a popup launcher

Task buttons in a Personal-tab `Task` column should DO the action,
not open a popup that explains the action. When `taskUrl` is
populated the button is an anchor (mailto, calendar URL, search
URL) — that's correct. When it's empty, the legacy fallback opens a
modal. That's wrong.

**Rule**:
- For `claude_code` rows OR `personal_items` rows whose `name`
  starts with "Dashboard Update — ", the task button MUST link to
  `https://claude.ai/new?q=<encoded prompt>` (Claude session
  pre-loaded with the task).
- Other rows lacking `taskUrl` get the modal fallback as a degraded
  path, but populating `taskUrl` in the source config is the right
  fix.
- The popup is for review/context, not task execution.

**Composition rule**: Claude-prompt encoded body =
`name + myAction + what` joined with blank lines, skipping empty
fields.

### 2026-05-04 — Universal text-fit rule: succinct, fits the box

Every text in a fixed-size component (table cell, tile, badge, modal
label) must fit. When source data has long text, the layout MUST
either:
- Use a `minmax(<min>, <fr>)` grid column with
  `min-width: 0; overflow-wrap: break-word` on direct children for
  graceful wrapping, OR
- Truncate with ellipsis and surface full text on hover/click.

Banned: fixed-px columns whose content blows them out; nowrap
content that overflows horizontally; long single-line strings
without word-break.

---

## TOPIC — VISUAL CONSISTENCY ACROSS ROUTES

### 2026-05-04 — All HTML routes use the cream-paper / serif chrome — no per-route navbar

Per-route navbars diverge silently. Different background tokens,
different typefaces, different tab labels for the same destination
all cause friction.

**Rule**: `_topnav.html` is the single source of truth. Any route
that renders a top-level navbar MUST either use the shared partial
via `_inject_shared_chrome()`, OR replicate its design tokens AND
label set EXACTLY.

**React parity**: when a React `GlobalNav` component is updated,
mirror with `_topnav.html` first; React follows. Label/route
divergence is a bug — fix in both files in the same commit.

**Build pipeline**: any React route is a CRA project; `npm run build`
outputs to its `build/` directory and must be `rsync -a --delete`-ed
to the server-served build directory. After updating App.js, rebuild
AND deploy.

### 2026-05-04 — Modal text contrast: never use slate-400 on white

`color: #94a3b8` (slate-400) on white fails WCAG AA. Reads as "too
light, hard to read."

**Rule**: modal/popover labels and subdued text use slate-600
(#475569) or darker. Slate-400 is acceptable only as a fourth-level
subdued token (overlay legends, ghost icons), never primary label
text.

---

## TOPIC — BRIEFING CONTENT FORMATS

### 2026-05-04 — Briefing parser must accept markdown AND fall back to a card on empty parse

A structured parser tuned for one briefing format (e.g.
`KEY TAKEAWAY:` / numbered sections) returns 0 cards when fed a
different format (`## H2`, `### H3`, `**bold**`, `---` rule). The
result: an empty briefing tab while the underlying data exists.

**Rule**: when a structured parser returns 0 AND the input is
non-empty, render via a lightweight markdown fallback
(`_markdownCard(fullText)` or equivalent). Never let the briefing
tab show "no items" while data exists.

**Future-proofing**: when a new briefing format ships, prefer
extending the structured parser over relying on the markdown
fallback. The fallback is graceful degradation, not the canonical
render path.

---

## TOPIC — AUTH / LOCALHOST TRUST

### 2026-05-04 — Trust the host's own LAN IPs as loopback for owner auth

Previously `_is_localhost()` returned True only for
`127.0.0.1`/`::1`/`localhost`. Users opening the dashboard at the
LAN IP from the same Mac saw the LAN IP as the connecting source —
`_is_localhost()` returned False, `/admin` re-prompted for login
despite being on the same machine.

**Rule** (codified 2026-05-04): `_is_localhost()` consults
`_OWN_HOST_IPS`, computed at process start via `socket.gethostname()`
plus a UDP socket trick to find the bound LAN IP. Connections from
any IP in this set are treated as loopback for auth. Safe in
single-tenant deployments because (a) the dashboard binds to specific
interfaces, (b) only same-host or same-LAN traffic can present those
source IPs.

**General rule**: never assume `127.0.0.1` is the only loopback case
for a host that listens on multiple interfaces.

---

## TOPIC — ROUTINES OBSERVABILITY

### 2026-05-04 — `/routines` log-stem must come from the plist's `StandardOutPath`, not the task name [SUPERSEDES the same-date "investigate" entry below]

**Bug pattern**: a plist whose label differs from its
`StandardOutPath` filename leaves the dashboard's routines surface
blind to every successful run, reporting uniform `never_run`.
Renaming a registry without renaming plist log paths is the typical
trigger.

**Fix**:
- `_routines_parse_plist` extracts `StandardOutPath`, derives
  `log_stem` (basename minus `.stdout.log` / `.run.log` /
  `.stderr.log` / `.log` suffix), and returns it in the meta dict.
- `_routines_data` reads logs at
  `<log_stem>.{run,stdout,stderr}.log` and exposes those exact
  paths in `log_paths`.
- `_routines_health` reuses `log_paths` from `_routines_data` instead
  of rebuilding from task names.
- `_handle_routines_log` (the per-task log tail endpoint) also
  resolves through the stem.

**Rule**: any new plist that lands in
`~/Library/LaunchAgents/com.<ns>.claude-task.<task>.plist` SHOULD use
`StandardOutPath = ~/dashboards/logs/claude-tasks/<task>.stdout.log`
— the task-name-matched canonical path. Existing plists with
mismatched stems are tolerated via the stem lookup. If you create a
new plist, follow the canonical convention so the stem table can
eventually be retired.

**Sub-finding (general)**: a routines health-check should fail
loudly when a plist exists on disk but is not loaded — silent missing
= silent broken pipeline. Reload via
`launchctl bootstrap gui/$UID <plist>`.

**Implementation (2026-05-04 EOD)**: `_routines_data()` now calls
`launchctl list` once per request and stamps `launchctl_loaded:
true|false|null` on every routine record (null = launchctl
unavailable). When `loaded == false`, the record carries a
`warning` field with the bootstrap command. Frontend should render
a chip off these two fields. Catch-up agent normally bootstraps
missing plists on first run, so this surface mainly catches
install-time and post-reboot drift before the next catch-up cycle.

### 5a — Routines `launchctl_loaded` chip on admin routines table *(documentation — UI rule)*

**Trigger**: A plist on disk that isn't loaded into launchctl is silently broken — no run history, no error, just absence. Renaming a plist or post-reboot drift is the typical cause. The principal can't tell from the routines table whether `never_run` means "scheduled but not yet fired" vs. "broken: not in launchctl." Server-side detection added 2026-05-04 EOD so the frontend can render a chip directly.

**Surface**:
- File: `/Users/ygontownik/cos-pipeline/templates/admin-dashboard.html` — the routines table on the admin route.
- Render location: a per-row chip in the status column, alongside the existing `status` field (`ok` | `running` | `failed` | `never_run`).
- File for reference (server side, do NOT modify): `/Users/ygontownik/cos-pipeline/cos-dashboard-server.py` — `_handle_routines_list` / `_handle_routines_health` (lines 3856–3859), `_routines_data()` payload construction (lines 1906–1985), `_launchctl_loaded_labels()` (line 1712).

**Data source**: `GET /routines` response — each routine record now includes:
```
{
  name: "<task-name>",
  status: "ok" | "running" | "failed" | "never_run",
  launchctl_loaded: true | false | null,     // null = launchctl unavailable
  warning: "<bootstrap command string>",     // only present when launchctl_loaded === false
  ...
}
```
Construction at server line 1966 (`launchctl_loaded`) and 1974–1980 (`warning` with full `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<full_label>.plist` command).

**Behavior spec**:
1. When `launchctl_loaded === true`: render no extra chip. Existing status indicator is sufficient.
2. When `launchctl_loaded === false`: render a red "NOT LOADED" chip in the status cell, alongside (not replacing) the existing status indicator.
   - Style: `border: 1px solid #dc2626; background: #fef2f2; color: #7a1a1a; padding: 2px 8px; border-radius: 3px; font-size: 10px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;`
   - Label: `NOT LOADED`
   - Tooltip (`title` attribute): full text of `record.warning` (the bootstrap command).
3. When `launchctl_loaded === null`: render a gray "UNKNOWN" chip with the same dimensions and `title="launchctl status unavailable"`.
4. Add a "Copy bootstrap command" affordance: clicking the red chip copies `record.warning` to the clipboard and briefly shows "Copied" confirmation. Don't open a new tab; the user runs the command in Terminal.
5. Sort: rows with `launchctl_loaded === false` should sort above all loaded rows in the routines table — broken pipelines deserve top-of-list visibility.
6. Refresh: re-fetch `/routines` on the existing routines-table refresh interval. Don't poll separately.

**Acceptance**:
- A plist that exists on disk but is not in `launchctl list` shows the red "NOT LOADED" chip; tooltip shows the bootstrap command.
- A loaded plist shows no chip beyond its normal status.
- Clicking the red chip copies the bootstrap command to clipboard.
- Rows with `NOT LOADED` chips appear at the top of the routines table.
- After running the bootstrap command and refreshing, the chip disappears for that row.

**Sub-finding (general)**: macOS launchd does not run missed
`StartCalendarInterval` events on wake. If routines must catch up
after sleep, switch to `StartInterval` or add a wake-time
`RunAtLoad` reconciliation script.

### 2026-05-04 [SUPERSEDED] — `/routines` reports `never_run` for everything; observability gap to investigate

Superseded by the entry above. Original observation: `GET /routines`
returned 15 routines all with `status: "never_run"`,
`last_run: None`. Inconsistent with observed reality
(`dashboard-data.json` IS regenerating; daily briefing has fresh
content).

**Hypotheses logged at the time** (now resolved by the stem fix):
1. The routines registry reads run history from
   `~/dashboards/logs/claude-tasks/<task>.run.log`. If logs write
   elsewhere, the registry never sees them.
2. Some plists need to be loaded:
   `launchctl load ~/Library/LaunchAgents/com.<ns>.claude-task.<task>.plist`.
3. The `/routines` endpoint may be reading the canonical-plist
   registry but plists fire under different identifiers.

**Rule**: when `/routines` shows uniform `never_run`, treat as a
critical observability gap. The routines surface is a health monitor
— silent uniformity = a broken monitor, not a quiet day.

---

## TOPIC — DEPLOY / SERVER LIFECYCLE

### 2026-05-04 — After a git push, restart the server to activate code changes

The dashboard server is a long-running LaunchAgent. It does NOT
hot-reload Python source files. After any commit that touches
`cos-dashboard-server.py`, the server must be restarted via
`launchctl kickstart -k gui/$(id -u)/<server-launchagent-label>` for
changes to take effect. If a feature "isn't working" after a git
push, check whether the server is still running pre-push code before
assuming the code is wrong.

---

## TOPIC — ACTION-DIRECTION INVERSION (Y2)

### 2026-05-04 — Transmission verbs require explicit sender identification

When extraction sees a transmission verb (`send`, `share`, `deliver`, `forward`, `provide`, `transmit`, `circulate`, `pass along`), the prompt MUST instruct the model to identify the sender BEFORE emitting an action item — not simply attribute the verb to the principal because their name appears in the row.

**Three canonical patterns**:

1. **Inbound pitch** (placement agents, banks, advisors pitching deal flow / capital to the principal): counterparty owns the action; emit `state: waiting`, `owner: external`, `counterparty: "Firm — Person"`. The principal RECEIVES — do NOT generate a `my_action` telling the principal to "send" what is being pitched IN.
2. **Outbound sponsorship** (principal sponsoring a deal to LPs, lenders, co-investors): principal sends — `owner: <whitelist>`, `state: active`.
3. **Mutual exchanges** (NDAs, term sheets, redlines): emit two items, one per direction.

**Default if unclear**: emit as `state: waiting` with the counterparty as owner. Better to under-attribute to the principal than fabricate a send-verb on the wrong side.

**Why this matters**: the failure mode (codified 2026-05-04) was a fundraising advisor pitching IN, written as the principal owing the send. The same wording — "send the materials" — has opposite ownership depending on which side is doing the sending. Role context (sender's firm role + email From/To headers + signature blocks) is the only reliable disambiguator. Ship this guidance in BOTH `BACKFILL_PREAMBLE` (transcripts) and `EMAIL_PREAMBLE` (email) so it can't be solved on one side and left broken on the other.

---

## TOPIC — DEAL-LOG PRECISION TAGGING (V1+)

### 2026-05-04 — Prefer LLM-emitted explicit deal tags over fuzzy token-match

Per-deal activity logs (rule V1) are populated by scanning extracted intel for items that "touch" a given deal. The original implementation used a fuzzy token-match against the canonical name, slug, and alias needles (high recall, false-positive prone — "MISO" matches both unrelated PJM/MISO chatter and the actual deal-specific signal).

**Two-pass strategy** (codified 2026-05-04):

- **Pass A — explicit `parent_id` match** (high precision): if the extracted item carries `parent_id` equal to the deal's id, it is a guaranteed-tag — skip text matching. Available today on `awaitingExternal`, `dealIntel`, `originationInbox`. Available on `followUps` via `linkedTo`.
- **Pass B — token match** (high recall): for items WITHOUT an explicit parent_id, fall back to the legacy alias-needle scan.

Each emitted log entry carries a `match` field (`explicit` | `token`) so analysts can audit precision-vs-recall mix. New entries added going forward will carry this field; legacy entries (written before this rule) remain `unset`.

**Forward signal**: extraction prompts now also emit `deal_log_entries[]` — an explicit array of `{deal_id, summary, evidence}` per call/email — for full LLM-driven tagging once the doc-routing plumbing carries the field through to `dashboard-data.json`. Until then, `parent_id` on envelope items is the precision input.

**Slug-alias precision (codified 2026-05-04 EVE)**: extractors slug-ify deal names (e.g. `<some-deal-name>` for a deal whose canonical id is a short ticker). Strict `parent_id == deal.id` equality misses these slug-ified variants. The Pass A check now uses a `precision_keys` set built from `id` + `ticker` + `name` (with hyphen↔space variants) plus alias-needles from `firm_context.yaml`. Reverse-substring matching is permitted only for multi-word keys ≥6 characters, so a multi-word phrase correctly matches its hyphenated slug-form, while short 2- or 3-character tickers (which often double as public-company tickers in dealIntel readthroughs — e.g. NEE, GEV, BE) cannot false-positive into another deal's log.

**dealIntel / originationInbox date field (codified 2026-05-04 EVE)**: those signal sources don't carry `addedDate` at the top level — the date lives at `source_ref.date`. The hard date-format filter in `_compute_deal_logs()` was silently dropping every dealIntel and originationInbox item. After adding the source_ref.date fallback, the first compile appended +116 entries that had been lost to the original V1 implementation. Net effect: dealIntel and originationInbox readthroughs now flow into per-deal activity logs as intended.

**Generic rule**: any auto-tagging routine that filters by date MUST inspect every signal source's actual schema before committing to a single field name. Where a signal source can produce items without the canonical date field, fall back to the embedded source-document date — silent date-filter drops are the worst kind of bug because the routine appears to run successfully while quietly producing partial output.


---

## TOPIC — STALE-READING DATE PHRASING

### AB1 — Absolute dates only; no relative phrasing in dashboard text *(extraction-prompt enrichment + silent auto-correct)*

**Codified 2026-05-05.** Universal rule across every surface that renders extracted text (awaitingExternal, followUps, recentActivity, dealIntel, action items, briefing memos).

**Rule:** Any reference to a date or week in extracted text MUST be an absolute form. Specifically:

- ✅ ALLOWED: `2026-05-12`, `week of 2026-05-12`, `May 12`, `5/12 2026`
- ❌ FORBIDDEN: `tomorrow`, `next week`, `this Friday`, `Wed 4/29`, `Friday 5/1`, `EOD`, `early next week`

**Why:** the extracted item lives in the dashboard for days. A line that reads "Confirm Friday 5/1 live call" is correct on the day it's extracted but reads stale every subsequent day — even when the action is still valid. The user's mental model treats stale-reading text as "the system is broken." Absolute dates never go stale; the date itself tells the reader whether the action is past or future without requiring memory of when the line was written.

**Two-layer enforcement:**

1. **Extraction-prompt enrichment** — the LLM extractors (cos_capture_pipeline.py / cos_email_backfill.py / cos_otter_backfill.py system prompts) instruct the model to resolve every relative date reference to YYYY-MM-DD against the email/transcript date BEFORE emitting. "Tomorrow" in an email dated 2026-05-04 must emit as `2026-05-05` in the structured output.

2. **Silent auto-correct at compile time** — `_materialize_next_week()` in `cos-dashboard-fetch.py` converts any relative phrasing the extractor missed. Patterns covered: `tomorrow`, `today`, `this/next [Mon|Tue|...|Friday]`, `next week`, `this week`, `early/late next week`, `[Mon|Tue|...|Friday] M/D`, `[Mon|Tue|...|Friday] M/D/YY`. All resolve against `addedDate` (the extraction date) and emit `YYYY-MM-DD` or `week of YYYY-MM-DD`.

**Failure mode (the bug this rule prevents):** dashboard awaiting items showing "Send draft Uber lease for Friday 5/1" on 2026-05-05 — the user asks "is this stale or current?" and the system has no answer. After the rule: same item reads "Send draft Uber lease for 2026-05-01" — past-due is unambiguous, the 14-day cutoff fires, the item drops.

[ENFORCED via tools/checks/check_relative_dates.py]

---

## TOPIC — HQ STAT BAR / TILE GOVERNANCE

### 2026-05-07 — Stat tiles must reflect what is actually rendered on that tab

A stat tile's count must use the same filter as the section it represents.
If a tile counts "all" awaiting counterparties but the rendered section uses
a `tc-only` filter, the tile shows a larger number than the section has rows
— visually broken and confusing. The rule: compute the stat using the exact
same filter logic as the rendered section, or remove the stat tile entirely.

**Retired tiles (2026-05-07):**
- Fundraising: count was premature (committed+warm numbers not yet real).
- Priority Targets / job search: job search is not surfaced on HQ Status tab.
- Awaiting: Awaiting External moved inside the Actions card; a separate stat
  tile duplicated what the card header already shows.

---

## TOPIC — OUTBOUND EMAIL AS ACTION SOURCE

### 2026-05-07 — Emails Yoni sends to Mark are not auto-ingested as actions

The email pipeline processes incoming/forwarded items into the COS capture
pipeline. Outbound emails from Yoni to Mark (or other team members) about
deal work are NOT automatically extracted as new TOMAC_CONFIG actions,
because they hit no ingest trigger. When Yoni says "I emailed Mark about X
yesterday — why wasn't it picked up?", the correct answer is: outbound
coordination emails between team members require manual entry in
`config/deal-config.yaml` (the `myAction` field on the relevant deal entry).
Do not assume the email pipeline will pick them up. Offer to add the action
manually and do so immediately.

---

## TOPIC — ACTIONS CARD ARCHITECTURE

### 2026-05-07 — Actions card subsection IDs must not collide with old awaiting IDs

The merged Actions card uses `aeid2 = 'actions-external'` for its expand-state
keys. The standalone `buildAwaitingExternal()` (kept for Personal tab) uses
`aeid = 'awaiting-external'`. These are intentionally different so Personal-tab
expand state does not bleed into the Actions card External section.
Do NOT unify them to the same key.
Do NOT unify them to the same key.

---

## TOPIC — OUTBOUND EMAIL INGEST (SENT ITEMS)

### 2026-05-07 — Sent items are now auto-ingested; rule on ingest lag and double-count

As of 2026-05-07, the capture pipeline (`cos_capture_pipeline.py`) fetches
`provider.search_sent()` in addition to the inbox. Both results are passed to
Claude in separate labeled sections ("INBOX EMAILS" / "SENT EMAILS").

**Updated rule on manual entry**: the "must add manually to deal-config.yaml"
rule still applies for SAME-SESSION immediacy (sent email → show on dashboard
within minutes). The automated pipeline has a ~24h lag (runs at 7:22am daily).
For time-sensitive commitments Yoni made in the last hour, manual entry is
still the right move. For yesterday's sent items, the next morning run picks
them up automatically.

**Double-count trap**: Claude now sees the same commitment twice — once in the
inbound email (someone asked Yoni) and once in the sent email (Yoni confirmed).
The A1b prompt rule says "emit ONE follow-up row." Watch for duplicates in the
`follow_ups_to_add` output and prune them if they appear. If dedup fails,
restrict sent email ingest to a shorter window (last 12h instead of 24h) to
reduce overlap with inbox items that have already been processed.

**direction field contract**: `serialize_emails_for_prompt()` emits
`direction: sent|received` per email. If you add new fields to `EmailMessage`,
update `serialize_emails_for_prompt()` in the same commit or Claude won't see
the new data.

---

## TOPIC — CAPTURE PIPELINE PROMPT SECTIONS

### 2026-05-07 — System prompt section labels must match user payload section headers

The `build_system_prompt()` function names the input sections ("A1. Inbox",
"A1b. Sent items"). The `user_payload` string in `run()` must use the same
labels ("INBOX EMAILS", "SENT EMAILS") consistently. If a new data source is
added, add both: (1) a named section in the system prompt explaining how to
interpret it, (2) a matching labeled section in the user payload. Missing
either half means Claude either sees data it wasn't told how to use, or gets
instructions that reference data that isn't present.

---

## TOPIC — DASHBOARD WORKSTREAM / TAB SCOPING

### 2026-05-08 — Job search content must be scoped to /personal only

`recActive` (the "Priority Targets / job search" stat tile) was set to null
only when `filter === '<slug>'` (the active tenant), so it appeared on the main Status (HQ) route
for all other filters. The fix: `recActive = (!onPersonalTab || f === '<slug>') ? null : ...`.
Rule: anything job-search related — stat tiles, panels, focused layouts —
must check `!onPersonalTab` as the gate, not just the workstream filter.

---

## TOPIC — AWAITING EXTERNAL CROSS-REFERENCE

### 2026-05-08 — Resolved follow-ups are the cheapest signal for closed awaiting items

When an awaiting item should have been closed by a call/action that happened,
the most reliable in-data signal is a `[RESOLVED]` prefix on a follow-up entry
with the same counterparty. Build `resolvedFuKeys` (Set of normalized CP keys)
from `DATA.followUps.filter(fu => fu.what.startsWith('[RESOLVED]'))` and
cross-reference it in cluster rendering. Compute it inside `buildAwaitingExternal()`
where `__cpClusterKey` is in scope. Do NOT try to derive it from call history —
`upcomingCalls` only covers the next 7 days and has no historical records.

---

## TOPIC — TEMPLATE VS RENDERED HTML

### 2026-05-11 — Always edit `.template.html`; never edit `.rendered.html` or `.html` directly

The dashboard has three HTML artifacts:

- `cos-dashboard.template.html` — **authoritative source** committed to git.
  Contains `{{DATA_BLOCK}}` sentinel (or equivalent). Edit this.
- `cos-dashboard.rendered.html` — gitignored. Generated by
  `app/cos-dashboard-refresh.py` from the template with live data injected.
  The server reads this file to respond to `GET /`.
- `cos-dashboard.html` — a dev reference copy that `cos-dashboard-refresh.py`
  writes alongside `.rendered.html` (mirrors the rendered output). NOT served
  by the server; NOT the template.

**Rule**: If you edit `.rendered.html` or `cos-dashboard.html`, your changes are
overwritten on the next `/refresh` or server warmup (every 20 min). The only
durable edit path is `.template.html` → run `python3 app/cos-dashboard-refresh.py`
→ verify the rendered file picked up the change.

**How to verify**: After editing the template and running the refresh script,
`grep` the target string in `.rendered.html` — not in `.template.html` or
`.html`. Only the rendered file is what the browser actually receives.

---

## TOPIC — AWAITING COUNTERPARTIES DEDUP

### 2026-05-12 — Thread-ID dedup added to _redupe_after_canonicalization

Multiple extractions from the same Gmail thread can produce distinct items
with different wording that escape the content-stem dedup (e.g. "confirm meeting
slot" vs "confirm call time" from the same iSquared Intro thread). Fix: after
the stem-dedup loop in `_redupe_after_canonicalization`, group by
`source_ref.thread_id` and keep only the richest (longest content) item per
thread. Applied in `cos-dashboard-fetch.py`.

### 2026-05-12 — Firm-name normalization required for camelCase vs spaced variants

"iSquared Capital" (camelCase) and "I Squared Capital" (spaced) produce different
`_normalize_cp` keys because the function lowercases before alias lookup. Both
must appear as needles in the `counterparty_aliases` entry in `firm_context.yaml`.
Adding only the canonical or only one spelling causes the other to fall through.

---

## TOPIC — FOLLOW-UPS DEDUP

### 2026-05-12 — Same-transcript dedup added as second pass

Transcript processors can emit the same action twice with minor wording
differences (e.g. "Analyze public info + AI to size Granite State" vs "Analyze
Granite State asset using public data and AI"). The stem dedup misses these
because key tokens appear in different order. Fix: second dedup pass in
`cos-dashboard-fetch.py` using `(normalized_who, source, linkedTo)` as key —
same person, same transcript URL → keep the longer `what`. Added 2026-05-12.

### 2026-05-12 — Team member naming must be first-name-only in who/owner fields

Extraction prompt in `cos_otter_backfill.py` now explicitly requires first name
only for team members in `who` and `owner` fields. Full names like "Yoni Gontownik"
create duplicate follow-ups because the `normalized_who` function can only
reliably collapse to first name when the first name appears as a substring.

### 2026-05-12 — WHO attribution rule (W1): external parties not on the call can never be `who`

`cos_otter_backfill.py` prompt now enforces: if the action is a team member
reaching out to someone NOT on the call, `who` must be the team member; the
external party belongs in `what`. External parties who WERE on the call and
explicitly committed to something can still be `who`. Previously, outreach
targets like "Eddie Dunn (John Hancock)", "Latano (NextEra)", "Doug Bogie" were
appearing as `who`, routing items to Awaiting Counterparties instead of Team
Actions.

### 2026-05-12 — ACTION CONSOLIDATION RULE (C1): multiple related actions from same person → one item

`cos_otter_backfill.py` prompt now requires: when the same person on the same
call has multiple related actions constituting one analytical task (same deal,
same due date, sequential steps), consolidate into one item with a comprehensive
`what`. Previously, e.g. "Run IRR sensitivity" and "Run financial scenarios on
LTV and reservation fee" from Brad Misialek / Thunderhead were emitted as two
separate items when they're both "run the deal math before the FIT meeting."

---

## TOPIC — TEAM ACTIONS CARD

### 2026-05-12 — myAction in deal-config.yaml must be manually cleared when done

The Team Actions card sources `myAction` from `deal-config.yaml` (via
TOMAC_CONFIG injected at serve time). No pipeline auto-clears this field when
an action is completed. When an action is done, clear `myAction: ""` and `task: ""`
in `deal-config.yaml` directly. The UI dismiss button also works but its key
is text-based — if the myAction text ever changes, the dismiss orphans and the
item re-appears.

---

## TOPIC — SUBSCRIPTION AUTH / API FALLBACK

### 2026-05-12 — Set CLAUDE_AUTH_MODE=subscription in any runner that loads ANTHROPIC_API_KEY

`_claude_dispatch.py` falls back to `ANTHROPIC_API_KEY` when a subscription
call fails. If the API key is exhausted (HTTP 400), this causes SILENT transcript
processing failures — no error in the log, just a 400 that gets swallowed. The
root symptom is transcripts that never appear in processed docs despite no logged
error. Fix: always export `CLAUDE_AUTH_MODE=subscription` in runner scripts AFTER
`load-secrets.sh` (which sets the API key). This forces a clean raise on
subscription failure instead of silent API fallback. Applies to any LaunchAgent
or cron runner that (a) uses Claude subscription auth and (b) loads the API key
from Keychain/env.

Also check `firm_context.yaml` — if `auth_mode` is "api" but the API key is
exhausted, changing it to "subscription" alone is insufficient because the fallback
chain still reaches the API key. The env var override is the correct belt.
