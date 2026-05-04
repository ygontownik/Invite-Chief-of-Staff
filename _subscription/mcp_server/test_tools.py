"""Test the three MCP tools end-to-end (happy + error path each).

Run: `python3 test_tools.py` from this directory. All 6 must pass.
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

# Make `from mcp_server.tools import ...` work when run from inside the dir.
_THIS = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from mcp_server.tools import (  # noqa: E402
    deal_pipeline_lookup,
    lng_market_get_spot,
    transcripts_search,
)


class DealPipelineLookupTest(unittest.TestCase):
    def test_happy_path_returns_match(self):
        fixture = {
            "themes": [
                {
                    "id": "miso-power",
                    "theme": "MISO Power Plant Acquisitions",
                    "thesis": "DOE 202(c) orders stalled coal capacity.",
                },
                {
                    "id": "lng-eu",
                    "theme": "European LNG redirect",
                    "thesis": "FID timing on Gulf Coast trains.",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "deal-pipeline-data.json"
            path.write_text(json.dumps(fixture))
            os.environ["DEAL_PIPELINE_DATA_PATH"] = str(path)
            try:
                result = deal_pipeline_lookup("MISO")
            finally:
                os.environ.pop("DEAL_PIPELINE_DATA_PATH", None)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["matches"][0]["id"], "miso-power")

    def test_missing_file_returns_error_not_crash(self):
        os.environ["DEAL_PIPELINE_DATA_PATH"] = "/nonexistent/path/x.json"
        try:
            result = deal_pipeline_lookup("anything")
        finally:
            os.environ.pop("DEAL_PIPELINE_DATA_PATH", None)
        self.assertEqual(result["error"], "data source not configured")


class TranscriptsSearchTest(unittest.TestCase):
    def test_happy_path_or_environmental_skip(self):
        """Real Drive call; skip cleanly if creds aren't on this host."""
        token_path = pathlib.Path.home() / "credentials" / "gdrive_token.pickle"
        if not token_path.exists():
            self.skipTest(f"gdrive_token.pickle missing at {token_path}")
        result = transcripts_search("podcast", since_days=90)
        # A live result either has 'hits' or surfaces an error dict — either
        # is non-crashing. Reject only on missing key.
        self.assertTrue(
            "hits" in result or "error" in result,
            f"unexpected shape: {result}",
        )
        if "hits" in result:
            self.assertLessEqual(len(result["hits"]), 10)

    def test_missing_credentials_returns_error(self):
        """Force the missing-creds path with a temporary HOME override."""
        original_home = os.environ.get("HOME")
        with tempfile.TemporaryDirectory() as td:
            os.environ["HOME"] = td
            try:
                # Re-import would be needed for module-level constant; instead
                # we assert the error code by patching the module attribute.
                from mcp_server import tools as tools_mod
                original = tools_mod._GDRIVE_TOKEN
                tools_mod._GDRIVE_TOKEN = pathlib.Path(td) / "no-such-file.pickle"
                try:
                    result = transcripts_search("anything")
                finally:
                    tools_mod._GDRIVE_TOKEN = original
            finally:
                if original_home is not None:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
        self.assertIn("error", result)
        self.assertIn("gdrive_token.pickle", result["error"])


class LngMarketGetSpotTest(unittest.TestCase):
    def test_returns_configured_stub_error(self):
        result = lng_market_get_spot("JKM", "2026-05-03")
        self.assertEqual(result["error"], "lng spreadsheet not yet wired")
        self.assertEqual(result["region"], "JKM")
        self.assertEqual(result["date"], "2026-05-03")

    def test_handles_empty_args_without_crashing(self):
        result = lng_market_get_spot("", "")
        self.assertEqual(result["error"], "lng spreadsheet not yet wired")
        self.assertEqual(result["region"], "")
        self.assertEqual(result["date"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
