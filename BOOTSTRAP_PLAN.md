# BOOTSTRAP_PLAN.md — tenant deployment packaging (malleable)

Authored 2026-05-03 during run-3 prep. Companion to HANDOFF_v2.md.

**Status as of end of session 4 (2026-05-03 evening):** 7 of 8 builds shipped. Only Build #7 (fresh-Mac dry run) remains, and it's user-side. Abstraction points 1–5 done; 6/7/9/10 are intentional YAGNI deferrals (see "What we intentionally don't build now" at the bottom). Tomac runs the production stack; P-onboarding is gated only on the user-side Pre-P checklist + fresh-Mac dry run.

## Design constraint

**The deployment topology must be a config decision, not a code rewrite.** Today's
target is "tenant runs on their own Mac" — but the same code should run unchanged
on:

- Your Mac Mini (centralized hosting)
- Tenant's own Mac (the immediate target — P)
- Cloud VM per tenant (Hetzner, Fly.io)
- Hybrid: tenant's Mac for data, cloud for always-on services
- Future deployment models we haven't thought of yet

If a future decision flips us from "P's Mac" to "cloud VM per tenant," we should
not need to rewrite pipelines. We should swap a config and reinstall.

To preserve that flexibility, every place that **assumes** something about the
host environment is an abstraction point that bootstrap.sh must respect.

═══════════════════════════════════════════════════════════════════════
ABSTRACTION POINTS — what bootstrap must NOT hardcode
═══════════════════════════════════════════════════════════════════════

| # | Concern | Today's assumption | Malleable approach | Status (end session 4) |
|---|---|---|---|---|
| 1 | **Tenant slug** | `tomac` (yours) | `$COS_TENANT_SLUG` env var, set once at bootstrap, read everywhere | ✓ DONE — `setup.sh --instance=<slug>` drives everything via `multi_tenant.slug_to_port()` + slug-suffixed paths |
| 2 | **Filesystem paths** | `~/cos-pipeline/data-tomac/` | `$COS_DATA_DIR` resolved from slug; never hardcode `/Users/ygontownik/` | ✓ DONE — slug-derived `data-<slug>/`, `logs-<slug>/`, `~/cos-pipeline-config-<slug>/` everywhere |
| 3 | **Config repo location** | `~/cos-pipeline-config-tomac/` | `$COS_CONFIG_DIR` env var; bootstrap symlinks/clones into the conventional location | ✓ DONE — C3 migration (session 4) made -tomac/ canonical; `_firm_context._find_config_dir()` searches in correct order |
| 4 | **Credential storage backend** | macOS keychain | Pluggable: keychain (Mac default), env vars (Linux/cloud fallback), AWS/GCP secrets (cloud premium). Single interface: `_fc.load_secret(key)`. | ✓ DONE — `_secrets.py` with backend detection (keychain/env), 21 tests; canonical entry shape `service=<prefix>/<KEY>, account=$USER` per session-4 fix |
| 5 | **Scheduler backend** | launchd plists | Pluggable: launchd (Mac default), systemd (Linux), cron (anywhere), cloud scheduler (Fly cron, Lambda). Single interface: register routine X to run on cron expression Y. | ✓ DONE — `_scheduler.py` with launchd backend; cron/systemd hooks scaffolded but YAGNI-deferred |
| 6 | **Dashboard host/port** | `localhost:7777` | `$COS_DASHBOARD_HOST` + `$COS_DASHBOARD_PORT` from `firm_context.yaml :: dashboard`; default `127.0.0.1:7777` for self-hosted | ⚠️ PARTIAL — `$DASH_PORT` derived from `multi_tenant.slug_to_port()` and passed to plist; `$DASHBOARD_HOST` env exists in plist; no `firm_context.yaml :: dashboard` block yet (deferred — not blocking P) |
| 7 | **OAuth flow** | Interactive browser on same machine | Bootstrap supports both: interactive (Mac default) AND headless paste-token-from-other-device (cloud) | ⚠️ INTERACTIVE ONLY — `oauth_bootstrap.sh` shipped (session 4) but only the browser flow; headless deferred until cloud-host scenario |
| 8 | **Pipeline → dashboard handoff** | Shared filesystem (JSON files) | Today: filesystem. Tomorrow: same code works over networked FS, or HTTP API. Don't introduce file locks or other same-machine assumptions. | ✓ AS-IS — filesystem JSON; no file locks introduced. Q9 migration (session 3) moved `~/dashboards/data/compiled/` → `~/cos-pipeline/data-tomac/compiled/` with symlink, preserving the per-tenant pattern |
| 9 | **Network ingress for webhooks** | ngrok tunnel + local listener | Pluggable: ngrok (current), cloudflared, tailscale-funnel, public IP. Tenant chooses at bootstrap. | ⚠️ NOT MENU-DRIVEN — ngrok is the chosen path (DECISIONS Q3 confirmed live); cloudflared coexists for inbound-dashboard but no setup.sh menu. Deferred until tenant #3 needs a different ingress |
| 10 | **Update mechanism** | Manual `git pull` | `cos-update` script: pulls universal code, runs migrations, re-registers scheduler entries. Same logic regardless of host. | ❌ NOT BUILT — `cos-update` script doesn't exist. Deferred until first cross-tenant breaking change |

**Summary:** 1–5 fully done (the ones that block P-onboarding). 6/7/9/10 are intentional YAGNI deferrals — none of them block P running on their own Mac with the default topology, but each will need to be built before scenario 2 (we sell as a hosted product).

═══════════════════════════════════════════════════════════════════════
WHAT bootstrap.sh DOES — phased, idempotent
═══════════════════════════════════════════════════════════════════════

### Phase 0 — Sanity & prerequisites (60 sec)

```
- Check OS (macOS 13+ for now; document Linux as future)
- Check Python ≥ 3.11
- Check Homebrew installed
- Check Claude Code CLI installed (or skip if cloud-mode = no SKILL daemons)
- Check git installed
- Print detected environment summary; abort with clear message if anything missing
```

### Phase 1 — Tenant identity

```
- Prompt: tenant slug (e.g. "re-dev"). Validate against C18 reserved list.
- Prompt: principal name + email
- Prompt: firm name + domain
- Pick deployment topology (interactive menu):
    [1] Self-hosted on this Mac (default — most private)
    [2] Hosted by another party (advanced — sets COS_HOSTED_MODE=remote)
    [3] Cloud VM (future — print "not yet supported, see roadmap")
- Write $COS_TENANT_SLUG to ~/.cos-pipeline.env
- Write $COS_DEPLOYMENT_MODE to ~/.cos-pipeline.env
```

### Phase 2 — Code + config

```
- Clone (or update if present) ~/cos-pipeline/ from public git URL
- Clone (or create) ~/cos-pipeline-config-<slug>/ from private git URL OR
  scaffold a fresh repo from re-dev template
- Symlink tenant config into discoverable location:
    $COS_CONFIG_DIR -> ~/cos-pipeline-config-<slug>/
- Verify firm_context.yaml has all REQUIRED fields populated; abort with
  list of unfilled fields if any are blank
```

### Phase 3 — Secrets

```
- Detect credential backend (keychain on Mac, env-file on Linux/cloud)
- Prompt and store (each is optional with skip-with-warning):
    ANTHROPIC_API_KEY     (required)
    ASSEMBLYAI_API_KEY    (required for call/podcast transcription)
    GOOGLE_CLIENT_ID + SECRET  (required for Google Workspace integration)
    NGROK_AUTHTOKEN       (required only if call-recording in scope)
    SMTP_APP_PASSWORD     (required for outbound notifications)
- Validate each key by making a probe call (1 token Claude call, 1 AssemblyAI
  status call, etc.); fail loudly with actionable error if invalid
```

### Phase 4 — Google OAuth

```
- Run interactive Google OAuth flow:
    - Open browser to consent URL
    - User logs in, grants Drive + Docs + Gmail scopes
    - Token saved to credential backend
- For headless mode (cloud, no browser): print device-code URL, prompt user
  to complete flow on another device, paste resulting token
- Verify token works by listing Drive folders
```

### Phase 5 — Routines registration

```
- Read routines.yaml (canonical source of all SKILLs + daemons)
- Filter to routines marked enabled-for-tenant in firm_context.yaml
- Register each via the scheduler backend abstraction:
    - macOS: write plist to ~/Library/LaunchAgents/com.cos.<slug>.<routine>.plist,
      bootstrap with launchctl
    - Linux/systemd: write .timer + .service to ~/.config/systemd/user/
    - Cloud/cron: write crontab entries
- Skip routines whose dependencies aren't met (e.g. no AssemblyAI key →
  skip podcast pipeline; no ngrok → skip Otter webhook)
- Print summary: N routines registered, M skipped (with reasons)
```

### Phase 6 — Dashboard

```
- Generate per-tenant users.json with first user (the principal) and a
  fresh strong password
- Allocate port from C17 range if not specified (default: 7777 for first
  tenant on a machine, then 7779+ if collision)
- Start dashboard agent (com.cos.<slug>.dashboard.plist)
- curl health check; abort + rollback if not 200 within 10s
- Email principal: "Your dashboard is live at http://<host>:<port>/, login
  X, password Y"
```

### Phase 7 — Verification

```
- Run all unit tests
- Run a heartbeat probe across all registered routines
- Print summary table: routine name, last fired (or "never"), status
- Print "Setup complete. Next: open dashboard, run cos-personal-briefing
  manually to seed initial state."
```

### Rollback (Phase R)

```
- bootstrap.sh --uninstall <slug>:
    - launchctl bootout all com.cos.<slug>.* agents
    - Delete ~/Library/LaunchAgents/com.cos.<slug>.*.plist
    - Optionally: rm -rf ~/cos-pipeline-config-<slug>/
                  rm -rf ~/cos-pipeline/data-<slug>/
                  remove keychain entries with prefix cos-pipeline-<slug>
    - Prompt before destructive deletions; default is "preserve config + data"
- Always reversible. Prove it by running install → uninstall → reinstall in CI.
```

═══════════════════════════════════════════════════════════════════════
BUILD ORDER
═══════════════════════════════════════════════════════════════════════

| # | Item | Effort | Blocks P? | Status |
|---|---|---|---|---|
| 1 | Inventory existing `.next` setup files; map gaps to phases above | 1h | Yes | ✓ DONE (session 3) |
| 2 | Build credential-backend abstraction (`_fc.load_secret()`); migrate Track B6 keychain code behind it | 2h | Yes | ✓ DONE — `_secrets.py` (session 3); shape bug fixed + canonical convention pinned (session 4) |
| 3 | Build scheduler-backend abstraction (registers launchd today, future-extensible); migrate `setup_launchagents.sh.next` behind it | 3h | Yes | ✓ DONE — `_scheduler.py` (session 3) |
| 4 | Wire ANTHROPIC_API_KEY through `_fc.load_secret()` in all 6 Python pipelines (replaces direct `os.environ` reads) | 2h | Yes | ✓ DONE — 5 pipelines wired (session 3) |
| 5 | Write bootstrap.sh phases 0–7 | 1 day | Yes | ✓ DONE — `setup.sh` upgraded with Phase 0 prereq + multi-tenant args (session 3); OAuth Step 6 consolidated into `oauth_bootstrap.sh` (session 4) |
| 6 | Write bootstrap.sh --uninstall + idempotency tests | 4h | No (but needed before second tenant) | ✓ DONE — `setup.sh --uninstall <slug>` with 7-test slug-isolation snapshot (session 4) |
| 7 | Test on a fresh Mac user account (your own machine, new user) | 4h | Yes | ⏳ PENDING — user-side; cannot run from session. Validate gate now reports truthful PASS/FAIL after session-4 fixes, so a fresh-Mac dry run is now meaningful |
| 8 | Document P-side prerequisites (Homebrew, Python, Claude Code, Anthropic account, etc.) in INSTALL.md | 2h | Yes | ✓ DONE — `INSTALL.md` (session 3); refreshed for OAuth consolidation, uninstall, validate, troubleshooting (session 4) |

**Total to "P can install on their Mac":** ~3 days of focused work — **ACTUAL: 7/8 done in 2 sessions; Build #7 is the only remaining gate and it's user-side.**

═══════════════════════════════════════════════════════════════════════
WHAT WE INTENTIONALLY DON'T BUILD NOW (YAGNI)
═══════════════════════════════════════════════════════════════════════

To stay malleable without over-engineering:

- **Cloud deployment** — abstractions allow it; don't build cloud-specific code until a tenant actually wants cloud. Fly.io / Hetzner instructions can be a 1-page README appended later.
- **Object storage backends** — local filesystem stays the default. Pipeline contracts use file paths today; if cloud comes, swap for s3:// URLs behind the same path-resolution helper.
- **Multi-host deployment** (pipelines on machine A, dashboard on machine B) — possible but no current need. Don't introduce networked-IPC complexity.
- **Tenant self-service portal** — too far from current scale. Bootstrap for now is a script you (Yoni) walk a tenant through, not a SaaS sign-up flow.
- **Audit log** — add when first tenant asks. Use existing log-rotation; don't build SOC-2 infrastructure speculatively.
- **Update auto-rollout** — `cos-update` script is in scope; auto-pull on a cron is not. Tenants pull updates manually; you announce releases.

═══════════════════════════════════════════════════════════════════════
HOW THIS STAYS MALLEABLE
═══════════════════════════════════════════════════════════════════════

The architecture stays flexible because of three rules:

1. **Every host-specific operation goes through an abstraction.** `_fc.load_secret()` for credentials, `_scheduler.register()` for cron-like work, `_paths.tenant_data_dir(slug)` for filesystem. The implementation can be swapped per-host without touching pipeline code.

2. **Config drives behavior, code stays generic.** Whether a tenant uses ngrok or cloudflared, launchd or systemd, keychain or env vars — these are config keys in `firm_context.yaml :: deployment`, not branches in Python code.

3. **Bootstrap is the only place that knows about specific hosts.** The Python pipelines never check `if host == "Mac"`. They call abstractions. Bootstrap configures which backend the abstractions use.

If we decide in 6 months to move all tenants to a managed cloud VM provider, the work is:
- Add `_scheduler.register_systemd()` implementation
- Add `_fc.load_secret_envfile()` implementation
- Write a `cloud-bootstrap.sh` that runs the abstractions in cloud mode

Pipelines unchanged. Dashboards unchanged. Test fixtures unchanged. **That's the payoff.**

═══════════════════════════════════════════════════════════════════════
RISKS WORTH NAMING
═══════════════════════════════════════════════════════════════════════

- **Abstraction overhead:** wrapping launchd in a generic interface costs ~2h vs. just calling launchctl directly. The 2h pays back the first time we add systemd. If we never add systemd, it was wasted. The bet: we will (cloud = systemd or container scheduler).
- **Maintenance burden of two paths:** if Mac and Linux get separate test matrices, that's overhead. Mitigation: only Mac is tier-1 supported until the second backend has a real user.
- **Hidden assumptions:** there are probably 3–5 places I haven't caught where Mac-isms leak in (e.g. `/usr/sbin/sendmail`, `osascript` for notifications, `pbcopy`). Audit during bootstrap.sh build; document or abstract each.
- **OAuth fragility:** Google OAuth tokens expire / revoke; cloud-mode flow is materially harder than Mac-mode flow. Build Mac-mode well first; document cloud-mode as "advanced."

═══════════════════════════════════════════════════════════════════════
DELIVERABLES THIS DOCUMENT GATES
═══════════════════════════════════════════════════════════════════════

- `~/cos-pipeline/bootstrap.sh` (new)
- `~/cos-pipeline/INSTALL.md` (new — for tenants)
- `~/cos-pipeline/_paths.py` (new — path-resolution helper)
- `~/cos-pipeline/_secrets.py` (new — credential-backend abstraction)
- `~/cos-pipeline/_scheduler.py` (new — scheduler-backend abstraction)
- Modifications to 6 Python pipelines to use `_fc.load_secret()`
- Test suite extension: integration test that runs bootstrap → uninstall → reinstall

═══════════════════════════════════════════════════════════════════════
START HERE (for the session that builds this)
═══════════════════════════════════════════════════════════════════════

Read in order:
1. This file (BOOTSTRAP_PLAN.md)
2. `~/cos-pipeline/setup.sh.next`
3. `~/cos-pipeline/setup_keychain.sh.next`
4. `~/cos-pipeline/setup_launchagents.sh.next`
5. `~/cos-pipeline/multi_tenant.py`
6. `~/cos-pipeline/_firm_context.py`

Confirm baseline canary (200/1/23/17/17, 92/92 tests).

Recommend: start with Build Order #2 (credential abstraction) because it's
the smallest piece that unlocks the rest, AND it's a useful improvement
even if we never reach P onboarding.

Tell me one sentence about what you'll do first; wait for my go.
