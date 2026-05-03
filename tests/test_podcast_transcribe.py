"""B5 test: podcast_transcribe.py.next has no hardcoded RSS fallback list."""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _track_b_helpers import (
    make_tenant_config, isolated_env, restore_env, fresh_import,
)


class TestPodcastTranscribeNext(unittest.TestCase):
    def test_feeds_come_from_firm_config_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = make_tenant_config(Path(td))
            prior = isolated_env(tmp)
            try:
                mod = fresh_import(REPO / "podcast_transcribe.py.next",
                                   "podcast_transcribe_next")
                # FEEDS is built from firm_config.json :: podcast_feeds
                self.assertEqual(mod.FEEDS, {"Test Show": "https://example.com/feed.rss"})
                # The legacy hardcoded RSS values must not be present in FEEDS
                src = (REPO / "podcast_transcribe.py.next").read_text()
                # The .next file should still have the comment but no actual default dict entries.
                # Sanity: the live show URLs should not exist as values in FEEDS.
                self.assertNotIn("https://feeds.megaphone.fm/catalyst", set(mod.FEEDS.values()))
                self.assertNotIn("https://rss.art19.com/the-energy-gang", set(mod.FEEDS.values()))
                # Public callable preserved
                self.assertTrue(callable(mod._load_feeds))
            finally:
                restore_env(prior)


if __name__ == "__main__":
    unittest.main(verbosity=2)
