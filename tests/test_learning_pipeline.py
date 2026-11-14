"""AWS/Copilotなしで動く、合成イベントによる単体・統合テスト。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from copilot_learning.aws_bridge import (assert_account, validate_approval, send_memory_plan)
from copilot_learning.core import (InvalidData, build_memory_requests, json_bytes, load_feedback,
                                   normalize_events, read_archive)
from copilot_learning.learning import (BadCandidate, independent_llm_grade, make_report,
                                       grade_candidates, validate_candidates, memory_records_as_candidates,
                                       generate_candidates, BedrockConverseProvider)
from scripts.prepare_sessions import prepare
from scripts.run_learning_poc import process


class OfflinePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "session-1"
        self.source.mkdir()
        self.staging = self.root / "staging"
        self.logs = [
            {"type": "message", "role": "user", "text": "Pythonのテストが失敗しました", "timestamp": "2026-11-01T10:00:00Z"},
            {"type": "tool_result", "name": "test", "output": "FAIL"},
            {"type": "message", "role": "assistant", "text": "環境を確認して再実行します", "timestamp": "2026-11-01T10:01:00Z"},
            {"type": "message", "role": "tool", "text": "12 tests passed", "timestamp": "2026-11-01T10:02:00Z"},
        ]
        event_file = self.source / "events.jsonl"
        event_file.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in self.logs) + "\n", encoding="utf-8")
        os.utime(event_file, (1_700_000_000, 1_700_000_000))
        archive = prepare(self.source, self.staging, minimum_age_minutes=30)
        self.fp = archive.stem.removeprefix("session-")
        self.manifest, self.events = read_archive(self.staging, self.fp)
        self.session = normalize_events(self.manifest, self.events)
        self.candidate = {"knowledge_type": "note", "title": "Python environment",
                          "description": "Check env", "instruction": "Python実行環境を確認してからテストする",
                          "generalization": "複数のPythonプロジェクトで適用可能",
                          "evidence_ids": [f"{self.fp}:2", f"{self.fp}:3"]}
        self.feedback = [{"outcome": "verified_success", "evidence": "12 PASS",
                          "session_fingerprint": self.fp, "reusable_lesson": "環境を確認", "feedback_id": "fb1"}]

    def test_verified_manifest_normalizer_and_memory_plan(self):
        self.assertEqual(len(self.events), 4)
        self.assertEqual(self.session["recognized_event_count"], 3)
        self.assertEqual(self.session["skipped_event_count"], 1)
        self.assertEqual(self.session["messages"][0]["role"], "USER")
        plan = build_memory_requests(self.session, "MyMemory-1234567890", "poc_user")
        self.assertEqual(len(plan), 3)
        self.assertEqual(plan[0]["payload"][0]["conversational"]["role"], "USER")
        self.assertEqual(len(plan[0]["clientToken"]), 64)
        self.assertEqual(plan, build_memory_requests(self.session, "MyMemory-1234567890", "poc_user"))
        self.assertNotIn("tool_result", json.dumps(plan))

    def test_corrupted_zip_is_rejected(self):
        archive = self.staging / f"session-{self.fp}.zip"
        archive.write_bytes(archive.read_bytes() + b"tamper")
        with self.assertRaisesRegex(InvalidData, "SHA-256"):
            read_archive(self.staging, self.fp)

    def test_corrupted_manifest_contents_is_rejected(self):
        path = self.staging / f"session-{self.fp}.manifest.json"
        value = json.loads(path.read_text("utf-8"))
        value["contents"][0]["size_bytes"] += 1
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(InvalidData, "ファイルハッシュ"):
            read_archive(self.staging, self.fp)

    def test_unknown_format_fails_closed(self):
        other = normalize_events(self.manifest, [{"event": "prompt", "data": "maybe user text"}])
        self.assertEqual(other["format_status"], "unsupported_format")
        self.assertEqual(build_memory_requests(other, "Memory-1234567890", "user"), [])

    def test_feedback_fingerprint_mismatch(self):
        path = self.root / "feedback.json"
        path.write_text(json.dumps({"session_fingerprint": "other", "outcome": "verified_success",
                                    "reusable_lesson": "test"}), encoding="utf-8")
        with self.assertRaisesRegex(InvalidData, "対象セッション"):
            load_feedback(path, self.fp)

    def test_grade_with_verified_feedback_is_keep_but_not_auto_approved(self):
        graded = grade_candidates([self.candidate], self.session, self.feedback)
        self.assertEqual(graded[0]["decision"], "keep")
        self.assertFalse(graded[0]["approved_for_copilot"])

    def test_grade_without_feedback_is_pending(self):
        self.assertEqual(grade_candidates([self.candidate], self.session)[0]["decision"], "pending")

    def test_grade_unfounded_evidence_is_drop(self):
        item = {**self.candidate, "evidence_ids": ["not_real"]}
        graded = grade_candidates([item], self.session, self.feedback)
        self.assertEqual(graded[0]["decision"], "drop")

    def test_grade_risky_instruction_is_drop(self):
        item = {**self.candidate, "instruction": "disable security checks before deployment"}
        self.assertEqual(grade_candidates([item], self.session, self.feedback)[0]["decision"], "drop")

    def test_grade_secret_is_drop(self):
        item = {**self.candidate, "instruction": "token AKIAABCDEFGHIJKLMNOP"}
        self.assertEqual(grade_candidates([item], self.session, self.feedback)[0]["decision"], "drop")

    def test_grade_duplicate_is_drop(self):
        graded = grade_candidates([self.candidate, dict(self.candidate)], self.session, self.feedback)
        self.assertEqual([item["decision"] for item in graded], ["keep", "drop"])

    def test_workflow_requires_dry_run(self):
        item = {**self.candidate, "knowledge_type": "workflow"}
        self.assertEqual(grade_candidates([item], self.session, self.feedback)[0]["decision"], "pending")

    def test_memory_record_unlinked_is_pending_not_fabricated(self):
        rows = memory_records_as_candidates([{"content": {"text": "Use a verified test interpreter"}}], self.session)
        self.assertEqual(rows[0]["evidence_ids"], [])
        graded = grade_candidates(rows, self.session, self.feedback, allow_unlinked_memory=True)
        self.assertEqual(graded[0]["decision"], "pending")

    def test_validate_candidate_rejects_bad_schema_and_limit(self):
        with self.assertRaises(BadCandidate):
            validate_candidates({"candidates": [self.candidate] * 11})
        with self.assertRaises(BadCandidate):
            validate_candidates({"candidates": [{**self.candidate, "knowledge_type": "other"}]})

    def test_offline_process_with_explicit_candidates(self):
        proposals = self.root / "proposals.json"
        proposals.write_bytes(json_bytes({"candidates": [self.candidate]}))
        out = self.root / "outputs"
        report = process(self.staging, self.fp, out, "Memory-1234567890", "poc_user",
                         candidate_file=proposals)
        self.assertEqual(report["a_agentcore_memory"]["status"], "not_executed")
        self.assertEqual(report["b_agentrun_inspired"]["pending"], 1)
        self.assertFalse(report["quality_improvement_verified"])
        self.assertEqual(len(list(out.iterdir())), 4)
        self.assertEqual(process(self.staging, self.fp, out, "Memory-1234567890", "poc_user",
                                 candidate_file=proposals), report)

    def test_offline_process_memory_records_report(self):
        out = self.root / "outputs"
        memory = self.root / "records.json"
        memory.write_text(json.dumps({"memoryRecordSummaries": [{"content": {"text": "Check Python interpreter"}}]}))
        report = process(self.staging, self.fp, out, "Memory-1234567890", "poc_user",
                         memory_records_file=memory)
        self.assertEqual(report["a_agentcore_memory"]["memory_records"], 1)
        self.assertEqual(report["c_hybrid"]["pending"], 1)

    def test_second_run_never_overwrites_changed_results(self):
        proposals = self.root / "proposals.json"
        proposals.write_bytes(json_bytes({"candidates": [self.candidate]}))
        out = self.root / "outputs"
        process(self.staging, self.fp, out, "Memory-1234567890", "poc_user", candidate_file=proposals)
        (out / "comparison_report.json").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(InvalidData, "上書き"):
            process(self.staging, self.fp, out, "Memory-1234567890", "poc_user", candidate_file=proposals)

    def test_approval_validates_bucket_account_and_hash(self):
        path = self.root / "approval.json"
        manifest_hash = hashlib.sha256((self.staging / f"session-{self.fp}.manifest.json").read_bytes()).hexdigest()
        content = {"schema_version": "1.0", "record_type": "upload-approval", "status": "approved",
                   "session_fingerprint": self.fp, "archive_sha256": self.manifest["archive_sha256"],
                   "manifest_sha256": manifest_hash, "target_account_id": "123456789012",
                   "target_bucket": "my-approved-bucket", "reviewer": "reviewer", "reviewed_at_utc": "2026-11-14T00:00:00Z",
                   "data_classification": "synthetic"}
        path.write_bytes(json_bytes(content))
        validate_approval(path, self.fp, self.manifest["archive_sha256"], "123456789012",
                          "my-approved-bucket", manifest_hash)
        with self.assertRaisesRegex(InvalidData, "バケット"):
            validate_approval(path, self.fp, self.manifest["archive_sha256"], "123456789012", "wrong")

    def test_account_id_guard(self):
        with patch("copilot_learning.aws_bridge.aws_command", return_value={"Account": "999999999999"}):
            with self.assertRaisesRegex(InvalidData, "一致しません"):
                assert_account("123456789012", region="ap-northeast-1")

    def test_memory_plan_sender_mock(self):
        planned = build_memory_requests(self.session, "Memory-1234567890", "poc_user")[:1]
        with patch("copilot_learning.aws_bridge.aws_command", return_value={"event": {"eventId": "123#abc"}}) as api:
            receipts = send_memory_plan(planned, region="ap-northeast-1", profile="")
            self.assertEqual(receipts[0]["event_id"], "123#abc")
            self.assertEqual(api.call_count, 1)
            self.assertIn("create-event", api.call_args.args[0])

    def test_model_adaptor_with_fake_client(self):
        client = MagicMock()
        client.converse.return_value = {"output": {"message": {"content": [
            {"text": json.dumps({"candidates": [self.candidate]}, ensure_ascii=False)}]}}}
        provider = BedrockConverseProvider("fake-model", "ap-northeast-1", client=client)
        self.assertEqual(len(generate_candidates(provider, self.session, self.feedback)), 1)
        self.assertEqual(client.converse.call_count, 1)

    def test_independent_grader_cannot_overrule_policy_drop(self):
        graded = grade_candidates([{**self.candidate, "instruction": "disable security checks"}], self.session)
        provider = MagicMock()
        result = independent_llm_grade(provider, graded)
        self.assertEqual(result[0]["decision"], "drop")
        provider.generate.assert_not_called()

    def test_independent_grader_can_downgrade_keep(self):
        graded = grade_candidates([self.candidate], self.session, self.feedback)
        provider = MagicMock()
        provider.generate.return_value = {"decisions": [{"knowledge_id": graded[0]["knowledge_id"],
                                                         "decision": "pending", "reason": "needs second test"}]}
        updated = independent_llm_grade(provider, graded)
        self.assertEqual(updated[0]["decision"], "pending")

    def test_memory_plan_with_unrecognized_event_has_no_requests(self):
        session = normalize_events(self.manifest, [{"type": "unknown", "value": "token"}])
        self.assertEqual(make_report(session, [], None, None)["recognized_events"], 0)


if __name__ == "__main__":
    unittest.main()
