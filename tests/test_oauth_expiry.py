"""test_oauth_expiry.py — unit tests for next/track-E/oauth_expiry.py.next.

Run:
    python3 ~/cos-pipeline/tests/test_oauth_expiry.py

Read-only safe — operates entirely in a tempdir. Never touches
~/credentials/. Skips the pickle test if google.oauth2.credentials is
not importable (do NOT install dependencies).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import pickle
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load the .next module by file path (it's not on sys.path).
HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "next" / "track-E" / "oauth_expiry.py.next"


def _load_module():
    # Filename ends in ``.py.next`` so importlib won't pick a loader by
    # extension — supply the SourceFileLoader explicitly.
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("oauth_expiry_next", str(MODULE_PATH))
    spec = importlib.util.spec_from_loader("oauth_expiry_next", loader)
    if spec is None:
        raise RuntimeError(f"could not load module at {MODULE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


oe = _load_module()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_jwt(exp_dt: datetime) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"exp": int(exp_dt.timestamp())}).encode())
    sig = _b64url(b"sig")
    return f"{header}.{payload}.{sig}"


class TestOAuthExpiry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # ------- JWT path -----------------------------------------------------

    def test_jwt_warning(self):
        exp = datetime.now(timezone.utc) + timedelta(days=5)
        p = self.dir / "jwt_warn.txt"
        p.write_text(_make_jwt(exp))
        r = oe.check_token_expiry(p)
        self.assertEqual(r["method"], "jwt")
        self.assertEqual(r["status"], "warning")
        self.assertAlmostEqual(r["days_until_expiry"], 5.0, places=1)

    def test_jwt_expired(self):
        exp = datetime.now(timezone.utc) - timedelta(days=2)
        p = self.dir / "jwt_old.txt"
        p.write_text(_make_jwt(exp))
        r = oe.check_token_expiry(p)
        self.assertEqual(r["status"], "expired")
        self.assertLess(r["days_until_expiry"], 0)

    def test_jwt_ok(self):
        exp = datetime.now(timezone.utc) + timedelta(days=30)
        p = self.dir / "jwt_ok.txt"
        p.write_text(_make_jwt(exp))
        r = oe.check_token_expiry(p)
        self.assertEqual(r["status"], "ok")

    # ------- JSON path ----------------------------------------------------

    def test_json_iso_expiry(self):
        exp = datetime.now(timezone.utc) + timedelta(days=1)
        p = self.dir / "token.json"
        # Strip tz to mimic google-auth's naive UTC ``expiry`` field.
        p.write_text(json.dumps({"expiry": exp.replace(tzinfo=None).isoformat()}))
        r = oe.check_token_expiry(p)
        self.assertEqual(r["method"], "json")
        self.assertEqual(r["status"], "warning")

    def test_json_expires_at_epoch(self):
        future = time.time() + 86400 * 100
        p = self.dir / "ms_token.json"
        p.write_text(json.dumps({"expires_at": future}))
        r = oe.check_token_expiry(p)
        self.assertEqual(r["method"], "json")
        self.assertEqual(r["status"], "ok")

    def test_json_expiration_epoch_ms(self):
        future_ms = int((time.time() + 86400 * 3) * 1000)
        p = self.dir / "gcal_watch_channels.json"
        p.write_text(json.dumps({"expiration": future_ms}))
        r = oe.check_token_expiry(p)
        self.assertEqual(r["status"], "warning")
        self.assertLessEqual(r["days_until_expiry"], 4)

    def test_json_id_token_jwt_fallback(self):
        # JSON file with no expiry field but with a JWT id_token.
        exp = datetime.now(timezone.utc) + timedelta(days=10)
        body = {"id_token": _make_jwt(exp), "scope": "openid"}
        p = self.dir / "ms_token.json"
        p.write_text(json.dumps(body))
        r = oe.check_token_expiry(p)
        self.assertEqual(r["status"], "warning")
        self.assertEqual(r["method"], "jwt")

    # ------- Missing-file + unknown -------------------------------------

    def test_missing_file(self):
        r = oe.check_token_expiry(self.dir / "nope.json")
        self.assertEqual(r["status"], "unknown")
        self.assertIn("error", r)

    def test_json_no_expiry_field(self):
        p = self.dir / "weird.json"
        p.write_text(json.dumps({"hello": "world"}))
        r = oe.check_token_expiry(p)
        self.assertEqual(r["status"], "unknown")

    # ------- scan + format_warnings -------------------------------------

    def test_scan_and_warnings(self):
        # Mix of files: one expired, one warning, one ok, one unrelated.
        (self.dir / "token.json").write_text(json.dumps(
            {"expires_at": time.time() + 86400 * 5}))           # warning
        (self.dir / "ms_token.json").write_text(json.dumps(
            {"expires_at": time.time() - 86400 * 1}))           # expired
        (self.dir / "gcal_token.json").write_text(json.dumps(
            {"expires_at": time.time() + 86400 * 60}))          # ok
        (self.dir / "README.md").write_text("not a token")      # ignored
        results = oe.scan_credentials_dir(self.dir)
        statuses = {Path(r["path"]).name: r["status"] for r in results}
        self.assertEqual(statuses.get("token.json"), "warning")
        self.assertEqual(statuses.get("ms_token.json"), "expired")
        self.assertEqual(statuses.get("gcal_token.json"), "ok")
        self.assertNotIn("README.md", statuses)
        warnings = oe.format_warnings(results)
        joined = "\n".join(warnings)
        self.assertIn("ms_token.json", joined)
        self.assertIn("token.json", joined)
        self.assertNotIn("gcal_token.json", joined)

    def test_scan_nonexistent_dir(self):
        r = oe.scan_credentials_dir(self.dir / "does-not-exist")
        self.assertEqual(r, [])

    # ------- pickle (skipped if google.oauth2 missing) -----------------

    def test_pickle_with_google_oauth(self):
        try:
            from google.oauth2.credentials import Credentials  # type: ignore
        except Exception:
            self.skipTest("google.oauth2.credentials not importable; skipping pickle test")
        # Build a real Credentials object with a future expiry.
        exp_dt = datetime.utcnow() + timedelta(days=7)  # naive UTC, like google-auth
        creds = Credentials(
            token="fake-access",
            refresh_token="fake-refresh",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="x",
            client_secret="y",
        )
        creds.expiry = exp_dt
        p = self.dir / "gdrive_token.pickle"
        with open(p, "wb") as f:
            pickle.dump(creds, f)
        r = oe.check_token_expiry(p)
        self.assertEqual(r["method"], "pickle")
        self.assertIn(r["status"], ("warning", "ok"))
        self.assertIsNotNone(r["exp_iso"])

    def test_pickle_fallback_no_dependency(self):
        # If google.oauth2 is unavailable, the module falls back to mtime+90d.
        # If it IS available, the fallback isn't exercised — skip.
        have_google = False
        try:
            import google.oauth2.credentials  # type: ignore  # noqa: F401
            have_google = True
        except ImportError:
            pass
        if have_google:
            self.skipTest("google.oauth2 IS importable; fallback path not exercised")
        p = self.dir / "gdrive_token.pickle"
        p.write_bytes(b"\x80\x04N.")  # any pickle-shaped bytes; fallback ignores body
        r = oe.check_token_expiry(p)
        self.assertEqual(r["method"], "pickle")
        # 90d default → must be "ok".
        self.assertEqual(r["status"], "ok")
        self.assertGreater(r["days_until_expiry"], 80)


if __name__ == "__main__":
    # Unittest's main reads sys.argv; strip our own.
    unittest.main(verbosity=2)
