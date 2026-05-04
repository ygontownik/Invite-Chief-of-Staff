# CLAUDE.md — Rules for operating in ~/dashboards/

This file tells Claude Code how to work within the `~/dashboards/` tree.
It is the single source of behavioral truth for any agent editing files here.

## The one-location rule

**Every concern lives in exactly one place.** Do not duplicate files, do
not shadow files under different names, do not keep "backup" copies
inside the tree. Archives go to `~/dashboards/archive/` only.

If you find a file that appears to duplicate logic somewhere else, STOP
and ask. The cost of an unintended fork is high; the cost of asking is
low.

## Folder rules

```
~/dashboards/
├── app/            # long-running dashboard server + its templates
├── routines/       # scheduled jobs (ingest / process / brief / compile / send)
│   ├── ingest/     # pulls from external sources (PDFs, Drive, web)
│   ├── process/    # transforms raw inputs (transcripts → memos)
│   ├── brief/      # produces human-facing briefings
│   ├── compile/    # aggregates + dashboards
│   └── send/       # emails, notifications
├── data/
│   ├── deals/      # per-deal source of truth (YAML + xlsx + memos)
│   └── compiled/   # json outputs produced by routines (regenerable)
├── config/         # schedule.yaml, drive-docs.yaml, launchd-plists/
├── docs/           # this file + ARCHITECTURE, RUNBOOK, templates
├── scripts/        # one-shot / verification utilities
├── archive/        # retired code, backups, snapshots (never imported)
└── logs/           # runtime logs (stdout/stderr from routines)
```

Placement rules:
- **New scheduled routine** → `routines/<group>/<name>.py`. Register in
  `config/schedule.yaml` with cron, skill, group.
- **Compiled artifact** (json, md outputs) → `data/compiled/`. Must be
  regenerable from sources. Never hand-edit.
- **Deal source of truth** → `data/deals/<TICKER>/`. Contains `deal.md`,
  `profit-model.xlsx`, `notes/`, etc.
- **Doc ID or folder ID** → add to `config/drive-docs.yaml`. Never
  hardcode Drive IDs in routines.
- **New launchd daemon** → canonical plist goes in
  `config/launchd-plists/`, then run `generate.sh` to deploy.

## Update rules

When editing a routine:
1. Preserve the `_HERE = Path(__file__).resolve().parent` / `_ROOT = _HERE.parent.parent`
   path pattern. `.resolve()` is critical — it follows the symlink
   compatibility shims so the script works whether invoked via the new
   path or an old symlinked path. Never introduce absolute paths like
   `/Users/<username>/...`.
2. If you change a scheduled cron, update `config/schedule.yaml` first,
   then the plist (if launchd) or the Claude Code scheduled-tasks UI.
3. If you rename a Drive doc, update `config/drive-docs.yaml` — routines
   read from that file.
4. If you add a dedup tracker, register it under `local_state:` in
   `drive-docs.yaml`.

## Security rules

- **No API keys or passwords in code.** Use env vars, Keychain, or
  `~/credentials/`. The dashboard server reads `OWNER_PASSWORD` and
  `PARTNER_PASSWORD` from env.
- **No credentials in git-tracked files.** `~/credentials/` is
  intentionally outside the tree.
- **Dashboard auth tiers**: owner gets everything; partner restricted to
  the partner-allowed tile URLs declared in `config/dashboard-tiles.yaml`.
  Localhost POSTs are unauthenticated (for warmup); remote POSTs require
  owner.

## Verification rules

Before reporting "done" on any structural change:
1. Run `scripts/verify-system.sh` — must pass.
2. If you touched a routine, execute it once manually and inspect output.
3. If you touched the server, curl the affected route(s) from localhost
   AND from `192.168.4.21:7777` to confirm auth still behaves.

## Operating Principles

These are the load-bearing rules for any change inside `~/dashboards/`.
They are additive to, not a replacement for, the rules above. When in
doubt, optimize for the principle, not the literal text.

1. **User state is sacred, content is regenerable.** Anything the user
   clicks, dismisses, reorders, or writes lives under
   `data/user-state/`. Anything a routine produces lives under
   `data/compiled/`. Upstream sync freely overwrites `data/compiled/`;
   it never touches `data/user-state/`. A "delete" that doesn't survive
   the next ingest is a bug, not a feature.

2. **Stable IDs or don't bother.** Items the user can act on
   persistently must carry a content-derived ID
   (`djb2(source + '|' + content[:60])`). Array indices and names-only
   hashes drift; they are banned for persistence keys.

3. **Default-deny on routes.** Every new HTTP route declares its auth
   tier up front: owner-only, partner-allowed, or localhost-only.
   There is no "public" tier. Static assets under `/static/*` are the
   single documented exception.

4. **One source of truth per concern.** Strings that appear in the
   topnav or button chrome come from `config/strings.yaml`. Drive doc
   IDs come from `config/drive-docs.yaml`. Schedules come from
   `config/schedule.yaml`. Duplicating any of these inline is a
   rejection-grade offense in review.

5. **Server-first preferences.** Preferences persist server-side and
   hydrate clients via `window.__*_INITIAL__`. localStorage is a cache,
   not a store. When the two disagree, the server wins.

6. **Chrome is shared, not copy-pasted.** `_topnav.html` +
   `design-system.css` render on every HTML route via
   `_inject_shared_chrome()`. A bespoke header in a template is a code
   smell — extract to the partial or explain in `DECISIONS.md` why it
   can't be.

7. **Every change walks PREFLIGHT.md.** The seven permanent checks in
   `docs/PREFLIGHT.md` Step 6 are non-negotiable. If a check fails,
   fix the cause; do not suppress the check. If a check is wrong,
   log a decision to change it in `DECISIONS.md` before editing.

8. **Fix the source, not just the symptom.** When a post-hoc filter
   (regex denylist, dedupe table, suppression rule) is added to the
   dashboard compile layer to scrub garbage, that's a signal the
   upstream LLM extraction prompt is emitting wrong output. Always
   update the preamble in the same pass — `BACKFILL_PREAMBLE` in
   `routines/process/cos_otter_backfill.py` for transcripts, and the
   extraction system prompt near the top of
   `routines/process/cos_email_backfill.py` for emails. If the prompt
   change is non-obvious, log it in `DECISIONS.md`. A filter without
   a corresponding prompt fix means the garbage keeps coming and the
   filter keeps growing.

## What NOT to do

- Do not create parallel file trees (no `~/dashboards-new/`, no
  `~/dashboards-v2/`).
- Do not duplicate logic across `app/` and `routines/` — pick one home.
- Do not introduce Drive doc IDs outside `config/drive-docs.yaml`.
- Do not hand-edit files in `data/compiled/` — they are regenerable.
- Do not edit plists in `~/Library/LaunchAgents/` directly. Edit the
  canonical copy in `config/launchd-plists/` and run `generate.sh`.
- Do not commit secrets, tokens, or `.pickle` files.
- Do not skip `scripts/verify-system.sh` after a structural change.
