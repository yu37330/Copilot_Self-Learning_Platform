"""合成入力のみで、結果の誤紐付け・二重計上・訂正・機密混入を検査する。"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, json_bytes
from copilot_learning.knowledge_store import KnowledgeStore
from copilot_learning.skill_preview import export_skill_preview
from copilot_learning.usage_feedback import begin_trial, record_result, usage_report
from scripts.usage_feedback_offline import main


class UsageFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "知識.sqlite"
        self.journal = self.root / "結果.sqlite"
        self.store = KnowledgeStore(self.db)
        item = {"knowledge_type": "skill", "title": "環境確認", "description": "Python環境を照合",
                "instruction": "仮想環境を確認する", "generalization": "環境不一致の原因調査",
                "evidence_ids": ["a" * 64 + ":0"], "applies_when": ["Python", "環境不一致"],
                "not_when": ["API仕様変更"], "counterexamples": [],
                "decision": "keep", "approved_for_copilot": False}
        row = self.store.register(item, {"session_fingerprint": "a" * 64, "archive_sha256": "b" * 64})
        for state in ("pending", "active"):
            row = self.store.transition(row["knowledge_id"], state, expected_revision=row["revision"],
                                        reviewer="synthetic-reviewer", reason="合成の確認")
        self.kid = row["knowledge_id"]
        self.policy = self.root / "辞書.json"
        self.policy.write_bytes(json_bytes({"schema_version": "1.0", "record_type": "offline-sensitive-literals",
                                            "sensitive_literals": ["合成非公開案件"]}))
        self.task = "Pythonの環境不一致を確認"
        self.preview = self.root / "preview"
        self.export = export_skill_preview(self.db, self.kid, 3, self.task, self.policy, self.preview)
        self.evidence = self.root / "evidence.txt"
        self.evidence.write_text("synthetic test: 12 passed", encoding="utf-8")

    def begin(self, **kw):
        args = dict(journal=self.journal, database=self.db, preview=self.preview, task=self.task,
                    policy=self.policy, expected_manifest_sha256=self.export["manifest_sha256"],
                    run_id="synthetic-run-1", source_kind="synthetic")
        args.update(kw)
        return begin_trial(**args)

    def result(self, trial, **kw):
        args = dict(journal=self.journal, trial_id=trial["trial_id"], expected_trial_sha256=trial["trial_sha256"],
                    expected_revision=0, outcome="reported_success", knowledge_used=True,
                    policy=self.policy, reporter="synthetic-reviewer", evidence=self.evidence)
        args.update(kw)
        return record_result(**args)

    def test_begin_is_not_success_and_is_idempotent(self):
        first = self.begin()
        again = self.begin()
        self.assertEqual(first["trial_id"], again["trial_id"])
        self.assertEqual(first["trial_sha256"], again["trial_sha256"])
        group = usage_report(self.journal)["groups"][0]
        self.assertEqual((group["trials"], group["unreported"], group["reported_success"]), (1, 1, 0))

    def test_result_replay_does_not_double_count(self):
        trial = self.begin()
        self.assertEqual(self.result(trial), self.result(trial))
        group = usage_report(self.journal)["groups"][0]
        self.assertEqual((group["reported_success"], group["corrections"]), (1, 0))

    def test_failure_and_correction_preserve_history(self):
        trial = self.begin()
        first = self.result(trial, outcome="reported_failure")
        second = self.result(trial, expected_revision=1, previous_result_sha256=first["result_sha256"])
        self.assertEqual(second["revision"], 2)
        group = usage_report(self.journal)["groups"][0]
        self.assertTrue(group["requires_review"])
        self.assertEqual((group["reported_success"], group["reported_failure"], group["corrections"]), (1, 0, 1))
        with sqlite3.connect(self.journal) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM usage_results").fetchone()[0], 2)

    def test_stale_or_changed_result_is_rejected(self):
        trial = self.begin()
        self.result(trial)
        with self.assertRaises(InvalidData):
            self.result(trial, outcome="reported_failure")
        with self.assertRaises(InvalidData):
            self.result(trial, expected_revision=1, previous_result_sha256="0" * 64)

    def test_trial_sha_mismatch_is_rejected(self):
        trial = self.begin()
        with self.assertRaises(InvalidData):
            self.result(trial, expected_trial_sha256="0" * 64)

    def test_success_requires_proof_and_use(self):
        trial = self.begin()
        for changes in ({"evidence": None}, {"knowledge_used": False}, {"expected_revision": True}):
            with self.subTest(changes=changes), self.assertRaises(InvalidData):
                self.result(trial, **changes)

    def test_not_used_and_inconclusive_not_counted_as_success(self):
        a = self.begin()
        self.result(a, outcome="not_used", knowledge_used=False, evidence=None)
        b = self.begin(run_id="second")
        self.result(b, outcome="inconclusive", knowledge_used=False, evidence=None)
        group = usage_report(self.journal)["groups"][0]
        self.assertEqual((group["reported_success"], group["not_used"], group["inconclusive"]), (0, 1, 1))

    def test_not_used_with_use_is_rejected(self):
        with self.assertRaises(InvalidData):
            self.result(self.begin(), outcome="not_used")

    def test_delayed_result_after_retirement_is_preserved(self):
        trial = self.begin()
        self.store.transition(self.kid, "retired", expected_revision=3, reviewer="r", reason="合成の退役")
        self.result(trial, outcome="reported_failure")
        with self.assertRaises(InvalidData):
            self.begin(run_id="new-trial")
        self.assertEqual(self.store.get(self.kid)["status"], "retired")

    def test_knowledge_db_is_unchanged_and_no_io(self):
        before = self.db.read_bytes()
        with patch("copilot_learning.aws_bridge.aws_command") as aws, patch("socket.socket.connect") as network:
            self.result(self.begin())
            report = usage_report(self.journal)
            aws.assert_not_called()
            network.assert_not_called()
        self.assertEqual(before, self.db.read_bytes())
        self.assertFalse(report["causal_improvement_verified"])
        self.assertFalse(report["copilot_execution_verified"])

    def test_report_excludes_task_proof_reporter_and_path(self):
        self.result(self.begin())
        with sqlite3.connect(self.journal) as db:
            stored = str(db.execute("SELECT document FROM usage_trials").fetchall())
            stored += str(db.execute("SELECT document FROM usage_results").fetchall())
        for text in (self.task, self.evidence.read_text(), "synthetic-reviewer", str(self.evidence)):
            self.assertNotIn(text, stored)

    def test_synthetic_and_manual_report_separated(self):
        self.result(self.begin())
        self.result(self.begin(source_kind="manual_report"))
        report = usage_report(self.journal)
        self.assertEqual({x["source_kind"] for x in report["groups"]}, {"synthetic", "manual_report"})

    def test_changed_policy_blocks_result(self):
        trial = self.begin()
        doc = json.loads(self.policy.read_text())
        doc["sensitive_literals"].append("追加辞書")
        self.policy.write_bytes(json_bytes(doc))
        with self.assertRaises(InvalidData):
            self.result(trial)

    def test_secret_proof_is_rejected_without_echo(self):
        trial = self.begin()
        for content in ("employee@example.invalid", "合成非公開案件", "AKIAABCDEFGHIJKLMNOP", ""):
            self.evidence.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(InvalidData) as raised:
                self.result(trial)
            if content:
                self.assertNotIn(content, str(raised.exception))

    def test_wrong_task_and_manifest_are_rejected(self):
        for kw in ({"task": self.task + "extra"}, {"expected_manifest_sha256": "0" * 64}):
            with self.subTest(kw=kw), self.assertRaises(InvalidData):
                self.begin(**kw)
        self.assertFalse(self.journal.exists())

    def test_wrong_db_cannot_be_modified(self):
        before = self.db.read_bytes()
        with self.assertRaises(InvalidData):
            self.begin(journal=self.db)
        other = self.root / "unrelated.sqlite"
        with sqlite3.connect(other) as db:
            db.execute("CREATE TABLE unrelated (id TEXT)")
        with self.assertRaises(InvalidData):
            self.begin(journal=other)
        self.assertEqual(self.db.read_bytes(), before)

    def test_missing_report_does_not_create_db(self):
        with self.assertRaises(InvalidData):
            usage_report(self.journal)
        self.assertFalse(self.journal.exists())

    def test_tampered_ledger_detected(self):
        self.result(self.begin())
        with sqlite3.connect(self.journal) as db:
            db.execute("UPDATE usage_results SET document='{}'")
        with self.assertRaises(InvalidData):
            usage_report(self.journal)

    def test_symlink_and_git_output_rejected(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(InvalidData):
            self.begin(journal=repo / "result.sqlite")
        with self.assertRaises(InvalidData):
            self.begin(journal=self.root / ".agents" / "result.sqlite")
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(InvalidData):
            self.begin(journal=link / "result.sqlite")

    def test_cli_round_trip_and_private_error(self):
        task = self.root / "task.txt"
        task.write_text(self.task, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["begin", "--journal", str(self.journal), "--db", str(self.db),
                "--preview", str(self.preview), "--task", str(task), "--policy", str(self.policy),
                "--expected-manifest-sha256", self.export["manifest_sha256"], "--run-id", "demo", "--source-kind", "synthetic"]), 0)
        trial = json.loads(out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["result", "--journal", str(self.journal), "--trial-id", trial["trial_id"],
                "--expected-trial-sha256", trial["trial_sha256"], "--expected-revision", "0", "--outcome", "reported_failure",
                "--used", "--evidence", str(self.evidence), "--policy", str(self.policy), "--reporter", "r"]), 0)
            self.assertEqual(main(["report", "--journal", str(self.journal)]), 0)
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main(["report", "--journal", str(self.root / "missing")]), 2)
        self.assertNotIn(str(self.root), error.getvalue())


if __name__ == "__main__":
    unittest.main()
