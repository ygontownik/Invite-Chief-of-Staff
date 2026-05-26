---
name: jane
description: Invoke Jane — read current strategic actions + suggestions, or trigger a refresh. Subcommands: refresh [quick|deep], <slug>, <path>.
---

# /jane — Jane on-demand

Reads current Jane output from dashboard-data.json prioritySynthesis and
displays. Or triggers a refresh (Sonnet for quick, Opus for deep).

## Usage

**Display modes** (no LLM, instant):
- `/jane` — show current janeStrategicActions + janeSuggestions panes
- `/jane <slug>` — filter to specific deal (e.g. `/jane fit`)
- `/jane <path>` — filter to career path (`/jane drw`, `/jane align`)

**Subcommands (v1.1):**
- `/jane today` — top 5-10 prioritized actions for today (instant)
- `/jane week` — weekly arc across deals + recruiting + market memory (Sonnet, ~10s)
- `/jane brief <slug>` — single-deal status pull (instant from jane_brief.md)
- `/jane prep <slug>` — prep card for an upcoming call (Sonnet, ~5s)
- `/jane ask <question>` — query market memory + knowledge base (Sonnet, ~5-10s)
- `/jane challenge [<topic>]` — devil's-advocate pressure test (Opus / Sonnet on budget tier, ~15-20s)

**Refresh modes:**
- `/jane refresh` — run critique_gaps.py (Sonnet hygiene)
- `/jane refresh deep` — run critique_strategic.py (Opus portfolio)

**Free-text input:**
- Any `/jane <natural language>` that doesn't exactly match the above
  is routed via `python3 ~/dashboards/scripts/jane_router.py "$ARGUMENTS"`.
  The router returns a JSON list of {subcommand, args} commands — regex
  fast-path for common phrasings, Sonnet fallback otherwise. E.g.
  `/jane I'm calling Baazov in 20 min` → `[{"subcommand": "prep", "args": "fit"}]`.
  Execute each returned command in sequence with `═══` separators between
  outputs.

## Procedure

1. Parse `$ARGUMENTS`:
   - Empty → display mode (show all)
   - First word matches a known v1.1 subcommand (today, week, brief, prep,
     ask, challenge) with appropriate args → dispatch directly (see
     subcommand procedure sections below)
   - First word is `refresh` (with optional `quick`/`deep`) → existing v1
     behavior: run critique_gaps.py or critique_strategic.py
   - First word matches a deal slug or path label → display mode with filter
   - Otherwise → call `python3 ~/dashboards/scripts/jane_router.py "$ARGUMENTS"`,
     parse JSON command list, execute each subcommand in sequence with
     `═══` separators

2. **Misroute logging:** if the user invokes a different subcommand within
   60 seconds of a Sonnet-routed invocation (the previous /jane call),
   call `python3 -c "import sys; sys.path.insert(0,'/Users/ygontownik/dashboards/scripts'); from jane_router import log_misroute; log_misroute('<original_input>', '<last_command>')"`.
   This feeds the drift monitor (Task 30).

3. **Display mode** (no args or filter):
   - Read `~/dashboards/data/compiled/dashboard-data.json`
   - Extract `prioritySynthesis.janeStrategicActions` and `.janeSuggestions`
   - If filter argument provided, narrow to entries whose `deal_or_path`
     (strategic) or `deal` (suggestions) matches
   - Render as a compact two-section view: strategic actions first, then
     hygiene suggestions
   - Cite generated_at timestamps (note staleness if >4h)

4. **Refresh mode**:
   - `quick` (default) → `python3 ~/dashboards/routines/compile/critique_gaps.py --apply`
   - `deep` → `python3 ~/dashboards/routines/compile/critique_strategic.py --apply`
   - Report success / quota error / non-fatal error
   - After refresh, automatically run display mode to show the fresh output

## Display format

For strategic actions, render each surfaced item as:

```
STRATEGIC ACTIONS  (generated <timestamp>, <age>)
─────────────────────────────────────────────────
[<deal_or_path>]  verdict: <elevate|matches_curated>
  Action: <jane_strategic_action>
  Why: <rationale>
  Evidence: <evidence list>
  Cross-deal: <cross_deal_link or none>
```

For hygiene suggestions, render each surfaced item as:

```
JANE SUGGESTS  (generated <timestamp>, <age>)
─────────────────────────────────────────────
[<deal>]  kind: <kind>  severity: <N>
  <rationale>
  Evidence: <evidence>
  Suggested: <suggested_action>
```

If both sections are empty, say: "Jane has no output yet. Run `/jane refresh` to populate,
or wait for the next scheduled warmup."

## Notes

- Jane's substrate is Drive-canonical (north_star + decision_state_jane +
  jane_brief Drive Docs). On a deep refresh, critique_strategic.py calls
  `_ensure_fresh_mirrors()` first to pull any out-of-date Drive Docs to
  local before reading.

- If critique_gaps.py or critique_strategic.py doesn't exist yet (Tasks C
  and C2 of Phase 1 critic build), `/jane refresh` will report "critic
  script not found yet" — display mode still works once gap_detector is
  wired (Task A).

- The 9 active deal slugs: align_infra, bbeh, cholla, fit, gridfree, pfs,
  pngts, thunderhead, unitil. Career paths: tomac_cove, drw, align.

- Phase 2 will extend this skill to conversational queries ("/jane what's
  stuck on cholla", "/jane who haven't I talked to in 30d") — out of scope
  for v1.

## Error handling

- If dashboard-data.json is missing: say so and suggest running `/check-system`.
- If critique script doesn't exist: print "critic script not found yet — Task C/C2
  of the Jane critic build hasn't shipped. Display mode still reads any existing
  output."
- If the script fails with a quota error: say "Claude quota hit — try again later."
- All errors are non-fatal. Display mode always runs after any refresh attempt.

---

## today subcommand

Read `~/dashboards/data/compiled/dashboard-data.json` →
`prioritySynthesis.janeStrategicActions`. Filter to items where:
- severity ≥ 60 OR
- deal is in HIGH-engagement set per north_star (cholla, gridfree, pngts, fit) OR
- has a due-today date

Render top 10. Each item: one-line header + 1-line rationale + accept/dismiss
hint. Cite generated_at age (note staleness if >4h). No LLM call.

## week subcommand

Build inputs from last 7 days:
- Per-deal log entries (top 5 per deal)
- Cross-deal patterns (entities active in ≥2 deals)
- Recruiting touches (path activity)
- Context Pack market_memory.current_week_briefing_text

Call Sonnet via `_claude_dispatch.call(task_type="jane_week", model="claude-sonnet-4-6", messages=[{"role": "user", "content": <prompt>}], max_tokens=2500)` with weekly-arc prompt. Output to terminal AND
`~/dashboards/data/compiled/weekly-arc-latest.md` (also pushed to Drive
edit-in-place if `weekly-arc-doc-id` registered).

## brief subcommand

Args: deal slug.
- Read `~/dashboards/data/deals/<slug>/jane_brief.md` (must exist; if not,
  trigger `/deal-sync <slug>` first to regenerate)
- Append last 7 days from `<slug>/log.json` (top 10 entries by date)
- Append open threads from gap_detector for that deal (filter
  `prioritySynthesis.gaps` to `deal == <slug>`)
- Append active counterparties (>1 mention last 14d from log scan)

Render as terminal-friendly block. No LLM call.

## prep subcommand

Args: deal slug.

Inputs (assemble all into Sonnet prompt):
- jane_brief.md(slug)
- Last 14 days of log entries for slug
- decision_state_jane.md(slug)
- Context Pack: persona.practice_patterns_compact (only diligence-sequence
  rule + relevant mental models), activating_events_registry filtered by
  path tied to this deal, behavioral_notes for counterparties on this deal
- Channel-of-record reminders from north_star (the channel_violation rules)

Call Sonnet via `_claude_dispatch.call(task_type="jane_prep", model="claude-sonnet-4-6", messages=..., max_tokens=1500)`.

Output: structured prep card —
  Header: who's on call, last touch with each
  Body: open threads, deal-killers, what to push on
  Footer: channel-of-record reminders (don't end-run), behavioral notes

## ask subcommand

Args: question text.

Inputs:
- Context Pack: market_memory (current week + prior 3 weeks)
- decisions.by_deal_index if question mentions a slug
- grid_signals_by_deal slice if question mentions power/midstream/ERCOT/MISO
- Active LEARNINGS rules (rules_ctx)

Call Sonnet via `_claude_dispatch.call(task_type="jane_ask", model="claude-sonnet-4-6", messages=..., max_tokens=2000)`.

Output: 3-section answer —
  1. Direct read (what does the data say)
  2. Supporting citations (which source(s), which week)
  3. Cycle frame (Marks-style — where is the pendulum)

## challenge subcommand

Args: topic text, or null (in which case challenge the most recent
suggestion from dashboard-data.json).

Inputs:
- Full Context Pack (persona, decisions, market memory, behavioral notes)
- Relevant jane_brief.md(s) if topic mentions a deal
- DECISIONS.md history filtered to topic
- Active LEARNINGS rules

**Budget tier check:** read `pack.budget_status.tier` from
`~/dashboards/data/compiled/jane-context-pack.json`.
- If tier is `green` or `yellow` → call Opus (`model="claude-opus-4-7"`)
- If tier is `orange` or `red` → call Sonnet (`model="claude-sonnet-4-6"`),
  prepend output with: *"⚠️ Sonnet fallback — Opus paused at NN% weekly cap"*

`_claude_dispatch.call(task_type="jane_challenge", model=<chosen>, messages=..., max_tokens=3000)`.

Output: 4-section devil's advocate —
  1. Steelman (best case for the position you're challenging)
  2. Counter (Munger inversion — what would make this wrong?)
  3. What would change my view (specific evidence)
  4. Risk citations (sources cited inline)
