# Admin runbook — onboarding a new subscriber

End-to-end checklist for what **you** (the platform owner) do when someone signs up. The subscriber side is documented at `docs/onboard.html`. This file is your side.

## Architecture context

- **OAuth model:** all subscribers use a shared OAuth client owned by your Google Cloud project (`decoded-badge-387412`). The app is in **Production mode** (not Testing mode), so the test-user list mechanism does NOT apply — there's nothing to add a subscriber to. Tokens are issued to your OAuth client but stored in the subscriber's local Keychain — their data stays in their Google account, not yours.
- **Production-mode + unverified scopes:** subscribers will see "Google hasn't verified this app" during OAuth. They click **Advanced → Go to TCIP (unsafe)** to proceed. The user cap is 100 (current usage shown at https://console.cloud.google.com/auth/audience?project=decoded-badge-387412); submit for verification when you approach the cap.
- **Why shared client (not per-subscriber):** each subscriber would need to set up their own Google Cloud project — 15+ min of console clicks, foreign UX. Shared client is one-click for the subscriber.
- **Remote access:** Tailscale (already wired into onboarding flow Step 3). Subscribers install Tailscale on Mac + iPhone, log in with their own Tailscale account (free tier, separate from yours). The dashboard URL becomes `http://<their-mac-tailscale-name>:7777`.
- **Phone bookmark:** standard Safari "Add to Home Screen" — already covered in Step 3 of the onboarding HTML.

## When a new subscriber signs up

### 1. Collect from them (one email)
- GitHub username
- **Gmail address** they'll use for the dashboard
- Preferred firm slug (lowercase, e.g. `acme`, `peakcap`) — optional; bootstrap defaults are fine
- Anthropic-tier preference: subscription (Tier 1, requires Claude Code) or BYO API key (Tier 2a)

### 2. Send them `gdrive_credentials.json` (secure channel) — your only manual step
- The file lives at `~/credentials/gdrive_credentials.json` on your machine
- Send via 1Password, Signal, or encrypted email — **not** plain Gmail
- This is your OAuth client config; treat as sensitive
- They save it to `~/Downloads/`; the installer auto-finds it from there

### 3. Send them the onboarding link
- New firm (no existing private config repo): `https://ygontownik.github.io/Invite-Chief-of-Staff/onboard-new-firm.html`
- Subscriber-with-existing-private-config-repo: `https://ygontownik.github.io/Invite-Chief-of-Staff/onboard.html`
- They run the bootstrap installer (one curl command, no flags), walk through ~15 min
- During Google sign-in: they click **Advanced → Go to TCIP (unsafe)** when they see the unverified-app warning. This is normal and expected — not an error.

### 6. Confirm with them when they're done
- Ask them to send you a screenshot of the dashboard chip showing 🟢 green
- If anything failed during install, the bootstrap script can be safely re-run

## Common subscriber-side issues you'll get pinged on

| Symptom | Cause | Fix |
|---|---|---|
| "This app isn't verified" warning during install | Normal — app is Production-mode + unverified scopes | Tell them to click **Advanced → Go to TCIP (unsafe)** to proceed |
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
