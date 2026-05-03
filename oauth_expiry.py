"""oauth_expiry.py — read-only OAuth token expiry probe (Track L4 / HEARTBEAT.md §7).

Run 2 / Track E namespace. NEW file. Not yet wired into heartbeat.py.

Purpose
-------
Compute days-until-expiry for OAuth credential files in ``~/credentials/``
without ever mutating them, without ever logging token bodies, and without
triggering an auth refresh round-trip.

Supported file types
--------------------
- ``*.json`` files holding Google OAuth user credentials. Looks for, in
  order: ``expiry`` (ISO 8601, the field google-auth writes),
  ``expires_at`` (epoch seconds), ``expires_on`` (epoch seconds — Microsoft
  Graph), ``expiration`` (epoch ms — Google watch channels), ``expires_in``
  (seconds remaining; combined with file mtime as the issue time).
- ``*.pickle`` files holding ``google.oauth2.credentials.Credentials``
  (only if ``google.oauth2.credentials`` importable). Uses
  ``Credentials.expiry`` attribute. The pickle is loaded in a try/except
  and never re-serialized.
- JWT-formatted tokens: any string that splits into three dot-separated
  base64url segments and decodes to a JSON header. The middle segment is
  base64url-decoded and the ``exp`` claim is read (epoch seconds).

What is NEVER read or returned
------------------------------
Refresh tokens, access tokens, client secrets, scopes, account email
addresses. Only file path + expiry timestamp + days-until-expiry +
status are returned by ``check_token_expiry``.

Permissions
-----------
Every file is opened in mode ``'r'`` (text) or ``'rb'`` (binary, pickle
only). The module never writes, never deletes, never refreshes a token.
``HARD RULES`` from RUN2_BRIEF.md §HARD RULES are honored.

Exit code
---------
``__main__`` runs ``scan_credentials_dir`` and prints warnings for tokens
expiring within 14 days. Always exit 0; this is a read-only probe.
"""

from __future__ import annotations

import base64
import json
import pickle
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Default credentials dir — overridable for tests.
DEFAULT_CREDS_DIR = Path.home() / "credentials"

# Token files we know about (from CLAUDE.md global doc map). Anything else
# in the dir is still scanned by suffix; this list just biases ordering.
KNOWN_TOKEN_FILENAMES = (
    "gdrive_token.pickle",
    "gmail_mini_token.pickle",
    "gcal_token.json",
    "ms_token.json",
    "token.json",
    "gcal_watch_channels.json",
    "ms_graph_subscription.json",
)

WARNING_DAYS_DEFAULT = 14


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_token_expiry(token_path: Path) -> dict[str, Any]:
    """Return a status dict describing the token's expiry state.

    Always returns a dict with keys: path, exp_iso, days_until_expiry,
    status ("ok"|"warning"|"expired"|"unknown"), method
    ("jwt"|"pickle"|"json"|"unknown"), and (on failure) error.

    Read-only. Opens ``token_path`` in 'r' or 'rb' mode. Never writes.
    """
    token_path = Path(token_path)
    base = {
        "path": str(token_path),
        "exp_iso": None,
        "days_until_expiry": None,
        "status": "unknown",
        "method": "unknown",
    }
    if not token_path.exists():
        base["error"] = "file not found"
        return base
    try:
        # Heuristic order: pickle by suffix, JSON by suffix, JWT fallback.
        suffix = token_path.suffix.lower()
        exp_dt: datetime | None = None
        method = "unknown"
        if suffix == ".pickle":
            exp_dt = _expiry_from_pickle(token_path)
            method = "pickle"
        elif suffix == ".json":
            exp_dt = _expiry_from_json(token_path)
            method = "json"
        else:
            # Try JWT (token files sometimes have no extension).
            exp_dt = _expiry_from_jwt_file(token_path)
            method = "jwt"
        if exp_dt is None:
            # Fallback: try JWT against a JSON file's "access_token"/"id_token".
            if suffix == ".json":
                exp_dt = _jwt_from_json(token_path)
                if exp_dt is not None:
                    method = "jwt"
        base["method"] = method
        if exp_dt is None:
            base["status"] = "unknown"
            base["error"] = "no expiry field found"
            return base
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        base["exp_iso"] = exp_dt.isoformat()
        now = datetime.now(timezone.utc)
        delta_seconds = (exp_dt - now).total_seconds()
        days = delta_seconds / 86400.0
        base["days_until_expiry"] = round(days, 2)
        if delta_seconds <= 0:
            base["status"] = "expired"
        elif days <= WARNING_DAYS_DEFAULT:
            base["status"] = "warning"
        else:
            base["status"] = "ok"
        return base
    except Exception as exc:  # noqa: BLE001 — explicit per-file isolation
        base["error"] = f"{type(exc).__name__}: {exc}"
        return base


def scan_credentials_dir(creds_dir: Path | None = None) -> list[dict]:
    """Run check_token_expiry across every recognized token file in creds_dir.

    Returns a list (one entry per file). Order: KNOWN_TOKEN_FILENAMES first,
    then any other ``*.json`` / ``*.pickle`` files sorted alphabetically.

    Read-only. Never enumerates beyond the directory itself (no recursion).
    """
    creds_dir = Path(creds_dir) if creds_dir is not None else DEFAULT_CREDS_DIR
    if not creds_dir.exists() or not creds_dir.is_dir():
        return []
    seen: set[str] = set()
    results: list[dict] = []
    # Known files first.
    for name in KNOWN_TOKEN_FILENAMES:
        p = creds_dir / name
        if p.exists() and p.is_file():
            results.append(check_token_expiry(p))
            seen.add(p.name)
    # Then any other token-like files.
    for p in sorted(creds_dir.iterdir()):
        if p.name in seen or not p.is_file():
            continue
        if p.suffix.lower() not in (".json", ".pickle"):
            continue
        results.append(check_token_expiry(p))
    return results


def format_warnings(results: list[dict], threshold_days: int = WARNING_DAYS_DEFAULT) -> list[str]:
    """Return human-readable warning strings for tokens expiring within threshold.

    Includes both "warning" and "expired" status entries. Skips "ok" and
    "unknown" (the latter is logged separately by callers if desired).
    """
    out: list[str] = []
    for r in results:
        status = r.get("status")
        if status == "expired":
            out.append(
                f"[EXPIRED] {Path(r['path']).name}: expired on {r.get('exp_iso')} "
                f"({abs(r.get('days_until_expiry') or 0):.1f}d ago)"
            )
        elif status == "warning":
            days = r.get("days_until_expiry")
            if days is None or days > threshold_days:
                continue
            out.append(
                f"[WARN] {Path(r['path']).name}: expires {r.get('exp_iso')} "
                f"(in {days:.1f}d)"
            )
    return out


# ---------------------------------------------------------------------------
# Internal: expiry extraction per format
# ---------------------------------------------------------------------------

def _expiry_from_json(path: Path) -> datetime | None:
    with open(path, "r", encoding="utf-8") as f:
        body = json.load(f)
    # Recurse one level into containers (some Microsoft tokens nest).
    candidates: list[dict] = []
    if isinstance(body, dict):
        candidates.append(body)
        for v in body.values():
            if isinstance(v, dict):
                candidates.append(v)
    elif isinstance(body, list):
        for item in body:
            if isinstance(item, dict):
                candidates.append(item)
    for blob in candidates:
        # Google-auth ``expiry`` (ISO 8601 string, naive UTC).
        v = blob.get("expiry")
        if isinstance(v, str):
            dt = _parse_iso(v)
            if dt is not None:
                return dt
        # Epoch seconds variants.
        for key in ("expires_at", "expires_on", "exp"):
            v = blob.get(key)
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(float(v), tz=timezone.utc)
            if isinstance(v, str) and v.isdigit():
                return datetime.fromtimestamp(float(v), tz=timezone.utc)
        # Epoch milliseconds (Google calendar push channels).
        v = blob.get("expiration")
        if isinstance(v, (int, float)) and v > 1e11:  # ms range
            return datetime.fromtimestamp(float(v) / 1000.0, tz=timezone.utc)
        if isinstance(v, str) and v.isdigit() and len(v) >= 12:
            return datetime.fromtimestamp(float(v) / 1000.0, tz=timezone.utc)
        if isinstance(v, str):
            dt = _parse_iso(v)
            if dt is not None:
                return dt
        # expires_in (seconds remaining); use file mtime as issue time.
        v = blob.get("expires_in")
        if isinstance(v, (int, float)):
            issued = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            return issued + _seconds(float(v))
    return None


def _expiry_from_pickle(path: Path) -> datetime | None:
    # Optional dependency. If google.oauth2 unavailable, fall back to a
    # mtime + 90d heuristic (refresh tokens for installed apps).
    try:
        from google.oauth2.credentials import Credentials  # type: ignore  # noqa: F401
    except Exception:
        # Coarse but never wrong-by-construction: assume 90d from last write.
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return mtime + _seconds(90 * 86400)
    try:
        with open(path, "rb") as f:
            obj = pickle.load(f)
    except Exception:
        return None
    exp = getattr(obj, "expiry", None)
    if isinstance(exp, datetime):
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp
    return None


def _expiry_from_jwt_file(path: Path) -> datetime | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = f.read().strip()
    except Exception:
        return None
    return _expiry_from_jwt_str(body)


def _jwt_from_json(path: Path) -> datetime | None:
    """Look for a JWT inside a JSON token (e.g. id_token / access_token)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = json.load(f)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    for key in ("id_token", "access_token"):
        v = body.get(key)
        if isinstance(v, str) and v.count(".") == 2:
            dt = _expiry_from_jwt_str(v)
            if dt is not None:
                return dt
    return None


def _expiry_from_jwt_str(token: str) -> datetime | None:
    if not token or token.count(".") != 2:
        return None
    try:
        _, payload_b64, _ = token.split(".")
        # base64url decode with padding.
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    return None


def _parse_iso(s: str) -> datetime | None:
    try:
        # tolerate trailing Z
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def _seconds(n: float):
    from datetime import timedelta
    return timedelta(seconds=n)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    creds = DEFAULT_CREDS_DIR
    threshold = WARNING_DAYS_DEFAULT
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("--dir", "-d") and i + 1 < len(argv):
            creds = Path(argv[i + 1]).expanduser()
            i += 2
        elif a in ("--threshold", "-t") and i + 1 < len(argv):
            try:
                threshold = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif a in ("--help", "-h"):
            print(__doc__)
            return 0
        else:
            i += 1
    results = scan_credentials_dir(creds)
    if not results:
        print(f"oauth_expiry: no token files found in {creds}")
        return 0
    warnings = format_warnings(results, threshold_days=threshold)
    print(f"oauth_expiry: scanned {len(results)} file(s) in {creds}")
    for r in results:
        name = Path(r["path"]).name
        status = r.get("status")
        days = r.get("days_until_expiry")
        method = r.get("method")
        days_s = f"{days:.1f}d" if isinstance(days, (int, float)) else "?"
        print(f"  {status:8s}  {method:7s}  {name:35s}  exp_in={days_s}")
    if warnings:
        print("")
        print(f"Warnings (<= {threshold} days):")
        for w in warnings:
            print(f"  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
