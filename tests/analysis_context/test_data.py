from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DataContextTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures/merge_statistics.json").read_text(encoding="utf-8"))

    def test_frequency_weights_and_pairwise_missingness(self):
        rows = [row for site in self.data["sites"] for row in site["rows"]]
        self.assertTrue(all(isinstance(row["weight"], int) and row["weight"] > 0 for row in rows))
        self.assertTrue(any(row.get("waiting") is None for row in rows))
        self.assertTrue(any(row.get("cost") is None for row in rows))

    def test_overlap_and_retraction_examples(self):
        sites = self.data["sites"]
        left = {row["observation_id"] for row in sites[0]["rows"]}
        right = {row["observation_id"] for row in sites[1]["rows"]}
        self.assertTrue(left & right)
        self.assertNotIn("private_note", self.data["analysis"]["allowed_fields"])
        self.assertTrue(any(row.get("withdraw") for row in self.data["transmissions"]))

