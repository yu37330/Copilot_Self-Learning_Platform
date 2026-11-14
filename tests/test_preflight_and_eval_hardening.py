"""外部通信禁止のまま、承認前スコープとHold-outリークを厳密に検証する。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, json_bytes
from copilot_learning.evals import evaluate_holdout, task_fingerprint
from copilot_learning.preflight import build_memory_preflight, verify_memory_preflight
from copilot_learning.review_safety import ExternalExecutionBlocked, require_online_release
from scripts.prepare_sessions import prepare
from scripts.preflight_offline import main as preview_main


class PreflightAndLeakageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / "session-one"
        self.session.mkdir()
        self.events = self.session / "events.jsonl"
        self.events.write_text(
            json.dumps({"type": "message", "role": "user", "text": "テスト環境を調べる",
                        "timestamp": "2026-11-01T00:00:00Z"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.utime(self.events, (1_700_000_000, 1_700_000_000))
        self.staging = self.root / "staging"
        self.archive = prepare(self.session, self.staging, 30)
        self.fp = self.archive.stem.replace("session-", "")
        self.kwargs = {"memory_id": "PocMemory-1234567890", "actor_id": "poc_actor",
                       "account_id": "123456789012", "region": "ap-northeast-1"}

    def test_preview_replays_identically_without_raw_text(self):
        a = build_memory_preflight(self.staging, self.fp, **self.kwargs)
        b = verify_memory_preflight(a, self.staging, self.fp, **self.kwargs)
        self.assertEqual(a, b)
        self.assertEqual(a["authorization_status"], "not_attested")
        self.assertEqual(a["network_calls"], 0)
        self.assertNotIn("テスト環境", json.dumps(a, ensure_ascii=False))

    def test_modified_scope_or_target_is_rejected(self):
        plan = build_memory_preflight(self.staging, self.fp, **self.kwargs)
        for key, value in (("actor_id", "other_actor"), ("memory_id", "PocMemory-0123456789"),
                           ("account_id", "000000000000"), ("region", "us-east-1")):
            with self.subTest(key=key), self.assertRaises(InvalidData):
                verify_memory_preflight(plan, self.staging, self.fp, **{**self.kwargs, key: value})

    def test_raw_zip_tamper_refused_and_aws_never_used(self):
        plan = build_memory_preflight(self.staging, self.fp, **self.kwargs)
        self.archive.write_bytes(self.archive.read_bytes() + b"tamper")
        with patch("copilot_learning.aws_bridge.aws_command") as aws:
            with self.assertRaises(InvalidData):
                verify_memory_preflight(plan, self.staging, self.fp, **self.kwargs)
            aws.assert_not_called()

    def test_feedback_changed_after_preview_is_rejected(self):
        feedback = self.root / "feedback.json"
        feedback.write_text(json.dumps({"session_fingerprint": self.fp, "outcome": "unverified",
                                        "reusable_lesson": "動作確認"}), encoding="utf-8")
        plan = build_memory_preflight(self.staging, self.fp, feedback_file=feedback, **self.kwargs)
        feedback.write_text(json.dumps({"session_fingerprint": self.fp, "outcome": "verified_success",
                                        "reusable_lesson": "別の結論"}), encoding="utf-8")
        with self.assertRaises(InvalidData):
            verify_memory_preflight(plan, self.staging, self.fp, feedback_file=feedback, **self.kwargs)

    def test_unknown_event_is_blocked_before_preview(self):
        self.events.write_text('{"type":"tool_result","output":"PASS"}\n', encoding="utf-8")
        os.utime(self.events, (1_700_000_000, 1_700_000_000))
        other = prepare(self.session, self.root / "other", 30)
        with self.assertRaisesRegex(InvalidData, "未知"):
            build_memory_preflight(other.parent, other.stem.replace("session-", ""), **self.kwargs)

    def test_preflight_cli_is_offline_only_and_does_not_overwrite(self):
        out = self.root / "preview.json"
        argv = ["preview", "--staging", str(self.staging), "--fingerprint", self.fp,
                "--memory-id", self.kwargs["memory_id"], "--actor-id", self.kwargs["actor_id"],
                "--account-id", self.kwargs["account_id"], "--region", self.kwargs["region"],
                "--out", str(out)]
        with patch("copilot_learning.aws_bridge.aws_command") as aws:
            self.assertEqual(preview_main(argv), 0)
            self.assertEqual(preview_main(["verify", *argv[1:]]), 0)
            self.assertEqual(preview_main(argv), 2)  # 上書きしない
            aws.assert_not_called()
        with self.assertRaises(ExternalExecutionBlocked):
            require_online_release()

    def _holdout(self):
        train = "Python テストで 仮想環境が 不一致"
        return {"label_source": "synthetic_fixture", "training_group_ids": ["train-env"],
                "training_task_sha256": [task_fingerprint(train)],
                "cases": [
                    {"case_id": "a", "group_id": "test-env", "expected": "applicable", "task": "環境変数を確認する"},
                    {"case_id": "b", "group_id": "test-api", "expected": "not_applicable", "task": "APIレスポンス形式が不一致"},
                ]}

    def test_task_fingerprint_normalizes_width_and_case(self):
        self.assertEqual(task_fingerprint("Ｐｙｔｈｏｎ  TEST"), task_fingerprint("python test"))

    def test_training_task_duplication_rejected(self):
        ds = self._holdout()
        ds["cases"][0]["task"] = "ＰＹＴＨＯＮ テストで 仮想環境が 不一致"
        with self.assertRaisesRegex(InvalidData, "重複"):
            evaluate_holdout(ds, {})

    def test_holdout_duplicate_task_different_id_rejected(self):
        ds = self._holdout()
        ds["cases"][1]["task"] = ds["cases"][0]["task"]
        with self.assertRaisesRegex(InvalidData, "重複"):
            evaluate_holdout(ds, {})

    def test_task_hash_tampering_rejected(self):
        ds = self._holdout()
        ds["cases"][1]["task_sha256"] = "a" * 64
        with self.assertRaisesRegex(InvalidData, "一致"):
            evaluate_holdout(ds, {})

    def test_holdout_reports_incomplete_when_no_task_evidence(self):
        ds = {"training_group_ids": ["train"], "cases": [
            {"case_id": "id", "group_id": "holdout", "expected": "not_applicable"}]}
        result = evaluate_holdout(ds, {})
        self.assertFalse(result["leakage_checks"]["complete"])
        self.assertEqual(result["leakage_checks"]["task_fingerprint_checked_count"], 0)
        self.assertIsNone(result["results"]["A"]["accuracy"])

    def test_fpr_and_fnr_have_disjoint_denominators(self):
        ds = self._holdout()
        data = evaluate_holdout(ds, {"B": {"a": "not_applicable", "b": "applicable"}})
        result = data["results"]["B"]
        self.assertEqual(result["false_application_rate"], 1.0)
        self.assertEqual(result["false_rejection_rate"], 1.0)
        self.assertTrue(data["leakage_checks"]["complete"])
        self.assertEqual(result["positive_cases"], 1)
        self.assertEqual(result["negative_cases"], 1)
        self.assertEqual(data["results"]["A"]["status"], "not_executed")


if __name__ == "__main__":
    unittest.main()
