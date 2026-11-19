"""Issue #31: existing model-plan + independent grade -> final unapproved diff."""
from __future__ import annotations

from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes, read_archive, normalize_events
from copilot_learning.instruction_review import instruction_inventory
from copilot_learning.retro_llm_review import prepare_independent_review
from copilot_learning.retro_review_finalizer import (
    prepare_final_review, export_final_review, verify_final_review,
)
from copilot_learning.session_retro import analyze_sessions
from scripts.finalize_retro_review_offline import main as cli
from scripts.prepare_sessions import prepare


class RetroFinalizerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "target"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.agent = self.repo / "AGENTS.md"
        self.agent.write_text("# Agent instructions\n\nRun tests.\n", encoding="utf-8")
        self.policy = self.root / "policy.json"
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["内部機密テスト語"],
        }))
        source = self.root / "synthetic-session"
        source.mkdir()
        events_file = source / "events.jsonl"
        events_file.write_text(json.dumps({
            "type": "message", "role": "user",
            "text": "テストしていないのに完了報告した",
            "timestamp": "2026-11-20T00:00:00Z",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        os.utime(events_file, (1700000000, 1700000000))
        self.staging = self.root / "staging"
        archive = prepare(source, self.staging, 30)
        self.fp = archive.stem.removeprefix("session-")
        manifest, events = read_archive(self.staging, self.fp)
        self.sessions = [normalize_events(manifest, events)]
        inventory = instruction_inventory(self.repo, ["内部機密テスト語"])
        self.proposals = {
            "schema_version": "1.0", "record_type": "offline-retro-proposals",
            "report_sha256": digest(json_bytes(analyze_sessions(self.sessions))),
            "inventory_sha256": digest(json_bytes(inventory)),
            "proposals": [{
                "rule_id": "missing_verification", "kind": "append_existing",
                "target_path": "AGENTS.md", "expected_sha256": inventory["files"][0]["sha256"],
                "evidence_ids": [f"{self.fp}:0"],
                "hypothesis": "完了条件が曖昧だった可能性がある",
                "proposed_block": "## Verification\n\nReport unexecuted tests as not verified.",
                "expected_benefit": "検証不足の完了報告を減らせる可能性がある",
                "validation": "未学習の再現ケースで未実施テストを区別する",
            }],
        }
        def model(name):
            return {"backend": "other_offline", "provider_family": "other",
                    "model_id": name, "max_input_tokens": 4096}
        self.plan = {
            "schema_version": "1.0", "record_type": "offline-dual-track-model-plan",
            "require_cross_vendor": False, "external_transfer_authorized": False,
            "tracks": {
                "knowledge": {"generator": model("knowledge-gen"), "reviewer": model("knowledge-rev")},
                "retrospective": {"generator": model("retro-gen"), "reviewer": model("retro-rev")},
            },
        }
        request = prepare_independent_review(
            self.sessions, self.repo, self.policy, self.proposals, self.plan)
        self.opinion = {
            "schema_version": "1.0",
            "record_type": "offline-retro-independent-review-decisions",
            "request_sha256": digest(json_bytes(request)),
            "decisions": [{
                "proposal_id": request["candidates"][0]["proposal_id"],
                "verdict": "support_for_manual_review",
                "reason": "The recommendation remains unverified in actual tasks.",
            }],
        }
        self.out = self.root / "final"
        self.plan_path = self.root / "plan.json"
        self.proposal_path = self.root / "proposal.json"
        self.opinion_path = self.root / "opinion.json"
        for path, data in ((self.plan_path, self.plan), (self.proposal_path, self.proposals),
                           (self.opinion_path, self.opinion)):
            path.write_bytes(json_bytes(data))

    def data(self, opinion=None):
        return (self.sessions, self.repo, self.policy, self.proposals,
                self.plan, self.opinion if opinion is None else opinion)

    def test_support_only_yields_manual_preview_without_edits(self):
        before = self.agent.read_bytes()
        manifest, files = prepare_final_review(*self.data())
        self.assertEqual(manifest["status"], "offline_unapproved_review_only")
        report = json.loads(files["final-review.preview.json"])
        self.assertEqual(report["status"], "manual_review_required")
        self.assertFalse(report["approved_for_copilot"])
        self.assertFalse(report["actual_llm_execution_verified"])
        self.assertIn("+## Verification", files["changes.preview.diff"].decode("utf-8"))
        self.assertEqual(report["review"]["proposals"][0]["grade"], "pending_manual_review")
        self.assertEqual(before, self.agent.read_bytes())

    def test_revise_or_reject_suppresses_entire_diff(self):
        for value in ("revise", "reject"):
            opinion = deepcopy(self.opinion)
            opinion["decisions"][0]["verdict"] = value
            with self.subTest(verdict=value):
                manifest, files = prepare_final_review(*self.data(opinion))
                self.assertEqual(files["changes.preview.diff"], b"")
                report = json.loads(files["final-review.preview.json"])
                self.assertEqual(report["status"], "independent_review_blocked_diff")
                self.assertEqual(report["review"]["proposals"][0]["grade"],
                                 "independent_model_review_hold")

    def test_stale_reviewer_output_and_model_plan_rejected(self):
        opinion = deepcopy(self.opinion)
        opinion["request_sha256"] = "0" * 64
        with self.assertRaises(InvalidData):
            prepare_final_review(*self.data(opinion))
        plan = deepcopy(self.plan)
        plan["tracks"]["retrospective"]["reviewer"]["model_id"] = "different"
        with self.assertRaises(InvalidData):
            prepare_final_review(self.sessions, self.repo, self.policy,
                                 self.proposals, plan, self.opinion)

    def test_wrong_source_sha_or_instruction_edit_rejected(self):
        old = deepcopy(self.proposals)
        old["proposals"][0]["expected_sha256"] = "0" * 64
        with self.assertRaises(InvalidData):
            prepare_final_review(self.sessions, self.repo, self.policy,
                                 old, self.plan, self.opinion)
        self.agent.write_text("Changed since review\n", encoding="utf-8")
        with self.assertRaises(InvalidData):
            prepare_final_review(*self.data())

    def test_export_and_independent_verify(self):
        receipt = export_final_review(*self.data(), self.out)
        self.assertFalse(receipt["approved_for_copilot"])
        self.assertEqual(set(p.name for p in self.out.iterdir()),
                         {"final-review.preview.json", "changes.preview.diff", "manifest.json"})
        res = verify_final_review(*self.data(), self.out, receipt["manifest_sha256"])
        self.assertEqual(res["status"], "verified_unapproved_review_only")
        with self.assertRaises(InvalidData):
            verify_final_review(*self.data(), self.out, "0" * 64)

    def test_tampered_diff_rejected(self):
        receipt = export_final_review(*self.data(), self.out)
        diff = self.out / "changes.preview.diff"
        diff.chmod(0o600)
        diff.write_text("tampered", encoding="utf-8")
        with self.assertRaises(InvalidData):
            verify_final_review(*self.data(), self.out, receipt["manifest_sha256"])

    def test_private_repo_and_auto_discovery_refused(self):
        for target in (self.repo / "exports", self.root / ".github" / "skills" / "review"):
            with self.subTest(target=target), self.assertRaises(InvalidData):
                export_final_review(*self.data(), target)
            self.assertFalse(target.exists())

    def test_cli_export_verify_no_auto_approval(self):
        base = [
            "--staging", str(self.staging), "--fingerprint", self.fp,
            "--repo", str(self.repo), "--policy", str(self.policy),
            "--model-plan", str(self.plan_path), "--proposals", str(self.proposal_path),
            "--reviewer-output", str(self.opinion_path), "--out", str(self.out),
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli(["export", *base]), 0)
        receipt = json.loads(buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["verify", *base,
                                  "--expected-manifest-sha256", receipt["manifest_sha256"]]), 0)
        self.assertTrue(self.agent.read_text("utf-8").startswith("# Agent instructions"))

    def test_no_network_or_subprocess(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as run:
            prepare_final_review(*self.data())
            connect.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
