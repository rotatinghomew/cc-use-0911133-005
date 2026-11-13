"""重发、修订、补传分区、撤销与乱序测试。

核心不变量：中央结果只取决于“当前有效摘要集合”，与信封到达顺序无关；
任何传输历史的最终状态都必须等于对当前有效摘要做一次全量重算。
"""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from registry_privacy_gateway.runtime import stable_json
from registry_privacy_gateway.statistics.merge import MergeCentral
from registry_privacy_gateway.statistics.replay import (
    _envelopes_for,
    _site_payloads,
    replay_fixture,
    session_from_fixture,
)
from registry_privacy_gateway.statistics.store import (
    JOURNAL_REPLACE,
    JOURNAL_REPLAY,
    JOURNAL_SUPPLEMENT,
    JOURNAL_WITHDRAW,
    SummaryStore,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "merge_statistics.json"


def _load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class SummaryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _load_fixture()
        self.session = session_from_fixture(self.fixture)
        self.sites = _site_payloads(self.fixture)
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(self.temp) / "state.sqlite3"

    def _all_envelopes(self, transmission_index):
        return _envelopes_for(
            self.fixture["transmissions"][transmission_index], self.sites, self.session)

    def _run(self, order):
        store = SummaryStore(self.db)
        central = MergeCentral(store, self.session)
        receipts = []
        for index in order:
            for envelope in self._all_envelopes(index):
                receipts.append(central.ingest(envelope))
        result = central.recompute()
        return central, store, receipts, result

    def test_canonical_replay_exercises_all_lifecycle_actions(self):
        outcome = replay_fixture(self.fixture, self.db)
        actions = [receipt.action for receipt in outcome.receipts]
        self.assertIn(JOURNAL_SUPPLEMENT, actions)
        self.assertIn(JOURNAL_REPLAY, actions)       # A rev1 的重发
        self.assertIn(JOURNAL_REPLACE, actions)      # A rev2 修订
        self.assertIn(JOURNAL_WITHDRAW, actions)     # B rev2 撤销
        self.assertEqual(outcome.withdrawn_batches, ["B-july"])
        self.assertTrue(outcome.result.recomputed_from_scratch)

    def test_duplicate_resend_is_idempotent(self):
        central = MergeCentral(self.db, self.session)
        first = [central.ingest(env) for env in self._all_envelopes(0)]
        second = [central.ingest(env) for env in self._all_envelopes(0)]
        self.assertTrue(all(receipt.accepted for receipt in first + second))
        self.assertTrue(all(receipt.action == JOURNAL_REPLAY for receipt in second))
        before = central.recompute()
        after = central.recompute()
        self.assertEqual(stable_json(before.statistics), stable_json(after.statistics))
        # 重发不改变有效摘要数量，只追加审计日志。
        self.assertEqual(len(central.store.active_envelopes()),
                         len(self._all_envelopes(0)))

    def test_revision_replacement_updates_and_then_matches_reference(self):
        central = MergeCentral(self.db, self.session)
        for envelope in self._all_envelopes(0):
            self.assertTrue(central.ingest(envelope).accepted)
        before = central.recompute()
        for envelope in self._all_envelopes(3):  # A rev2 修订 visit-5
            receipt = central.ingest(envelope)
            self.assertTrue(receipt.accepted)
            self.assertEqual(receipt.action, JOURNAL_REPLACE)
        after = central.recompute()
        self.assertNotEqual(stable_json(before.statistics),
                            stable_json(after.statistics))
        current = central.store.current("A-july", "A-only")
        self.assertEqual(current["revision"], 2)
        # 修订后的 visit-5 waiting=14 必须体现在合并结果里。
        overall = after.statistics["scopes"][0]
        waiting = next(m for m in overall["marginals"] if m["field"] == "waiting")
        self.assertNotEqual(waiting["mean"],
                            before.statistics["scopes"][0]["marginals"][0]["mean"])

    def test_supplemental_partition_joins_without_touching_others(self):
        central = MergeCentral(self.db, self.session)
        for envelope in self._all_envelopes(0):  # 仅 A 的两个分区
            self.assertTrue(central.ingest(envelope).accepted)
        owners_before = dict(central.store.active_partition_owners())
        for envelope in self._all_envelopes(1):  # B 补传 B-only
            receipt = central.ingest(envelope)
            self.assertTrue(receipt.accepted)
            self.assertEqual(receipt.action, JOURNAL_SUPPLEMENT)
        owners_after = central.store.active_partition_owners()
        self.assertEqual(owners_before["shared"], owners_after["shared"])
        self.assertEqual(owners_before["A-only"], owners_after["A-only"])
        self.assertEqual(owners_after["B-only"], "B-july")

    def test_withdraw_removes_batch_from_active_set(self):
        central = MergeCentral(self.db, self.session)
        for index in (0, 1):
            for envelope in self._all_envelopes(index):
                self.assertTrue(central.ingest(envelope).accepted)
        self.assertIn("B-only", central.store.active_partition_owners())
        for envelope in self._all_envelopes(4):  # B 撤销
            receipt = central.ingest(envelope)
            self.assertTrue(receipt.accepted)
            self.assertEqual(receipt.action, JOURNAL_WITHDRAW)
        self.assertNotIn("B-only", central.store.active_partition_owners())
        result = central.recompute()
        active_batches = {item["batch"] for item in result.active_batches}
        self.assertEqual(active_batches, {"A-july"})

    def test_order_independence_across_shuffled_transmissions(self):
        _, _, _, baseline = self._run(list(range(len(self.fixture["transmissions"]))))
        baseline_blob = stable_json(baseline.statistics)
        baseline_active = stable_json(baseline.active_batches)
        rng = random.Random(20261113)
        seen = set()
        for _ in range(25):
            order = list(range(len(self.fixture["transmissions"])))
            rng.shuffle(order)
            seen.add(tuple(order))
            with tempfile.TemporaryDirectory() as directory:
                shuffled_db = Path(directory) / "shuffled.sqlite3"
                store = SummaryStore(shuffled_db)
                central = MergeCentral(store, self.session)
                for index in order:
                    for envelope in self._all_envelopes(index):
                        central.ingest(envelope)
                result = central.recompute()
            self.assertEqual(stable_json(result.statistics), baseline_blob, order)
            self.assertEqual(stable_json(result.active_batches), baseline_active, order)
        self.assertGreater(len(seen), 10)

    def test_early_withdraw_blocks_late_arriving_lower_revision(self):
        # 撤销先于摘要到达：墓碑先生效，随后迟到的 rev1 摘要必须被挡住。
        central = MergeCentral(self.db, self.session)
        for envelope in self._all_envelopes(4):  # B rev2 withdraw 先到
            self.assertTrue(central.ingest(envelope).accepted)
        for envelope in self._all_envelopes(1):  # B rev1 摘要迟到
            receipt = central.ingest(envelope)
            self.assertFalse(receipt.accepted)
            self.assertEqual(receipt.reason_code, "stale-revision")
        self.assertNotIn("B-only", central.store.active_partition_owners())

    def test_full_recompute_equals_effective_summary_set(self):
        # 任意中间状态下，recompute 都等于把当前 active 信封清空后重新喂入。
        central = MergeCentral(self.db, self.session)
        for index in (0, 1, 2, 3):
            for envelope in self._all_envelopes(index):
                central.ingest(envelope)
        first = central.recompute()
        active = central.store.active_envelopes()
        with tempfile.TemporaryDirectory() as directory:
            rebuild_store = SummaryStore(Path(directory) / "rebuild.sqlite3")
            rebuild = MergeCentral(rebuild_store, self.session)
            for envelope in active:
                receipt = rebuild.ingest(envelope)
                self.assertTrue(receipt.accepted, (receipt.reason_code, receipt.message))
            second = rebuild.recompute()
        self.assertEqual(stable_json(first.statistics), stable_json(second.statistics))

    def test_journal_is_append_only_and_orders_all_receipts(self):
        _, store, receipts, _ = self._run(list(range(len(self.fixture["transmissions"]))))
        journal = store.journal()
        self.assertEqual(len(journal), len(receipts))
        self.assertEqual([row["seq"] for row in journal],
                         list(range(1, len(journal) + 1)))
        self.assertEqual([row["digest"] for row in journal],
                         [receipt.digest for receipt in receipts])


if __name__ == "__main__":
    unittest.main()
