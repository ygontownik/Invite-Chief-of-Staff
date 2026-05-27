---
name: nda-log
description: Manually trigger a merge of an ---NDA-LESSONS--- block into the NDA Reviewer doc in Drive. Normally auto-runs via dash-state-hook on every session stop. Use this only when you need to force a specific block or debug a failed auto-capture.
argument-hint: "[paste NDA-LESSONS block inline, or leave blank to be prompted]"
---

# /nda-log — Force-merge NDA-LESSONS into NDA Reviewer doc

**Normally you don't need this.** The Stop hook (`dash-state-hook.py →
run_nda_lessons_scan()`) auto-scans Claude Code transcripts for
`---NDA-LESSONS---` blocks after every session and merges them into the
NDA Reviewer doc (Drive ID `1Z_ohniOGLK3avordlS7tlVzmBzoorx6W6F3JDzjmlw0`)
without any manual step.

Use `/nda-log` when:
- You want to force a merge right now (can't wait for next stop hook)
- The auto-capture missed a block (check `~/dashboards/data/nda_lessons_state.json`)
- You're pasting a lessons block from a claude.ai session manually
- You want a `--dry-run` preview of what would change before committing

---

## STEP 1 — Get the NDA-LESSONS block

If `$ARGUMENTS` contains a `---NDA-LESSONS---` block, use it directly.

Otherwise ask:

> Paste the `---NDA-LESSONS---` block (from `---NDA-LESSONS---` to
> `---END-NDA-LESSONS---`), then press Enter.

---

## STEP 2 — Run dry-run preview

```bash
echo "<LESSONS BLOCK>" | python3 ~/cos-pipeline/tools/nda_log_processor.py parse-stdin --dry-run
```

Show the merge plan to the user (what would change in §7, §3, §9). Ask:
> Apply these changes? (yes/no)

---

## STEP 3 — Apply (on confirmation)

```bash
echo "<LESSONS BLOCK>" | python3 ~/cos-pipeline/tools/nda_log_processor.py parse-stdin
```

---

## STEP 4 — Confirm

Print:
```
NDA Reviewer updated ✓
  §7:  +1 row — <Counterparty> (<Date>)
  §3:  <changes or "no change">
  §9:  <changes or "no change">
  View: https://docs.google.com/document/d/1Z_ohniOGLK3avordlS7tlVzmBzoorx6W6F3JDzjmlw0/edit
```

---

## Checking auto-capture state

To see what's been processed automatically:

```bash
cat ~/dashboards/data/nda_lessons_state.json
```

To force a re-scan of all transcripts (ignores dedup state):

```bash
# Clear state first if you want to force reprocess
python3 ~/cos-pipeline/tools/nda_log_processor.py scan-transcript \
  ~/.claude/projects/<project-dir>/<session>.jsonl
```
