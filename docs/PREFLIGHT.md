# PREFLIGHT.md — Pre-merge checks for ~/dashboards/

This file is authoritative. Every non-trivial change to this repo walks
through these steps before being considered complete. The goal is to
catch drift (visual, behavioral, contractual) before it reaches the
principal's dashboard in the morning.

---

## Step 1 — Understand what you're changing

- [ ] Read the relevant sections of `ARCHITECTURE.md` and `CLAUDE.md`.
- [ ] Identify which data contract (dashboard-data.json, deal-system-data.json,
      deal-pipeline-data.json, cos-run-state.json) your change touches, if any.
- [ ] If it touches a user-state file (`data/user-state/*.json`), confirm
      the file's schema and whether existing entries must be migrated.

## Step 2 — Scope discipline

- [ ] Confirm the change is limited to what was asked. No drive-by refactors.
- [ ] No new files unless necessary. Prefer editing existing modules.
- [ ] No new dependencies without an explicit principal decision logged
      in `DECISIONS.md`.

## Step 3 — Data contract integrity

- [ ] If you changed a producer (compile routine, fetch script), re-run it
      and diff the output. No unexpected key removals or renames.
- [ ] If you changed a consumer (template, server route), confirm the
      contract fields you read still exist in the latest compiled payload.
- [ ] **`scripts/verify-system.sh` runs a contract check automatically** (section 5b).
      It validates every `DATA.*` reference in `cos-dashboard.html` and
      `admin-dashboard.html` against the live `dashboard-data.json` keys.
      If it FAILs, the template is reading a key that doesn't exist — fix
      the template or the producer before shipping.
- [ ] For changes touching JS functions that access *fields within* a DATA
      object (e.g. `f.status`, `item.nextStep`), manually spot-check that
      the field name matches what the producer actually writes. The contract
      check catches top-level key mismatches; it does not catch field-level
      mismatches inside nested objects.

## Step 4 — Server behavior

- [ ] If you added or modified an HTTP route, confirm auth tier
      (owner-only, partner-allowed, localhost-only).
- [ ] If you wrote to disk, confirm the write is guarded by a `threading.Lock`
      and that reads use mtime-based caching.
- [ ] If you added a new user-state file, add it under `data/user-state/`
      and wire `_ensure_user_state_dir` into its loader.

## Step 5 — Template hygiene

- [ ] Shared chrome (`_topnav.html` + `design-system.css`) must inject on
      every HTML route. No bespoke buttons that re-implement chrome.
- [ ] No hard-coded strings that duplicate `config/strings.yaml`.
- [ ] Escape user-supplied content with `esc()` before interpolation.

## Step 6 — The seven permanent checks

These are non-negotiable. Every change runs this list:

1. **Tombstone respect.** If you render a list of items (followups,
   relationships, recruiting targets, deals), it must filter through
   `__isDeleted(source, content)`. Server-side filters must filter
   through `_deleted_ids()`. A render that bypasses tombstones will
   resurrect dismissed items on every refresh — that's the exact bug
   tombstones were introduced to fix.
2. **User-state separation.** Preferences (deletions, topics, order,
   collapse state) live in `data/user-state/*.json`. Content
   (compiled artifacts from routines) lives in `data/compiled/*.json`.
   Never cross the streams. Upstream sync is allowed to overwrite
   `data/compiled/`; it is never allowed to touch `data/user-state/`.
3. **Stable item IDs.** Any item the user can delete, reorder, or
   otherwise act on persistently must have a stable ID derived from
   `djb2(source + '|' + content[:60])` — not an array index, not a
   name-only hash, not a timestamp.
4. **Server is authoritative for preferences.** `window.__*_INITIAL__`
   values injected server-side must merge *into* localStorage on boot,
   with server values winning when both exist. This is how a
   preference roams across devices.
5. **Idempotent POST endpoints.** `/item/delete`, `/topics/save`,
   `/order/save` and siblings must be safe to call twice with the same
   payload. No side effects beyond the stated write.
6. **Auth tier check on every new route.** Default-deny. If a route is
   owner-only, confirm the partner session receives 403. If it's
   localhost-only (e.g. `/warmup`, `/refresh`), confirm 403 for any
   non-loopback source.
7. **Chrome injection opt-out is explicit.** A page opts out of shared
   chrome by placing `<!-- NO_TC_CHROME -->` in its `<head>`, never by
   the injector "deciding" to skip. If you find yourself special-casing
   in `_inject_shared_chrome()`, stop and reconsider.

## Step 7 — Smoke test

- [ ] `scripts/verify-system.sh` — must be 0 FAIL.
- [ ] `scripts/design-drift-check.sh` — must be 0 FAIL.
- [ ] Curl the affected route(s) with owner auth and confirm HTTP 200.
- [ ] Click through the UI flow you changed, in a real browser, once.

## Step 8 — Write it down

- [ ] Append a CHANGELOG.md entry grouped under today's date.
- [ ] If you made a non-obvious judgment call, log it in `DECISIONS.md`
      with the Why and the How-to-apply lines.
