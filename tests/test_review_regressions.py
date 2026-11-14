"""外部レビューの再現例と修正後の境界値を合成データだけで検証する。"""
from __future__ import annotations
import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from copilot_learning.core import (InvalidData, json_bytes, digest, read_archive, normalize_events,
                                   build_memory_requests)
from copilot_learning.learning import (BadCandidate, candidate_sha256, grade_candidates,
                                       independent_llm_grade, BedrockConverseProvider)
from copilot_learning.review_safety import (ExternalExecutionBlocked, require_online_release,
    regenerate_memory_plan, ObjectSnapshot, put_snapshot, S3OperationError,
    operation_scope, validate_operation_scope)
from copilot_learning.aws_bridge import aws_command
from scripts.prepare_sessions import prepare
from scripts.run_learning_poc import main as learning_main


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "session"
        self.source.mkdir()
        self.events = [
            {"type": "message", "role": "tool", "text": "ビルド失敗", "timestamp": "2026-11-01T01:00:00Z"},
            {"type": "message", "role": "tool", "text": "文書確認成功", "timestamp": "2026-11-01T01:01:00Z"},
        ]
        log = self.source / "events.jsonl"
        log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in self.events), encoding="utf-8")
        os.utime(log, (1700000000, 1700000000))
        self.staging = self.root / "staging"
        archive = prepare(self.source, self.staging, 30)
        self.fp = archive.stem.removeprefix("session-")
        self.manifest, self.raw = read_archive(self.staging, self.fp)
        self.session = normalize_events(self.manifest, self.raw)
        self.bad = {"knowledge_type": "skill", "title": "Build", "description": "Build success",
                    "instruction": "ビルドは成功する", "generalization": "別のプロジェクトでも適用できる",
                    "evidence_ids": [f"{self.fp}:0"]}
        self.good = {**self.bad, "knowledge_type": "note", "title": "Docs",
                     "instruction": "文書確認を独立したチェックとして行う", "evidence_ids": [f"{self.fp}:1"]}
        self.feedback = [{"session_fingerprint": self.fp, "outcome": "verified_success",
                          "evidence": "文書確認成功", "evidence_ids": [f"{self.fp}:1"],
                          "candidate_sha256": candidate_sha256(self.good), "claim_verdict": "supported"},
                         {"session_fingerprint": self.fp, "outcome": "verified_failure",
                          "evidence": "ビルド失敗", "evidence_ids": [f"{self.fp}:0"]}]
        self.plan = build_memory_requests(self.session, "Memory-1234567890", "team:user")

    def test_reviewer_false_acceptance_is_pending(self):
        result = grade_candidates([self.bad], self.session, self.feedback)[0]
        self.assertEqual(result["decision"], "pending")
        self.assertIn("counterevidence_requires_review", result["reasons"])

    def test_only_scoped_correct_candidate_can_be_kept(self):
        unrelated = {**self.good, "title": "Unrelated", "instruction": "ビルド結果は確認不要"}
        results = grade_candidates([self.good, self.bad, unrelated], self.session, self.feedback)
        self.assertEqual([r["decision"] for r in results], ["keep", "pending", "pending"])
        self.assertTrue(all(not r["approved_for_copilot"] for r in results))

    def test_legacy_session_success_cannot_promote(self):
        feedback = [{"session_fingerprint": self.fp, "outcome": "verified_success", "evidence": "PASS"}]
        self.assertEqual(grade_candidates([self.bad], self.session, feedback)[0]["decision"], "pending")

    def test_claim_edit_invalidates_support(self):
        changed = {**self.good, "generalization": "全ての状況で正しい"}
        self.assertEqual(grade_candidates([changed], self.session, self.feedback)[0]["decision"], "pending")

    def test_contradiction_wins_over_scoped_success(self):
        conflicting = [*self.feedback, {**self.feedback[0], "outcome": "verified_failure"}]
        self.assertEqual(grade_candidates([self.good], self.session, conflicting)[0]["decision"], "pending")

    def test_feedback_for_other_session_does_not_promote(self):
        changed = [{**self.feedback[0], "session_fingerprint": "b" * 64}]
        self.assertEqual(grade_candidates([self.good], self.session, changed)[0]["decision"], "pending")

    def test_skipped_results_block_adoption(self):
        session = normalize_events(self.manifest, self.events + [{"type": "unknown", "result": "FAILED"}])
        self.assertEqual(session["skipped_details"], [{"event_index": 2, "reason": "unsupported_type"}])
        self.assertEqual(grade_candidates([self.good], session, self.feedback)[0]["decision"], "pending")

    def test_grader_receives_quotes_generalization_and_counterevidence(self):
        rows = grade_candidates([self.good], self.session, self.feedback)
        model = MagicMock()
        model.generate.return_value = {"decisions": [{"knowledge_id": rows[0]["knowledge_id"], "decision": "pending", "reason": "more evidence"}]}
        independent_llm_grade(model, rows, self.session, self.feedback)
        data = json.loads(model.generate.call_args.args[0])
        self.assertIn("generalization", data["candidates"][0])
        self.assertEqual(data["evidence"][0]["text"], "ビルド失敗")
        self.assertEqual(data["feedback_including_counterevidence"][1]["outcome"], "verified_failure")
        self.assertIn("system", model.generate.call_args.kwargs)

    def test_grader_missing_context_does_not_keep(self):
        rows = grade_candidates([self.good], self.session, self.feedback)
        model = MagicMock()
        self.assertEqual(independent_llm_grade(model, rows)[0]["decision"], "pending")
        model.generate.assert_not_called()

    def test_grader_reason_is_scanned_before_storage(self):
        rows = grade_candidates([self.good], self.session, self.feedback)
        model = MagicMock()
        model.generate.return_value = {"decisions": [{"knowledge_id": rows[0]["knowledge_id"], "decision": "keep", "reason": "AKIAABCDEFGHIJKLMNOP"}]}
        result = independent_llm_grade(model, rows, self.session, self.feedback)
        self.assertEqual(result[0]["decision"], "pending")
        self.assertNotIn("AKIA", json.dumps(result))

    def test_model_truncation_and_nontext_are_rejected(self):
        client = MagicMock()
        provider = BedrockConverseProvider("fake", "ap-northeast-1", client=client)
        for response in [
            {"stopReason": "max_tokens", "output": {"message": {"content": [{"text": "{}"}]}}},
            {"stopReason": "end_turn", "output": {"message": {"content": [{"toolUse": {}}]}}},
        ]:
            with self.subTest(response=response):
                client.converse.return_value = response
                with self.assertRaises(BadCandidate): provider.generate("{}")

    def test_usage_and_system_are_preserved(self):
        client = MagicMock()
        client.converse.return_value = {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 2},
            "output": {"message": {"content": [{"text": "{}"}]}}}
        provider = BedrockConverseProvider("fake", "ap-northeast-1", client=client)
        provider.generate("{}")
        self.assertEqual(provider.last_metadata["usage"]["inputTokens"], 1)
        self.assertIn("system", client.converse.call_args.kwargs)

    def test_memory_regenerates_equal_new_object(self):
        result = regenerate_memory_plan(self.manifest, self.raw, self.plan, "Memory-1234567890", "team:user")
        self.assertEqual(result, self.plan)
        self.assertIsNot(result, self.plan)

    def test_memory_mutations_are_rejected_before_any_send(self):
        edits = [
            lambda p: p[0]["payload"][0]["conversational"]["content"].update(text="未承認本文"),
            lambda p: p[0].update(actorId="other"),
            lambda p: p[0].update(clientToken="a" * 64),
            lambda p: p[0].update(eventTimestamp="2026-11-02T00:00:00Z"),
            lambda p: p.append(copy.deepcopy(p[0])),
        ]
        for edit in edits:
            with self.subTest(edit=edit):
                modified = copy.deepcopy(self.plan); edit(modified)
                sender = MagicMock()
                with self.assertRaises(InvalidData):
                    regenerated = regenerate_memory_plan(self.manifest, self.raw, modified, "Memory-1234567890", "team:user")
                    sender(regenerated)
                sender.assert_not_called()

    def test_invalid_real_memory_id_is_rejected(self):
        with self.assertRaises(InvalidData):
            regenerate_memory_plan(self.manifest, self.raw, self.plan, "REPLACE_WITH_REAL_MEMORY_ID", "team:user")

    def test_scope_binds_namespace_model_region_actor_feedback(self):
        target = {"namespace": "/actor/session/", "model": "m1", "region": "ap-northeast-1", "actor": "u1"}
        expected = operation_scope("memory-export", target, {}, {"feedback": "a" * 64})
        validate_operation_scope(expected, expected)
        for field in target:
            with self.subTest(field=field):
                actual = operation_scope("memory-export", {**target, field: "other"}, {}, {"feedback": "a" * 64})
                with self.assertRaises(InvalidData): validate_operation_scope(expected, actual)
        changed = operation_scope("memory-export", target, {}, {"feedback": "b" * 64})
        with self.assertRaises(InvalidData): validate_operation_scope(expected, changed)

    def test_repack_preserves_manifest_and_memory_plan(self):
        path = self.staging / f"session-{self.fp}.manifest.json"
        before = path.read_bytes()
        prepare(self.source, self.staging, 30)
        self.assertEqual(path.read_bytes(), before)
        manifest, raw = read_archive(self.staging, self.fp)
        self.assertEqual(build_memory_requests(normalize_events(manifest, raw), "Memory-1234567890", "team:user"), self.plan)

    def test_fingerprint_mismatch_is_rejected(self):
        wrong = "b" * 64
        archive = self.staging / f"session-{self.fp}.zip"
        bad = {k: v for k, v in self.manifest.items() if not k.startswith("_")}
        bad.update(session_fingerprint=wrong, archive_file=f"session-{wrong}.zip")
        (self.staging / f"session-{wrong}.zip").write_bytes(archive.read_bytes())
        (self.staging / f"session-{wrong}.manifest.json").write_bytes(json_bytes(bad))
        with self.assertRaisesRegex(InvalidData, "fingerprint"): read_archive(self.staging, wrong)

    def test_timestamp_fallback_is_explicit(self):
        event = {"type": "message", "role": "user", "text": "hello"}
        self.assertEqual(normalize_events(self.manifest, [event])["timestamp_fallback_count"], 1)

    def test_s3_uses_immutable_snapshot_and_service_checksum(self):
        file = self.root / "data.bin"; file.write_bytes(b"approved")
        snapshot = ObjectSnapshot.capture(file, digest(b"approved"))
        file.write_bytes(b"changed after review")
        observed = []
        def fake(args):
            observed.append(args)
            self.assertEqual(Path(args[args.index("--body") + 1]).read_bytes(), b"approved")
            self.assertEqual(args[args.index("--checksum-sha256") + 1], snapshot.checksum_base64)
            self.assertIn("--expected-bucket-owner", args)
            return {"ChecksumSHA256": snapshot.checksum_base64, "VersionId": "synthetic-v1"}
        result = put_snapshot(snapshot, bucket="test-bucket", key="p/key", account_id="123456789012",
            kms_key_arn="arn:aws:kms:ap-northeast-1:123456789012:key/test", command=fake)
        self.assertEqual(result["status"], "stored")
        self.assertEqual(len(observed), 1)

    def test_s3_metadata_only_match_is_rejected(self):
        file = self.root / "data.bin"; file.write_bytes(b"approved")
        snapshot = ObjectSnapshot.capture(file, digest(b"approved"))
        fake = MagicMock(side_effect=[S3OperationError("412"), {"Metadata": {"sha256": snapshot.sha256}}])
        with self.assertRaises(InvalidData):
            put_snapshot(snapshot, bucket="test-bucket", key="p/key", account_id="123456789012",
                kms_key_arn="arn:aws:kms:ap-northeast-1:123456789012:key/test", command=fake)

    def test_s3_conflict_is_success_only_with_verified_matching_object(self):
        file = self.root / "data.bin"; file.write_bytes(b"approved")
        snapshot = ObjectSnapshot.capture(file, digest(b"approved"))
        kms = "arn:aws:kms:ap-northeast-1:123456789012:key/test"
        for code in ("409", "412"):
            fake = MagicMock(side_effect=[S3OperationError(code), {"ChecksumSHA256": snapshot.checksum_base64,
                "ContentLength": len(snapshot.body), "ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms}])
            result = put_snapshot(snapshot, bucket="test-bucket", key="p/key", account_id="123456789012", kms_key_arn=kms, command=fake)
            self.assertEqual(result["status"], "already_present")

    def test_unreviewed_changed_file_is_rejected(self):
        file = self.root / "data.bin"; file.write_bytes(b"new")
        with self.assertRaises(InvalidData): ObjectSnapshot.capture(file, digest(b"old"))

    def test_aws_cli_is_blocked_before_subprocess(self):
        with patch("copilot_learning.aws_bridge.subprocess.run") as run:
            with self.assertRaises(ExternalExecutionBlocked):
                aws_command(["sts", "get-caller-identity"], region="ap-northeast-1")
            run.assert_not_called()

    def test_bedrock_without_injected_client_is_blocked(self):
        with self.assertRaises(ExternalExecutionBlocked): BedrockConverseProvider("real", "ap-northeast-1")

    def test_online_cli_is_blocked_before_account_or_sdk(self):
        with patch("scripts.run_learning_poc.assert_account") as account:
            result = learning_main(["--staging", str(self.staging), "--fingerprint", self.fp,
                "--out", str(self.root / "out"), "--use-bedrock"])
            self.assertEqual(result, 2)
            account.assert_not_called()

    def test_powershell_is_utf8_with_bom_and_has_egress_guard(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "upload_sessions.ps1"
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        text = path.read_text("utf-8-sig")
        self.assertLess(text.index("if ($Upload)"), text.index("& aws"))


if __name__ == "__main__": unittest.main()
