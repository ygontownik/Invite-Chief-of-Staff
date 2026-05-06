"""Multi-tenant scaffolding for the COS pipeline.

Pure-Python helpers for mapping a tenant *slug* (short name like ``acme`` or
``re-dev``) onto the per-tenant resources defined by the run-2 plan and
DECISIONS log:

    slug          -> port             (C6 / J1)
    slug.routine  -> LaunchAgent label (C7)
    slug          -> keychain service  (C11)
    slug          -> data dir          (J3)
    slug          -> logs dir          (J3)
    slug          -> config repo       (~/cos-pipeline-config-<slug>)

This module does **no I/O** beyond ``Path.exists()`` / ``Path.iterdir()`` for
``list_known_tenants`` — it never creates files, never binds ports, and never
talks to Keychain or LaunchAgents. Instantiation is intentionally deferred to
the onboarding script (see ``J_SETUP.md``).

Examples
--------
>>> slug_to_port("acme")
7777
>>> slug_to_port("re-dev")
7778
>>> launchagent_label("re-dev", "morning-briefing")
'com.cos.re-dev.morning-briefing'
>>> keychain_service("acme")
'cos-pipeline-acme'

Run as a script for a self-test::

    python3 ~/cos-pipeline/multi_tenant.py
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import List

# ── Constants ────────────────────────────────────────────────────────────────

HOME = Path.home()

#: Reserved fixed-port assignments (per DECISIONS C6 / PLAN J1).
#: Port 7777 is the default for the primary tenant; "re-dev" reserves 7778
#: for a development/staging clone. Tenants not listed here get hash-allocated ports.
RESERVED_PORTS: dict[str, int] = {
    "re-dev": 7778,
}

#: First port for hash-based allocation. Reserved range is 7777-7778, so
#: dynamic tenants start at 7779.
DYNAMIC_PORT_START = 7779
#: Inclusive ceiling for dynamic ports. 7777 + 200 leaves comfortable headroom
#: while staying inside the user-space bracket and well clear of common dev
#: ports (8000, 8080, 8443, 5000).
DYNAMIC_PORT_END = 7977

#: Slugs that must never be used as a tenant identifier — would collide with
#: shared infrastructure paths (``data-shared/``, etc.).
RESERVED_SLUGS = frozenset({"shared", "all", "default", "common", "template"})

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


# ── Slug validation ──────────────────────────────────────────────────────────


def validate_slug(slug: str) -> None:
    """Raise ``ValueError`` if *slug* is not a legal tenant short-name.

    Rules: lowercase ASCII, leading letter, only ``[a-z0-9-]``, no spaces,
    not in ``RESERVED_SLUGS``.
    """
    if not isinstance(slug, str):
        raise ValueError(f"slug must be a string, got {type(slug).__name__}")
    if not slug:
        raise ValueError("slug must be non-empty")
    if slug != slug.lower():
        raise ValueError(f"slug must be lowercase: {slug!r}")
    if " " in slug or "\t" in slug:
        raise ValueError(f"slug must not contain whitespace: {slug!r}")
    if slug[0].isdigit():
        raise ValueError(f"slug must not start with a digit: {slug!r}")
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"slug must match {_SLUG_RE.pattern}: {slug!r} "
            "(lowercase letters, digits, hyphens; must start with a letter)"
        )
    if slug in RESERVED_SLUGS:
        raise ValueError(f"slug is reserved: {slug!r}")


# ── Path helpers ─────────────────────────────────────────────────────────────


def port_registry_path() -> Path:
    """Return the canonical path to the dynamic port registry JSON.

    The file itself is **not** created here — callers (the onboarding script)
    own its lifecycle. Format on disk is ``{"<slug>": <int port>, ...}``.
    """
    return HOME / "cos-pipeline" / "data-shared" / "tenant-ports.json"


def tenant_data_dir(slug: str) -> Path:
    """Return ``~/cos-pipeline/data-<slug>`` (per PLAN J3)."""
    validate_slug(slug)
    return HOME / "cos-pipeline" / f"data-{slug}"


def tenant_logs_dir(slug: str) -> Path:
    """Return ``~/cos-pipeline/logs-<slug>`` (per PLAN J3)."""
    validate_slug(slug)
    return HOME / "cos-pipeline" / f"logs-{slug}"


def tenant_config_repo(slug: str) -> Path:
    """Return ``~/cos-pipeline-config-<slug>`` (per PLAN J2)."""
    validate_slug(slug)
    return HOME / f"cos-pipeline-config-{slug}"


# ── Identifier helpers ───────────────────────────────────────────────────────


def launchagent_label(slug: str, routine: str) -> str:
    """Return ``com.cos.<slug>.<routine>`` (per DECISIONS C7).

    *routine* should match an existing scheduled-task name
    (e.g. ``morning-briefing``, ``cos-capture``). Validated only loosely:
    must be non-empty and contain no whitespace or dots.
    """
    validate_slug(slug)
    if not routine or " " in routine or "." in routine or "\t" in routine:
        raise ValueError(
            f"routine must be non-empty and contain no spaces/dots: {routine!r}"
        )
    return f"com.cos.{slug}.{routine}"


def keychain_service(slug: str) -> str:
    """Return ``cos-pipeline-<slug>`` (per DECISIONS C11)."""
    validate_slug(slug)
    return f"cos-pipeline-{slug}"


# ── Port allocation ──────────────────────────────────────────────────────────


def _load_port_registry() -> dict[str, int]:
    p = port_registry_path()
    if not p.exists():
        return {}
    try:
        with p.open("r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}


def _hash_port(slug: str) -> int:
    """Deterministic hash → port in [DYNAMIC_PORT_START, DYNAMIC_PORT_END]."""
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    span = DYNAMIC_PORT_END - DYNAMIC_PORT_START + 1
    return DYNAMIC_PORT_START + (int.from_bytes(digest[:4], "big") % span)


def slug_to_port(slug: str) -> int:
    """Return the dashboard port for *slug*.

    - ``re-dev`` → 7778 (reserved for staging/dev clone, per DECISIONS C6).
    - Otherwise: hash-based starting at ``DYNAMIC_PORT_START`` (7779) with
      collision check against the on-disk registry plus the reserved table.
      Linear probing within the dynamic range until a free port is found.
    """
    validate_slug(slug)
    if slug in RESERVED_PORTS:
        return RESERVED_PORTS[slug]

    registry = _load_port_registry()
    # Idempotent: if this slug already has a port assigned, return it.
    if slug in registry:
        return registry[slug]

    used: set[int] = set(RESERVED_PORTS.values()) | set(registry.values())
    candidate = _hash_port(slug)
    span = DYNAMIC_PORT_END - DYNAMIC_PORT_START + 1
    for i in range(span):
        port = DYNAMIC_PORT_START + ((candidate - DYNAMIC_PORT_START + i) % span)
        if port not in used:
            return port
    raise RuntimeError(
        f"No free port in [{DYNAMIC_PORT_START}, {DYNAMIC_PORT_END}] "
        f"for slug {slug!r}; registry has {len(used)} entries"
    )


# ── Discovery ────────────────────────────────────────────────────────────────


def list_known_tenants() -> List[str]:
    """Return sorted list of slugs by scanning ``~`` for ``cos-pipeline-config-*``.

    The bare ``cos-pipeline-config`` directory (no slug suffix) is the legacy
    multi-tenant-unaware layout and is *excluded* from this list.
    """
    slugs: list[str] = []
    prefix = "cos-pipeline-config-"
    if not HOME.exists():
        return []
    for entry in HOME.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if not name.startswith(prefix):
            continue
        slug = name[len(prefix):]
        if not slug:
            continue
        try:
            validate_slug(slug)
        except ValueError:
            # Skip dirs whose suffix isn't a legal slug (e.g. backups).
            continue
        slugs.append(slug)
    return sorted(slugs)


# ── Self-test ────────────────────────────────────────────────────────────────


def _selftest() -> None:
    print("multi_tenant.py self-test")
    print("=" * 60)
    for slug in ("acme", "re-dev"):
        print(f"\nslug = {slug!r}")
        print(f"  port              : {slug_to_port(slug)}")
        print(f"  data dir          : {tenant_data_dir(slug)}")
        print(f"  logs dir          : {tenant_logs_dir(slug)}")
        print(f"  config repo       : {tenant_config_repo(slug)}")
        print(f"  keychain service  : {keychain_service(slug)}")
        print(f"  LA label (capture): {launchagent_label(slug, 'cos-capture')}")
    print(f"\nport registry path  : {port_registry_path()}")
    print(f"known tenants on disk: {list_known_tenants()}")


if __name__ == "__main__":
    _selftest()
