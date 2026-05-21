#!/usr/bin/env python3
"""
coordination.py — Shared locking + system-state for the TCIP/CoS pipeline.

Every long-running script that mutates shared state (drive-docs.yaml, GAS files
via clasp, log.json, deal status docs) should import this and bracket its
writes with acquire_lock() / release_lock().

State file:  ~/dashboards/data/system-state.json
Schema:
  {
    "processes": {
      "<name>": {"pid": int, "op": str, "started_at": iso}
    },
    "locks": {
      "<resource>": {"holder": str, "acquired_at": iso, "ttl_seconds": int}
    },
    "last_run": {
      "<name>": iso
    }
  }

Resources are free-form strings; convention:
  "drive-docs.yaml"         — registry writes
  "gas:<project>"           — clasp push per project
  "log.json:<deal_id>"      — per-deal log writes
  "deal-status:<deal_id>"   — deal status doc writes

Locks are advisory + crash-safe (acquired_at + ttl_seconds; stale locks expire).

Usage:
    from coordination import lock, mark_run, last_run, is_running

    with lock("drive-docs.yaml", holder="sync_registry.py", ttl_seconds=300):
        # ... write to drive-docs.yaml ...
    mark_run("sync_registry.py")
"""

from __future__ import annotations
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

STATE_PATH = Path(os.environ.get(
    "COS_SYSTEM_STATE",
    str(Path.home() / "dashboards/data/system-state.json"),
))
LOCK_POLL_SECONDS = 2
DEFAULT_TTL = 300  # 5 minutes; long enough for most ops, short enough to recover from crashes


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_state() -> dict:
    if not STATE_PATH.exists():
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        return {"processes": {}, "locks": {}, "last_run": {}}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        # If corrupted, return empty rather than crash. Caller will overwrite.
        return {"processes": {}, "locks": {}, "last_run": {}}


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)  # atomic on POSIX


def _is_stale(lock_entry: dict, now_ts: float) -> bool:
    """Lock is stale if acquired_at + ttl_seconds < now."""
    try:
        acquired_at = datetime.fromisoformat(lock_entry["acquired_at"]).timestamp()
    except (KeyError, ValueError):
        return True
    ttl = lock_entry.get("ttl_seconds", DEFAULT_TTL)
    return now_ts > acquired_at + ttl


def try_acquire(resource: str, holder: str, ttl_seconds: int = DEFAULT_TTL) -> bool:
    """Single attempt. Returns True if acquired, False if held by someone else."""
    state = _read_state()
    locks = state.setdefault("locks", {})
    now = time.time()
    existing = locks.get(resource)
    if existing and not _is_stale(existing, now) and existing.get("holder") != holder:
        return False
    locks[resource] = {
        "holder": holder,
        "acquired_at": _now_iso(),
        "ttl_seconds": ttl_seconds,
    }
    _write_state(state)
    return True


def acquire(resource: str, holder: str, ttl_seconds: int = DEFAULT_TTL,
            timeout_seconds: int = 120) -> bool:
    """Block until acquired or timeout. Returns True/False."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if try_acquire(resource, holder, ttl_seconds):
            return True
        time.sleep(LOCK_POLL_SECONDS)
    return False


def release(resource: str, holder: str) -> None:
    """Release a lock you hold. Idempotent."""
    state = _read_state()
    locks = state.setdefault("locks", {})
    existing = locks.get(resource)
    if existing and existing.get("holder") == holder:
        del locks[resource]
        _write_state(state)


@contextmanager
def lock(resource: str, holder: str, ttl_seconds: int = DEFAULT_TTL,
         timeout_seconds: int = 120) -> Iterator[None]:
    """Context manager. Raises TimeoutError if the lock can't be acquired."""
    if not acquire(resource, holder, ttl_seconds, timeout_seconds):
        state = _read_state()
        held_by = state.get("locks", {}).get(resource, {}).get("holder", "<unknown>")
        raise TimeoutError(
            f"Could not acquire lock on '{resource}' after {timeout_seconds}s "
            f"(held by '{held_by}')."
        )
    try:
        yield
    finally:
        release(resource, holder)


def mark_running(name: str, op: str = "") -> None:
    """Register a process as currently running. Pair with clear_running on exit."""
    state = _read_state()
    state.setdefault("processes", {})[name] = {
        "pid": os.getpid(),
        "op": op,
        "started_at": _now_iso(),
    }
    _write_state(state)


def clear_running(name: str) -> None:
    state = _read_state()
    state.setdefault("processes", {}).pop(name, None)
    _write_state(state)


def is_running(name: str) -> bool:
    state = _read_state()
    return name in state.get("processes", {})


def mark_run(name: str) -> None:
    """Record that <name> completed a run successfully. Powers last_run lookups."""
    state = _read_state()
    state.setdefault("last_run", {})[name] = _now_iso()
    _write_state(state)


def last_run(name: str) -> str | None:
    state = _read_state()
    return state.get("last_run", {}).get(name)


def snapshot() -> dict:
    """Return the full state for diagnostic / /check-system reporting."""
    return _read_state()


# ── CLI entry points for /check-system + diagnostics ─────────────────────────

def _cli_status() -> int:
    """Print a one-screen view of system state."""
    state = _read_state()
    print("─── Processes currently running ─────────────────────")
    procs = state.get("processes", {})
    if not procs:
        print("  (none)")
    for name, info in sorted(procs.items()):
        print(f"  {name:30}  pid={info.get('pid')}  op={info.get('op','')}  since {info.get('started_at')}")
    print()
    print("─── Locks held ──────────────────────────────────────")
    locks = state.get("locks", {})
    now = time.time()
    if not locks:
        print("  (none)")
    for res, info in sorted(locks.items()):
        stale = " STALE" if _is_stale(info, now) else ""
        print(f"  {res:30}  holder={info.get('holder')}  since {info.get('acquired_at')}{stale}")
    print()
    print("─── Last successful runs ────────────────────────────")
    runs = state.get("last_run", {})
    if not runs:
        print("  (none)")
    for name, ts in sorted(runs.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {name:30}  {ts}")
    return 0


def _cli_clear_stale() -> int:
    """Remove stale locks. Useful after a crash."""
    state = _read_state()
    locks = state.get("locks", {})
    now = time.time()
    removed = [r for r, info in locks.items() if _is_stale(info, now)]
    for r in removed:
        del locks[r]
    if removed:
        _write_state(state)
    print(f"Removed {len(removed)} stale lock(s): {', '.join(removed) or '(none)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="System coordination state inspector")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Show running processes + locks + last_run")
    sub.add_parser("clear-stale", help="Remove stale locks")
    args = p.parse_args(argv)
    if args.cmd == "status":
        return _cli_status()
    if args.cmd == "clear-stale":
        return _cli_clear_stale()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
