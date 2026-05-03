"""B2 test: cos_gmail_mini_v2.py.next requires firm_config.json :: docs."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _track_b_helpers import (
    make_tenant_config, isolated_env, restore_env, fresh_import,
)


class TestCosGmailMiniNext(unittest.TestCase):
    def test_module_loads_and_load_config_returns_docs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = make_tenant_config(Path(td))
            prior = isolated_env(tmp)
            try:
                mod = fresh_import(REPO / "cos_gmail_mini_v2.py.next",
                                   "cos_gmail_mini_v2_next")
                # DEFAULT_CONFIG no longer carries hardcoded doc IDs
                self.assertEqual(mod.DEFAULT_CONFIG["docs"], {})
                self.assertEqual(mod.DEFAULT_CONFIG["research_senders"], {})
                # load_config picks them up from firm_config.json
                cfg = mod.load_config()
                self.assertEqual(cfg["docs"]["followups"],  "TEST_FOLLOWUPS_DOC")
                self.assertEqual(cfg["docs"]["pipeline"],   "TEST_PIPELINE_DOC")
                self.assertEqual(cfg["docs"]["recruiting"], "TEST_RECRUITING_DOC")
                self.assertEqual(cfg["docs"]["people"],     "TEST_PEOPLE_DOC")
                self.assertEqual(cfg["firm_name"], "Test Firm Partners")
                # Public callable preserved
                self.assertTrue(callable(mod.load_config))
                self.assertTrue(callable(mod.load_state))
            finally:
                restore_env(prior)


if __name__ == "__main__":
    unittest.main(verbosity=2)
