"""知識台帳の衝突・退役、Hold-out評価の未実行扱い。すべて合成データ。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from copilot_learning.core import InvalidData
from copilot_learning.learning import candidate_sha256
from copilot_learning.knowledge_store import KnowledgeStore
from copilot_learning.evals import evaluate_holdout, wilson_interval


class KnowledgeAndEvalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = KnowledgeStore(Path(self.tmp.name) / "ledger.sqlite")
        self.source = {"session_fingerprint": "a"*64, "archive_sha256": "b"*64}
        self.candidate = {
            "knowledge_type": "note", "title": "Pythonテストの点検", "description": "環境を確認",
            "instruction": "仮想環境を確認", "generalization": "Python開発で有効",
            "evidence_ids": ["a"*64+":0"], "decision": "keep",
            "approved_for_copilot": False,
        }

    def _make(self, item=None):
        item = dict(item or self.candidate)
        item["candidate_sha256"] = candidate_sha256(item)
        return self.db.register(item, self.source)

    def test_register_is_idempotent_and_proposed(self):
        first = self._make()
        second = self._make()
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "proposed")
        self.assertEqual(len(self.db.history(first["knowledge_id"])), 1)
        self.assertEqual(self.db.active(), [])

    def test_active_requires_review_and_two_transitions(self):
        row = self._make()
        with self.assertRaises(InvalidData):
            self.db.transition(row["knowledge_id"], "active", expected_revision=1,
                               reviewer="reviewer", reason="確認")
        pending = self.db.transition(row["knowledge_id"], "pending", expected_revision=1,
                                     reviewer="reviewer", reason="確認中")
        with self.assertRaises(InvalidData):
            self.db.transition(row["knowledge_id"], "active", expected_revision=2,
                               reviewer="", reason="承認")
        active = self.db.transition(row["knowledge_id"], "active", expected_revision=2,
                                    reviewer="reviewer", reason="合成データを承認")
        self.assertEqual(active["revision"], 3)
        self.assertEqual(len(self.db.active()), 1)
        self.assertEqual([x["status"] for x in self.db.history(row["knowledge_id"])],
                         ["proposed", "pending", "active"])

    def test_stale_revision_and_content_changed_are_rejected(self):
        row = self._make()
        self.db.transition(row["knowledge_id"], "pending", expected_revision=1,
                           reviewer="r", reason="review")
        with self.assertRaisesRegex(InvalidData, "CAS"):
            self.db.transition(row["knowledge_id"], "active", expected_revision=1,
                               reviewer="r", reason="review")
        item = dict(self.candidate)
        item["source"] = "different"
        # 同じハッシュなのに登録済み候補本体を変えて、上書きできないこと。
        with self.assertRaises(InvalidData):
            self._make(item)

    def test_retired_and_superseded_are_not_active(self):
        old = self._make()
        self.db.transition(old["knowledge_id"], "pending", expected_revision=1, reviewer="r", reason="review")
        self.db.transition(old["knowledge_id"], "active", expected_revision=2, reviewer="r", reason="review")
        new_item = {**self.candidate, "instruction": "Python実行環境を先に点検"}
        new = self._make(new_item)
        self.db.transition(new["knowledge_id"], "pending", expected_revision=1, reviewer="r", reason="review")
        self.db.transition(new["knowledge_id"], "active", expected_revision=2, reviewer="r", reason="review")
        self.db.transition(old["knowledge_id"], "superseded", expected_revision=3,
                           reviewer="r", reason="new replaces old", replacement_id=new["knowledge_id"])
        self.assertEqual([x["knowledge_id"] for x in self.db.active()], [new["knowledge_id"]])
        with self.assertRaises(InvalidData):
            self.db.transition(old["knowledge_id"], "active", expected_revision=4, reviewer="r", reason="no")
        self.db.transition(new["knowledge_id"], "retired", expected_revision=3,
                           reviewer="r", reason="obsolete")
        self.assertEqual(self.db.active(), [])

    def test_source_digest_must_exist(self):
        with self.assertRaises(InvalidData):
            self.db.register(self.candidate, {"session_fingerprint": "invalid"})
        with self.assertRaises(InvalidData):
            self.db.register({**self.candidate, "decision": "drop"}, self.source)

    def test_wilson_and_not_run(self):
        ds = {"label_source": "synthetic_fixture", "training_group_ids": ["train-1"], "cases": [
            {"case_id": "one", "group_id": "holdout-1", "expected": "applicable"},
            {"case_id": "two", "group_id": "holdout-2", "expected": "not_applicable"},
        ]}
        output = evaluate_holdout(ds, {"B": {"one": "applicable", "two": "needs_review"}})
        self.assertEqual(output["results"]["A"]["status"], "not_executed")
        self.assertIsNone(output["results"]["D"]["accuracy"])
        self.assertEqual(output["results"]["B"]["accuracy"], 0.5)
        self.assertEqual(output["results"]["B"]["coverage"], 0.5)
        self.assertFalse(output["results"]["B"]["real_copilot_success_verified"])
        self.assertIsNone(wilson_interval(0, 0))

    def test_holdout_leakage_and_missing_prediction(self):
        ds = {"training_group_ids": ["train"], "cases": [
            {"case_id": "x", "group_id": "train", "expected": "applicable"}]}
        with self.assertRaises(InvalidData):
            evaluate_holdout(ds, {"A": {"x": "applicable"}})
        ds["cases"][0]["group_id"] = "holdout"
        with self.assertRaises(InvalidData):
            evaluate_holdout(ds, {"A": {"y": "applicable"}})

    def test_false_application_rate(self):
        ds = {"training_group_ids": [], "cases": [
            {"case_id": "x", "group_id": "holdout", "expected": "not_applicable"}]}
        result = evaluate_holdout(ds, {"C": {"x": "applicable"}})["results"]["C"]
        self.assertEqual(result["fp"], 1)
        self.assertEqual(result["false_application_rate"], 1.0)
        self.assertEqual(result["accuracy"], 0.0)


if __name__ == "__main__":
    unittest.main()
