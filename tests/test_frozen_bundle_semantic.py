"""AWSを呼ばず、送信前スナップショットと意味レビューの安全条件を検査。"""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData
from copilot_learning.frozen_bundle import freeze_memory_bundle, verify_memory_bundle
from copilot_learning.semantic_gate import (semantic_review_prompt, task_hash,
                                            verify_semantic_review)
from copilot_learning.learning import candidate_sha256
from scripts.prepare_sessions import prepare
from scripts.freeze_memory_offline import main as freeze_cli
from scripts.review_applicability_offline import main as semantic_cli


class FrozenBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sess = self.root / "s"
        self.sess.mkdir()
        event = self.sess / "events.jsonl"
        event.write_text(json.dumps({"type": "message", "role": "user",
                                     "text": "合成Pythonテスト失敗", "timestamp": "2026-11-01T00:00:00Z"},
                                    ensure_ascii=False) + "\n", encoding="utf-8")
        os.utime(event, (1_700_000_000, 1_700_000_000))
        self.staging = self.root / "staging"
        archive = prepare(self.sess, self.staging, 30)
        self.fp = archive.stem.removeprefix("session-")
        self.out = self.root / "frozen"
        self.kw = {"memory_id": "PocMemory-1234567890", "actor_id": "poc_actor",
                   "account_id": "123456789012", "region": "ap-northeast-1"}

    def _freeze(self):
        return freeze_memory_bundle(self.staging, self.fp, self.out, **self.kw)

    def test_freeze_verify_and_original_change_do_not_change_snapshot(self):
        r = self._freeze()
        self.assertEqual(r["authorization_status"], "not_attested")
        self.assertEqual(r["network_calls"], 0)
        self.assertEqual(verify_memory_bundle(self.out, **self.kw), r)
        (self.staging / f"session-{self.fp}.zip").write_bytes(b"corrupted")
        self.assertEqual(verify_memory_bundle(self.out, **self.kw), r)

    def test_every_frozen_file_rejects_tampering(self):
        self._freeze()
        for name in (self.out / "receipt.json", *[p for p in self.out.iterdir() if p.name != "receipt.json"]):
            before = name.read_bytes()
            try:
                name.write_bytes(before + b"X")
                with self.subTest(file=name.name), self.assertRaises((InvalidData, ValueError, UnicodeError, json.JSONDecodeError)):
                    verify_memory_bundle(self.out, **self.kw)
            finally:
                name.write_bytes(before)

    def test_wrong_target_has_no_aws_calls(self):
        self._freeze()
        for name, value in [("memory_id", "ElseMemory-1234567890"), ("actor_id", "other"),
                            ("region", "us-east-1"), ("account_id", "000000000000")]:
            with self.subTest(name=name), patch("copilot_learning.aws_bridge.aws_command") as aws:
                with self.assertRaises(InvalidData):
                    verify_memory_bundle(self.out, **{**self.kw, name: value})
                aws.assert_not_called()

    def test_independent_receipt_digest_detects_full_bundle_rewrite(self):
        self._freeze()
        original_digest = hashlib.sha256((self.out / "receipt.json").read_bytes()).hexdigest()
        self.assertEqual(verify_memory_bundle(self.out, expected_receipt_sha256=original_digest,
                                              **self.kw)["session_fingerprint"], self.fp)
        receipt_path = self.out / "receipt.json"
        receipt = json.loads(receipt_path.read_text("utf-8"))
        receipt["files"]["preflight.json"]["size_bytes"] += 1
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(InvalidData):
            verify_memory_bundle(self.out, expected_receipt_sha256=original_digest, **self.kw)

    def test_freeze_and_verify_cli_do_not_call_aws(self):
        common = ["--out", str(self.out), "--memory-id", self.kw["memory_id"],
                  "--actor-id", self.kw["actor_id"], "--account-id", self.kw["account_id"],
                  "--region", self.kw["region"]]
        with patch("copilot_learning.aws_bridge.aws_command") as aws:
            self.assertEqual(freeze_cli(["freeze", "--staging", str(self.staging),
                                         "--fingerprint", self.fp, *common]), 0)
            independent = hashlib.sha256((self.out / "receipt.json").read_bytes()).hexdigest()
            self.assertEqual(freeze_cli(["verify", *common, "--expected-receipt-sha256", independent]), 0)
            aws.assert_not_called()

    def test_no_overwrite_and_no_git_export(self):
        self._freeze()
        with self.assertRaises(InvalidData):
            self._freeze()
        git_root = self.root / "git-root"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        with self.assertRaises(InvalidData):
            freeze_memory_bundle(self.staging, self.fp, git_root / "out", **self.kw)

    def test_feedback_included_and_changed_feedback_rejected(self):
        feedback = self.root / "feedback.json"
        feedback.write_text(json.dumps({"session_fingerprint": self.fp, "outcome": "unverified",
                                        "reusable_lesson": "成功か不明"}, ensure_ascii=False), encoding="utf-8")
        freeze_memory_bundle(self.staging, self.fp, self.out, feedback_file=feedback, **self.kw)
        self.assertEqual(verify_memory_bundle(self.out, **self.kw)["files"]["feedback.json"]["size_bytes"], feedback.stat().st_size)
        (self.out / "feedback.json").write_text('{"tampered":true}', encoding="utf-8")
        with self.assertRaises(InvalidData):
            verify_memory_bundle(self.out, **self.kw)

    def test_unknown_events_fail_closed(self):
        event = self.sess / "events.jsonl"
        event.write_text('{"type":"tool_result","output":"PASS"}\n', encoding="utf-8")
        os.utime(event, (1_700_000_000, 1_700_000_000))
        other = prepare(self.sess, self.root / "other", 30)
        with self.assertRaises(InvalidData):
            freeze_memory_bundle(other.parent, other.stem.removeprefix("session-"), self.out, **self.kw)


class SemanticGateTests(unittest.TestCase):
    def setUp(self):
        self.item = {"knowledge_type": "skill", "title": "Python仮想環境", "description": "テスト原因調査",
                     "instruction": "Pythonの仮想環境を確認", "generalization": "環境差分に適用",
                     "evidence_ids": ["a"*64+":0"], "applies_when": ["Python", "仮想環境"],
                     "not_when": ["API仕様不一致"], "counterexamples": ["認証エラー"]}
        self.task = "Python 仮想環境は正常だがAPIの仕様変更で失敗した"

    def _review(self, task=None, **updates):
        task = self.task if task is None else task
        return {"candidate_sha256": candidate_sha256(self.item), "task_sha256": task_hash(task),
                "verdict": "not_applicable", "condition": "仮想環境", "quote": "仮想環境は正常",
                "reason": "この環境は正常と明示されており原因ではない", **updates}

    def test_positive_vote_cannot_escalate_unproven_task(self):
        task = "Python テスト失敗、原因不明"
        vote = {"candidate_sha256":candidate_sha256(self.item),"task_sha256":task_hash(task),
                "verdict":"applicable","condition":"","quote":"","reason":"使いたい"}
        r = verify_semantic_review(self.item, task, vote)
        self.assertEqual(r["decision"], "needs_review")
        self.assertFalse(r["approved_for_copilot"])

    def test_negative_claim_with_scope_and_quote_requires_review(self):
        r = verify_semantic_review(self.item, self.task, self._review())
        self.assertEqual(r["decision"], "needs_review")
        self.assertFalse(r["approved_for_copilot"])

    def test_negative_explicit_exclusion(self):
        task = "Pythonの仮想環境で認証エラーが出た"
        vote = self._review(task, condition="認証エラー", quote="認証エラー")
        self.assertEqual(verify_semantic_review(self.item, task, vote)["decision"], "not_applicable")

    def test_invalid_sha_or_fabricated_quote(self):
        for replacement in ({"task_sha256": "0"*64}, {"quote": "本文にない文章"},
                            {"condition": "別のスキルの条件"}):
            with self.subTest(replacement=replacement), self.assertRaises(InvalidData):
                verify_semantic_review(self.item, self.task, {**self._review(), **replacement})

    def test_uncertain_vote_downgrades_positive(self):
        task = "Pythonの仮想環境を確認する"
        vote = {**self._review(task), "verdict":"needs_review", "condition":"", "quote":""}
        self.assertEqual(verify_semantic_review(self.item, task, vote)["decision"], "needs_review")

    def test_semantic_cli_uses_exact_task_and_only_downgrades(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            task = root / "task.txt"
            task.write_text(self.task, encoding="utf-8")
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps(self.item, ensure_ascii=False), encoding="utf-8")
            review = root / "review.json"
            review.write_text(json.dumps(self._review(), ensure_ascii=False), encoding="utf-8")
            output = root / "result.json"
            with patch("copilot_learning.aws_bridge.aws_command") as aws:
                self.assertEqual(semantic_cli(["--candidate", str(candidate), "--task", str(task),
                                               "--review", str(review), "--out", str(output)]), 0)
                aws.assert_not_called()
            self.assertEqual(json.loads(output.read_text("utf-8"))["decision"], "needs_review")
            self.assertEqual(semantic_cli(["--candidate", str(candidate), "--task", str(task),
                                           "--review", str(review), "--out", str(output)]), 2)

    def test_semantic_candidate_secret_is_rejected(self):
        bad = {**self.item, "instruction": "token AKIAABCDEFGHIJKLMNOP"}
        with self.assertRaises(InvalidData):
            semantic_review_prompt(bad, self.task)

    def test_prompt_is_offline_data_with_hashes(self):
        p = semantic_review_prompt(self.item, self.task)
        self.assertEqual(p["user_data"]["task_sha256"], task_hash(self.task))
        self.assertNotIn("Python", p["system"])


if __name__ == "__main__":
    unittest.main()
