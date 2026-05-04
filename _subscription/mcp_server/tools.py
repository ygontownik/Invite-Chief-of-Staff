"""Tool implementations for the subscription MCP prototype.

Functions are plain Python so the test suite can drive them directly
without standing up an MCP transport. Each function returns a JSON-
serializable dict.

Read-only by construction:
  - deal_pipeline.lookup reads a JSON file
  - transcripts.search uses Drive API with drive.metadata.readonly
  - lng_market.get_spot is a deliberate stub
"""
import json
import os
import pathlib
import pickle
from datetime import datetime, timedelta, timezone
from typing import Any

# Spec path. Override via env for tests; production uses the literal location
# named in the build prompt.
_DEFAULT_DEAL_DATA = pathlib.Path.home() / "cos-pipeline" / "deal-pipeline-data.json"
_TRANSCRIPTS_FOLDER_ID = "1B7UgpFCElgyZMLbq1yrf-N7PsB-UA4SE"
_GDRIVE_TOKEN = pathlib.Path.home() / "credentials" / "gdrive_token.pickle"


def deal_pipeline_lookup(target_name: str) -> dict[str, Any]:
    """Look up a deal/theme by name in deal-pipeline-data.json.

    Returns a dict with either a `match` (best fuzzy result) or `error` key.
    Missing data file → {"error": "data source not configured"} per spec.
    """
    path = pathlib.Path(os.environ.get("DEAL_PIPELINE_DATA_PATH", str(_DEFAULT_DEAL_DATA)))
    if not path.exists():
        return {"error": "data source not configured", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"failed to read data file: {e.__class__.__name__}"}

    needle = (target_name or "").strip().lower()
    if not needle:
        return {"error": "target_name is required"}

    themes = data.get("themes", []) if isinstance(data, dict) else []
    matches = []
    for theme in themes:
        haystack_parts = [
            str(theme.get("id", "")),
            str(theme.get("theme", "")),
            str(theme.get("thesis", "")),
        ]
        haystack = " | ".join(haystack_parts).lower()
        if needle in haystack:
            matches.append({
                "id": theme.get("id"),
                "theme": theme.get("theme"),
                "thesis": (theme.get("thesis", "") or "")[:300],
            })

    if not matches:
        return {"target_name": target_name, "matches": [], "count": 0}
    return {"target_name": target_name, "matches": matches[:5], "count": len(matches)}


def transcripts_search(query: str, since_days: int = 30) -> dict[str, Any]:
    """Drive search over the Transcripts folder. Returns up to 10 hits.

    Uses ONLY drive.metadata.readonly scope. No file content download.
    """
    if not _GDRIVE_TOKEN.exists():
        return {"error": "gdrive_token.pickle not found", "path": str(_GDRIVE_TOKEN)}

    try:
        from googleapiclient.discovery import build
    except ImportError:
        return {"error": "google-api-python-client not installed"}

    try:
        with _GDRIVE_TOKEN.open("rb") as f:
            creds = pickle.load(f)
    except (pickle.PickleError, OSError) as e:
        return {"error": f"failed to load credentials: {e.__class__.__name__}"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, since_days))
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    safe_query = (query or "").replace("'", "\\'")
    q_parts = [
        f"'{_TRANSCRIPTS_FOLDER_ID}' in parents",
        "trashed = false",
        f"modifiedTime > '{cutoff_iso}'",
    ]
    if safe_query:
        q_parts.append(f"fullText contains '{safe_query}'")
    q = " and ".join(q_parts)

    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        result = service.files().list(
            q=q,
            pageSize=10,
            fields="files(id,name,modifiedTime)",
            supportsAllDrives=False,
        ).execute()
    except Exception as e:
        return {"error": f"drive api call failed: {e.__class__.__name__}: {e}"}

    files = result.get("files", [])
    hits = [
        {"title": f.get("name"), "doc_id": f.get("id"), "modified": f.get("modifiedTime")}
        for f in files
    ]
    return {"query": query, "since_days": since_days, "count": len(hits), "hits": hits}


def lng_market_get_spot(region: str, date: str) -> dict[str, Any]:
    """STUB. Returns the configured error so the surface is wireable today.

    Replace when the LNG spreadsheet ingestion lands.
    """
    return {
        "error": "lng spreadsheet not yet wired",
        "region": region,
        "date": date,
    }
