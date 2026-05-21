#!/opt/homebrew/bin/python3
"""
local_file_organizer.py — Local-filesystem router for ~/Downloads, ~/Desktop, ~/Documents
==========================================================================================
Mirrors the Drive Organizer design pattern, but for the local Mac filesystem.

Goal: no loose files at the top of ~/Downloads, ~/Desktop, ~/Documents after 24h.
Files are routed into one of these subfolders (auto-created):

  _Routed/<deal_slug>/    matches a deal alias_regex in drive-docs.yaml
  _Junk/                  known junk patterns (DMG installers, .crdownload, screenshots-of-screenshots, ...)
  _Personal/YYYY-MM/      matches personal keywords (tax, insurance, mortgage, kids, school, doctor, vet, costco, amazon-order)
  _Unsorted/YYYY-MM/      fallback: >24h old at folder top and no other rule matched
  _Archive/YYYY-Q?/       roll-off: _Unsorted entries older than 90 days land here

Routing precedence (per LF2): state-check → junk → deal-alias → personal-keyword → unsorted

State: ~/credentials/local_organizer_state.json — sha256(path|mtime) → idempotent.
Log:   ~/dashboards/logs/local-organizer.log — one-line summary per run.

CLI:
  --apply      actually move files (default is dry-run)
  --folder P   only process this folder (default: all three)
  --force      re-route files already in state
  --list       show what would be moved without writing state

PD1: lives in PUBLIC ~/cos-pipeline/tools/. No tenant slugs or usernames hardcoded.
"""

from __future__ import annotations

import argparse
import glob as _glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Sibling import: coordination.py lives in the same directory ───────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    from coordination import lock as coord_lock
    _COORD_AVAILABLE = True
except ImportError:
    _COORD_AVAILABLE = False

try:
    import yaml
except ImportError:
    print("Missing dependency: pyyaml. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ── Paths ─────────────────────────────────────────────────────────────────────
HOME = Path.home()
DEFAULT_FOLDERS = [HOME / "Downloads", HOME / "Desktop", HOME / "Documents"]
STATE_PATH = HOME / "credentials" / "local_organizer_state.json"
LOG_PATH = HOME / "dashboards" / "logs" / "local-organizer.log"

HOLDER = "local_file_organizer.py"
LOCK_RESOURCE = "local-organizer"
LOCK_TTL_SEC = 600

STALE_AFTER_SEC = 24 * 3600       # 24h since last access (atime) — LF1, LF3
MIN_QUIET_SEC = 60                # don't touch files modified in the last minute
ARCHIVE_AFTER_DAYS = 90           # _Unsorted → _Archive — LF4


# ── Default junk + personal rules (overridable via tenant config) ─────────────
DEFAULT_JUNK_PATTERNS = [
    r"\.crdownload$",
    r"\.part$",
    r"\.tmp$",
    r"\.download$",
    r"^Google ?Chrome.*\.dmg$",
    r"^Firefox.*\.dmg$",
    r"^Zoom.*\.(dmg|pkg)$",
    r"^Slack.*\.dmg$",
    r"^Notion.*\.dmg$",
    r"Installer.*\.(dmg|pkg)$",
    r"^Screen ?Shot .* at .*\.(png|jpg|jpeg)\s+\(copy\)",  # screenshot duplicates
    r"^Screenshot .*Screenshot.*\.png$",                   # screenshot-of-screenshot
    r"-[0-9]+\(\d+\)\.(pdf|docx|xlsx)$",                   # browser-duplicate suffix
    r"^IMG_\d{4,}\s*\(\d+\)\.(jpg|jpeg|png|heic)$",
]

DEFAULT_PERSONAL_KEYWORDS = [
    r"\btax(?:es)?\b",
    r"\bw[-_]?2\b",
    r"\b1099\b",
    r"\binsurance\b",
    r"\bmortgage\b",
    r"\b(?:kids?|child(?:ren)?)\b",
    r"\bschool\b",
    r"\bdoctor\b",
    r"\bdds\b",
    r"\bdentist\b",
    r"\bvet(?:erinarian)?\b",
    r"\bcostco\b",
    r"\bamazon[-_ ]?order\b",
    r"\binvoice\b.*\b(home|personal)\b",
    r"\bpassport\b",
    r"\bdmv\b",
    r"\butility\b.*\bbill\b",
]

DEFAULT_SKIP_NAME_PREFIXES = ("_Routed", "_Junk", "_Personal", "_Unsorted", "_Archive")


# ── Tenant config loader ──────────────────────────────────────────────────────
def find_drive_docs() -> Path | None:
    """Locate drive-docs.yaml via $COS_CONFIG_DIR or glob fallback (PD1 pattern)."""
    env = os.environ.get("COS_CONFIG_DIR")
    if env:
        p = Path(env) / "drive-docs.yaml"
        if p.exists():
            return p
    cands = sorted(_glob.glob(str(HOME / "cos-pipeline-config-*/drive-docs.yaml")))
    return Path(cands[0]) if cands else None


def find_local_organizer_yaml() -> Path | None:
    env = os.environ.get("COS_CONFIG_DIR")
    if env:
        p = Path(env) / "local_organizer.yaml"
        if p.exists():
            return p
    cands = sorted(_glob.glob(str(HOME / "cos-pipeline-config-*/local_organizer.yaml")))
    return Path(cands[0]) if cands else None


def load_deal_aliases() -> list[tuple[str, re.Pattern]]:
    """Return [(deal_slug, compiled_alias_regex), ...] from drive-docs.yaml."""
    p = find_drive_docs()
    if not p:
        return []
    try:
        docs = yaml.safe_load(p.read_text()) or {}
    except Exception as e:
        print(f"WARN: could not parse {p}: {e}", file=sys.stderr)
        return []
    out: list[tuple[str, re.Pattern]] = []
    for deal_id, entry in (docs.get("deal_docs") or {}).items():
        rx = entry.get("alias_regex")
        if not rx:
            continue
        try:
            out.append((deal_id, re.compile(rx, re.IGNORECASE)))
        except re.error as e:
            print(f"WARN: bad alias_regex for {deal_id}: {e}", file=sys.stderr)
    return out


def load_rules() -> tuple[list[re.Pattern], list[re.Pattern]]:
    """Return (junk_patterns, personal_patterns). Tenant overrides defaults if present."""
    junk_src = DEFAULT_JUNK_PATTERNS
    personal_src = DEFAULT_PERSONAL_KEYWORDS
    cfg_path = find_local_organizer_yaml()
    if cfg_path:
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
            if isinstance(cfg.get("junk_patterns"), list):
                junk_src = cfg["junk_patterns"]
            if isinstance(cfg.get("personal_keywords"), list):
                personal_src = cfg["personal_keywords"]
        except Exception as e:
            print(f"WARN: bad {cfg_path}: {e}", file=sys.stderr)
    return (
        [re.compile(p, re.IGNORECASE) for p in junk_src],
        [re.compile(p, re.IGNORECASE) for p in personal_src],
    )


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as e:
            print(f"WARN: state corrupt, resetting: {e}", file=sys.stderr)
    return {"version": 1, "moves": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def file_key(path: Path) -> str:
    """Stable key — sha256 of (resolved-path | mtime_int) so re-creates re-route."""
    try:
        mtime = int(path.stat().st_mtime)
    except FileNotFoundError:
        mtime = 0
    raw = f"{path}|{mtime}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


# ── Helpers ───────────────────────────────────────────────────────────────────
def yyyy_mm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m")


def yyyy_q(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{q}"


def is_open_by_other_process(path: Path) -> bool:
    """Best-effort: skip files that lsof says are open. Errors → assume not open."""
    try:
        r = subprocess.run(
            ["/usr/sbin/lsof", "--", str(path)],
            capture_output=True, text=True, timeout=3,
        )
        # lsof returns 0 when it finds a match, 1 when not. Lines mean "open".
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def unique_destination(dst_dir: Path, name: str) -> Path:
    """Avoid collisions: foo.pdf → foo (1).pdf → foo (2).pdf ..."""
    dst = dst_dir / name
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    n = 1
    while True:
        cand = dst_dir / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


def classify(
    path: Path,
    junk_pats: list[re.Pattern],
    personal_pats: list[re.Pattern],
    deal_aliases: list[tuple[str, re.Pattern]],
) -> tuple[str, str | None]:
    """Return (bucket, detail). bucket ∈ {junk, deal, personal, unsorted}.

    Precedence (LF2): junk → deal-alias → personal-keyword → unsorted.
    (state-check happens upstream.)
    """
    name = path.name
    for rx in junk_pats:
        if rx.search(name):
            return ("junk", rx.pattern)
    for slug, rx in deal_aliases:
        if rx.search(name):
            return ("deal", slug)
    for rx in personal_pats:
        if rx.search(name):
            return ("personal", rx.pattern)
    return ("unsorted", None)


# ── Core walk ─────────────────────────────────────────────────────────────────
def iter_top_files(folder: Path):
    """Yield immediate child files (not dirs, not symlinks, not dotfiles, not our subfolders)."""
    if not folder.exists():
        return
    for entry in folder.iterdir():
        if entry.is_symlink():
            continue
        if entry.name.startswith("."):
            continue
        if entry.name.startswith(DEFAULT_SKIP_NAME_PREFIXES):
            continue
        if entry.is_dir():
            continue
        if not entry.is_file():
            continue
        yield entry


def ensure_subfolders(folder: Path) -> None:
    for sub in DEFAULT_SKIP_NAME_PREFIXES:
        (folder / sub).mkdir(exist_ok=True)


def move_one(src: Path, dst: Path, apply: bool) -> None:
    if not apply:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def process_folder(
    folder: Path,
    state: dict,
    junk_pats: list[re.Pattern],
    personal_pats: list[re.Pattern],
    deal_aliases: list[tuple[str, re.Pattern]],
    apply: bool,
    force: bool,
) -> dict:
    """Process a single top-level folder. Return per-folder counters."""
    counters = {
        "scanned": 0, "moved": 0, "skipped": 0, "errors": 0,
        "routed": 0, "junked": 0, "personal": 0, "unsorted": 0,
    }
    if not folder.exists():
        print(f"  (skip — does not exist: {folder})")
        return counters

    ensure_subfolders(folder)
    now = time.time()

    for f in iter_top_files(folder):
        counters["scanned"] += 1
        try:
            st = f.stat()
        except FileNotFoundError:
            counters["skipped"] += 1
            continue

        # Don't touch files modified < MIN_QUIET_SEC ago
        if (now - st.st_mtime) < MIN_QUIET_SEC:
            counters["skipped"] += 1
            print(f"  skip (just modified): {f.name}")
            continue

        # LF1/LF3 — 24h since user last touched (atime)
        if (now - st.st_atime) < STALE_AFTER_SEC:
            counters["skipped"] += 1
            continue

        key = file_key(f)
        if not force and key in state["moves"]:
            counters["skipped"] += 1
            continue

        if is_open_by_other_process(f):
            counters["skipped"] += 1
            print(f"  skip (open elsewhere): {f.name}")
            continue

        try:
            bucket, detail = classify(f, junk_pats, personal_pats, deal_aliases)
            if bucket == "junk":
                dst_dir = folder / "_Junk"
            elif bucket == "deal":
                dst_dir = folder / "_Routed" / detail
            elif bucket == "personal":
                dst_dir = folder / "_Personal" / yyyy_mm(st.st_mtime)
            else:
                dst_dir = folder / "_Unsorted" / yyyy_mm(st.st_mtime)

            dst = unique_destination(dst_dir, f.name)
            mode = "MOVE" if apply else "DRY"
            print(f"  {mode} [{bucket}] {f.name} → {dst.relative_to(folder)}")
            move_one(f, dst, apply)

            counters["moved"] += 1
            counters[{
                "junk": "junked",
                "deal": "routed",
                "personal": "personal",
                "unsorted": "unsorted",
            }[bucket]] += 1

            if apply:
                state["moves"][key] = {
                    "src": str(f),
                    "dst": str(dst),
                    "bucket": bucket,
                    "detail": detail,
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
        except Exception as e:
            counters["errors"] += 1
            print(f"  ERROR moving {f.name}: {e}")

    return counters


def archive_sweep(folder: Path, apply: bool) -> int:
    """Move _Unsorted files older than 90d into _Archive/YYYY-Q?/. Returns count."""
    unsorted_root = folder / "_Unsorted"
    if not unsorted_root.exists():
        return 0
    cutoff = time.time() - ARCHIVE_AFTER_DAYS * 86400
    archived = 0
    for f in unsorted_root.rglob("*"):
        if not f.is_file():
            continue
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        if st.st_mtime > cutoff:
            continue
        dst_dir = folder / "_Archive" / yyyy_q(st.st_mtime)
        dst = unique_destination(dst_dir, f.name)
        mode = "MOVE" if apply else "DRY"
        print(f"  {mode} [archive] _Unsorted/{f.relative_to(unsorted_root)} → _Archive/{dst.relative_to(folder / '_Archive')}")
        if apply:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dst))
                archived += 1
            except Exception as e:
                print(f"  ERROR archiving {f.name}: {e}")
        else:
            archived += 1
    return archived


def append_log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true",
                    help="actually move files (default: dry-run)")
    ap.add_argument("--folder", action="append", type=Path,
                    help="folder to process (can repeat). Default: all three.")
    ap.add_argument("--force", action="store_true",
                    help="re-route files already recorded in state")
    ap.add_argument("--list", action="store_true",
                    help="show what would be moved without writing state")
    args = ap.parse_args(argv)

    folders = args.folder if args.folder else DEFAULT_FOLDERS
    list_only = args.list
    apply = args.apply and not list_only

    junk_pats, personal_pats = load_rules()
    deal_aliases = load_deal_aliases()

    print(f"local_file_organizer  apply={apply}  list_only={list_only}  force={args.force}")
    print(f"  junk_patterns={len(junk_pats)}  personal_keywords={len(personal_pats)}  deal_aliases={len(deal_aliases)}")
    print(f"  folders={[str(f) for f in folders]}")

    def _do_run() -> dict:
        state = load_state()
        totals = {
            "scanned": 0, "moved": 0, "skipped": 0, "errors": 0,
            "routed": 0, "junked": 0, "personal": 0, "unsorted": 0,
            "archived": 0,
        }
        for folder in folders:
            print(f"\n── {folder} ──")
            c = process_folder(
                folder, state, junk_pats, personal_pats, deal_aliases,
                apply=apply, force=args.force,
            )
            archived = archive_sweep(folder, apply=apply)
            c_total = {**c, "archived": archived}
            for k in totals:
                totals[k] += c_total.get(k, 0)

        if apply:
            save_state(state)
        return totals

    if _COORD_AVAILABLE:
        try:
            with coord_lock(LOCK_RESOURCE, holder=HOLDER, ttl_seconds=LOCK_TTL_SEC):
                totals = _do_run()
        except Exception as e:
            print(f"ERROR: could not acquire lock: {e}", file=sys.stderr)
            return 2
    else:
        totals = _do_run()

    print()
    print(f"{totals['scanned']} scanned | {totals['moved']} moved | {totals['skipped']} skipped | {totals['errors']} errors")

    if apply:
        today = datetime.now().strftime("%Y-%m-%d")
        line = (
            f"{today} · scanned {totals['scanned']} · routed {totals['routed']} · "
            f"junked {totals['junked']} · personal {totals['personal']} · "
            f"unsorted {totals['unsorted']} · archived {totals['archived']}"
        )
        append_log(line)
        print(f"\nlog: {line}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
