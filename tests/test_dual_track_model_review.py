"""Issue #31: 2系統・別モデル採点の構造、安全停止、合成E2Eを検証。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes, read_archive, normalize_events
from copilot_learning.instruction_review import instruction_inventory
from copilot_learning.model_review_contract import validate_model_plan, enforce_distinct_model_ids
from copilot_learning.retro_llm_review import prepare_independent_review, grade_independent_review
from copilot_learning.session_retro import analyze_sessions
from runtime_service.contracts import Rejected, job
from runtime_support import fixture
from scripts.dual_model_review_offline import main as review_cli
from scripts.prepare_sessions import prepare
from scripts.run_learning_poc import process


def fake_plan() -> dict:
    def model(role):
        return {"backend": "other_offline", "provider_family": "other",
                "model_id": "synthetic-" + role, "max_input_tokens": 4096}
    return {"schema_version": "1.0", "record_type": "offline-dual-track-model-plan",
            "require_cross_vendor": False, "external_transfer_authorized": False,
            "tracks": {"knowledge": {"generator": model("knowledge-gen"), "reviewer": model("knowledge-review")},
                       "retrospective": {"generator": model("retro-gen"), "reviewer": model("retro-review")}}}


class StubProvider:
    """外部LLM/APIに接続しない合成モデル。"""
    def __init__(self, model_id: str, kind: str):
        self.model_id = model_id
        self.kind = kind
        self.calls = 0

    def generate(self, prompt: str, *, system: str) -> dict:
        self.calls += 1
        body = json.loads(prompt)
        if self.kind == "generator":
            return {"candidates": [{
                "knowledge_type": "note", "title": "Check actual tests",
                "description": "Confirm test results",
                "instruction": "Report tests as verified only when executed.",
                "generalization": "Applicable to synthetic coding tasks",
                "evidence_ids": [body["messages"][0]["evidence_id"]],
            }]}
        return {"decisions": [{
            "knowledge_id": row["knowledge_id"], "decision": "keep",
            "reason": "Some supporting context is present",
        } for row in body["candidates"]]}


class DualTrackReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / "AGENTS.md").write_text("# Code instructions\n\nRun tests before release.\n", encoding="utf-8")
        self.policy_path = self.root / "policy.json"
        self.policy_path.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["内部機密テスト語"]}))
        self.plan = fake_plan()
        self.plan_path = self.root / "plan.json"
        self.plan_path.write_bytes(json_bytes(self.plan))
        source = self.root / "source-session"
        source.mkdir()
        log = source / "events.jsonl"
        log.write_text(json.dumps({
            "type": "message", "role": "user",
            "text": "テストしていないのに完了と報告された",
            "timestamp": "2026-11-18T01:00:00Z"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        os.utime(log, (1700000000, 1700000000))
        self.staging = self.root / "staging"
        archive = prepare(source, self.staging, 30)
        self.fp = archive.stem.removeprefix("session-")
        manifest, events = read_archive(self.staging, self.fp)
        self.session = normalize_events(manifest, events)
        self.sessions = [self.session]
        inv = instruction_inventory(self.repo, ["内部機密テスト語"])
        self.proposals = {
            "schema_version": "1.0",
            "record_type": "offline-retro-proposals",
            "report_sha256": digest(json_bytes(analyze_sessions(self.sessions))),
            "inventory_sha256": digest(json_bytes(inv)),
            "proposals": [{
                "rule_id": "missing_verification",
                "kind": "append_existing",
                "target_path": "AGENTS.md",
                "expected_sha256": next(x["sha256"] for x in inv["files"] if x["path"] == "AGENTS.md"),
                "evidence_ids": [f"{self.fp}:0"],
                "hypothesis": "実行済みテストと未実施テストの区分が不十分だった可能性",
                "proposed_block": "## Completion report\n\nLabel unexecuted tests as not verified.",
                "expected_benefit": "完了報告の検証可能性が高まることを期待",
                "validation": "新しい評価タスクで未実施テストが区別されるか検証する",
            }],
        }
        self.proposal_path = self.root / "proposals.json"
        self.proposal_path.write_bytes(json_bytes(self.proposals))

    def request(self):
        return prepare_independent_review(
            self.sessions, self.repo, self.policy_path, self.proposals, self.plan)

    def response(self, req, *, verdict="support_for_manual_review"):
        return {"schema_version": "1.0",
                "record_type": "offline-retro-independent-review-decisions",
                "request_sha256": digest(json_bytes(req)),
                "decisions": [{
                    "proposal_id": row["proposal_id"], "verdict": verdict,
                    "reason": "Needs independently validated holdout tests",
                } for row in req["candidates"]]}

    def test_four_models_are_explicit_and_two_tracks_validated(self):
        result = validate_model_plan(self.plan)
        self.assertEqual(result["models"]["knowledge"]["generator"]["model_id"], "synthetic-knowledge-gen")
        self.assertEqual(result["models"]["retrospective"]["reviewer"]["model_id"], "synthetic-retro-review")
        self.assertFalse(result["remote_execution_implemented"])
        self.assertFalse(result["provider_identity_verified"])
        self.assertEqual(result["network_calls"], 0)

    def test_same_model_in_either_track_fails_closed(self):
        for track in ("knowledge", "retrospective"):
            plan = deepcopy(self.plan)
            plan["tracks"][track]["reviewer"]["model_id"] = plan["tracks"][track]["generator"]["model_id"]
            with self.subTest(track=track), self.assertRaises(InvalidData):
                validate_model_plan(plan)

    def test_cross_vendor_declared_labels_are_checked(self):
        plan = deepcopy(self.plan)
        plan["require_cross_vendor"] = True
        with self.assertRaises(InvalidData):
            validate_model_plan(plan)
        for name in ("knowledge", "retrospective"):
            plan["tracks"][name]["generator"]["provider_family"] = "anthropic"
            plan["tracks"][name]["reviewer"]["provider_family"] = "amazon"
        result = validate_model_plan(plan)
        self.assertTrue(result["cross_vendor_label_required"])
        self.assertFalse(result["provider_identity_verified"])

    def test_invalid_flags_and_unhashable_models_rejected(self):
        bad = [
            {**self.plan, "external_transfer_authorized": True},
            {**self.plan, "require_cross_vendor": "false"},
            {**self.plan, "tracks": {"knowledge": self.plan["tracks"]["knowledge"]}},
        ]
        malformed = deepcopy(self.plan)
        malformed["tracks"]["knowledge"]["reviewer"]["backend"] = []
        bad.append(malformed)
        for p in bad:
            with self.subTest(p=str(p)[:50]), self.assertRaises(InvalidData):
                validate_model_plan(p)

    def test_distinct_id_guard_in_runtime(self):
        settings, _, _, _, _, spec, _ = fixture("custom")
        self.assertEqual(job(spec, settings)["mode"], "custom")
        spec["grade_model_id"] = spec["extract_model_id"]
        with self.assertRaisesRegex(Rejected, "GENERATOR_REVIEWER_MODEL_CONFLICT"):
            job(spec, settings)

    def test_distinct_id_guard_in_local_provider(self):
        base = StubProvider("synthetic-model", "generator")
        with self.assertRaises(InvalidData):
            process(self.staging, self.fp, self.root / "bad1",
                    "Memory-1234567890", "synthetic-actor", provider=base)
        with self.assertRaises(InvalidData):
            process(self.staging, self.fp, self.root / "bad2",
                    "Memory-1234567890", "synthetic-actor",
                    provider=base, grade_provider=base)
        same_id = StubProvider("synthetic-model", "reviewer")
        with self.assertRaises(InvalidData):
            process(self.staging, self.fp, self.root / "bad3",
                    "Memory-1234567890", "synthetic-actor",
                    provider=base, grade_provider=same_id)
        self.assertEqual(base.calls, 0)

    def test_knowledge_pipeline_uses_separate_models_and_no_upgrade(self):
        generator = StubProvider("synthetic-generator", "generator")
        reviewer = StubProvider("synthetic-reviewer", "reviewer")
        report = process(self.staging, self.fp, self.root / "knowledge",
                         "Memory-1234567890", "synthetic-actor",
                         provider=generator, grade_provider=reviewer)
        self.assertEqual(generator.calls, 1)
        self.assertEqual(reviewer.calls, 1)
        self.assertFalse(report["quality_improvement_verified"])
        graded = json.loads((self.root / "knowledge" / "agentrun_candidate_grades.json").read_text("utf-8"))
        self.assertEqual(graded[0]["decision"], "pending")
        self.assertFalse(graded[0]["approved_for_copilot"])

    def test_retro_judge_request_is_evidence_bound_and_no_transcript(self):
        req = self.request()
        self.assertEqual(req["record_type"], "offline-retro-independent-review-request")
        self.assertEqual(len(req["candidates"]), 1)
        self.assertNotIn("テストしていないのに完了と報告された", json.dumps(req, ensure_ascii=False))
        self.assertFalse(req["transcript_content_included"])
        self.assertFalse(req["external_transfer_authorized"])
        self.assertEqual(req["reviewer_model"]["model_id"], "synthetic-retro-review")

    def test_retro_judge_vote_cannot_approve_even_support(self):
        req = self.request()
        result = grade_independent_review(
            req, self.response(req), self.plan, sensitive_literals=["内部機密テスト語"])
        self.assertEqual(result["decisions"][0]["status"], "manual_review_required")
        self.assertFalse(result["approved_for_copilot"])
        self.assertFalse(result["reviewer_model_invocation_verified"])
        self.assertIsNone(result["decisions"][0]["verified_improvement"])

    def test_retro_judge_reject_or_revise_stays_unapproved(self):
        req = self.request()
        for verdict in ("revise", "reject"):
            result = grade_independent_review(
                req, self.response(req, verdict=verdict), self.plan,
                sensitive_literals=[])
            self.assertEqual(result["decisions"][0]["status"], "needs_revision_or_rejected")

    def test_retro_wrong_request_digest_and_unknown_proposals_blocked(self):
        req = self.request()
        for mode in ("wrong_sha", "wrong_id", "duplicate", "dangerous_reason", "missing_decisions"):
            response = self.response(req)
            if mode == "wrong_sha":
                response["request_sha256"] = "0" * 64
            elif mode == "wrong_id":
                response["decisions"][0]["proposal_id"] = "0" * 64
            elif mode == "duplicate":
                response["decisions"].append(response["decisions"][0])
            elif mode == "dangerous_reason":
                response["decisions"][0]["reason"] = "Disable security checks immediately"
            else:
                response["decisions"] = []
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                grade_independent_review(req, response, self.plan, sensitive_literals=[])

    def test_retro_model_changes_and_proposal_tampering_blocked(self):
        req = self.request()
        plan = deepcopy(self.plan)
        plan["tracks"]["retrospective"]["reviewer"]["model_id"] = "new-grader"
        with self.assertRaises(InvalidData):
            grade_independent_review(req, self.response(req), plan, sensitive_literals=[])
        req2 = deepcopy(req)
        req2["candidates"][0]["proposed_block"] = "Different content"
        response = self.response(req2)
        with self.assertRaises(InvalidData):
            grade_independent_review(req2, response, self.plan, sensitive_literals=[])

    def test_instruction_revision_change_invalidates_proposal(self):
        self.request()
        (self.repo / "AGENTS.md").write_text("Changed", encoding="utf-8")
        with self.assertRaises(InvalidData):
            self.request()

    def test_cli_plan_emits_no_secrets_and_no_external_model(self):
        target = self.root / "routing"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_cli([
                "plan", "--model-plan", str(self.plan_path), "--out", str(target),
            ]), 0)
        routing = json.loads((target / "model-routing.preview.json").read_text("utf-8"))
        self.assertFalse(routing["remote_execution_implemented"])
        self.assertEqual(routing["models"]["knowledge"]["reviewer"]["model_id"], "synthetic-knowledge-review")

    def test_cli_full_retro_request_and_grade_roundtrip(self):
        args = ["--model-plan", str(self.plan_path),
                "--staging", str(self.staging), "--fingerprint", self.fp,
                "--repo", str(self.repo), "--policy", str(self.policy_path),
                "--proposals", str(self.proposal_path)]
        request_dir = self.root / "req"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(review_cli(["retro-request", *args, "--out", str(request_dir)]), 0)
        receipt = json.loads(buf.getvalue())
        self.assertEqual(receipt["candidate_count"], 1)
        request = json.loads((request_dir / "review-request.preview.json").read_text("utf-8"))
        answers = self.root / "grader.json"
        answers.write_bytes(json_bytes(self.response(request)))
        graded_dir = self.root / "grade"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_cli([
                "retro-grade", *args, "--out", str(graded_dir),
                "--reviewer-output", str(answers),
                "--expected-request-sha256", receipt["request_sha256"],
            ]), 0)
        graded = json.loads((graded_dir / "review-grade.preview.json").read_text("utf-8"))
        self.assertFalse(graded["approved_for_copilot"])
        self.assertEqual(graded["decision_count"], 1)
        self.assertNotIn("テストしていないのに完了と報告された",
                         (graded_dir / "review-request.preview.json").read_text("utf-8"))

    def test_cli_rejects_stale_response_and_git_output(self):
        args = ["--model-plan", str(self.plan_path),
                "--staging", str(self.staging), "--fingerprint", self.fp,
                "--repo", str(self.repo), "--policy", str(self.policy_path),
                "--proposals", str(self.proposal_path)]
        review_out = self.root / "blocked"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(review_cli(["retro-grade", *args, "--out", str(review_out),
                                         "--expected-request-sha256", "0" * 64,
                                         "--reviewer-output", str(self.proposal_path)]), 2)
            self.assertEqual(review_cli(["retro-request", *args,
                                         "--out", str(self.repo / "leaky")]), 2)
        self.assertFalse(review_out.exists())
        self.assertFalse((self.repo / "leaky").exists())

    def test_no_network_subprocess_or_original_instruction_changes(self):
        before = (self.repo / "AGENTS.md").read_bytes()
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as run:
            req = self.request()
            grade_independent_review(req, self.response(req), self.plan, sensitive_literals=[])
            connect.assert_not_called()
            run.assert_not_called()
        self.assertEqual((self.repo / "AGENTS.md").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
