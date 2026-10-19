import unittest
from external_score_format_v1 import format_external_score

class ExternalScoreTests(unittest.TestCase):
    def test_drj(self):
        msg = format_external_score(dict(system="DRJ", classification="PAPER_ONLY_UNVERIFIED",
                                         wins=1, losses=3, open=1, resolved=4))
        self.assertIn("25.00%", msg)
        self.assertIn("PAPER_ONLY", msg)

    def test_jj_jame(self):
        msg = format_external_score(dict(system="JJ Jame", classification="RESEARCH_UNVERIFIED",
                                         wins=5, losses=10, open=0, resolved=15))
        self.assertIn("33.33%", msg)
        self.assertIn("RESEARCH", msg)

    def test_fail_closed(self):
        base = dict(system="DRJ", classification="PAPER_ONLY_UNVERIFIED",
                    wins=1, losses=0, open=0, resolved=1)
        for changes in ({"wins": True}, {"resolved": 2}, {"classification": "CONFIRMED"},
                        {"open": 2}, {"losses": -1}, {"system": "JJ"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    format_external_score({**base, **changes})

if __name__ == "__main__":
    unittest.main()
