"""B1 test: cos_personal_briefing.py.next loads from firm_context with no hardcoded IDs."""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _track_b_helpers import (
    make_tenant_config, isolated_env, restore_env, fresh_import,
)


class TestCosPersonalBriefingNext(unittest.TestCase):
    def test_loads_doc_ids_from_drive_docs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = make_tenant_config(Path(td))
            prior = isolated_env(tmp)
            try:
                mod = fresh_import(REPO / "cos_personal_briefing.py.next",
                                   "cos_personal_briefing_next")
                self.assertEqual(mod.DOC_FOLLOWUPS,      "TEST_FOLLOWUPS_DOC")
                self.assertEqual(mod.DOC_RECRUITING,     "TEST_RECRUITING_DOC")
                self.assertEqual(mod.DOC_TOMAC_PIPELINE, "TEST_PIPELINE_DOC")
                self.assertEqual(mod.DOC_MARKET_UPDATE,  "TEST_MARKET_DOC")
                self.assertEqual(mod.DOC_BRIEFING_LOG,   "TEST_BRIEFING_DOC")
                # System prompt is built from firm_context — should mention principal name
                self.assertIn("Test Principal", mod._SYSTEM)
                self.assertIn("Test Firm Partners", mod._SYSTEM)
                # Public functions preserved
                for fn in ("get_google_token", "fetch_doc", "append_to_doc",
                           "call_claude", "trigger_warmup", "main"):
                    self.assertTrue(callable(getattr(mod, fn)), f"missing {fn}")
            finally:
                restore_env(prior)


if __name__ == "__main__":
    unittest.main(verbosity=2)
