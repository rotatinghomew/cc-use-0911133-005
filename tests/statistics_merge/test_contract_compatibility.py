"""交换契约与不兼容原因测试。

覆盖字段版本、群体维度、口径、分区重叠、权限和用途检查；任何违反合作约定的
信封都必须返回稳定的原因码，而不是产出表面可用的合并结果。
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from registry_privacy_gateway.statistics.contract import (
    SCHEMA_VERSION,
    SessionContract,
    SummaryContractError,
    envelope_digest,
)
from registry_privacy_gateway.statistics.merge import MergeCentral
from registry_privacy_gateway.statistics.replay import session_from_fixture
from registry_privacy_gateway.statistics.summarize import build_summary_envelopes

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "merge_statistics.json"


def _load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ContractCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _load_fixture()
        self.session = session_from_fixture(self.fixture)
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(self.temp) / "state.sqlite3"
        self.central = MergeCentral(self.db, self.session)

    def _a_envelopes(self, batch="A-july", revision=1):
        return build_summary_envelopes(
            {"site": "A", "batch": batch, "revision": revision,
             "rows": self.fixture["sites"][0]["rows"]}, self.session)

    def _ingest(self, envelope):
        return self.central.ingest(envelope)

    def test_fixture_incompatible_examples_return_specific_reasons(self):
        for envelope in self._a_envelopes():
            self.assertTrue(self._ingest(envelope).accepted)
        # unknown-overlap：另一个 batch 声称已被占用的 shared 分区。
        overlap_rows = [{
            "observation_id": "x", "partition": "shared", "group": "south",
            "purpose": "capacity-analysis", "weight": 1,
            "waiting": 1, "cost": 1,
        }]
        overlap = build_summary_envelopes(
            {"site": "A", "batch": "unknown-overlap", "revision": 1,
             "rows": overlap_rows}, self.session)
        receipt = self._ingest(overlap[0])
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "inseparable-overlap")
        self.assertIn("shared", receipt.message)

        # wrong-units：字段版本与中央会话锚定的 units-v1 不一致。
        wrong_session = SessionContract.from_analysis(
            {**self.fixture["analysis"], "field_version": "minutes-v2"},
            self.fixture["partition_contract"])
        wrong_envelopes = build_summary_envelopes(
            {"site": "B", "batch": "B-units", "revision": 1,
             "rows": self.fixture["sites"][1]["rows"]}, wrong_session)
        receipt = self._ingest(wrong_envelopes[0])
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "field-version-mismatch")

    def test_purpose_mismatch_is_quarantined_at_site(self):
        with self.assertRaises(SummaryContractError) as context:
            build_summary_envelopes(
                {"site": "C", "batch": "C-july", "revision": 1,
                 "rows": self.fixture["sites"][2]["rows"]}, self.session)
        self.assertEqual(context.exception.code, "summary-empty")
        reasons = {item["reason_code"] for item in context.exception.detail["excluded"]}
        self.assertIn("purpose-mismatch", reasons)

    def test_non_authoritative_site_cannot_submit_partition(self):
        rows = [{
            "observation_id": "z", "partition": "A-only", "group": "south",
            "purpose": "capacity-analysis", "weight": 1,
            "waiting": 1, "cost": 2,
        }]
        with self.assertRaises(SummaryContractError) as context:
            build_summary_envelopes(
                {"site": "B", "batch": "B-fake", "revision": 1, "rows": rows},
                self.session)
        self.assertEqual(context.exception.code, "summary-empty")
        reasons = {item["reason_code"] for item in context.exception.detail["excluded"]}
        self.assertIn("site-not-authoritative", reasons)

    def test_partition_outside_contract_rejected_centrally(self):
        envelope = self._a_envelopes()[0]
        envelope = copy.deepcopy(envelope)
        envelope["partition"] = "mystery-zone"
        envelope["contract"]["partition"] = "mystery-zone"
        envelope["digest"] = envelope_digest(envelope)
        receipt = self._ingest(envelope)
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "partition-not-contracted")

    def test_tampered_digest_rejected(self):
        envelope = copy.deepcopy(self._a_envelopes()[0])
        envelope["scopes"][0]["marginals"][0]["sum"] = 999999.0
        receipt = self._ingest(envelope)  # digest 未重算
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "digest-mismatch")

    def test_observation_unit_mismatch_rejected(self):
        envelope = copy.deepcopy(self._a_envelopes(batch="A-unit")[0])
        envelope["contract"]["observation_unit"] = "resident"
        envelope["digest"] = envelope_digest(envelope)
        receipt = self._ingest(envelope)
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "observation-unit-mismatch")

    def test_group_dimension_mismatch_rejected(self):
        envelope = copy.deepcopy(self._a_envelopes(batch="A-group")[0])
        envelope["contract"]["group_by"] = ["region"]
        envelope["digest"] = envelope_digest(envelope)
        receipt = self._ingest(envelope)
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "group-dimension-mismatch")

    def test_cross_moment_gap_rejected_instead_of_zero_correlation(self):
        rows = [{
            "observation_id": "a", "partition": "C-only", "group": "south",
            "purpose": "capacity-analysis", "weight": 2,
            "waiting": 10, "cost": None,
        }, {
            "observation_id": "b", "partition": "C-only", "group": "south",
            "purpose": "capacity-analysis", "weight": 3,
            "waiting": 20, "cost": None,
        }]
        envelope = build_summary_envelopes(
            {"site": "C", "batch": "C-gap", "revision": 1, "rows": rows},
            self.session)[0]
        for scope in envelope["scopes"]:
            scope["pairs"] = []
        envelope["digest"] = envelope_digest(envelope)
        receipt = self._ingest(envelope)
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "cross-moment-coverage-gap")

    def test_envelope_rejects_unknown_top_level_fields(self):
        envelope = copy.deepcopy(self._a_envelopes(batch="A-raw")[0])
        envelope["rows"] = [{"observation_id": "leak", "waiting": 1}]
        envelope["digest"] = envelope_digest(envelope)
        receipt = self._ingest(envelope)
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "envelope-field-not-allowed")

    def test_stale_revision_and_conflict_are_distinct_rejections(self):
        first = self._a_envelopes(revision=1)
        for envelope in first:
            self.assertTrue(self._ingest(envelope).accepted)
        stale = copy.deepcopy(first[0])
        stale["revision"] = 0  # 低于当前 revision
        stale["digest"] = envelope_digest(stale)
        # revision=0 在形状检查阶段就应被拒绝。
        self.assertEqual(self._ingest(stale).reason_code, "revision-invalid")
        conflicting = copy.deepcopy(first[0])
        conflicting["scopes"][0]["marginals"][0]["sum"] += 1.0
        conflicting["digest"] = envelope_digest(conflicting)  # 同 revision 不同内容
        receipt = self._ingest(conflicting)
        self.assertFalse(receipt.accepted)
        self.assertEqual(receipt.reason_code, "revision-conflict")

    def test_schema_and_type_guards(self):
        envelope = copy.deepcopy(self._a_envelopes(batch="A-schema")[0])
        envelope["schema_version"] = SCHEMA_VERSION + 1
        envelope["digest"] = envelope_digest(envelope)
        self.assertEqual(self._ingest(envelope).reason_code, "schema-version-mismatch")
        bad_type = copy.deepcopy(self._a_envelopes(batch="A-type")[0])
        bad_type["scopes"][0]["marginals"][0]["n"] = -3
        bad_type["digest"] = envelope_digest(bad_type)
        self.assertEqual(self._ingest(bad_type).reason_code, "envelope-malformed")

    def test_field_minimization_strips_private_note_but_keeps_trace(self):
        envelopes = self._a_envelopes(batch="A-minimize")
        serialized = json.dumps(envelopes, ensure_ascii=False)
        self.assertNotIn("仅站内", serialized)  # 私有值绝不离开站点
        traces = [
            item for envelope in envelopes for item in envelope["excluded"]
            if item["reason_code"] == "field-minimized"
        ]
        self.assertTrue(traces)
        self.assertIn("private_note", traces[0]["detail"]["removed_fields"])
        for item in traces:
            self.assertNotIn("raw_value", item)


if __name__ == "__main__":
    unittest.main()
