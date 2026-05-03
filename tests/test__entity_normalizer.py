"""B4 test: _entity_normalizer.py.next builds VAGUE_PATTERNS from firm_context, not yoni|mark|nik."""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _track_b_helpers import (
    make_tenant_config, isolated_env, restore_env, fresh_import,
)


class TestEntityNormalizerNext(unittest.TestCase):
    def test_owner_pattern_uses_tenant_roster(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = make_tenant_config(Path(td))
            prior = isolated_env(tmp)
            try:
                mod = fresh_import(REPO / "_entity_normalizer.py.next",
                                   "_entity_normalizer_next")
                # First pattern is the owner-possessive — should match "Test's buddy"
                # (from owner_whitelist in helpers) and NOT match "yoni's buddy"
                first_pat = mod.VAGUE_PATTERNS[0]
                self.assertIsNotNone(first_pat.search("Test's buddy at X"))
                self.assertIsNotNone(first_pat.search("Codealee's buddy at X"))
                self.assertIsNone(first_pat.search("yoni's buddy at X"))
                self.assertIsNone(first_pat.search("mark's buddy at X"))
                # Pattern source must NOT contain hardcoded tenant names
                pat_src = first_pat.pattern.lower()
                self.assertNotIn("yoni", pat_src)
                self.assertNotIn("mark", pat_src)
                self.assertNotIn("nik", pat_src)
                # Public class signature preserved
                self.assertTrue(hasattr(mod, "EntityNormalizer"))
                self.assertTrue(hasattr(mod, "ResolvedEntity"))
            finally:
                restore_env(prior)


if __name__ == "__main__":
    unittest.main(verbosity=2)
