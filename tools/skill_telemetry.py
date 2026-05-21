#!/usr/bin/env python3
"""
skill_telemetry.py — Claude Code skill invocation logging.

Append-only JSONL log of every skill the user invokes during a Claude
Code session. Powers the weekly skills curation pass (audit §3 #2,
§3 #8). Outcome capture (✓/✏️/✗) is a follow-up — for now we log the
invocation and leave `outcome: null`.

Log path:  ~/dashboards/data/compiled/skill-telemetry.jsonl
Schema (one JSON object per line):
    {
      "skill":       "skill-name",            # required
      "plugin":      "anthropic-skills",      # optional, if namespaced
      "started_at":  "2026-05-20T22:14:05+00:00",
      "ended_at":    "2026-05-20T22:14:07+00:00",  # optional
      "duration_ms": 2120,                    # optional, computed if both timestamps
      "session_id":  "abc123",                # optional
      "outcome":     null,                    # null | "used" | "edited" | "discarded"
      "outcome_at":  null,                    # timestamp when outcome was set
      "args":        "drive_org",             # optional, args string
      "source":      "stop_hook"              # which logger wrote the row
    }

This module is the writer. Read it via:
    python3 -c "import json; [print(json.loads(l)) for l in
        open('/Users/ygontownik/dashboards/data/compiled/skill-telemetry.jsonl')]"

INTEGRATION POINTS
==================
1. Stop hook (dash-state-hook.py — Chat B owns):
   At end of run, call `scan_transcripts_for_skills()` to extract any
   Skill tool calls from the current Claude Code session transcript
   and write rows. See skill-telemetry-integration.patch for the
   3-line patch.

2. Direct invocation:
   Any pipeline script that *itself* invokes a Claude Code skill
   (e.g., via `claude -p /deal-sync`) can call `log_invocation()`
   directly before+after the spawn.

3. Follow-up outcome capture:
   The next morning briefing can read rows where `outcome is null`
   and surface a "rate yesterday's skills" tile. The rating UI calls
   `set_outcome(row_id, outcome)`.

Safe to call concurrently; uses an O_APPEND open which is atomic for
single writes up to PIPE_BUF (4096+ bytes on macOS). Each row stays
well under that.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Iterator

HOME = Path.home()
LOG_PATH = HOME / "dashboards" / "data" / "compiled" / "skill-telemetry.jsonl"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _ensure_dir() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def log_invocation(
    skill: str,
    *,
    plugin: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    duration_ms: int | None = None,
    session_id: str | None = None,
    args: str | None = None,
    source: str = "manual",
) -> str:
    """Append one row to the telemetry log. Returns the row's id."""
    _ensure_dir()
    row_id = uuid.uuid4().hex[:12]
    row = {
        "id": row_id,
        "skill": skill,
        "plugin": plugin,
        "started_at": started_at or _now_iso(),
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "session_id": session_id,
        "outcome": None,
        "outcome_at": None,
        "args": args,
        "source": source,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row_id


def set_outcome(row_id: str, outcome: str) -> bool:
    """Mark a logged invocation's outcome. Returns True if the row was found.

    Outcome values: "used" | "edited" | "discarded".
    Implementation: read-modify-write the whole JSONL — small file, weekly
    review pass is the only consumer of mutation.
    """
    if outcome not in {"used", "edited", "discarded"}:
        raise ValueError(f"invalid outcome: {outcome!r}")
    if not LOG_PATH.exists():
        return False
    rows = []
    found = False
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            rows.append(line)
            continue
        if r.get("id") == row_id:
            r["outcome"] = outcome
            r["outcome_at"] = _now_iso()
            found = True
        rows.append(json.dumps(r, ensure_ascii=False))
    if found:
        LOG_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return found


def iter_rows() -> Iterator[dict]:
    if not LOG_PATH.exists():
        return iter([])
    out = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return iter(out)


# ── Stop-hook integration: scan a transcript for Skill invocations ───────────

SKILL_TOOL_RE = re.compile(
    r'"name"\s*:\s*"Skill"\s*,\s*"input"\s*:\s*\{\s*"skill"\s*:\s*"([^"]+)"'
    r'(?:[^}]*?"args"\s*:\s*"([^"]*)")?',
    re.DOTALL,
)


def scan_transcript(transcript_path: str | Path, session_id: str | None = None) -> int:
    """Walk a Claude Code transcript file, log each Skill invocation found.

    Transcript files live under ~/.claude/projects/<slug>/<session>.jsonl.
    Each line is a JSON object containing tool calls. We grep for Skill
    tool invocations and emit one telemetry row per match.

    Idempotent on a per-session basis: if rows already exist for this
    session_id, this run does nothing.

    Returns the number of rows written.
    """
    path = Path(transcript_path)
    if not path.exists():
        return 0
    session_id = session_id or path.stem
    if session_id:
        for existing in iter_rows():
            if existing.get("session_id") == session_id:
                return 0  # already logged
    text = path.read_text(encoding="utf-8", errors="replace")
    n = 0
    for m in SKILL_TOOL_RE.finditer(text):
        skill = m.group(1)
        args = m.group(2)
        plugin = None
        if ":" in skill:
            plugin, skill = skill.split(":", 1)
        log_invocation(
            skill,
            plugin=plugin,
            args=args,
            session_id=session_id,
            source="stop_hook_transcript_scan",
        )
        n += 1
    return n


def scan_recent_transcripts(project_slug: str | None = None, limit: int = 5) -> dict:
    """Walk the most-recently-modified transcript files and scan each.

    project_slug = subdirectory under ~/.claude/projects/. If omitted,
    walks all projects.

    Returns a summary dict.
    """
    base = HOME / ".claude" / "projects"
    if not base.is_dir():
        return {"scanned": 0, "rows": 0}
    project_dirs = (
        [base / project_slug] if project_slug else [p for p in base.iterdir() if p.is_dir()]
    )
    transcripts: list[Path] = []
    for d in project_dirs:
        transcripts.extend(d.glob("*.jsonl"))
    transcripts.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    scanned = 0
    rows = 0
    for p in transcripts[:limit]:
        rows += scan_transcript(p)
        scanned += 1
    return {"scanned": scanned, "rows": rows}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Claude Code skill invocation telemetry")
    sub = p.add_subparsers(dest="cmd", required=True)

    log_cmd = sub.add_parser("log", help="Log a skill invocation directly")
    log_cmd.add_argument("skill")
    log_cmd.add_argument("--plugin", default=None)
    log_cmd.add_argument("--args", default=None)
    log_cmd.add_argument("--source", default="cli")

    out_cmd = sub.add_parser("outcome", help="Set outcome on a logged row")
    out_cmd.add_argument("row_id")
    out_cmd.add_argument("outcome", choices=["used", "edited", "discarded"])

    sub.add_parser("show", help="Print the full log")

    scan_cmd = sub.add_parser("scan", help="Scan recent transcripts for Skill calls")
    scan_cmd.add_argument("--project", default=None)
    scan_cmd.add_argument("--limit", type=int, default=5)

    args = p.parse_args()

    if args.cmd == "log":
        rid = log_invocation(args.skill, plugin=args.plugin, args=args.args, source=args.source)
        print(rid)
        return 0
    if args.cmd == "outcome":
        ok = set_outcome(args.row_id, args.outcome)
        print("ok" if ok else "row_not_found")
        return 0 if ok else 1
    if args.cmd == "show":
        for r in iter_rows():
            print(json.dumps(r))
        return 0
    if args.cmd == "scan":
        summary = scan_recent_transcripts(args.project, args.limit)
        print(json.dumps(summary))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
