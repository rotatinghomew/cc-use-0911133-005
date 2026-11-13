"""加权统计语义测试。

参照真值直接在授权明细上独立计算（测试专用，不是中央接口的一部分），
用于核对站点摘要与中央合并的均值、总体/样本方差、协方差和相关系数。
"""
from __future__ import annotations

import math
import unittest
from pathlib import Path

from registry_privacy_gateway.runtime import stable_json
from registry_privacy_gateway.statistics.merge import MergeCentral
from registry_privacy_gateway.statistics.replay import (
    reference_statistics,
    replay_fixture,
    session_from_fixture,
)
from registry_privacy_gateway.statistics.summarize import build_summary_envelopes
from registry_privacy_gateway.statistics.store import SummaryStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "merge_statistics.json"


def _load_fixture():
    import json
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _site_envelopes(fixture, site_index, revision=None, replacements=None):
    site = fixture["sites"][site_index]
    session = session_from_fixture(fixture)
    payload = {
        "site": site["site"], "batch": site["batch"],
        "revision": revision or site["revision"], "rows": site["rows"],
    }
    if replacements:
        payload["replace_values"] = replacements
    return session, build_summary_envelopes(payload, session)


class WeightedStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _load_fixture()
        self.temp = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.db = Path(self.temp) / "state.sqlite3"

    def _central_from_envelopes(self, session, envelopes):
        central = MergeCentral(self.db, session)
        for envelope in envelopes:
            receipt = central.ingest(envelope)
            self.assertTrue(receipt.accepted, (receipt.reason_code, receipt.message))
        return central

    def test_site_moments_match_manual_frequency_weighting(self):
        # 站点 A 的 A-only 分区，手工按频数权重核对 waiting 的一阶、二阶矩。
        session, envelopes = _site_envelopes(self.fixture, 0)
        envelope = next(e for e in envelopes if e["partition"] == "A-only")
        rows = [row for row in self.fixture["sites"][0]["rows"]
                if row["partition"] == "A-only" and row["waiting"] is not None]
        n = sum(row["weight"] for row in rows)
        total = sum(row["weight"] * row["waiting"] for row in rows)
        total_sq = sum(row["weight"] * row["waiting"] ** 2 for row in rows)
        overall = envelope["scopes"][0]
        marginal = next(m for m in overall["marginals"] if m["field"] == "waiting")
        self.assertEqual(marginal["n"], n)
        self.assertAlmostEqual(marginal["sum"], float(total))
        self.assertAlmostEqual(marginal["sum_squares"], float(total_sq))
        self.assertAlmostEqual(marginal["sum"] / marginal["n"], total / n)

    def test_central_recompute_matches_authorized_detail_reference(self):
        outcome = replay_fixture(self.fixture, self.db)
        self.assertEqual(outcome.rejected, [])
        central_scopes = {
            stable_json(scope["group"]): scope
            for scope in outcome.result.statistics["scopes"]
        }
        reference = reference_statistics(self.fixture)
        reference_scopes = {
            stable_json(scope["group"]): scope for scope in reference["scopes"]
        }
        self.assertEqual(set(central_scopes), set(reference_scopes))
        for key, central_scope in central_scopes.items():
            ref_scope = reference_scopes[key]
            for central_m, ref_m in zip(central_scope["marginals"],
                                        ref_scope["marginals"]):
                self.assertEqual(central_m["n"], ref_m["n"])
                if ref_m["n"] == 0:
                    continue
                self.assertAlmostEqual(central_m["mean"], ref_m["mean"], places=9)
                self.assertAlmostEqual(central_m["population_variance"],
                                       ref_m["population_variance"], places=9)
                if ref_m["sample_variance"] is None:
                    self.assertFalse(central_m["sample_variance"]["defined"])
                    self.assertEqual(central_m["sample_variance"]["reason"],
                                     "sample-estimate-undefined")
                else:
                    self.assertAlmostEqual(central_m["sample_variance"],
                                           ref_m["sample_variance"], places=9)
            for central_p, ref_p in zip(central_scope["pairs"], ref_scope["pairs"]):
                self.assertEqual(central_p["pair_n"], ref_p["pair_n"])
                if ref_p["pair_n"] == 0:
                    continue
                self.assertAlmostEqual(central_p["population_covariance"],
                                       ref_p["population_covariance"], places=9)
                if ref_p["sample_covariance"] is None:
                    self.assertFalse(central_p["sample_covariance"]["defined"])
                else:
                    self.assertAlmostEqual(central_p["sample_covariance"],
                                           ref_p["sample_covariance"], places=9)
                if ref_p["correlation"] is None:
                    self.assertFalse(central_p["correlation"]["defined"])
                else:
                    self.assertAlmostEqual(
                        central_p["correlation"],
                        max(-1.0, min(1.0, ref_p["correlation"])), places=9)

    def test_marginal_and_pair_denominators_are_distinct(self):
        # waiting 有 2 条非缺失、cost 只有 1 条、共同观测 1 条：三种分母互不冒充。
        fixture = _load_fixture()
        session = session_from_fixture(fixture)
        rows = [
            {"observation_id": "a", "partition": "C-only", "group": "south",
             "purpose": "capacity-analysis", "weight": 1, "waiting": 5, "cost": 10},
            {"observation_id": "b", "partition": "C-only", "group": "north",
             "purpose": "capacity-analysis", "weight": 1, "waiting": 7, "cost": None},
        ]
        envelopes = build_summary_envelopes(
            {"site": "C", "batch": "C-denominator", "revision": 1, "rows": rows}, session)
        central = self._central_from_envelopes(session, envelopes)
        overall = central.recompute().statistics["scopes"][0]
        waiting_n = next(m["n"] for m in overall["marginals"]
                         if m["field"] == "waiting")
        cost_n = next(m["n"] for m in overall["marginals"] if m["field"] == "cost")
        self.assertEqual((waiting_n, cost_n, overall["pairs"][0]["pair_n"]), (2, 1, 1))

    def test_sample_estimate_undefined_when_effective_frequency_one(self):
        fixture = _load_fixture()
        session = session_from_fixture(fixture)
        rows = [
            {"observation_id": "a", "partition": "C-only", "group": "south",
             "purpose": "capacity-analysis", "weight": 1, "waiting": 5, "cost": 10},
        ]
        envelopes = build_summary_envelopes(
            {"site": "C", "batch": "C-one", "revision": 1, "rows": rows}, session)
        central = self._central_from_envelopes(session, envelopes)
        overall = central.recompute().statistics["scopes"][0]
        for marginal in overall["marginals"]:
            self.assertEqual(marginal["n"], 1)
            self.assertFalse(marginal["sample_variance"]["defined"])
            self.assertEqual(marginal["sample_variance"]["reason"],
                             "sample-estimate-undefined")
        self.assertFalse(overall["pairs"][0]["sample_covariance"]["defined"])
        # 总体口径在 n=1 时仍然有定义（方差为 0）。
        self.assertEqual(overall["marginals"][0]["population_variance"], 0.0)

    def test_correlation_undefined_on_zero_variance_not_fake_zero(self):
        fixture = _load_fixture()
        session = session_from_fixture(fixture)
        rows = [
            {"observation_id": "a", "partition": "C-only", "group": "south",
             "purpose": "capacity-analysis", "weight": 2, "waiting": 5, "cost": 10},
            {"observation_id": "b", "partition": "C-only", "group": "south",
             "purpose": "capacity-analysis", "weight": 3, "waiting": 5, "cost": 12},
        ]
        envelopes = build_summary_envelopes(
            {"site": "C", "batch": "C-zero", "revision": 1, "rows": rows}, session)
        central = self._central_from_envelopes(session, envelopes)
        pair = central.recompute().statistics["scopes"][0]["pairs"][0]
        self.assertFalse(pair["correlation"]["defined"])
        self.assertEqual(pair["correlation"]["reason"], "zero-variance")
        self.assertNotIn(0.0, (pair["correlation"],))

    def test_group_statistics_partition_by_declared_dimension(self):
        outcome = replay_fixture(self.fixture, self.db)
        groups = {
            stable_json(scope["group"]): scope
            for scope in outcome.result.statistics["scopes"]
        }
        self.assertIn(stable_json(None), groups)
        self.assertIn(stable_json({"group": "north"}), groups)
        self.assertIn(stable_json({"group": "south"}), groups)
        north = groups[stable_json({"group": "north"})]
        reference = reference_statistics(self.fixture)
        ref_north = next(scope for scope in reference["scopes"]
                         if scope["group"] == {"group": "north"})
        for central_m, ref_m in zip(north["marginals"], ref_north["marginals"]):
            self.assertEqual(central_m["n"], ref_m["n"])
            self.assertAlmostEqual(central_m["mean"], ref_m["mean"], places=9)

    def test_observation_unit_declared_and_preserved_in_output(self):
        outcome = replay_fixture(self.fixture, self.db)
        self.assertEqual(outcome.result.statistics["observation_unit"], "service-visit")
        self.assertEqual(outcome.result.contract["observation_unit"], "service-visit")
        # service-visit 口径下，同一人的多次服务是独立观测：频数直接累加，
        # 中央不做任何按人去重。
        envelope = build_summary_envelopes(
            {"site": "A", "batch": "A-check", "revision": 1,
             "rows": self.fixture["sites"][0]["rows"]},
            session_from_fixture(self.fixture))
        total_n = sum(
            marginal["n"]
            for env in envelope for scope in env["scopes"] if scope["group"] is None
            for marginal in scope["marginals"] if marginal["field"] == "waiting")
        raw_n = sum(row["weight"] for row in self.fixture["sites"][0]["rows"]
                    if row["waiting"] is not None)
        self.assertEqual(total_n, raw_n)


if __name__ == "__main__":
    unittest.main()
