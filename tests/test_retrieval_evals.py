"""知識台帳からの検索評価を合成データで検証。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from copilot_learning.core import InvalidData
from copilot_learning.knowledge_store import KnowledgeStore
from copilot_learning.learning import candidate_sha256
from copilot_learning.retrieval_evals import evaluate_retrieval


class RetrievalEvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = KnowledgeStore(Path(self.tmp.name) / "knowledge.sqlite")
        self.ids = []
        for title, applies, excludes in [
            ("Python仮想環境の確認", "仮想環境", "APIレスポンス"),
            ("SQL接続点検", "DB接続", "仮想環境"),
        ]:
            candidate = {"knowledge_type": "note", "title": title,
                         "description": "確認手順", "instruction": title + "を確認する",
                         "generalization": "別案件にも再利用", "evidence_ids": ["a" * 64 + ":0"],
                         "applies_when": [applies], "not_when": [excludes],
                         "decision": "keep", "approved_for_copilot": False}
            candidate["candidate_sha256"] = candidate_sha256(candidate)
            source = {"session_fingerprint": "a"*64, "archive_sha256": "b"*64}
            saved = self.store.register(candidate, source)
            self.store.transition(saved["knowledge_id"], "pending", expected_revision=1,
                                  reviewer="synthetic", reason="mock")
            self.store.transition(saved["knowledge_id"], "active", expected_revision=2,
                                  reviewer="synthetic", reason="mock")
            self.ids.append(saved["knowledge_id"])
        self.dataset = {"label_source": "synthetic_only", "training_group_ids": ["train"],
                        "cases": [
                            {"case_id": "pos", "group_id": "case-a", "task": "Pythonの仮想環境が誤っている", "relevant_ids": [self.ids[0]], "forbidden_ids": [self.ids[1]]},
                            {"case_id": "neg", "group_id": "case-b", "task": "APIレスポンス形式が変更", "relevant_ids": [], "forbidden_ids": [self.ids[0]]},
                        ]}

    def test_eval_has_hit_and_negative_metrics(self):
        result = evaluate_retrieval(self.store, self.dataset, top_k=2)
        self.assertEqual(result["n_positive"], 1)
        self.assertEqual(result["n_negative"], 1)
        self.assertEqual(result["hit_rate_at_k"], 1.0)
        self.assertTrue(result["knowledge_revision_sha256"])
        self.assertFalse(result["real_copilot_improvement_verified"])
        self.assertTrue(all("text" not in x for x in result["details"]))

    def test_train_group_leakage_fails(self):
        ds = {**self.dataset, "cases": [dict(self.dataset["cases"][0], group_id="train")]}
        with self.assertRaises(InvalidData):
            evaluate_retrieval(self.store, ds)

    def test_duplicate_holdout_tasks_fail(self):
        a, b = self.dataset["cases"]
        ds = {**self.dataset, "cases": [a, dict(b, task=a["task"])]}
        with self.assertRaises(InvalidData):
            evaluate_retrieval(self.store, ds)

    def test_relevant_must_be_active(self):
        ds = {**self.dataset, "cases": [dict(self.dataset["cases"][0], relevant_ids=["missing"]) ]}
        with self.assertRaises(InvalidData):
            evaluate_retrieval(self.store, ds)

    def test_result_is_deterministic(self):
        a = evaluate_retrieval(self.store, self.dataset)
        b = evaluate_retrieval(self.store, self.dataset)
        self.assertEqual(a, b)

    def test_no_positive_returns_null_hit_rate(self):
        ds = {**self.dataset, "cases": [self.dataset["cases"][1]]}
        result = evaluate_retrieval(self.store, ds)
        self.assertIsNone(result["hit_rate_at_k"])
        self.assertIsNone(result["mrr_at_k"])


if __name__ == "__main__":
    unittest.main()
