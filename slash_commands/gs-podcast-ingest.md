---
description: Scan the GS Marquee "Podcasts" alert emails, resolve each report in authenticated Chrome, and ingest any audio/video ones as "Goldman Sachs Research" podcast episodes (transcribe → memo → show doc + Podcast Summaries). Dedupes automatically.
argument-hint: "[days_back | url <report_url>]"
---

# /gs-podcast-ingest — Gmail-triggered Goldman Sachs podcast ingest

Goldman's Marquee research audio (Research Unplugged, On The Road, etc.) has no
public RSS feed and the Marquee search UI is behind GS SSO — so this routine
discovers new episodes from the **"Podcasts" saved-search alert emails** Yoni
already receives, then ingests each via the same pipeline every other podcast
uses. All GS audio publishes to ONE unified show: **"Goldman Sachs Research"**.

Pipeline split (deliberate):
- **Discovery + audio extraction** (this skill, in-session): Gmail MCP finds the
  alert emails; Chrome MCP resolves each report's SSO `trustedLink` and reads the
  audio URL off the page. These steps need an authenticated browser and adapt to
  email/page layout, so they live here, not in a headless script.
- **Transcribe → memo → publish** (deterministic, tested):
  [`gs_podcast_ingest.py`](../gs_podcast_ingest.py) wraps
  `podcast_transcribe.process_episode` — the canonical flow.

The audio is a **public CloudFront MP3** (no auth) once you can see the page, so
AssemblyAI fetches it directly. See [[project_gs_marquee_audio_ingest]].

State: `~/credentials/processed_podcasts.json` (guid `gs-marquee-<report-uuid>`)

---

## STEP 0 — Connect to the authenticated Chrome

This skill needs Yoni's logged-in Chrome (the GS Marquee session lives there).

```
mcp__Claude_in_Chrome__list_connected_browsers      → pick the local browser deviceId
mcp__Claude_in_Chrome__select_browser({ deviceId })
mcp__Claude_in_Chrome__tabs_context_mcp({ createIfEmpty: true })   → get a tabId
```

If no local browser is connected: STOP and tell Yoni "no Chrome connected — open
Chrome with the Claude extension and re-run /gs-podcast-ingest." Do NOT attempt to
log in to GS yourself (entering GS credentials / defeating SSO is off-limits).

If `$ARGUMENTS` starts with `url ` → skip Gmail discovery (Step 1) and use the
single report URL given. Otherwise treat `$ARGUMENTS` as `days_back` (default 14).

---

## STEP 1 — Discover candidate reports from the "Podcasts" alert emails

Use the Gmail MCP. The alerts come from `gs-portal-emails@alerts.publishing.gs.com`,
subject `Goldman Sachs Summary Alert for <alert name>`.

```
mcp__ddf7cc1d-c583-439f-8736-5877360cf818__search_threads({
  query: 'from:gs-portal-emails@alerts.publishing.gs.com newer_than:<days_back>d',
  pageSize: 30 })
```

Identify the thread(s) for the **Podcasts** alert (subject contains the alert name
Yoni gave it — "Podcasts"/"Podcast"/"Audio"; it is the alert filtered to Audio/Video
+ Equity Research + Data centers, NOT the morning calls). If you cannot tell which
alert is the podcast one from subjects alone, open the most likely thread and check
its body — the podcast alert lists audio/video reports.

For each matching thread:
```
mcp__ddf7cc1d-c583-439f-8736-5877360cf818__get_thread({ threadId, messageFormat: "FULL_CONTENT" })
```
(The body is large HTML — if it exceeds the token limit it is saved to a file path;
`grep` that file for `marquee.gs.com` / `trustedLink` links and report titles.)

Build a candidate list: for each report entry, capture its **title** and its
**report link** (a `https://marquee.gs.com/research/trustedLink?opentoken=...&id=/content/research/...`
deep link, or a direct `/content/research/.../<uuid>.html`).

---

## STEP 2 — Dedup against already-ingested episodes

```bash
zsh -ic 'python3 ~/cos-pipeline/gs_podcast_ingest.py --list-processed'
```

The guid for any report is `gs-marquee-<uuid>` where `<uuid>` is the last path
segment of the report URL (`/content/research/.../<uuid>.html`). Drop any candidate
whose guid already appears. (Ingest is also idempotent as a backstop.)

---

## STEP 3 — Per new candidate: resolve + extract the audio URL

For each remaining candidate, in the Chrome tab from Step 0:

```
mcp__Claude_in_Chrome__navigate({ tabId, url: <report_or_trustedLink_url> })
```

Wait ~2s for the SPA to render, then read the media source + canonical URL:

```
mcp__Claude_in_Chrome__javascript_tool({ tabId, action: "javascript_exec", text:
  "JSON.stringify({" +
  " url: location.href," +
  " title: document.title," +
  " media: [...document.querySelectorAll('audio,video,source')].map(e=>e.currentSrc||e.src).filter(Boolean)" +
  "})" })
```

(REPL note: top-level expression only; no `await` inside `if`/blocks.)

- **`media` is empty** → text-only report, NOT a podcast → skip it (log "no audio").
- **`media` has a CloudFront URL** → that is the audio. Derive:
  - `audio_url` = the media URL
  - `uuid`      = last `/content/research/.../<uuid>.html` segment of `location.href`
  - `guid`      = `gs-marquee-<uuid>`
  - `date`      = from the URL path `/reports/YYYY/MM/DD/` → `YYYY-MM-DD`
  - `title`     = `document.title`

---

## STEP 4 — Ingest each (transcribe → memo → publish)

For each audio candidate (per-item; a failure here must not abort the others):

```bash
zsh -ic 'python3 ~/cos-pipeline/gs_podcast_ingest.py --ingest \
  --url "<audio_url>" \
  --title "<title>" \
  --date "<YYYY-MM-DD>" \
  --guid "gs-marquee-<uuid>"'
```

This runs `process_episode` under **"Goldman Sachs Research"**: AssemblyAI
transcription (speaker labels), the seven-section investor memo, routing-v2 intel
emission, and writes to the show doc + Podcast Summaries. It is idempotent.

---

## STEP 5 — Rebuild TOCs once, after all ingests

```bash
zsh -ic 'python3 ~/cos-pipeline/gs_podcast_ingest.py --rebuild-tocs'
```

---

## STEP 6 — Tab cleanup + summary

Leave Chrome clean (it is Yoni's daily browser):
```
mcp__Claude_in_Chrome__navigate({ tabId, url: "about:blank" })
```

Print:
```
GS PODCAST INGEST COMPLETE
==========================
Scanned: <N> alert email(s), <M> candidate report(s)
Ingested: <K> new audio episode(s) → Goldman Sachs Research
  • <date> — <title>
Skipped: <S> (already ingested) | <T> (no audio / text-only)
Show doc: https://docs.google.com/document/d/<id>/edit
```

---

## RULES (non-negotiable)

- **Never automate GS login.** If a page bounces to `idfs.gs.com`/SSO, the session
  isn't authenticated — STOP and ask Yoni to log in. Do not enter credentials or
  defeat the login.
- **Dedup by `gs-marquee-<uuid>`.** Re-running must be idempotent.
- **Audio check is mandatory.** Only ingest reports that actually expose an
  `<audio>`/`<video>` source. Text-only reports are skipped — this is a *podcast*
  routine.
- **Per-report failures don't abort the run.** Log + continue to the next.
- **Rebuild TOCs once at the end**, not per episode.
- **Run python via `zsh -ic`** so API keys load from ~/.zshrc. Ignore the
  "Remote Control requires full-scope token" stderr line — it's shell-init noise.

---

## ERROR HANDLING

- No Chrome connected → Step 0 stops cleanly, ask Yoni to open Chrome.
- Page bounces to SSO → not authenticated → stop, ask Yoni to log in.
- `get_thread` body too large → it's saved to a file; grep the file for links.
- AssemblyAI / memo failure on one episode → `process_episode` returns False, that
  episode is logged as FAIL and not marked processed (retries next run); continue.

---

## CADENCE NOTE

This is invoke-on-demand (needs authenticated Chrome, so not a headless cron). Run
it after the daily "Podcasts" alert lands (the alert emails ~once/day when there are
matches). If you later want it fired on a schedule, it can hang off the same
`dash-state-hook` cadence as `/artifact-pull` — but only when Chrome is reachable.
