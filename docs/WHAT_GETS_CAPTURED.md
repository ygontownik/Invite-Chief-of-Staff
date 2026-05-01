# What Gets Captured — Where to Save Things

> The pipeline only processes content you explicitly point it at. Nothing in your Google Drive, inbox, or computer is touched unless it matches the sources configured in your `firm_context.yaml`. This document tells you exactly what gets picked up automatically, what you need to save somewhere specific, and what stays out of scope entirely.

---

## Automatically captured (no action needed)

### Email
Your Gmail or Outlook inbox is scanned every 2 hours on weekdays.

- **What's captured:** Every inbound email received since the last run
- **What's skipped:** Emails already processed (tracked in `~/credentials/processed_emails.json`)
- **Where results go:** Follow-ups Doc (action items), Pipeline Doc (deal threads), Recruiting Doc (job search threads)
- **You don't save anything** — the pipeline reads directly from your inbox via OAuth

### Podcasts and blogs
Any RSS feeds listed under `personal.content_feeds` in your `firm_context.yaml` are checked daily.

- **What's captured:** New episodes published since the last run
- **What's skipped:** Episodes already transcribed (tracked in `~/credentials/processed_podcasts.json`)
- **Where results go:** Per-show Google Doc in your transcripts folder + aggregate Podcast Summaries Doc
- **You don't save anything** — the pipeline fetches audio from the RSS feed URL and transcribes it

---

## What you need to save somewhere specific

### Call transcripts (the most important one)

The pipeline scans the Google Drive folders listed in `transcript_sources` in your `firm_context.yaml`. It processes any text file it finds there that hasn't been processed before.

**You need to get your transcript into one of those folders.** How depends on your recording service:

| Service | How to get it into Drive |
|---------|------------------------|
| **Otter AI** | Set up a Zapier automation: *New Otter recording → Upload to Google Drive folder*. Use the folder ID from your `transcript_sources` config. One-time setup, then automatic. |
| **Beside AI** | In Beside AI settings, enable Google Drive sync and point it at your configured folder. |
| **Fireflies** | In Fireflies integrations, connect Google Drive and select your transcript folder. |
| **Grain** | In Grain workspace settings, enable Drive export to your configured folder. |
| **Read AI** | Enable Drive integration in Read AI settings, point at your folder. |
| **Fathom** | In Fathom settings, enable Drive sync. |
| **Zoom / Teams / Meet (desktop recorder)** | If using the built-in `call_recorder.py`, recordings save to `~/recordings` automatically. Configure `local_folder` source pointing there. |
| **Manual transcript** | Copy transcript text into a `.txt` file, save it to any of your configured Drive folders. The filename becomes the call title. |

**What folder IDs to use:**
Look at your `firm_context.yaml` → `transcript_sources`. The `folder_ids` listed there are exactly what gets scanned. These are Google Drive folder IDs — you can find them in the Drive URL when you open the folder: `drive.google.com/drive/folders/THIS_IS_THE_FOLDER_ID`.

**Supported file formats:** `.txt`, `.vtt`, `.srt`, `.md`, `.rtf`, Google Docs
**Ignored automatically:** audio files (`.m4a`, `.mp3`, `.wav`), PDFs, spreadsheets, images

---

## Where to find your folder IDs

Your configured transcript folders are in `firm_context.yaml`:

```yaml
transcript_sources:
  - type: "google_drive_folder"
    name: "Otter AI"
    folder_ids:
      - "1zJly0cCiqsbZ3umYBXse7nYE7tUpFGOr"   ← this is the folder ID
      - "1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S"
```

To open a folder in Drive from its ID:
```
https://drive.google.com/drive/folders/PASTE_FOLDER_ID_HERE
```

To find a folder ID from an existing Drive folder: open it in Drive, copy the last segment of the URL.

---

## Adding a new transcript source

If you start using a new recording service, add a new entry to `transcript_sources` in your `firm_context.yaml`:

```yaml
transcript_sources:
  - type: "google_drive_folder"
    name: "Beside AI"                         # shown in logs and memo headers
    folder_ids:
      - "YOUR_BESIDE_AI_FOLDER_ID"            # from your Beside AI Drive sync settings
    category_hint: "auto"                     # or "Recruiting" / "Deal" / "Other"
```

Then configure the recording service to sync to that folder. No code changes needed.

---

## What does NOT get captured

The following are explicitly out of scope — the pipeline has no access to them and never will:

| Content | Why it's excluded |
|---------|------------------|
| Google Drive files outside your configured folders | Drive API only queries specific folder IDs you listed |
| Google Docs you didn't create or explicitly share | OAuth scope limits access to designated files only |
| PDFs (research reports, pitch decks, etc.) | Not yet supported — text extraction from PDFs is a planned future feature |
| Slack / Teams messages | No integration built |
| Calendar event content / meeting notes | Calendar is read for scheduling context only, not content |
| Files on your Desktop not in a configured folder | Not scanned unless you add a `local_folder` source pointing there |
| Other people's emails | Pipeline reads your inbox only, authenticated with your credentials |

---

## Quick reference — "Where do I put X?"

| You have... | Save it here |
|-------------|-------------|
| A call transcript from Otter AI | Configure Zapier → auto-deposits to your Otter folder |
| A call transcript from Beside AI | Configure Drive sync in Beside settings |
| A transcript you copied manually | Paste into a `.txt` file → save to any configured Drive folder |
| A recording from your desktop | `call_recorder.py` saves to `~/recordings` automatically (if configured as a `local_folder` source) |
| A podcast episode you want summarized | Add the RSS feed to `personal.content_feeds.podcasts` in your `firm_context.yaml` |
| A blog or newsletter you want in your briefing | Add the RSS URL to `personal.content_feeds.blogs` |
| A research PDF you want processed | Not yet supported — planned for a future release |
| An email thread | Nothing to do — your inbox is scanned automatically every 2 hours |

---

## How the pipeline decides what to process

For each source configured in `transcript_sources`, the pipeline:

1. **Lists files** in each `folder_id` — only files modified since the last run (or all files on `--backfill`)
2. **Checks dedup** — skips files already in `~/credentials/processed_cos_transcripts.json`
3. **Filters format** — skips audio files, unsupported formats
4. **Processes** — Pass 1 (Sonnet memo) → Pass 2 (Opus extraction) → writes to shared Docs
5. **Marks done** — adds file ID to dedup tracker so it won't be processed again

To re-process a file that was already processed: `python3 cos_otter_backfill.py --force --id FILE_ID`

To check what would be processed without actually processing: `python3 cos_otter_backfill.py --list`
