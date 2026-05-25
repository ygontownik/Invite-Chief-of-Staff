---
description: Pressure-test a high-stakes action against accumulated context. Queries entity knowledge graph + Practice Patterns + DECISIONS.md before you commit. Use before sending an LP email, signing an NDA, committing to a term, or any action you can't undo cleanly.
argument-hint: "<action description>"
---

# /pressure-test — second-opinion pass before committing

Use this before any action that's hard to walk back. Pulls all relevant context
from the entity knowledge graph + behavioral rules + prior decisions, then
surfaces "before you commit: 3 things to consider."

## STEP 0 — Parse argument

`$ARGUMENTS` is the action description. Examples:
- "send this NDA to APS"
- "commit to $20M equity check on Cholla"
- "propose tax-equity structure to Mark for PNGTS"
- "draft email to Gardner re: Cholla angle"

If empty, ask Yoni for the action description.

## STEP 1 — Identify entities + deal context

Extract from the action description:
- **Named people** (counterparty execs, advisors, LPs)
- **Firms** (APS, Mercuria, BlackRock, etc.)
- **Deal(s)** (cholla, pngts, bbeh, etc. — match against registered deal aliases)
- **Topics** (NDA, term sheet, equity, gas storage, etc.)

If the action references a specific deal, run:
```bash
/load-context deal_general <deal_id>
```

### Jane substrate context

Also load the following Jane substrate files for the relevant deal:

- `~/dashboards/data/jane/north_star.md` — persona orienting context:
  career arcs (Tomac Cove / DRW / Align Infra), activating events per path,
  and personal-engagement weights per deal (HIGH / MEDIUM / LOW). Use this
  to assess whether the action you're testing advances or conflicts with a
  North Star activating event.

- `~/dashboards/data/deals/<slug>/decision_state_jane.md` — per-deal
  strategic frame: current frame, deal-killers, sequencing dependencies,
  "what would change my view." Cross-reference: does the proposed action
  contradict the deal-killers or sequencing dependencies stated here?

- `~/dashboards/data/deals/<slug>/jane_brief.md` — per-deal Jane synthesis:
  Proposed next action, Open threads, Blockers. Cross-reference: does the
  action you're testing conflict with Jane's read of the most important next
  move for this deal?

If any of these files are missing (not yet populated), note it in the
pressure-test output and proceed without them.

## STEP 2 — Query entity knowledge graph

```bash
python3 -c "
import sys
sys.path.insert(0, '/Users/ygontownik/cos-pipeline/tools')
from entity_graph_build import who_knows, connections, last_spoke
# Query for each named entity from STEP 1
"
```

Pull:
- Most recent intel mentioning each named person/firm (via `last_spoke`)
- All connections within 2 hops (via `connections`)
- Cross-referenced topics (via `who_knows`)

## STEP 3 — Check behavioral rules

Read LEARNINGS-LEDGER for any rules that apply to this action class:

```bash
python3 -c "
import yaml
d = yaml.safe_load(open('/Users/ygontownik/dashboards/docs/LEARNINGS-LEDGER.yaml'))
# Search for rules in domains: deal, drive, meta, cos_pipeline
# Surface rules whose applies_to or rule text matches keywords from the action
"
```

Common rules to consider:
- **Counterparty communication** (Practice Patterns): "lead with capital and what it enables, not concerns"
- **Counterparty alignment** (Practice Patterns): "never present to investors without counterparty alignment first"
- **Edit-in-place (EP1/I11)**: if action involves modifying a Drive doc, must be via setContent on registered ID
- **Test emails (L0009)**: any "send email" action that's a test goes to ygontownik@gmail.com only
- **Absolute dates (AB1)**: any date in the action must be YYYY-MM-DD

## STEP 4 — Check prior decisions

```bash
grep -iE "$KEYWORDS" ~/dashboards/docs/DECISIONS.md | head -10
```

Surface any DECISIONS.md entries that touched this action class — past prior
choices that should constrain the current one.

## STEP 5 — Check counterparty + deal patterns

For each named counterparty:
- Read their prior intel entries in deal log.json
- Note any walk-back patterns ("led with X, walked back to Y under questioning")
- Note any contradictions between what they've said and what's in regulatory text

For the deal:
- Read current status.md
- Note any past-due actions tied to this counterparty
- Note any open commitments Yoni made to them

## STEP 6 — Synthesize

Present back to Yoni with this structure:

```
PRESSURE-TEST — <action>

CONTEXT
  Deal:           <deal_id> at stage <stage>
  Counterparties: <named entities>
  Relevant rules: <up to 3 rule codes>
  Prior decisions: <up to 2 DECISIONS.md entries>

THREE THINGS TO CONSIDER

1. <First consideration — most important>
   <2-3 sentence rationale referencing specific intel/rule>

2. <Second consideration>
   <Rationale>

3. <Third consideration>
   <Rationale>

OPEN QUESTIONS
  - <Question 1 that should be answered before committing>
  - <Question 2>

RECOMMENDATION
  <Proceed | Defer | Modify> with rationale.
  If Modify: what specific change to the action.
```

## STEP 7 — Offer next step

After the pressure-test surfaces, offer:
- "Proceed as-is" — Yoni commits to the original action
- "Modify per recommendation" — Yoni accepts the suggested change
- "Defer to gather more intel" — pause, identify what's missing
- "Capture this as a learning" — invoke `/propose-learning` to add a new rule

## OUTSTANDING REQUESTS
(per OR1)
