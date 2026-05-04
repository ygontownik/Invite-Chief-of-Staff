# STATUS.md — Track A-SLOW.3 (Apps Script clasp clones) overnight ledger

Date: 2026-05-02 (overnight Phase 2)
Helper: A-SLOW.3 (no worktree isolation — touches global tooling)

---

## Done

- [x] Verified Node 25.9.0 and npm 11.12.1 present (no install needed).
- [x] Created `~/cos-pipeline/.tools/` directory.
- [x] Created `~/cos-pipeline/.tools/package.json` (per-user package, name `cos-clasp-tools`).
- [x] Installed `@google/clasp@3.3.0` per-user at `~/cos-pipeline/.tools/node_modules/.bin/clasp`. Install log at `~/cos-pipeline/.tools/install.log`.
- [x] Verified `clasp --version` → `3.3.0`.
- [x] Updated `~/cos-pipeline/.gitignore` to exclude:
  - `.tools/` (node_modules)
  - `google-scripts/substack-sync/` and `google-scripts/substack-sync*/` (unrotated RBN credential in source)
  - `google-scripts/*-clasped/` (any clasp working dir)
  - `.clasp.json`, `.clasprc.json`
- [x] Verified via `git status --short` and `git ls-files google-scripts/` that **no substack-sync source is tracked or staged**. The `google-scripts/` tree is wholly untracked.
- [x] Wrote `MULTI_TENANT_PORTING.md` with placeholder convention, substitution sites, and `setup.sh` flow for second-tenant installs.
- [x] Wrote `CLASP_BLOCKED.md` with morning steps.
- [x] Appended 5-line summary to `~/cos-pipeline/PHASE2_CLASP.md`.

---

## Blocked

- [ ] `clasp clone` of three Apps Script projects.
  - **Reason:** `~/.clasprc.json` does not exist on this machine. Per overnight rules, this helper cannot run `clasp login` (interactive OAuth, not safely automatable). See `CLASP_BLOCKED.md`.
- [ ] Source diff verification (cloned vs. partial source).
  - **Reason:** depends on `clasp clone`.
- [ ] Looking up Apps Script IDs for **otter-drive-watcher** and **substack-sync**.
  - Only `beside-action-router` script ID is known: `1H33i-KG_KmB-MBB5Mwmslu0oDLglGzqKfZn5Jwo471_gDsX7Km0HTCLc`.
  - The other two are owned by Yoni's Google account; IDs must be fetched from script.google.com → Project Settings after morning login. Recorded in `CLASP_BLOCKED.md`.

---

## Morning steps (in order)

1. Run `~/cos-pipeline/.tools/node_modules/.bin/clasp login` (browser OAuth — sign in with the Google account that owns all three Apps Script projects).
2. Open script.google.com, find the **substack-sync** project and **otter-drive-watcher** ("Untitled project") project, copy their script IDs.
3. Run the three `clasp clone` commands listed in `CLASP_BLOCKED.md` (they target `*-clasped/` dirs, all gitignored).
4. Run `diff -q` between each `<project>/Code.gs` and the matching `<project>-clasped/Code.gs` to confirm parity. Investigate any drift.
5. Then proceed to template-extraction (Privacy P4 — out of scope for this overnight track).

Estimated morning time: ~10 minutes if no drift, ~30 minutes if drift exists.

---

## Hard rules honored

- No sudo, no Homebrew, no global npm install.
- No interactive `clasp login`.
- No overwrite of any pre-existing `~/.clasprc.json` (file did not exist; no risk).
- No git commit of any source under `google-scripts/`.
- No push to GitHub.
- No modification of `~/credentials/`, `~/dashboards/app/templates/`, or LaunchAgents.
- No silent fix of contradictions; none encountered for this track.

---

## Files written this session

- `~/cos-pipeline/.tools/package.json`
- `~/cos-pipeline/.tools/install.log`
- `~/cos-pipeline/.tools/node_modules/...` (clasp + deps)
- `~/cos-pipeline/google-scripts/CLASP_BLOCKED.md`
- `~/cos-pipeline/google-scripts/MULTI_TENANT_PORTING.md`
- `~/cos-pipeline/google-scripts/STATUS.md` (this file)
- `~/cos-pipeline/PHASE2_CLASP.md` (appended)
- `~/cos-pipeline/.gitignore` (extended)
