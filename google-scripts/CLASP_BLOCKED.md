# CLASP_BLOCKED.md — clasp clone blocked overnight (2026-05-02)

## Status as of overnight Phase 2 Track A-SLOW.3

| Step | Status |
|------|--------|
| Node 25.9.0 / npm 11.12.1 present | DONE |
| Per-user clasp install at `~/cos-pipeline/.tools/node_modules/.bin/clasp` (v3.3.0) | DONE |
| `.gitignore` updated (`.tools/`, `google-scripts/substack-sync*/`, `google-scripts/*-clasped/`) | DONE |
| `~/.clasprc.json` exists | **MISSING — interactive login required** |
| `clasp clone` of three projects | **BLOCKED on login** |

## Why blocked

Per overnight task rules, this helper is forbidden from:
- Running `clasp login` (requires interactive OAuth flow / browser)
- Overwriting any existing `~/.clasprc.json`
- Installing anything system-wide / via sudo / via Homebrew

`~/.clasprc.json` does not exist on this machine, so a fresh interactive login is the only path to enable `clasp clone`. That cannot be performed safely overnight.

## Morning steps (Yoni — ~5 minutes)

1. Open a terminal.
2. Run:
   ```
   ~/cos-pipeline/.tools/node_modules/.bin/clasp login
   ```
   This will open a browser; sign in with the **same Google account that owns the three Apps Script projects** (the account that owns the Beside AI / Otter watcher / Substack-sync scripts).
3. Confirm `~/.clasprc.json` was created:
   ```
   ls -la ~/.clasprc.json
   ```
4. Re-run Track A-SLOW.3 (or run the three `clasp clone` commands manually — see "Clone commands" below). The blocking step is just the login.

## Clone commands (run after login)

```bash
CLASP=~/cos-pipeline/.tools/node_modules/.bin/clasp
GS=~/cos-pipeline/google-scripts

# 1. Beside AI / beside-action-router
mkdir -p "$GS/beside-action-router-clasped"
cd "$GS/beside-action-router-clasped"
$CLASP clone 1H33i-KG_KmB-MBB5Mwmslu0oDLglGzqKfZn5Jwo471_gDsX7Km0HTCLc

# 2. Substack sync — script ID UNKNOWN; look up in script.google.com → Project Settings.
#    IMPORTANT: this dir is gitignored. The source contains an unrotated RBN
#    credential (REDACTED — see ~/credentials/ for live value) in backfillRBN(). DO NOT git add this directory.
mkdir -p "$GS/substack-sync-clasped"
cd "$GS/substack-sync-clasped"
$CLASP clone <SUBSTACK_SCRIPT_ID>

# 3. Otter Drive watcher — name was "Untitled project"; script ID UNKNOWN.
#    Look up in script.google.com.
mkdir -p "$GS/otter-drive-watcher-clasped"
cd "$GS/otter-drive-watcher-clasped"
$CLASP clone <OTTER_SCRIPT_ID>
```

## Verification after clone

For each successful clone:
```bash
diff -q ~/cos-pipeline/google-scripts/beside-action-router/Code.gs \
        ~/cos-pipeline/google-scripts/beside-action-router-clasped/Code.gs
```
Any difference means the local partial source has drifted from the live Apps Script project — investigate before pushing.

## What was NOT done overnight

- No clasp login attempted (intentional — interactive only)
- No `clasp clone` run (blocked on login)
- No `clasp push` (out of scope for this track)
- No commit of any source from `google-scripts/substack-sync/` or `*-clasped/`

## What WAS done overnight

- Verified node + npm exist; recorded versions
- Installed `@google/clasp@3.3.0` per-user at `~/cos-pipeline/.tools/`
- Updated `.gitignore` to block `.tools/`, all `substack-sync*/`, all `*-clasped/` dirs
- Verified no substack-sync source is tracked or staged in git
- Wrote `MULTI_TENANT_PORTING.md` with placeholder convention
- Wrote `STATUS.md` with overnight ledger
