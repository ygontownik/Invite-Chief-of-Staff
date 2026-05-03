#!/usr/bin/env python3
"""heartbeat.py — Phase 2 Track L1 routine staleness probe.

Loads ``routines.yaml`` (single source of truth per DECISION C10) and reports,
per skill/daemon, whether it has produced a fresh log/output within the
configured staleness threshold (default 24h).

Read-only: never modifies live .py, HTML, ~/credentials/, or LaunchAgent.
Probes only inspect mtime/size/launchctl-list output. Stdlib only.

CLI:
  python3 heartbeat.py [--tenant tomac] [--config ~/cos-pipeline-config-tomac]
                       [--out json|markdown] [--threshold 24]
                       [--write-state]

Default behaviour: print to stdout. ``--write-state`` writes
``~/cos-pipeline/data-<tenant>/heartbeat.json`` (mkdir parents).

Exit:
  0  always (per-routine error isolation; a probe failure is not fatal).

Footer line: "X ok | Y stale | Z probe-error".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


HOME = Path(os.path.expanduser("~"))

# Probe locations (cited paths only; we never write here).
LOG_DIRS = [
    HOME / "dashboards" / "logs",
    HOME / "dashboards" / "logs" / "claude-tasks",
    HOME / "cos-pipeline" / "logs-tomac",  # populated once Track B3 lands
    Path("/tmp"),
]
CREDENTIALS_DIR = HOME / "credentials"
ROUTINES_YAML_DEFAULT = HOME / "cos-pipeline" / "routines.yaml"

# One-shot daemons to exclude per spec (calendar-fired, self-expiring).
ONE_SHOT_PREFIXES = ("recorder.start.gcal", "recorder.stop.gcal")

DEFAULT_THRESHOLD_HOURS = 24.0


# ─────────────────────────────────────────────────────────────────────
# Minimal YAML parser
# ─────────────────────────────────────────────────────────────────────
# We avoid a PyYAML dep (stdlib-only constraint). routines.yaml is a flat
# list-of-mappings document under top-level keys (server/skills/daemons).
# This parser handles that subset — quoted strings, simple scalars, lists.


def _strip_inline_comment(line: str) -> str:
    # Don't strip inside quoted strings. routines.yaml uses both single and
    # double quotes; we walk char-by-char.
    out = []
    in_single = in_double = False
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            break
        out.append(ch)
    return "".join(out).rstrip()


def _coerce_scalar(s: str) -> Any:
    s = s.strip()
    if not s:
        return None
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    if s in ("null", "~"):
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _parse_routines_yaml(path: Path) -> dict[str, Any]:
    """Parse the subset of YAML produced by routines.yaml.

    Returns a dict with keys ``server``, ``skills``, ``daemons`` each holding
    a list of dicts. Best-effort; on any structural surprise, returns whatever
    was parsed so far rather than raising.
    """
    text = path.read_text()
    top: dict[str, list[dict[str, Any]]] = {}
    current_section: Optional[str] = None
    current_item: Optional[dict[str, Any]] = None
    item_indent: Optional[int] = None

    for raw in text.splitlines():
        line = _strip_inline_comment(raw)
        if not line.strip():
            continue
        # Top-level key: starts at col 0, no leading dash.
        if not line.startswith(" ") and not line.startswith("-"):
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
            if not m:
                continue
            key, rest = m.group(1), m.group(2)
            if key in ("server", "skills", "daemons"):
                current_section = key
                top.setdefault(key, [])
                current_item = None
                item_indent = None
            else:
                current_section = None  # ignore other top-level keys
            continue
        if current_section is None:
            continue
        # New list item: leading "- name: foo" or "  - key: val"
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("- "):
            current_item = {}
            top[current_section].append(current_item)
            item_indent = indent
            kv = stripped[2:]
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", kv)
            if m:
                current_item[m.group(1)] = _coerce_scalar(m.group(2))
            continue
        # Continuation key on same item
        if current_item is None:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", stripped)
        if m:
            current_item[m.group(1)] = _coerce_scalar(m.group(2))
    return top


# ─────────────────────────────────────────────────────────────────────
# Probes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    name: str
    kind: str  # skill | daemon | server
    package: Optional[str] = None
    schedule: Optional[str] = None
    wrapper_label: Optional[str] = None
    most_recent_path: Optional[str] = None
    most_recent_mtime_iso: Optional[str] = None
    staleness_hours: Optional[float] = None
    threshold_hours: float = DEFAULT_THRESHOLD_HOURS
    status: str = "unknown"  # ok | stale | probe-error | excluded
    last_exit_status: Optional[int] = None  # from launchctl list
    pid: Optional[int] = None
    notes: list[str] = field(default_factory=list)


def _candidate_log_names(name: str, wrapper_label: Optional[str]) -> list[str]:
    """Return filename stems we'll glob for, in priority order."""
    stems = [name]
    if wrapper_label:
        # com.yoni.claude-task.cos-personal-briefing -> cos-personal-briefing
        # com.tomaccove.email-resolver -> email-resolver
        tail = wrapper_label.rsplit(".", 1)[-1]
        if tail and tail not in stems:
            stems.append(tail)
        # Also try without the "claude-task." infix
        if ".claude-task." in wrapper_label:
            tail2 = wrapper_label.split(".claude-task.", 1)[-1]
            if tail2 not in stems:
                stems.append(tail2)
    return stems


def _find_most_recent_log(
    name: str, wrapper_label: Optional[str]
) -> tuple[Optional[Path], Optional[float]]:
    """Search known log directories for the freshest file matching this routine.

    Returns (path, mtime_epoch) or (None, None).
    """
    stems = _candidate_log_names(name, wrapper_label)
    best: tuple[Optional[Path], Optional[float]] = (None, None)
    for d in LOG_DIRS:
        if not d.exists() or not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            fname = entry.name
            if not any(fname.startswith(stem) for stem in stems):
                continue
            try:
                mt = entry.stat().st_mtime
            except OSError:
                continue
            if best[1] is None or mt > best[1]:
                best = (entry, mt)
    return best


def _launchctl_status(label: str) -> tuple[Optional[int], Optional[int]]:
    """Return (last_exit_status, pid) from `launchctl list <label>`.

    Read-only — equivalent to inspecting the running launchd state.
    """
    if not shutil.which("launchctl"):
        return (None, None)
    try:
        proc = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return (None, None)
    if proc.returncode != 0:
        return (None, None)
    last_exit: Optional[int] = None
    pid: Optional[int] = None
    for line in proc.stdout.splitlines():
        m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', line)
        if m:
            last_exit = int(m.group(1))
        m = re.search(r'"PID"\s*=\s*(\d+)', line)
        if m:
            pid = int(m.group(1))
    return (last_exit, pid)


def _probe_doc_indices() -> dict[str, str]:
    """Snapshot mtimes of doc index JSONs in ~/credentials/ (no read of body)."""
    out: dict[str, str] = {}
    if not CREDENTIALS_DIR.exists():
        return out
    try:
        for f in CREDENTIALS_DIR.glob("*_doc_index.json"):
            try:
                mt = f.stat().st_mtime
                out[f.name] = dt.datetime.fromtimestamp(mt).isoformat(timespec="seconds")
            except OSError:
                continue
    except OSError:
        pass
    return out


def _probe_per_tenant_state(tenant: str) -> dict[str, Any]:
    """Snapshot mtimes under ~/cos-pipeline/data-<tenant>/ (read-only)."""
    base = HOME / "cos-pipeline" / f"data-{tenant}"
    out: dict[str, Any] = {"path": str(base), "exists": base.exists(), "files": {}}
    if not base.exists():
        return out
    try:
        for f in base.rglob("*"):
            if f.is_file():
                try:
                    mt = f.stat().st_mtime
                    out["files"][str(f.relative_to(base))] = (
                        dt.datetime.fromtimestamp(mt).isoformat(timespec="seconds")
                    )
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _is_one_shot(item: dict[str, Any]) -> bool:
    name = str(item.get("name", ""))
    return any(name.startswith(p) for p in ONE_SHOT_PREFIXES)


def probe_routine(
    item: dict[str, Any], kind: str, threshold_hours: float, now: dt.datetime
) -> ProbeResult:
    name = str(item.get("name", "<unknown>"))
    res = ProbeResult(
        name=name,
        kind=kind,
        package=item.get("package"),
        schedule=item.get("schedule"),
        wrapper_label=item.get("wrapper_label"),
        threshold_hours=threshold_hours,
    )
    try:
        if _is_one_shot(item):
            res.status = "excluded"
            res.notes.append("one-shot calendar-driven; excluded per spec")
            return res

        # 1. Find freshest log file.
        path, mt = _find_most_recent_log(name, item.get("wrapper_label"))
        if path is not None and mt is not None:
            res.most_recent_path = str(path)
            res.most_recent_mtime_iso = dt.datetime.fromtimestamp(mt).isoformat(
                timespec="seconds"
            )
            staleness = (now.timestamp() - mt) / 3600.0
            res.staleness_hours = round(staleness, 2)
            res.status = "stale" if staleness > threshold_hours else "ok"
        else:
            res.notes.append("no log/output file matched in probed directories")
            # No log found at all: mark stale so it surfaces in the email.
            res.status = "stale"

        # 2. launchctl status (if wrapper_label present).
        wlabel = item.get("wrapper_label")
        if wlabel and isinstance(wlabel, str) and wlabel and "*" not in wlabel:
            last_exit, pid = _launchctl_status(wlabel)
            res.last_exit_status = last_exit
            res.pid = pid
            if last_exit is not None and last_exit != 0:
                res.notes.append(f"launchctl LastExitStatus={last_exit}")
        return res
    except Exception as exc:  # pragma: no cover — error isolation
        res.status = "probe-error"
        res.notes.append(f"probe exception: {type(exc).__name__}: {exc}")
        return res


# ─────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────


def render_json(
    tenant: str, generated_at: str, routines: list[ProbeResult], extras: dict[str, Any]
) -> str:
    payload = {
        "tenant": tenant,
        "generated_at": generated_at,
        "threshold_hours": (
            routines[0].threshold_hours if routines else DEFAULT_THRESHOLD_HOURS
        ),
        "routines": [asdict(r) for r in routines],
        "probes": extras,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def render_markdown(
    tenant: str, generated_at: str, routines: list[ProbeResult], extras: dict[str, Any]
) -> str:
    lines: list[str] = []
    lines.append(f"# Heartbeat — tenant `{tenant}`")
    lines.append("")
    lines.append(f"_Generated: {generated_at}_")
    lines.append("")
    by_status = {"stale": [], "probe-error": [], "ok": [], "excluded": []}
    for r in routines:
        by_status.setdefault(r.status, []).append(r)
    for status in ("stale", "probe-error", "ok", "excluded"):
        items = by_status.get(status, [])
        if not items:
            continue
        lines.append(f"## {status.upper()} ({len(items)})")
        lines.append("")
        lines.append("| Name | Kind | Pkg | Last seen | Stale (h) | Notes |")
        lines.append("|---|---|---|---|---|---|")
        for r in items:
            stale = "" if r.staleness_hours is None else f"{r.staleness_hours:.1f}"
            note = "; ".join(r.notes) if r.notes else ""
            lines.append(
                f"| `{r.name}` | {r.kind} | {r.package or ''} | "
                f"{r.most_recent_mtime_iso or '—'} | {stale} | {note} |"
            )
        lines.append("")
    # Probes summary
    lines.append("## Probe snapshot")
    lines.append("")
    di = extras.get("doc_indices", {})
    lines.append(f"- Doc indices in `~/credentials/`: {len(di)} file(s)")
    for k, v in sorted(di.items()):
        lines.append(f"  - `{k}` mtime={v}")
    pts = extras.get("per_tenant_state", {})
    if pts.get("exists"):
        lines.append(
            f"- Per-tenant state `{pts.get('path')}`: "
            f"{len(pts.get('files', {}))} file(s)"
        )
    else:
        lines.append(f"- Per-tenant state `{pts.get('path')}`: (does not exist)")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def build_routine_set(parsed: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    out: list[tuple[dict[str, Any], str]] = []
    for kind in ("server", "skills", "daemons"):
        for item in parsed.get(kind, []) or []:
            singular = {"server": "server", "skills": "skill", "daemons": "daemon"}[kind]
            out.append((item, singular))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", default="tomac")
    parser.add_argument(
        "--config",
        default=str(HOME / "cos-pipeline-config-tomac"),
        help="Per-tenant config dir (read-only; reserved for future use).",
    )
    parser.add_argument("--routines", default=str(ROUTINES_YAML_DEFAULT))
    parser.add_argument("--out", choices=("json", "markdown"), default="json")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD_HOURS,
        help="Staleness threshold in hours (default 24).",
    )
    parser.add_argument(
        "--write-state",
        action="store_true",
        help="Write JSON to ~/cos-pipeline/data-<tenant>/heartbeat.json. "
        "Default is print-only.",
    )
    args = parser.parse_args(argv)

    routines_path = Path(os.path.expanduser(args.routines))
    if not routines_path.exists():
        print(
            f"[heartbeat] routines.yaml not found at {routines_path}", file=sys.stderr
        )
        return 1

    parsed = _parse_routines_yaml(routines_path)
    routine_set = build_routine_set(parsed)

    now = dt.datetime.now()
    generated_at = now.isoformat(timespec="seconds")
    results: list[ProbeResult] = []
    for item, kind in routine_set:
        results.append(probe_routine(item, kind, args.threshold, now))

    extras = {
        "doc_indices": _probe_doc_indices(),
        "per_tenant_state": _probe_per_tenant_state(args.tenant),
        "log_dirs_probed": [str(d) for d in LOG_DIRS],
    }

    if args.out == "json":
        rendered = render_json(args.tenant, generated_at, results, extras)
    else:
        rendered = render_markdown(args.tenant, generated_at, results, extras)

    if args.write_state:
        out_dir = HOME / "cos-pipeline" / f"data-{args.tenant}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "heartbeat.json"
        # Always write JSON form to disk regardless of --out.
        json_payload = render_json(args.tenant, generated_at, results, extras)
        out_path.write_text(json_payload)
        print(f"[heartbeat] wrote {out_path}", file=sys.stderr)

    print(rendered)

    counts = {"ok": 0, "stale": 0, "probe-error": 0, "excluded": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(
        f"[heartbeat] {counts['ok']} ok | {counts['stale']} stale | "
        f"{counts['probe-error']} probe-error | {counts['excluded']} excluded",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
