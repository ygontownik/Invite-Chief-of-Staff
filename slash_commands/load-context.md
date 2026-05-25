---
description: Load the doc set for a TCIP/CoS task type (dashboard, deal_general, new_deal, drive_org, cos_pipeline, financial_modeling) from CONTEXT-MANIFEST.yaml. Eliminates the "which docs do I read for X" guessing game.
argument-hint: "<task_type> [deal_id]"
---

# /load-context — Load the right docs for the task

Reads `~/dashboards/docs/CONTEXT-MANIFEST.yaml`, finds the `contexts.<task_type>`
block, and loads every doc in its `must_read` list. If `<task_type>` is
`deal_general` and a `[deal_id]` is provided, also loads the per-deal
docs (`deal.md`, `log.json`, `actions.md`, `profit-model.xlsx`).

---

## STEP 1 — Parse argument

`$ARGUMENTS` should be:
- A task type: `universal`, `dashboard`, `deal_general`, `new_deal`,
  `drive_org`, `cos_pipeline`, `financial_modeling`
- Optionally followed by a deal_id (e.g. `deal_general cholla`)

If empty: print the list of known task types from the manifest and exit.

---

## STEP 2 — Load the manifest

```bash
python3 - <<'EOF'
import yaml, json, sys, os
manifest_path = os.path.expanduser("~/dashboards/docs/CONTEXT-MANIFEST.yaml")
with open(manifest_path) as f:
    m = yaml.safe_load(f)

args = "$ARGUMENTS".strip().split()
if not args:
    print("Available task types:")
    for k, v in m["contexts"].items():
        print(f"  {k:20s} — {v.get('when', '')}")
    sys.exit(0)

task = args[0]
deal_id = args[1] if len(args) > 1 else None

if task not in m["contexts"]:
    print(f"Unknown task type: {task}. Known: {list(m['contexts'].keys())}")
    sys.exit(1)

ctx = m["contexts"][task]
out = {
    "task": task,
    "when": ctx.get("when"),
    "must_read": ctx.get("must_read", []),
    "recommended": ctx.get("recommended", []),
    "skill": ctx.get("skill") or ctx.get("skills"),
    "deal_id": deal_id,
}

# Resolve per-deal pattern if deal_id given
if deal_id and "per_deal_docs" in ctx:
    pattern = ctx["per_deal_docs"]["pattern"]
    deal_dir = os.path.expanduser(f"~/dashboards/data/deals/{deal_id}/")
    out["per_deal_docs"] = [
        os.path.join(deal_dir, n)
        for n in ["deal.md", "log.json", "actions.md", "profit-model.xlsx"]
        if os.path.exists(os.path.join(deal_dir, n))
    ]

print(json.dumps(out, indent=2))
EOF
```

Parse the JSON output. You now have:
- The `must_read` list (paths)
- The `recommended` list (paths)
- The skill(s) associated with this task
- Optional per-deal docs

---

## STEP 3 — Read every must_read doc

For each path in `must_read`:
- Use the `Read` tool to load it (resolve `~/` to the actual home dir)
- Display a one-line summary: "Loaded {path} ({N} lines)"

For per-deal docs (if applicable): load each one.

**Jane substrate** — if the context block has a `jane_substrate` list AND a `deal_id` is provided:
- Load `~/dashboards/data/jane/north_star.md` (always)
- Load `~/dashboards/data/deals/{deal_id}/decision_state_jane.md` if it exists
- Load `~/dashboards/data/deals/{deal_id}/jane_brief.md` if it exists
- Display: "Loaded Jane substrate for {deal_id}: north_star + decision_state_jane + jane_brief"

If no `deal_id` but context has `jane_substrate`, load only `north_star.md`.

Do NOT load `recommended` automatically — surface them as "additionally available" but only load if the task warrants it.

---

## STEP 4 — Announce what's loaded

Print a short summary:

```
Loaded context for: {task}
  Purpose: {when}
  Docs read: {N}
  Skill: {skill}
  {if deal_id:} Per-deal docs for {deal_id}: {N}

Ready to proceed.
```

---

## STEP 5 — Surface the relevant invariants

If the task block lists `key_invariants` or `invariants_doc`, print a one-liner reminder:

> Invariants in play: I4 (one source of truth), I11 (edit in place — never recreate registered docs).

This keeps the rules of the road top-of-mind for the session.

---

## Notes

- This skill exists so Yoni doesn't have to say "read CLAUDE.md, then PREFLIGHT, then..." every time he starts work
- The manifest is the single source of truth. If it drifts from reality, fix it once and every future session benefits
- If you're about to edit a Drive doc, double-check it's in `drive-docs.yaml` and edit by ID (don't create a new one — invariant I11)
