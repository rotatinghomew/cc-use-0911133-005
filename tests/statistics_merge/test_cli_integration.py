"""命令行集成测试：summarize 与 merge 通过 --input/--output/--db 串联。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from registry_privacy_gateway.runtime import stable_json
from registry_privacy_gateway.statistics.replay import replay_fixture

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "merge_statistics.json"
MODULE = "registry_privacy_gateway.statistics"


class StatisticsCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.work = Path(self.temp)
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.contract_path = self.work / "contract.json"
        self.contract_path.write_text(
            json.dumps({"analysis": self.fixture["analysis"],
                        "partition_contract": self.fixture["partition_contract"]},
                       ensure_ascii=False), encoding="utf-8")
        self._write_site_files()

    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", MODULE, *arguments],
            cwd=ROOT, capture_output=True, text=True, timeout=30)

    def _write_site_files(self):
        site_a = self.fixture["sites"][0]
        site_b = self.fixture["sites"][1]
        self._write("a1.json", {"site": "A", "batch": "A-july", "revision": 1,
                                "rows": site_a["rows"]})
        self._write("a2.json", {"site": "A", "batch": "A-july", "revision": 2,
                                "rows": site_a["rows"],
                                "replace_values": [
                                    {"observation_id": "visit-5", "waiting": 14}]})
        self._write("b1.json", {"site": "B", "batch": "B-july", "revision": 1,
                                "rows": site_b["rows"]})
        self._write("b-withdraw.json", {"site": "B", "batch": "B-july",
                                        "revision": 2, "withdraw": True,
                                        "partitions": ["B-only"]})

    def _write(self, name, payload):
        (self.work / name).write_text(json.dumps(payload, ensure_ascii=False),
                                      encoding="utf-8")

    def _summarize(self, src, dst):
        result = self._run("summarize", "--input", str(self.work / src),
                           "--output", str(self.work / dst),
                           "--contract", str(self.contract_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.work / dst).read_text(encoding="utf-8"))

    def _merge(self, src, dst, db):
        result = self._run("merge", "--input", str(self.work / src),
                           "--output", str(self.work / dst), "--db", str(db),
                           "--contract", str(self.contract_path),
                           "--allow-rejections")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.work / dst).read_text(encoding="utf-8"))

    def test_full_cli_workflow_matches_library_replay(self):
        db = self.work / "central.sqlite3"
        steps = [("a1.json", "a1.env.json"), ("b1.json", "b1.env.json"),
                 ("a1.json", "a1-resend.env.json"), ("a2.json", "a2.env.json"),
                 ("b-withdraw.json", "bw.env.json")]
        final = None
        for src, env_name in steps:
            self._summarize(src, env_name)
            final = self._merge(env_name, env_name.replace(".env", ".merge"), db)
        self.assertTrue(all(r["accepted"] for r in final["receipts"]))
        active = {item["batch"] for item in final["merge"]["active_batches"]}
        self.assertEqual(active, {"A-july"})

        outcome = replay_fixture(self.fixture, self.work / "library.sqlite3")
        self.assertEqual(
            stable_json(final["merge"]["statistics"]),
            stable_json(outcome.result.statistics))

    def test_central_input_rejects_raw_detail_file(self):
        # 中央 merge 的输入若是逐人明细而不是信封，必须被拒绝。
        result = self._run("merge", "--input", str(FIXTURE),
                           "--output", str(self.work / "bad.json"),
                           "--db", str(self.work / "bad.sqlite3"),
                           "--contract", str(self.contract_path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("envelope-malformed", result.stderr)

    def test_summarize_rejects_unknown_purpose_rows(self):
        bad = self.work / "c.json"
        bad.write_text(json.dumps(
            {"site": "C", "batch": "C-july", "revision": 1,
             "rows": self.fixture["sites"][2]["rows"]}, ensure_ascii=False),
            encoding="utf-8")
        result = self._run("summarize", "--input", str(bad),
                           "--output", str(self.work / "c.env.json"),
                           "--contract", str(self.contract_path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("summary-empty", result.stderr)

    def test_envelope_files_never_contain_raw_values(self):
        envelopes = self._summarize("a1.json", "a1.check.json")
        blob = json.dumps(envelopes, ensure_ascii=False)
        self.assertNotIn("private_note\":", blob.replace(" ", "").split('"removed_fields"')[0])
        self.assertNotIn("仅站内", blob)
        self.assertNotIn("observation_id", blob)
        for envelope in envelopes["envelopes"]:
            self.assertNotIn("rows", envelope)

    def test_state_persists_across_invocations(self):
        db = self.work / "persist.sqlite3"
        self._summarize("a1.json", "a1.env.json")
        first = self._merge("a1.env.json", "first.merge.json", db)
        self.assertTrue(db.exists())
        # 第二次进程打开同一个状态文件，重发同一信封应被识别为 resend。
        second = self._merge("a1.env.json", "second.merge.json", db)
        self.assertTrue(all(r["action"] == "resend" for r in second["receipts"]))
        self.assertEqual(
            stable_json(first["merge"]["statistics"]),
            stable_json(second["merge"]["statistics"]))


if __name__ == "__main__":
    unittest.main()
