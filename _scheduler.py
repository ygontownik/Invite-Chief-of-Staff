"""_scheduler.py — scheduler abstraction (BOOTSTRAP_PLAN Build #3).

Single interface for registering routines that run on a schedule. Today's
backend: launchd (macOS LaunchAgents). Future backends: systemd (Linux),
cron (anywhere), cloud schedulers (Fly cron, Lambda).

The interface stays stable so bootstrap.sh and pipeline code never branch
on the host. Backend selection is config + auto-detection.

Schedule format (normalized across backends):
    {"keepalive": True}                              — always-on daemon
    {"interval_sec": 900}                            — every 15 min
    {"calendar": [{"hour": 7, "minute": 22,
                   "weekday": [1, 2, 3, 4, 5]}]}     — Mon–Fri @ 07:22
    {"calendar": [{"hour": 7, "minute": 30},
                  {"hour": 18, "minute": 0}]}        — twice daily

Weekday: 1=Mon … 7=Sun (matches launchd convention; backends translate).
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Optional


def _have_launchctl() -> bool:
    """True if macOS `launchctl` is available."""
    try:
        result = subprocess.run(
            ['launchctl', 'help'],
            capture_output=True, timeout=2,
        )
        # `launchctl help` returns nonzero on macOS but emits help text — both indicate presence.
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _detect_backend() -> str:
    """Pick the active scheduler backend.

    Order: $COS_SCHEDULER_BACKEND override → 'launchd' if launchctl present → 'cron'.
    """
    explicit = os.environ.get('COS_SCHEDULER_BACKEND', '').strip().lower()
    if explicit in ('launchd', 'systemd', 'cron'):
        return explicit
    return 'launchd' if _have_launchctl() else 'cron'


def _launchagents_dir() -> Path:
    """Directory where LaunchAgent plists live for the current user."""
    return Path.home() / 'Library' / 'LaunchAgents'


def _gui_target(label: str) -> str:
    """launchctl bootstrap/bootout target for a user-domain agent."""
    return f'gui/{os.getuid()}/{label}'


# ── Plist generation ────────────────────────────────────────────────────────

def _build_plist_dict(
    label: str,
    program: list[str],
    schedule: dict,
    env: Optional[dict] = None,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    run_at_load: bool = False,
    session_type: Optional[str] = 'Aqua',
) -> dict:
    """Translate the normalized schedule + run params into a launchd plist dict."""
    plist: dict = {
        'Label': label,
        'ProgramArguments': list(program),
        'RunAtLoad': bool(run_at_load),
    }
    if env:
        plist['EnvironmentVariables'] = dict(env)
    if stdout:
        plist['StandardOutPath'] = str(stdout)
    if stderr:
        plist['StandardErrorPath'] = str(stderr)
    if session_type:
        plist['LimitLoadToSessionType'] = session_type

    # Schedule translation
    if schedule.get('keepalive'):
        plist['KeepAlive'] = True
        plist['RunAtLoad'] = True   # KeepAlive without RunAtLoad is rarely useful
    elif 'interval_sec' in schedule:
        plist['StartInterval'] = int(schedule['interval_sec'])
    elif 'calendar' in schedule:
        entries = []
        for spec in schedule['calendar']:
            weekdays = spec.get('weekday')
            if weekdays:
                # Expand multi-weekday specs into one calendar entry per day.
                for wd in (weekdays if isinstance(weekdays, list) else [weekdays]):
                    entry = {'Weekday': int(wd)}
                    if 'hour' in spec:
                        entry['Hour'] = int(spec['hour'])
                    if 'minute' in spec:
                        entry['Minute'] = int(spec['minute'])
                    entries.append(entry)
            else:
                entry = {}
                if 'hour' in spec:
                    entry['Hour'] = int(spec['hour'])
                if 'minute' in spec:
                    entry['Minute'] = int(spec['minute'])
                if 'day' in spec:
                    entry['Day'] = int(spec['day'])
                if 'month' in spec:
                    entry['Month'] = int(spec['month'])
                if entry:
                    entries.append(entry)
        plist['StartCalendarInterval'] = entries
    else:
        raise ValueError(
            f'unrecognized schedule shape: {schedule!r} '
            f'(expected keepalive | interval_sec | calendar)'
        )
    return plist


def render_plist(
    label: str,
    program: list[str],
    schedule: dict,
    env: Optional[dict] = None,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    run_at_load: bool = False,
    session_type: Optional[str] = 'Aqua',
) -> bytes:
    """Build a plist as XML bytes (writable to disk)."""
    pd = _build_plist_dict(
        label=label, program=program, schedule=schedule, env=env,
        stdout=stdout, stderr=stderr, run_at_load=run_at_load,
        session_type=session_type,
    )
    return plistlib.dumps(pd)


# ── launchctl operations ────────────────────────────────────────────────────

def _is_loaded_launchd(label: str) -> bool:
    """True if the agent is currently loaded in launchd."""
    try:
        result = subprocess.run(
            ['launchctl', 'list', label],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _bootstrap_launchd(plist_path: Path) -> None:
    subprocess.run(
        ['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist_path)],
        capture_output=True, check=True, timeout=10,
    )


def _bootout_launchd(label: str) -> None:
    # bootout is allowed to fail with "service not loaded" — we only care
    # that no copy is loaded at the end.
    subprocess.run(
        ['launchctl', 'bootout', _gui_target(label)],
        capture_output=True, timeout=10,
    )


def _kickstart_launchd(label: str) -> None:
    subprocess.run(
        ['launchctl', 'kickstart', '-k', _gui_target(label)],
        capture_output=True, check=True, timeout=10,
    )


# ── Public API ──────────────────────────────────────────────────────────────

def register(
    label: str,
    program: list[str],
    schedule: dict,
    env: Optional[dict] = None,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    run_at_load: bool = False,
    session_type: Optional[str] = 'Aqua',
    activate: bool = True,
) -> Path:
    """Register a routine and (optionally) load it.

    Writes the backend-native unit file (plist on macOS), then bootstraps
    if `activate=True`. Idempotent: bootouts an existing copy first so a
    re-register replaces in place.

    Returns the path to the unit file written.
    """
    backend = _detect_backend()
    if backend != 'launchd':
        raise NotImplementedError(
            f"scheduler backend {backend!r} not implemented yet "
            f"(launchd only for now per BOOTSTRAP_PLAN YAGNI)"
        )

    body = render_plist(
        label=label, program=program, schedule=schedule, env=env,
        stdout=stdout, stderr=stderr, run_at_load=run_at_load,
        session_type=session_type,
    )

    plist_path = _launchagents_dir() / f'{label}.plist'
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(body)

    if activate:
        # Idempotent: bootout any existing copy before bootstrap.
        if _is_loaded_launchd(label):
            _bootout_launchd(label)
        _bootstrap_launchd(plist_path)

    return plist_path


def unregister(label: str, remove_file: bool = True) -> bool:
    """Stop and remove a registered routine.

    Returns True if anything was actually removed (loaded or file present).
    """
    backend = _detect_backend()
    if backend != 'launchd':
        raise NotImplementedError(
            f"scheduler backend {backend!r} not implemented yet"
        )
    changed = False
    if _is_loaded_launchd(label):
        _bootout_launchd(label)
        changed = True
    plist_path = _launchagents_dir() / f'{label}.plist'
    if remove_file and plist_path.exists():
        plist_path.unlink()
        changed = True
    return changed


def is_registered(label: str) -> bool:
    """True if either the unit file exists or the agent is loaded."""
    backend = _detect_backend()
    if backend != 'launchd':
        raise NotImplementedError(
            f"scheduler backend {backend!r} not implemented yet"
        )
    if _is_loaded_launchd(label):
        return True
    return (_launchagents_dir() / f'{label}.plist').exists()


def list_registered(prefix: str = '') -> list[str]:
    """List labels of currently-installed unit files matching `prefix`.

    Includes unit files even if not loaded; that's the canonical inventory.
    """
    backend = _detect_backend()
    if backend != 'launchd':
        raise NotImplementedError(
            f"scheduler backend {backend!r} not implemented yet"
        )
    out = []
    for p in _launchagents_dir().glob('*.plist'):
        label = p.stem
        if not prefix or label.startswith(prefix):
            out.append(label)
    return sorted(out)


def kickstart(label: str) -> None:
    """Force-restart a registered routine (no-op if already stopped)."""
    backend = _detect_backend()
    if backend != 'launchd':
        raise NotImplementedError(
            f"scheduler backend {backend!r} not implemented yet"
        )
    _kickstart_launchd(label)


# Diagnostic CLI: `python3 _scheduler.py list com.tomaccove.`
if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == 'list':
        prefix = sys.argv[2] if len(sys.argv) >= 3 else ''
        for label in list_registered(prefix):
            loaded = ' [LOADED]' if _is_loaded_launchd(label) else ''
            print(f'{label}{loaded}')
    else:
        print(f'Usage: python3 _scheduler.py list [prefix]')
        print(f'Active backend: {_detect_backend()}')
