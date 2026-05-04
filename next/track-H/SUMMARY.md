# Track H — PLAN_v3.2.md delta — SUMMARY

Completed by Phase 2 sub-agent, run 2 (2026-05-03). Persisted by parent.

Files written: `~/cos-pipeline/PLAN_v3.2.md` (~165 lines).
PLAN_v3.1.md NOT modified (HARD RULE).

## Decisions logged in PLAN_v3.2

(a) Privacy P1: distribution-list externalization DONE (C-1). Residual = `cos-otter-transcripts/SKILL.md:516` runtime PII (C-3) + `ygontownik@gmail.com` → `firm_context.principal.gmail`.

(b) Path topology inversion: runtime canonical SKILL tree is `~/.claude/scheduled-tasks/`, NOT `~/dashboards/`. v3.1 line 20 + line 157 reversed. Memory file `project_cos_architecture.md` line 32 needs same correction.

(c) Third tree `~/tomac-cove-pipeline/` (6 of 17 daemons) added to scope. Default rec = migrate during Wave 2. **Gated on user.**

(d) Wave 2 morning sequence: redact PII → resolve C-2 symlinks → write 7 DEPRECATED.md markers → SKILL renames → Wave 4 LaunchAgent reload.

(e) 14-item merge-order ranking: HTML strip (1) → config split (2) → A-SLOW review (3) → Track B .nexts (4) → M-MIN (5) → Track C router (6) → Track L heartbeat (7) → Track H research (8) → Track D setup (9) → Track G (10) → Track CLASP (11) → Track E1 (12) → Track F-NOW (13) → Wave 4 E2 (14).

(f) 17 canonical references catalogued.

(g) 11 items still gated on user input.

## Contradictions noted

None new. Consumed C-1, C-2, C-3.

## Cross-references

CONTRADICTION_FOUND.md, PATH_TOPOLOGY.md, SKILL_TREE_RESOLUTION.md, DUP_RESOLUTION.md, HTML_STRIP_RUNBOOK.md, CONFIG_SPLIT_RUN2.md, DAEMONS.md, MORNING_REPORT.md.

## Verification commands

```bash
ls -la ~/cos-pipeline/PLAN_v3.{1,2}.md
wc -l ~/cos-pipeline/PLAN_v3.2.md
head -3 ~/cos-pipeline/PLAN_v3.2.md | grep -q "supersedes v3.1" && echo OK
for f in CONTRADICTION_FOUND.md PATH_TOPOLOGY.md SKILL_TREE_RESOLUTION.md DUP_RESOLUTION.md HTML_STRIP_RUNBOOK.md CONFIG_SPLIT_RUN2.md DAEMONS.md MORNING_REPORT.md; do
  grep -q "$f" ~/cos-pipeline/PLAN_v3.2.md && echo "OK $f" || echo "MISSING $f"
done
grep -E 'GPEcictr24200|saxe\.mark@gmail|msaxe@gmail|tcooper@pipermaddox' ~/cos-pipeline/PLAN_v3.2.md   # expect empty
```

## What was NOT done

- No edit to PLAN_v3.1.md
- No git push
- No edit to memory file (flagged for morning)
- No edit to live runtime files

## Key findings to surface

- **PLAN v3.2 is the integration doc the next chat reads second** (after MORNING_REPORT_2.md). v3.1 still governs everything not delta'd.
- **Three plan/memory corrections** v3.2 makes load-bearing: P1 status flip, SKILL tree canonical inversion, third tree scope addition.
- **Sequencing dependency new in v3.2**: C-2 symlink swap + 7 DEPRECATED markers must precede A-fast.7 SKILL renames, which must precede Wave 4 E2 LaunchAgent reload.
- **Memory file correction outstanding**: `project_cos_architecture.md` line 32 still says "are symlinks pointing in" — flagged but not edited.
- **No new contradictions** surfaced.
