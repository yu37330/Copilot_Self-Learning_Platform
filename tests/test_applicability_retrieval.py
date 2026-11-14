"""適用例/適用除外例、日本語検索、保留知識の除外を合成データで検証。"""
import tempfile
import unittest
from pathlib import Path

from copilot_learning.applicability import evaluate_applicability, validate_scope
from copilot_learning.core import InvalidData
from copilot_learning.learning import candidate_sha256, validate_candidates, grade_candidates
from copilot_learning.knowledge_store import KnowledgeStore
from copilot_learning.retrieval import search_active, tokenize


class ApplicabilityAndRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = KnowledgeStore(self.path / "knowledge.db")
        self.source = {"session_fingerprint": "a"*64, "archive_sha256": "b"*64,
                       "changed_paths": ["src/test_python.py"]}
        self.rule = {"knowledge_type": "skill", "title": "Python仮想環境の確認",
                     "description": "テスト失敗時に環境を点検", "instruction": "Python仮想環境を確認してからテストする",
                     "generalization": "複数のPythonプロジェクトに適用可能",
                     "evidence_ids": ["a"*64+":0"],
                     "applies_when": ["Python", "仮想環境"],
                     "not_when": ["API仕様不一致"], "counterexamples": ["認証エラー"]}

    def _add(self, candidate, activate):
        graded = {**candidate, "decision": "pending", "approved_for_copilot": False,
                  "candidate_sha256": candidate_sha256(candidate)}
        row = self.store.register(graded, self.source)
        if activate:
            row = self.store.transition(row["knowledge_id"], "pending", expected_revision=1,
                                        reviewer="test-reviewer", reason="合成テスト確認")
            row = self.store.transition(row["knowledge_id"], "active", expected_revision=2,
                                        reviewer="test-reviewer", reason="合成テスト明示承認")
        return row

    def test_applies_positive_and_negative(self):
        self.assertEqual(evaluate_applicability(self.rule, "Pythonの仮想環境がずれてテスト失敗")["decision"], "applicable")
        self.assertEqual(evaluate_applicability(self.rule, "Python仮想環境ではなくAPI仕様不一致で失敗")["decision"], "not_applicable")
        self.assertEqual(evaluate_applicability(self.rule, "Pythonのテスト失敗")["decision"], "needs_review")
        self.assertEqual(evaluate_applicability(self.rule, "Python仮想環境 認証エラー")["decision"], "not_applicable")

    def test_missing_boundaries_never_auto_apply(self):
        self.assertEqual(evaluate_applicability({"applies_when": ["Python"]}, "Pythonの問題")["decision"], "needs_review")
        self.assertEqual(evaluate_applicability({}, "Pythonの問題")["decision"], "needs_review")

    def test_schema_safe_and_hash_bound(self):
        clean = validate_candidates({"candidates": [self.rule]})[0]
        self.assertEqual(clean["not_when"], ["API仕様不一致"])
        changed = {**self.rule, "not_when": ["DB接続失敗"]}
        self.assertNotEqual(candidate_sha256(self.rule), candidate_sha256(changed))
        with self.assertRaises(InvalidData):
            validate_scope({"not_when": [123]})
        with self.assertRaises(InvalidData):
            validate_scope({"not_when": ["AKIAABCDEFGHIJKLMNOP"]})

    def test_japanese_tokenizer(self):
        self.assertIn("仮想", tokenize("仮想環境"))
        self.assertIn("python", tokenize("Python API"))

    def test_only_active_is_retrieved(self):
        self._add(self.rule, activate=False)
        self.assertEqual(search_active(self.store, "Python 仮想環境 テスト"), [])
        added = self._add({**self.rule, "title": "仮想環境の別の確認方法"}, activate=True)
        found = search_active(self.store, "Python 仮想環境 テスト")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["knowledge_id"], added["knowledge_id"])
        self.assertEqual(found[0]["applicability"], "applicable")
        self.assertFalse(found[0]["approved_for_copilot"])

    def test_exclusion_hides_active_knowledge(self):
        self._add(self.rule, activate=True)
        self.assertEqual(search_active(self.store, "Python 仮想環境 API仕様不一致"), [])

    def test_unknown_scope_is_only_advisory(self):
        item = {k:v for k,v in self.rule.items() if k not in {"applies_when","not_when","counterexamples"}}
        self._add(item, activate=True)
        got = search_active(self.store, "Python 仮想環境")
        self.assertEqual(got[0]["applicability"], "needs_review")
        self.assertFalse(got[0]["approved_for_copilot"])

    def test_rejected_secret_search(self):
        with self.assertRaises(InvalidData):
            search_active(self.store, "AKIAABCDEFGHIJKLMNOP")


if __name__ == "__main__":
    unittest.main()
