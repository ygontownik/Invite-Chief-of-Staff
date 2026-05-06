"""_secrets.py — credential storage abstraction (BOOTSTRAP_PLAN Build #2).

Single interface for retrieving secrets across deployment topologies.
Today's backends: macOS keychain (default), env vars (fallback).
Future: Linux keyring, AWS Secrets Manager, GCP Secret Manager — added by
implementing a new _load_<backend>() and registering in _detect_backend().

Usage:
    import _secrets
    api_key = _secrets.load_secret("ANTHROPIC_API_KEY")

The helper resolves through both active and inactive backends so a key
stored in either location is found. This eases dev/prod parity (set env
locally, keychain in production) and migration (move keys from env to
keychain without code changes).

Backend selection:
  - $COS_SECRETS_BACKEND=keychain | env (explicit override)
  - else: keychain on Mac (security CLI present), env elsewhere

Keychain entry shape (matches setup_keychain.sh, the canonical writer):
  service = "<prefix>/<KEY>"   e.g. "cos-pipeline-<slug>/ANTHROPIC_API_KEY"
  account = current $USER       e.g. "<unix-user>"
where <prefix> comes from firm_config.json :: keychain_service_prefix
(falls back to "cos-pipeline"). This shape lets multi-tenant installs
coexist in one login keychain without account collisions.
"""
import getpass
import os
import subprocess
from typing import Optional


def _have_security_cmd() -> bool:
    """True if macOS `security` CLI is available."""
    try:
        result = subprocess.run(
            ['security', 'help'],
            capture_output=True, timeout=2,
        )
        return result.returncode in (0, 1)  # `security help` returns 1 on macOS
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _detect_backend() -> str:
    """Pick the active credential backend.

    Order: $COS_SECRETS_BACKEND override → 'keychain' if available → 'env'.
    """
    explicit = os.environ.get('COS_SECRETS_BACKEND', '').strip().lower()
    if explicit in ('keychain', 'env'):
        return explicit
    return 'keychain' if _have_security_cmd() else 'env'


def _service_prefix() -> str:
    """Keychain service prefix for the current tenant.

    Reads from firm_config.json :: keychain_service_prefix (matches what
    setup_keychain.sh writes); falls back to 'cos-pipeline' so manual
    entries created before B6 still resolve.
    """
    try:
        import _firm_context as _fc
        cfg = _fc.load_firm_config()
        prefix = (cfg or {}).get('keychain_service_prefix')
        if prefix:
            return str(prefix)
    except Exception:
        pass
    return 'cos-pipeline'


def _current_user() -> str:
    """Account name for keychain lookups. $USER, then getpass fallback."""
    return os.environ.get('USER') or getpass.getuser()


def _load_keychain(key: str, service: Optional[str] = None) -> Optional[str]:
    """Lookup via macOS keychain. Returns None if not found or unavailable.

    Entry shape: service="<prefix>/<KEY>", account=current $USER. Matches
    setup_keychain.sh; do not change without updating that script too.
    """
    prefix = service or _service_prefix()
    svc = f'{prefix}/{key}'
    acct = _current_user()
    try:
        result = subprocess.run(
            ['security', 'find-generic-password', '-s', svc, '-a', acct, '-w'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.rstrip('\n')
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _load_env(key: str) -> Optional[str]:
    """Lookup via process env. Tries the key as-is, then UPPER_SNAKE_CASE.

    e.g. load_secret('anthropic-api-key') matches both:
      - $anthropic-api-key (literal)
      - $ANTHROPIC_API_KEY (UPPER_SNAKE)
    """
    val = os.environ.get(key)
    if val:
        return val
    upper = key.replace('-', '_').upper()
    if upper != key:
        val = os.environ.get(upper)
        if val:
            return val
    return None


def load_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a secret by key.

    Lookup chain (always exhausted before returning default):
      1. Active backend (keychain on Mac, env elsewhere).
      2. Cross-backend fallback (keychain ↔ env).
      3. Default value.

    Returns the resolved string or `default` if nothing matches.
    """
    backend = _detect_backend()
    if backend == 'keychain':
        val = _load_keychain(key)
        if val is None:
            val = _load_env(key)
    else:
        val = _load_env(key)
        if val is None:
            val = _load_keychain(key)
    return val if val is not None else default


def _store_keychain(key: str, value: str, service: Optional[str] = None) -> bool:
    """Write a secret to the keychain. Updates if entry exists (`-U` flag).

    Entry shape: service="<prefix>/<KEY>", account=current $USER. Matches
    setup_keychain.sh; do not change without updating that script too.
    """
    prefix = service or _service_prefix()
    svc = f'{prefix}/{key}'
    acct = _current_user()
    try:
        subprocess.run(
            ['security', 'add-generic-password',
             '-s', svc, '-a', acct, '-w', value, '-U'],
            capture_output=True, check=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired,
            subprocess.CalledProcessError, OSError):
        return False


def store_secret(key: str, value: str) -> bool:
    """Store a secret via the active backend. Returns True on success.

    Env-backend "store" is unsupported — env vars are inherited from the
    shell or LaunchAgent plist. Set them through bootstrap.sh which knows
    the deployment topology.
    """
    backend = _detect_backend()
    if backend == 'keychain':
        return _store_keychain(key, value)
    raise NotImplementedError(
        "store_secret() requires the keychain backend. "
        "On hosts without keychain, set the env var via bootstrap or systemd."
    )


# Diagnostic CLI: `python3 _secrets.py probe ANTHROPIC_API_KEY`
if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == 'probe':
        target = sys.argv[2]
        backend = _detect_backend()
        prefix = _service_prefix()
        val = load_secret(target)
        masked = ('<unset>' if val is None
                  else val[:4] + '…' + val[-4:] if len(val) > 8
                  else '<short>')
        print(f'backend={backend} service={prefix} key={target} value={masked}')
    else:
        print(f'Usage: python3 _secrets.py probe <KEY>')
        print(f'Active backend: {_detect_backend()}')
        print(f'Service prefix: {_service_prefix()}')
