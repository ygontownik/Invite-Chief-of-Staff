# Admin runbook — onboarding a new subscriber

End-to-end checklist for what **you** (the platform owner) do when someone signs up. The subscriber side is documented at `docs/onboard.html`. This file is your side.

## Architecture context

- **OAuth model:** all subscribers use a shared OAuth client owned by your Google Cloud project (`decoded-badge-387412`). You add each subscriber as a **Test User** in the consent screen. Tokens are issued to your OAuth client but stored in the subscriber's local Keychain — their data stays in their Google account, not yours.
- **Why shared client (not per-subscriber):** each subscriber would need to set up their own Google Cloud project — 15+ min of console clicks, foreign UX. Shared client is one-click for the subscriber. Cap is 100 test users in Testing mode; revisit verification at ~50 active subscribers.
- **Remote access:** Tailscale (already wired into onboarding flow Step 3). Subscribers install Tailscale on Mac + iPhone, log in with their own Tailscale account (free tier, separate from yours). The dashboard URL becomes `http://<their-mac-tailscale-name>:7777`.
- **Phone bookmark:** standard Safari "Add to Home Screen" — already covered in Step 3 of the onboarding HTML.

## When a new subscriber signs up

### 1. Collect from them (one email)
- GitHub username
- **Gmail address** they'll use for the dashboard ← critical, controls OAuth access
- Preferred firm slug (lowercase, e.g. `acme`, `peakcap`)
- Anthropic-tier preference: subscription (Tier 1, requires Claude Code) or BYO API key (Tier 2a)

### 2. Add them as Google OAuth test user (2 min) ← THIS IS THE BLOCKER
1. Open https://console.cloud.google.com/apis/credentials/consent?project=decoded-badge-387412
2. Scroll to **Test users**
3. **+ Add users** → enter their Gmail → Save

Without this, their OAuth flow will fail with "This app isn't verified" and **no "Advanced → unsafe" escape hatch** (Google removed that for non-test users in 2024).

### 3. Grant GitHub access (1 min)
- Add them as a collaborator to `ygontownik/Invite-Chief-of-Staff` (public — no-op, but confirms repo)
- Add them as a collaborator to whatever private config repo they get (or fork-and-grant)

### 4. Send them `gdrive_credentials.json` (secure channel)
- The file lives at `~/credentials/gdrive_credentials.json` on your machine
- Send via 1Password, Signal, or encrypted email — **not** plain Gmail
- This is your OAuth client config; it's not a secret per se but treat it as one

### 5. Send them the onboarding link
- `https://ygontownik.github.io/Invite-Chief-of-Staff/onboard.html`
- They run the bootstrap installer, walk through the 3 steps, done
- Total time on their side: ~15 min

### 6. Confirm with them when they're done
- Ask them to send you a screenshot of the dashboard chip showing 🟢 green
- If anything failed during install, the bootstrap script can be safely re-run

## Common subscriber-side issues you'll get pinged on

| Symptom | Cause | Fix |
|---|---|---|
| "This app isn't verified" + no Advanced link | You haven't added them as test user | Step 2 above; ask them to retry installer |
| "redirect_uri_mismatch" | They have a stale OAuth client | Re-send fresh `gdrive_credentials.json` |
| "Calendar fetch failed: 403" | Old token without calendar.readonly scope | Have them delete `~/credentials/gdrive_token.pickle` and re-run installer |
| Dashboard loads but is empty | Capture pipeline hasn't run yet | Manually trigger: `~/dashboards/scripts/cos-capture-pipeline-runner.sh` |
| Tailscale URL doesn't work from phone | Mac asleep, or different Tailscale account | Confirm Mac awake; confirm same Tailscale login on both devices |

## When you stop accepting new test users (~50 subscribers)

You'll need to publish the OAuth app for verification. Steps:
1. Privacy policy + terms hosted on a domain you own
2. Logo + branding on the consent screen
3. Submit for verification at https://console.cloud.google.com/apis/credentials/consent — Google review takes 4–8 weeks
4. After verification, anyone can sign in without test-user enrollment

Until then, just watch the test-user count and add manually.
