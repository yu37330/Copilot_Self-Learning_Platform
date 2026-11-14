"""#6/#7: 外部I/OなしのPII検査とHold-out適用評価。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.frozen_bundle import freeze_memory_bundle
from copilot_learning.privacy_scan import load_policy, scan_frozen_bundle
from copilot_learning.semantic_holdout import evaluate_semantic_holdout
from copilot_learning.semantic_gate import task_hash
from copilot_learning.learning import candidate_sha256
from scripts.prepare_sessions import prepare
from scripts.scan_privacy_offline import main as privacy_main
from scripts.evaluate_semantic_holdout import main as semantic_main


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        self.events = self.session / "events.jsonl"
        self.staging = self.root / "staging"
        self.bundle = self.root / "frozen"
        self.policy = self.root / "policy.json"
        self.params = dict(memory_id="PocMemory-1234567890", actor_id="poc_actor",
                           account_id="123456789012", region="ap-northeast-1")
        self.policy.write_text(json.dumps({"schema_version": "1.0", "record_type": "offline-sensitive-literals",
                                          "sensitive_literals": ["非公開案件ABC"]}, ensure_ascii=False), encoding="utf-8")

    def freeze(self, content="合成テスト完了", feedback=None):
        self.events.write_text(json.dumps({"type": "message", "role": "user", "text": content,
                                          "timestamp": "2026-11-01T00:00:00Z"}, ensure_ascii=False) + "\n", encoding="utf-8")
        os.utime(self.events, (1_700_000_000, 1_700_000_000))
        archive = prepare(self.session, self.staging, 30)
        fp = archive.stem.removeprefix("session-")
        freeze_memory_bundle(self.staging, fp, self.bundle, feedback_file=feedback, **self.params)
        receipt = digest((self.bundle / "receipt.json").read_bytes())
        return fp, receipt

    def scan(self, receipt):
        return scan_frozen_bundle(self.bundle, self.policy, expected_receipt_sha256=receipt, **self.params)

    def test_no_findings_does_not_authorize_transfer(self):
        _, receipt = self.freeze()
        data = self.scan(receipt)
        self.assertEqual(data["status"], "manual_review_required")
        self.assertFalse(data["external_transfer_authorized"])
        self.assertEqual(data["network_calls"], 0)
        self.assertEqual(data["findings_total"], 0)

    def test_email_org_literal_and_phone_detected_without_raw_values(self):
        _, receipt = self.freeze("非公開案件ABC の連絡先 employee@example.invalid、090-1234-5678")
        with patch("copilot_learning.aws_bridge.aws_command") as aws:
            data = self.scan(receipt)
            aws.assert_not_called()
        self.assertEqual(data["status"], "blocked_findings")
        self.assertIn("organization_literal", data["findings_by_file_and_rule"]["archive/events.jsonl"])
        self.assertIn("email_address", data["findings_by_file_and_rule"]["archive/events.jsonl"])
        self.assertIn("jp_mobile_number", data["findings_by_file_and_rule"]["archive/events.jsonl"])
        serialized = json.dumps(data, ensure_ascii=False)
        for secret in ("非公開案件ABC", "employee@example.invalid", "090-1234-5678"):
            self.assertNotIn(secret, serialized)

    def test_feedback_is_scanned_and_not_returned(self):
        feedback = self.root / "feedback.json"
        # 合成のメールアドレスを任意フィードバック側で追加する
        self.events.write_text('')
        content = "合成テスト"
        self.events.write_text(json.dumps({"type": "message", "role": "user", "text": content,
                                          "timestamp": "2026-11-01T00:00:00Z"}, ensure_ascii=False)+"\n", encoding="utf-8")
        os.utime(self.events, (1_700_000_000, 1_700_000_000))
        archive = prepare(self.session, self.staging, 30)
        fp = archive.stem.removeprefix("session-")
        feedback.write_text(json.dumps({"session_fingerprint":fp,"outcome":"unverified",
                                        "reusable_lesson":"reviewer@company.invalid"}), encoding="utf-8")
        freeze_memory_bundle(self.staging, fp, self.bundle, feedback_file=feedback, **self.params)
        receipt = digest((self.bundle / "receipt.json").read_bytes())
        data = self.scan(receipt)
        self.assertGreater(data["findings_by_file_and_rule"]["feedback.json"]["email_address"], 0)
        self.assertNotIn("company.invalid", json.dumps(data))

    def test_receipt_tamper_fails_closed(self):
        _, receipt = self.freeze()
        with self.assertRaises(InvalidData):
            self.scan("0"*64)
        with self.assertRaises(InvalidData):
            self.scan("")
        (self.bundle / "memory_create_event_plan.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(InvalidData):
            self.scan(receipt)

    def test_policy_validation_and_symlink_rejection(self):
        self.policy.write_text(json.dumps({"schema_version":"1.0","record_type":"offline-sensitive-literals",
                                          "sensitive_literals":["社内限定語", "社内限定語"]}, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(InvalidData):
            load_policy(self.policy)
        self.policy.write_text(json.dumps({"schema_version":"1.0","record_type":"offline-sensitive-literals",
                                          "sensitive_literals":["private", "[a-z]*"]}), encoding="utf-8")
        loaded, sha = load_policy(self.policy)
        self.assertEqual(loaded["sensitive_literals"][0], "private")
        self.assertEqual(len(sha), 64)
        sym = self.root / "policy-link.json"
        sym.symlink_to(self.policy)
        with self.assertRaises(InvalidData):
            load_policy(sym)

    def test_cli_exit_codes_and_no_overwrite(self):
        _, receipt = self.freeze("test@example.invalid")
        output = self.root / "report.json"
        args=["--bundle",str(self.bundle),"--policy",str(self.policy),
              "--memory-id",self.params["memory_id"],"--actor-id",self.params["actor_id"],
              "--account-id",self.params["account_id"],"--region",self.params["region"],
              "--expected-receipt-sha256",receipt,"--out",str(output)]
        self.assertEqual(privacy_main(args),1)
        self.assertEqual(privacy_main(args),2)
        self.assertTrue(output.exists())
        self.assertNotIn("test@example.invalid",output.read_text("utf-8"))


class SemanticHoldoutTests(unittest.TestCase):
    def setUp(self):
        self.item = {
            "knowledge_type":"skill", "title":"Python環境確認", "description":"テスト環境の切り分け",
            "instruction":"Pythonの仮想環境をテスト前に確認", "generalization":"環境不一致に適用",
            "evidence_ids":["a"*64+":0"], "applies_when":["Python","仮想環境"],
            "not_when":["API仕様不一致"], "counterexamples":["認証エラー"],
        }
        cases = [
            {"case_id":"positive","group_id":"group-p","task":"Python 仮想環境のテスト", "expected":"applicable"},
            {"case_id":"explicit-negative","group_id":"group-n","task":"Python 仮想環境 API仕様不一致", "expected":"not_applicable"},
            {"case_id":"implicit-negative","group_id":"group-b","task":"Python 仮想環境は正常だがテスト失敗", "expected":"not_applicable"},
            {"case_id":"unknown","group_id":"group-u","task":"Pythonのテストで失敗", "expected":"applicable"},
        ]
        for case in cases:
            case["candidate"] = self.item
            case["review"] = None
        cases[0]["review"] = self.review(cases[0]["task"], "applicable", "", "")
        cases[1]["review"] = self.review(cases[1]["task"], "not_applicable", "API仕様不一致", "API仕様不一致")
        cases[2]["review"] = self.review(cases[2]["task"], "not_applicable", "仮想環境", "仮想環境は正常")
        self.dataset = {"schema_version":"1.0","label_source":"synthetic","training_group_ids":["train-group"],
                        "training_task_sha256":[],"cases":cases}

    def review(self, task, verdict, condition, quote):
        return {"candidate_sha256":candidate_sha256(self.item),"task_sha256":task_hash(task),
                "verdict":verdict,"condition":condition,"quote":quote,
                "reason":"合成レビューによる判定"}

    def test_evidence_gate_abstains_where_literal_misapplies(self):
        report = evaluate_semantic_holdout(self.dataset)
        self.assertEqual(report["total_cases"],4)
        self.assertEqual(report["literal_baseline"]["fp"],1)
        self.assertEqual(report["evidence_review_gate"]["fp"],0)
        self.assertEqual(report["evidence_review_gate"]["abstain"],2)
        self.assertFalse(report["real_llm_quality_verified"])
        self.assertFalse(report["copilot_improvement_verified"])
        self.assertEqual(len(report["cases"]),4)
        self.assertNotIn("Python 仮想環境は正常", json.dumps(report, ensure_ascii=False))

    def test_review_hash_tampering_rejected(self):
        dataset=json.loads(json.dumps(self.dataset,ensure_ascii=False))
        dataset["cases"][1]["review"]["candidate_sha256"]="0"*64
        with self.assertRaises(InvalidData):
            evaluate_semantic_holdout(dataset)

    def test_train_test_group_overlap_rejected(self):
        dataset=json.loads(json.dumps(self.dataset,ensure_ascii=False))
        dataset["training_group_ids"]=["group-p"]
        with self.assertRaises(InvalidData):
            evaluate_semantic_holdout(dataset)

    def test_duplicate_task_rejected(self):
        dataset=json.loads(json.dumps(self.dataset,ensure_ascii=False))
        dataset["cases"][3]["task"]=dataset["cases"][0]["task"]
        with self.assertRaises(InvalidData):
            evaluate_semantic_holdout(dataset)

    def test_missing_source_rejected(self):
        dataset={**self.dataset,"label_source":"LLM verified"}
        with self.assertRaises(InvalidData):
            evaluate_semantic_holdout(dataset)

    def test_cli_is_offline_and_idempotence_requires_new_path(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/"dataset.json"
            out=Path(temp)/"out.json"
            source.write_bytes(json_bytes(self.dataset))
            with patch("copilot_learning.aws_bridge.aws_command") as aws:
                self.assertEqual(semantic_main(["--dataset",str(source),"--out",str(out)]),0)
                aws.assert_not_called()
            self.assertEqual(semantic_main(["--dataset",str(source),"--out",str(out)]),2)
            report=json.loads(out.read_text("utf-8"))
            self.assertNotIn("仮想環境は正常",json.dumps(report,ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
