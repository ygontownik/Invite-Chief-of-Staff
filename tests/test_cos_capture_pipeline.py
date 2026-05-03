"""B3 test: cos_capture_pipeline.py.next resolves _LOG_DIR per-tenant from firm_context."""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _track_b_helpers import (
    make_tenant_config, isolated_env, restore_env, fresh_import,
)


class TestCosCapturePipelineNext(unittest.TestCase):
    def test_log_dir_uses_tenant_slug(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = make_tenant_config(Path(td))
            prior = isolated_env(tmp)
            try:
                mod = fresh_import(REPO / "cos_capture_pipeline.py.next",
                                   "cos_capture_pipeline_next")
                # firm_context.yaml in helpers sets tenant_slug: testtenant
                self.assertEqual(
                    mod._LOG_DIR,
                    Path.home() / "cos-pipeline" / "logs-testtenant",
                )
                # Public callable preserved
                self.assertTrue(callable(mod.main))
            finally:
                restore_env(prior)


if __name__ == "__main__":
    unittest.main(verbosity=2)
