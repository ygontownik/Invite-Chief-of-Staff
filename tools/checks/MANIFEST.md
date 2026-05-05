# tools/checks — Runtime Enforcement Manifest

This directory holds runtime checks that promote documented rules in
`config/dash_corrections.md` from analyst-pass discipline into code-enforced
invariants. Per AA2 — "documented rules without code enforcement aren't
actually rules" — each check below blocks a previously-burned bug shape
from silently re-emerging.

## How to run

Each module exposes `run() -> dict` with keys
`{name, rule_ref, status, summary, details}`. Status is one of
`pass | warn | fail`. Modules are auto-discovered by
`~/cos-pipeline/tools/system_health.py` via
`glob('tools/checks/check_*.py')`. To run one standalone:

```bash
cd ~/cos-pipeline
python3 -c "from tools.checks.check_aa1 import run; print(run())"
```

A `warn` indicates a soft issue (data missing, possible regression).
A `fail` is a hard violation that mirrors a real production bug and
deserves immediate attention.

## Checks

| Module                          | Rule         | Triggers FAIL when…                                                                                                       |
|---------------------------------|--------------|---------------------------------------------------------------------------------------------------------------------------|
| `check_aa1.py`                  | AA1          | Any stable-id tombstone (awaitingExternal/dealAction) has an id not matching `^[0-9a-f]{8}$` — re-introduces the 80-ghost-items bug. |
| `check_y2.py`                   | Y2           | An action with a transmission verb (send, share, deliver, …) is owned by a team member but the counterparty is advisor/bank-shaped. (warn-only) |
| `check_m3.py`                   | M3           | Briefing prose claims a deal is closed/received/complete while followUps in the last 14d show active drafting/awaiting verbs on the same deal. (warn) |
| `check_u2.py`                   | U2           | `marketCommentary[]` has items but zero deals carry `recent_readthroughs[]` or intel-source log entries — `_compute_deal_readthroughs()` likely never ran. |
| `check_v1.py`                   | V1           | A deal directory has `deal.md` but no `log.json`, OR a deal log has >10 entries with zero `match=explicit` (parent_id propagation regression). |
| `check_g2.py`                   | G2           | Any compiled row in fundraising / portfolio is missing `name`. Soft fields (lastAction, owner, nextTouchBase, myAction) trigger warn. |
| `check_g3.py`                   | G3           | An owner field on awaitingExternal[].owner or deal action.owner is outside `firm_context.yaml > owner_whitelist` (and not "external" / ""). |
| `check_g4.py`                   | G4           | Any deal directory under `data/deals/<slug>/` is missing one of `deal.md`, `actions.md`, `LPs.md`, `TERMS.md`. |
| `check_next_milestone.py`       | next_milestone | Any active deal (stage ∈ Watch/Sourcing/Active Bid/Diligence/Advisory/Memo/IC) has `next_milestone_due` < today. Empty triggers warn. |
| `check_capture_freshness.py`    | captureSummary | `briefingSynopsis.captureSummary.date` is more than 3 days old → fail (stale); 2–3 days → warn; missing → warn. |
| `check_past_due_actions.py`     | past-due actions sweep | Any deal action with status open/in-progress has `due` < today. Missing `due` triggers warn. |
| `check_tenant_leak.py` (Agent A)| tenant-leak  | Any tenant-identifying token from the verbatim denylist appears in public `cos-pipeline/config/*.md` or `cos-pipeline/docs/*.md` outside the allow-listed shapes. |
| `check_alias_precision.py` (Agent A) | G5      | Owned by Agent A; see that module for failure conditions. |

## Adding a new check

1. Read the relevant rule in `config/dash_corrections.md` end-to-end.
2. Drop a `check_<rule_id>.py` into this directory exposing `run()`.
3. Read existing data files (`~/dashboards/data/compiled/dashboard-data.json`,
   `~/dashboards/data/compiled/deal-system-data.json`,
   `~/dashboards/data/deals/<slug>/log.json`,
   `~/dashboards/data/user-state/deletions.json`). Don't re-derive from
   raw transcripts — that's compile's job.
4. Return `{name, rule_ref, status, summary, details}`. Use `warn` for
   soft regressions and missing data; `fail` only for hard violations
   that mirror a real production bug.
5. Append a row to the table above.
6. In `config/dash_corrections.md`, append `[ENFORCED via tools/checks/check_<id>.py]`
   to the rule body.
7. Verify standalone: `python3 -c "from tools.checks.check_<id> import run; print(run())"`.

## Tenant-purity rule

These check modules ship in the public `cos-pipeline` repo. They MUST
NOT name a tenant deal, principal, counterparty, or firm. The
`check_tenant_leak.py` denylist is the source of truth — if a check
violates it, the leak check itself will flag the violation.

Detection patterns (regexes for advisor-shape, transmission-verb,
counterparty-shape) are tenant-agnostic by design. Owner whitelists
load from `firm_context.yaml` at runtime — not hardcoded.
