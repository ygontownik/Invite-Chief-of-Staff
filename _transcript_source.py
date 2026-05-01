#!/usr/bin/env python3
"""
_transcript_source.py — Transcript source abstraction for the COS pipeline.

Mirrors _email_provider.py: each recording service (Otter AI, Beside AI,
Fireflies, local folder, etc.) is an implementation of TranscriptSource.
Pipeline scripts call get_transcript_sources() to get a list of configured
sources and iterate over them, regardless of where transcripts come from.

Configuration in firm_context.yaml:

    transcript_sources:
      - type: "google_drive_folder"
        name: "Otter AI"
        folder_ids:
          - "1zJly0cCiqsbZ3umYBXse7nYE7tUpFGOr"   # root (Zapier drops here)
          - "1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S"   # deal calls
          - "1tMEGofeqzfF93YhPCyGe0dgJj8tzdRlF"   # recruiting
          - "1dt-s-D1SWaTrpIEsi0GiBAu1BCQCoPGq"   # other
        root_folder_id: "1zJly0cCiqsbZ3umYBXse7nYE7tUpFGOr"  # files moved here after triage
        category_hint: "auto"   # auto | Recruiting | Deal | Other

      - type: "google_drive_folder"
        name: "Beside AI"
        folder_ids:
          - "YOUR_BESIDE_AI_FOLDER_ID"
        category_hint: "auto"

      - type: "local_folder"
        name: "Desktop Recorder"
        path: "~/recordings"
        category_hint: "auto"

If transcript_sources is omitted, falls back to the legacy hardcoded Otter
folder IDs read from drive-docs.yaml — so existing installations continue
working with no config changes.
"""
from __future__ import annotations

import io
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── PDF text extraction ───────────────────────────────────────────────────────

def _extract_pdf_text(pdf_bytes: bytes, filename: str = "") -> str:
    """Extract plain text from PDF bytes using pypdf (preferred) or PyPDF2 fallback.

    Requires at least one of: pip install pypdf  OR  pip install PyPDF2
    If neither is installed, raises ImportError with installation instructions.
    Returns extracted text joined by newlines; empty pages are skipped.
    """
    try:
        import pypdf  # preferred (actively maintained)
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = [reader.pages[i].extract_text() or "" for i in range(len(reader.pages))]
    except ImportError:
        try:
            import PyPDF2  # fallback (legacy name)
            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            pages = [reader.pages[i].extract_text() or "" for i in range(len(reader.pages))]
        except ImportError:
            raise ImportError(
                f"PDF extraction requires pypdf: pip install pypdf\n"
                f"  (failed to extract text from '{filename}')"
            )

    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text.strip():
        import sys
        print(
            f"  ⚠️   PDF text extraction returned empty for '{filename}' — "
            f"may be a scanned/image-only PDF. Consider OCR.",
            file=sys.stderr,
        )
    return text


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class TranscriptFile:
    """Represents a single transcript file from any source."""
    id: str                          # stable unique ID (Drive file ID, local path hash, etc.)
    name: str                        # display name / filename
    source_name: str                 # name of the source (e.g. "Otter AI", "Beside AI")
    source_type: str                 # "google_drive_folder" | "local_folder"
    category_hint: str = "auto"      # routing hint: "auto" | "Recruiting" | "Deal" | "Other"
    mime_type: str = ""              # Drive MIME type if available
    folder_id: Optional[str] = None  # Drive folder ID this file lives in
    is_root_folder: bool = False     # True if this is the triage/root folder (files get moved after processing)
    root_folder_id: Optional[str] = None  # the root folder to move processed files out of
    local_path: Optional[Path] = None    # for local_folder sources


# ── Abstract base ──────────────────────────────────────────────────────────────

class TranscriptSource(ABC):
    """Abstract base for transcript sources.

    Each implementation knows how to list new transcript files and download
    their content. Processing (memo generation, extraction, Doc writing)
    is handled by the pipeline script and is source-agnostic.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable source name shown in logs."""
        ...

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Machine-readable type identifier."""
        ...

    @abstractmethod
    def list_new(
        self,
        drive_token: Optional[str],
        since: Optional[str],
        processed_ids: set,
        target_ids: Optional[set] = None,
    ) -> list[TranscriptFile]:
        """Return unprocessed transcript files from this source.

        Args:
            drive_token:   Google OAuth token (None for local sources)
            since:         ISO datetime string — only files modified after this
            processed_ids: set of already-processed file IDs (dedup tracker)
            target_ids:    if set, only return files whose ID is in this set (--id flag)
        """
        ...

    @abstractmethod
    def download_text(self, tf: TranscriptFile, drive_token: Optional[str]) -> str:
        """Download and return the transcript text for a given file."""
        ...


# ── Google Drive folder source ─────────────────────────────────────────────────

class GoogleDriveFolderSource(TranscriptSource):
    """Scans one or more Google Drive folders for transcript files.

    Covers: Otter AI, Beside AI, Fireflies (Drive sync), Grain (Drive sync),
    any recording service that deposits files into Google Drive.
    """

    AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".aac", ".opus"}
    SUPPORTED_EXTENSIONS = {".txt", ".md", ".rtf", ".vtt", ".srt", ".pdf"}

    def __init__(self, cfg: dict):
        """cfg is one entry from firm_context.yaml transcript_sources list."""
        self._name = cfg.get("name", "Drive Transcripts")
        self._folder_ids: list[str] = cfg.get("folder_ids", [])
        self._root_folder_id: Optional[str] = cfg.get("root_folder_id")
        self._category_hint: str = cfg.get("category_hint", "auto")

        # Optional: per-folder category hint overrides.
        # Maps folder_id → category_hint. Useful when one source has subfolders
        # by category (e.g. Otter AI root + deal + recruiting + other subfolders).
        # Keys not in this dict fall back to self._category_hint.
        # Accepted as both public config key and private key (legacy fallback path).
        self._folder_hints: dict[str, str] = (
            cfg.get("folder_hints") or cfg.get("_folder_hints") or {}
        )

        # Optional: maps detected category name → destination folder_id.
        # When a file is processed from the root folder, it is moved to the
        # matching subfolder. Omit to skip file moving.
        # Example: {"Tomac Cove": "DEAL_FOLDER_ID", "Recruiting": "REC_FOLDER_ID"}
        self._category_folders: dict[str, str] = cfg.get("category_folders") or {}

        # Validate
        if not self._folder_ids:
            raise ValueError(f"TranscriptSource '{self._name}': folder_ids must be a non-empty list")

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return "google_drive_folder"

    def hint_for_folder(self, folder_id: str) -> str:
        """Return the category hint for a specific folder_id.

        Checks per-folder overrides first; falls back to the source-level hint.
        """
        return self._folder_hints.get(folder_id, self._category_hint)

    def iter_folders(self) -> list[tuple[str, str, bool]]:
        """Return [(folder_id, category_hint, is_root), ...] for all configured folders.

        is_root is True for the triage/root folder — files processed from there
        should be moved to a category subfolder afterwards (if category_folders is set).
        """
        return [
            (fid, self.hint_for_folder(fid), fid == self._root_folder_id)
            for fid in self._folder_ids
        ]

    def category_folder_for(self, category: str, fallback: Optional[str] = None) -> Optional[str]:
        """Return the destination folder_id for a detected category.

        Returns None if no mapping configured (caller should skip file moving).
        """
        return self._category_folders.get(category, fallback)

    def list_new(
        self,
        drive_token: Optional[str],
        since: Optional[str],
        processed_ids: set,
        target_ids: Optional[set] = None,
    ) -> list[TranscriptFile]:
        import urllib.request, urllib.parse, json

        results: list[TranscriptFile] = []
        for folder_id in self._folder_ids:
            is_root = (folder_id == self._root_folder_id)
            hint = self.hint_for_folder(folder_id)
            files = self._list_drive_folder(drive_token, folder_id, since)
            for f in files:
                fid = f["id"]
                fname = f["name"]
                mime = f.get("mimeType", "")

                if target_ids and fid not in target_ids:
                    continue

                ext = Path(fname).suffix.lower()
                if ext in self.AUDIO_EXTENSIONS or "audio" in mime:
                    continue  # skip audio files
                is_pdf = mime == "application/pdf" or ext == ".pdf"
                if (mime
                    and not is_pdf
                    and "google-apps.document" not in mime
                    and "text" not in mime
                    and "plain" not in mime
                    and ext not in self.SUPPORTED_EXTENSIONS
                ):
                    continue  # unsupported format

                if fid in processed_ids and not target_ids:
                    continue  # already processed

                results.append(TranscriptFile(
                    id=fid,
                    name=fname,
                    source_name=self._name,
                    source_type=self.source_type,
                    category_hint=hint,
                    mime_type=mime,
                    folder_id=folder_id,
                    is_root_folder=is_root,
                    root_folder_id=self._root_folder_id,
                ))

        return results

    def download_text(self, tf: TranscriptFile, drive_token: Optional[str]) -> str:
        """Download transcript text from Drive (Google Doc export, text, or PDF).

        Supports:
          - Google Docs: exported as plain text via Drive export API
          - PDF files: downloaded and text extracted via pypdf (if installed)
          - All other text formats: downloaded as raw bytes and decoded
        """
        import urllib.request, urllib.parse, json

        mime = tf.mime_type
        fid = tf.id
        ext = Path(tf.name).suffix.lower()

        if "google-apps.document" in mime:
            # Export as plain text
            url = f"https://www.googleapis.com/drive/v3/files/{fid}/export?mimeType=text%2Fplain"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {drive_token}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                content = r.read()
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return content.decode("latin-1", errors="replace")

        # Download raw bytes (PDF or text file)
        url = f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {drive_token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            content = r.read()

        # PDF: extract text with pypdf
        if mime == "application/pdf" or ext == ".pdf":
            return _extract_pdf_text(content, tf.name)

        # Plain text / other decodable formats
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("latin-1", errors="replace")

    def _list_drive_folder(
        self,
        token: str,
        folder_id: str,
        since: Optional[str] = None,
    ) -> list[dict]:
        """Return list of file metadata dicts for all files in a Drive folder."""
        import urllib.request, urllib.parse, json

        q = f"'{folder_id}' in parents and trashed=false"
        if since:
            q += f" and modifiedTime >= '{since}'"

        params = urllib.parse.urlencode({
            "q": q,
            "fields": "files(id,name,mimeType,modifiedTime,size)",
            "pageSize": 1000,
            "orderBy": "modifiedTime desc",
        })
        url = f"https://www.googleapis.com/drive/v3/files?{params}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return data.get("files", [])


# ── Local folder source ────────────────────────────────────────────────────────

class LocalFolderSource(TranscriptSource):
    """Scans a local directory for transcript text files and PDFs.

    Covers: desktop recorder output, manually saved transcripts,
    research PDFs, any recording workflow that produces local files.
    """

    SUPPORTED_EXTENSIONS = {".txt", ".md", ".vtt", ".srt", ".rtf", ".pdf"}

    def __init__(self, cfg: dict):
        self._name = cfg.get("name", "Local Recordings")
        raw_path = cfg.get("path", "~/recordings")
        self._path = Path(raw_path).expanduser()
        self._category_hint = cfg.get("category_hint", "auto")

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return "local_folder"

    def list_new(
        self,
        drive_token: Optional[str],
        since: Optional[str],
        processed_ids: set,
        target_ids: Optional[set] = None,
    ) -> list[TranscriptFile]:
        if not self._path.exists():
            return []

        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except Exception:
                pass

        results = []
        for fp in self._path.iterdir():
            if fp.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
                continue
            if not fp.is_file():
                continue

            # Use path-based stable ID
            fid = str(fp.resolve())
            if fid in processed_ids and not target_ids:
                continue
            if target_ids and fid not in target_ids:
                continue

            if since_dt:
                mtime = datetime.fromtimestamp(fp.stat().st_mtime)
                # Make both naive for comparison
                since_naive = since_dt.replace(tzinfo=None) if since_dt.tzinfo else since_dt
                if mtime < since_naive:
                    continue

            results.append(TranscriptFile(
                id=fid,
                name=fp.name,
                source_name=self._name,
                source_type=self.source_type,
                category_hint=self._category_hint,
                local_path=fp,
            ))

        return results

    def download_text(self, tf: TranscriptFile, drive_token: Optional[str]) -> str:
        path = tf.local_path or Path(tf.id)
        if path.suffix.lower() == ".pdf":
            return _extract_pdf_text(path.read_bytes(), path.name)
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="latin-1", errors="replace")


# ── Factory ────────────────────────────────────────────────────────────────────

_SOURCE_TYPES = {
    "google_drive_folder": GoogleDriveFolderSource,
    "local_folder": LocalFolderSource,
}


def get_transcript_sources(ctx: dict, docs: Optional[dict] = None) -> list[TranscriptSource]:
    """Return configured transcript sources from firm_context.yaml.

    If transcript_sources is not configured, falls back to the legacy
    Otter AI folder IDs (from drive-docs.yaml or hardcoded defaults)
    so existing installations work with zero config changes.

    Args:
        ctx:  firm context dict from load_firm_context()
        docs: drive docs dict from load_drive_docs() — used for legacy fallback
    """
    raw_sources = ctx.get("transcript_sources")

    # ── Legacy fallback: no transcript_sources in YAML ─────────────────────────
    if not raw_sources:
        return _legacy_otter_sources(ctx, docs or {})

    # ── Config-driven sources ──────────────────────────────────────────────────
    sources = []
    for cfg in raw_sources:
        stype = cfg.get("type", "google_drive_folder")
        cls = _SOURCE_TYPES.get(stype)
        if cls is None:
            import sys
            print(
                f"[transcript_source] WARNING: unknown source type '{stype}' — skipping.",
                file=sys.stderr,
            )
            continue
        try:
            sources.append(cls(cfg))
        except Exception as e:
            import sys
            print(
                f"[transcript_source] WARNING: could not initialize source "
                f"'{cfg.get('name', stype)}': {e}",
                file=sys.stderr,
            )

    return sources


def _legacy_otter_sources(ctx: dict, docs: dict) -> list[TranscriptSource]:
    """Build sources from the legacy hardcoded Otter folder IDs.

    Reads from drive-docs.yaml if folder keys are present, otherwise
    falls back to the original hardcoded IDs. This keeps every existing
    installation working without any config changes.
    """
    # Try drive-docs.yaml keys first
    root       = docs.get("otter_ai")        or "1zJly0cCiqsbZ3umYBXse7nYE7tUpFGOr"
    tomac      = docs.get("otter_tomac")     or "1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S"
    recruiting = docs.get("otter_recruiting") or "1tMEGofeqzfF93YhPCyGe0dgJj8tzdRlF"
    other      = docs.get("otter_other")     or "1dt-s-D1SWaTrpIEsi0GiBAu1BCQCoPGq"
    calls      = docs.get("call_transcripts") or "1jYntgSVBsW5-5rdx18TeZhHRsI9xT74p"

    deal_ws = ctx.get("workstream_categories", {}).get("deal", "Deal")
    recruit_ws = ctx.get("workstream_categories", {}).get("recruiting", "Recruiting")

    otter_source = GoogleDriveFolderSource({
        "name": "Otter AI",
        "folder_ids": [root, tomac, recruiting, other],
        "root_folder_id": root,
        "category_hint": "auto",
        "folder_hints": {
            # Root gets auto-detect; subfolders have explicit category routing
            tomac: deal_ws,
            recruiting: recruit_ws,
            other: "Other",
        },
        "category_folders": {
            # After processing root-folder files, move them to the right subfolder
            deal_ws: tomac,
            recruit_ws: recruiting,
            "Other": other,
        },
    })
    calls_source = GoogleDriveFolderSource({
        "name": "Call Recordings",
        "folder_ids": [calls],
        "category_hint": "auto",
    })

    return [otter_source, calls_source]
