import json
import unittest
from unittest.mock import patch
from external_score_intake_v1 import fetch_score

class Response:
    status = 200
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, n):
        return self.data[:n]

class IntakeTests(unittest.TestCase):
    def test_rejects_insecure_or_ambiguous_url(self):
        for url in ("http://localhost/score", "https://user:pass@example.test/score",
                    "https://example.test/score#fragment", "https:///missing-host"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch_score(url, "x" * 32, "DRJ")

    def test_rejects_short_token(self):
        with self.assertRaises(ValueError):
            fetch_score("https://example.test/score", "weak", "DRJ")

    def test_validated_score(self):
        data = json.dumps(dict(system="DRJ", classification="PAPER_ONLY_UNVERIFIED",
                               wins=2, losses=2, open=0, resolved=4)).encode()
        msg = fetch_score("https://example.test/score", "x" * 32, "DRJ",
                          opener=lambda request, timeout: Response(data))
        self.assertIn("50.00%", msg)

    def test_rejects_wrong_system(self):
        data = json.dumps(dict(system="JJ Jame", classification="RESEARCH_UNVERIFIED",
                               wins=1, losses=0, open=0, resolved=1)).encode()
        with self.assertRaises(ValueError):
            fetch_score("https://example.test/score", "x" * 32, "DRJ",
                        opener=lambda request, timeout: Response(data))

if __name__ == "__main__":
    unittest.main()
