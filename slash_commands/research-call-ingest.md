---
description: Ingest a third-party research provider's call/webinar REPLAY (Capstone, etc.) as a per-provider podcast-style show. Resolves the replay link in authenticated Chrome, extracts the audio, transcribes → memo → publishes to the provider's show doc + Podcast Summaries.
argument-hint: "<provider> [url <replay_url> | days_back]"
---

# /research-call-ingest — per-provider research-call replay ingest

Third-party research/advisory firms (Capstone, etc.) host webinars and expert calls
with **no dial-in** — just a web link, often on a platform the auto-recorder doesn't
recognize. This skill ingests their **on-demand replay** the same way `/gs-podcast-ingest`
handles GS reports: resolve the replay in your authenticated Chrome, pull the audio,
then transcribe → seven-section memo → publish to a **per-provider show**
(e.g. "Capstone") that rolls into Podcast Summaries.

Companion to [`/gs-podcast-ingest`](gs-podcast-ingest.md). Both wrap the deterministic
[`research_audio_ingest.py`](../research_audio_ingest.py) (`--show <provider>`).
See [[project_gs_marquee_audio_ingest]].

State: `~/credentials/processed_podcasts.json` (guid `<provider-slug>-<event-id>`)

## PROVIDER ALLOWLIST

| Provider | Show name | Invite/replay sender | Platform | Notes |
|----------|-----------|----------------------|----------|-------|
| Capstone | `Capstone` | `events@capstonedc.com`, `*@capstonedc.com` | Cvent **Attendee Hub** | "Join Event"/"watch the recording" links; on-demand replay after live webinar |

To add a provider: append a row (sender domain + show name) and, if needed, a
platform note for media extraction. Nothing else to wire — the show doc and
Podcast Summaries rollup are auto-created on first ingest.

---

## STEP 0 — Parse arguments + connect Chrome

`$ARGUMENTS` = `<provider> [url <replay_url> | <days_back>]`.
- First token = provider name (must match a PROVIDER ALLOWLIST row, case-insensitive).
- `url <replay_url>` → ingest that single replay (skip Gmail discovery).
- otherwise → treat the rest as `days_back` (default 30) for Gmail discovery.

Connect to the authenticated Chrome (the provider's session/registration lives there):
```
mcp__Claude_in_Chrome__list_connected_browsers   → local deviceId
mcp__Claude_in_Chrome__select_browser({ deviceId })
mcp__Claude_in_Chrome__tabs_context_mcp({ createIfEmpty: true })   → tabId
```
No local browser → STOP, ask Yoni to open Chrome with the extension. Never automate
a provider login / defeat SSO.

---

## STEP 1 — Discover replay links from the provider's emails (Gmail mode)

Skip if a `url` was given. Otherwise use the Gmail MCP with the provider's sender:
```
mcp__ddf7cc1d-c583-439f-8736-5877360cf818__search_threads({
  query: 'from:<provider sender domain> (recording OR replay OR "on-demand" OR "watch" OR webinar OR "join event") newer_than:<days_back>d',
  pageSize: 20 })
```
Open the relevant thread(s) with `get_thread` (FULL_CONTENT; if body too large it is
saved to a file — grep that file for the join/replay link). Build a candidate list of
`{title, date, replay_url}`. The title comes from the email subject (strip the
"Capstone Event:" prefix); the date from the subject/body (the live event date).

Capstone specifics: the link is a Cvent **Attendee Hub** "Join Event" URL. The same
hub serves the on-demand replay after the live session — open it and look for a
recording/playback control.

---

## STEP 2 — Dedup against already-ingested episodes

```bash
zsh -ic 'python3 ~/cos-pipeline/research_audio_ingest.py --list-processed --show "<provider>"'
```
guid = `<provider-slug>-<event-id>` (slug = lowercased provider; event-id = a stable
id from the replay URL, e.g. the Cvent event/session id, else a slug of the title+date).
Drop candidates whose guid already appears (ingest is idempotent as a backstop).

---

## STEP 3 — Resolve the replay + extract the audio URL

For each new candidate, in the Chrome tab:
```
mcp__Claude_in_Chrome__navigate({ tabId, url: <replay_url> })
```
Wait for the player to render, start playback if needed (click the play control), then
read the media source:
```
mcp__Claude_in_Chrome__javascript_tool({ tabId, action: "javascript_exec", text:
  "JSON.stringify({ url: location.href, title: document.title," +
  " media: [...document.querySelectorAll('audio,video,source')].map(e=>e.currentSrc||e.src).filter(Boolean) })" })
```
(REPL note: top-level expression only; no `await` inside `if`/blocks. If the `<audio>/<video>`
src is empty, also check the Network tab via `mcp__Claude_in_Chrome__read_network_requests`
for a media URL — `.mp4`, `.m3u8`, `.mp3`, or a CDN media response.)

- **Direct media URL** (`.mp4`/`.mp3`, public/CDN) → that is the `audio_url`; AssemblyAI
  fetches it directly. Proceed to Step 4.
- **HLS manifest** (`.m3u8`) → AssemblyAI can't fetch a manifest. Mux to a file first:
  ```bash
  ffmpeg -i "<m3u8_url>" -c copy ~/Downloads/<slug>.mp4
  ```
  then ingest the **local file path** as `--url` is URL-only — instead transcribe the
  local file via the call_recorder engine: `python3 call_recorder.py transcribe --file ~/Downloads/<slug>.mp4`
  is NOT wired to provider shows, so prefer a direct media URL when available and log
  an HLS case for follow-up rather than guessing.
- **No media / login wall** → STOP for that item, log "replay not accessible — Yoni may
  need to open it once while logged in," continue to the next.

---

## STEP 4 — Ingest each (transcribe → memo → publish)

Per item (a failure must not abort the others):
```bash
zsh -ic 'python3 ~/cos-pipeline/research_audio_ingest.py --ingest --show "<provider>" \
  --url "<audio_url>" \
  --title "<title>" \
  --date "<YYYY-MM-DD>" \
  --guid "<provider-slug>-<event-id>"'
```
Runs `process_episode` under the provider's show: AssemblyAI transcription, the
seven-section investor memo, routing-v2 intel, writes to the provider show doc +
Podcast Summaries. Idempotent.

---

## STEP 5 — Rebuild TOCs once, after all ingests

```bash
zsh -ic 'python3 ~/cos-pipeline/research_audio_ingest.py --rebuild-tocs --show "<provider>"'
```

---

## STEP 6 — Tab cleanup + summary

```
mcp__Claude_in_Chrome__navigate({ tabId, url: "about:blank" })
```
```
RESEARCH-CALL INGEST COMPLETE — <provider>
==========================================
Ingested: <K> new replay(s) → <provider>
  • <date> — <title>
Skipped: <S> (already ingested) | <T> (no accessible audio)
Show doc: https://docs.google.com/document/d/<id>/edit
```

---

## RULES (non-negotiable)

- **Never automate a provider login / defeat SSO.** Login wall → STOP, ask Yoni.
- **Per-provider show.** Always pass `--show "<provider>"`; never publish a third-party
  call under "Goldman Sachs Research" or an RSS show.
- **Dedup by guid.** Re-running must be idempotent.
- **Audio check mandatory.** Only ingest items with an accessible media source.
- **Per-item failures don't abort the run.** Log + continue.
- **Rebuild TOCs once at the end.**
- **Run python via `zsh -ic`**; ignore the "Remote Control requires full-scope token"
  stderr line (shell-init noise).

---

## ERROR HANDLING

- Unknown provider (not in allowlist) → STOP, ask Yoni to add a row or correct the name.
- No Chrome / login wall → stop that item cleanly, ask Yoni.
- HLS-only media → log for follow-up (direct-URL path is the validated one).
- AssemblyAI / memo failure → `process_episode` returns False, item logged FAIL and not
  marked processed (retries next run); continue.

---

## STATUS NOTE (2026-06-11)

The direct-media-URL path is the validated one (same mechanism proven for GS reports).
The **Cvent Attendee Hub** replay-media specifics (direct mp4 vs HLS, and whether the
replay needs Yoni logged in) are **unvalidated** until the first real Capstone replay is
run through this skill — pin the extraction details then.
