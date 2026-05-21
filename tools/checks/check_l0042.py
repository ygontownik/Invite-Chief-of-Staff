#!/usr/bin/env python3
"""check_l0042.py — Rule L0042: chrome-devtools-mcp config in settings.json.

Claude Code does NOT honor args inside a plugin-bundled .mcp.json file.
The chrome-devtools-mcp config (browser-url, user-data-dir, etc.) must
live in ~/.claude/settings.json under mcpServers, or it's silently
ignored and the MCP launches with default args (wrong Chrome profile,
no remote debugging port).

Check logic:
  - ~/.claude/settings.json must exist
  - mcpServers must contain a chrome-devtools key (also accept the legacy
    `chrome-devtools-mcp` alias)
  - the entry must have an `args` array (i.e. caller is actively
    parameterizing the launch — not relying on default args)

Status:
  pass — settings.json has chrome-devtools entry with args
  warn — entry present but args is empty
  fail — entry not found in settings.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HOME = Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
LOCAL_SETTINGS = HOME / ".claude" / "settings.local.json"
PLUGIN_CACHE = HOME / ".claude" / "plugins" / "cache"

# Accept both the canonical key and the legacy alias.
_CDT_KEYS = ("chrome-devtools", "chrome-devtools-mcp")
_NAME = "L0042: chrome-devtools-mcp settings location"
_REF = "L0042"


def _r(status: str, summary: str, **details: Any) -> dict[str, Any]:
    return {"name": _NAME, "rule_ref": _REF, "status": status,
            "summary": summary, "details": details}


def _check_settings_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"unreadable {path.name}: {exc}"}
    servers = (data or {}).get("mcpServers") or {}
    for key in _CDT_KEYS:
        if key in servers:
            entry = servers[key] or {}
            return {
                "path": str(path),
                "key": key,
                "args": entry.get("args") or [],
                "command": entry.get("command"),
            }
    return {"path": str(path), "key": None}


def _find_plugin_mcp_jsons() -> list[str]:
    """Find any plugin .mcp.json that declares chrome-devtools args. These are
    silently ignored by Claude Code, but worth noting in details."""
    found: list[str] = []
    if not PLUGIN_CACHE.exists():
        return found
    for p in PLUGIN_CACHE.rglob(".mcp.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        servers = (data or {}).get("mcpServers") or {}
        if any(k in servers for k in _CDT_KEYS):
            found.append(str(p))
    return found


def run() -> dict[str, Any]:
    # Check primary settings.json first, then settings.local.json fallback.
    primary = _check_settings_file(SETTINGS)
    local = _check_settings_file(LOCAL_SETTINGS)

    if primary is None and local is None:
        return _r("fail", f"settings.json not found at {SETTINGS}",
                  searched=[str(SETTINGS), str(LOCAL_SETTINGS)])

    found_entry = next((c for c in (primary, local) if c and c.get("key")), None)
    plugin_mcp_files = _find_plugin_mcp_jsons()

    if not found_entry:
        return _r("fail",
                  "L0042: no chrome-devtools entry in ~/.claude/settings.json "
                  f"(but {len(plugin_mcp_files)} plugin .mcp.json file(s) define one — "
                  "those are ignored by Claude Code)",
                  settings_path=str(SETTINGS), plugin_mcp_files=plugin_mcp_files[:5])

    args = found_entry.get("args") or []
    fname = Path(found_entry["path"]).name
    if not args:
        return _r("warn",
                  f"L0042: chrome-devtools entry found in {fname} but args[] empty",
                  settings_path=found_entry["path"], mcp_key=found_entry["key"],
                  args=args, command=found_entry.get("command"),
                  plugin_mcp_files_ignored=plugin_mcp_files[:5])
    return _r("pass",
              f"L0042: chrome-devtools entry found in {fname} with {len(args)} arg(s); "
              f"{len(plugin_mcp_files)} plugin .mcp.json file(s) coexist (harmlessly ignored)",
              settings_path=found_entry["path"], mcp_key=found_entry["key"],
              args=args, command=found_entry.get("command"),
              plugin_mcp_files_ignored=plugin_mcp_files[:5])


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
