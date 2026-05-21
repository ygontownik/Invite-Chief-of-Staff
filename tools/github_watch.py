#!/usr/bin/env python3
"""
github_watch.py — Weekly GitHub scout.

Reads ~/dashboards/config/repos-to-watch.yaml, hits the GitHub REST API
for each repo's recent releases + commits, writes a consolidated report
to ~/dashboards/data/compiled/github-intelligence.json.

Scheduled by ~/Library/LaunchAgents/com.<principal>.github-watch.plist
(Sunday 07:00). LaunchAgent is installed-but-disabled until the user
flips RunAtLoad → true or runs `launchctl load …`.

Outputs JSON shape:
    {
      "ts": "<ISO 8601 UTC>",
      "lookback_days": 7,
      "repos": [
        {
          "repo": "owner/name",
          "group": "claude",
          "reason": "...",
          "stars": 12345,
          "releases": [{"tag_name": ..., "name": ..., "published_at": ..., "url": ...}],
          "recent_commits": int,
          "top_commit_messages": [str, ...],
          "verdict": "release" | "active" | "quiet"
        }, ...
      ]
    }
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("github_watch: PyYAML not installed; run `pip3 install pyyaml`.", file=sys.stderr)
    raise SystemExit(2)

HOME = Path.home()
CONFIG_PATH = HOME / "dashboards" / "config" / "repos-to-watch.yaml"
DEFAULT_OUTPUT = HOME / "dashboards" / "data" / "compiled" / "github-intelligence.json"

API_BASE = "https://api.github.com"


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _gh_get(url: str, token: str | None) -> Any:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-watch-scout/1.0",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "_url": url}
    except Exception as e:
        return {"_error": str(e), "_url": url}


def _normalize_entries(group_data: Any, group_name: str) -> list[dict[str, Any]]:
    """Each entry in a group block can be a string (owner/repo) or a dict."""
    out: list[dict[str, Any]] = []
    if not isinstance(group_data, list):
        return out
    for entry in group_data:
        if isinstance(entry, str):
            out.append({"repo": entry, "group": group_name, "reason": ""})
        elif isinstance(entry, dict) and "repo" in entry:
            d = dict(entry)
            d.setdefault("group", group_name)
            d.setdefault("reason", "")
            out.append(d)
    return out


def _scan_repo(entry: dict, since_iso: str, token: str | None) -> dict[str, Any]:
    repo = entry["repo"]
    meta = _gh_get(f"{API_BASE}/repos/{repo}", token)
    if "_error" in meta:
        return {
            "repo": repo,
            "group": entry["group"],
            "reason": entry["reason"],
            "verdict": "error",
            "error": meta["_error"],
        }

    stars = meta.get("stargazers_count", 0)
    description = meta.get("description") or ""

    releases = _gh_get(f"{API_BASE}/repos/{repo}/releases?per_page=5", token)
    fresh_releases: list[dict[str, Any]] = []
    if isinstance(releases, list):
        for rel in releases:
            published = rel.get("published_at")
            if published and published >= since_iso:
                fresh_releases.append({
                    "tag_name": rel.get("tag_name"),
                    "name": rel.get("name"),
                    "published_at": published,
                    "url": rel.get("html_url"),
                    "body_excerpt": (rel.get("body") or "")[:300],
                })

    recent_commits = 0
    top_commit_messages: list[str] = []
    if not entry.get("skip_commits"):
        commits = _gh_get(
            f"{API_BASE}/repos/{repo}/commits?since={since_iso}&per_page=30", token
        )
        if isinstance(commits, list):
            recent_commits = len(commits)
            top_commit_messages = [
                (c.get("commit", {}).get("message") or "").splitlines()[0][:140]
                for c in commits[:5]
            ]

    if fresh_releases:
        verdict = "release"
    elif recent_commits >= 3:
        verdict = "active"
    else:
        verdict = "quiet"

    return {
        "repo": repo,
        "group": entry["group"],
        "reason": entry["reason"],
        "description": description,
        "stars": stars,
        "releases": fresh_releases,
        "recent_commits": recent_commits,
        "top_commit_messages": top_commit_messages,
        "verdict": verdict,
    }


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise SystemExit(f"github_watch: config not found at {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text())


def _flatten_targets(config: dict) -> list[dict[str, Any]]:
    watch = config.get("watch") or {}
    out: list[dict[str, Any]] = []
    for group_name, entries in watch.items():
        out.extend(_normalize_entries(entries, group_name))
    return out


def main() -> int:
    config = _load_config()
    cfg = config.get("config") or {}
    lookback = int(cfg.get("lookback_days", 7))
    min_commits = int(cfg.get("min_commits_for_inclusion", 0))
    out_rel = cfg.get("output_path") or str(DEFAULT_OUTPUT.relative_to(HOME))
    out_path = HOME / out_rel

    token_env = cfg.get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(token_env) or None

    targets = _flatten_targets(config)
    if not targets:
        print("github_watch: no repos configured", file=sys.stderr)
        return 1

    since = _now_utc() - _dt.timedelta(days=lookback)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    repos: list[dict[str, Any]] = []
    for entry in targets:
        result = _scan_repo(entry, since_iso, token)
        if result["verdict"] == "quiet" and result.get("recent_commits", 0) < min_commits:
            # Still include in the report so the user knows it was scanned,
            # but the dashboard tile filter can hide quiet repos by default.
            result["dashboard_filter"] = "hidden_by_min_commits"
        repos.append(result)

    counts = {
        "release": sum(1 for r in repos if r["verdict"] == "release"),
        "active": sum(1 for r in repos if r["verdict"] == "active"),
        "quiet": sum(1 for r in repos if r["verdict"] == "quiet"),
        "error": sum(1 for r in repos if r["verdict"] == "error"),
        "total": len(repos),
    }

    report = {
        "ts": _now_utc().isoformat(timespec="seconds"),
        "lookback_days": lookback,
        "since": since_iso,
        "counts": counts,
        "token_auth": bool(token),
        "repos": repos,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"github_watch: scanned {counts['total']} repos · "
        f"{counts['release']} releases · {counts['active']} active · "
        f"{counts['quiet']} quiet · {counts['error']} errors"
    )
    print(f"  report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
