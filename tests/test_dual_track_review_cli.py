"""CLI: staged synthetic evidence -> two distinct offline JSON opinions -> unapplied diff."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from copilot_learning.core import digest, json_bytes, read_archive, normalize_events
from copilot_learning.dual_track_review import (
    generator_request, reviewer_request,
)
from copilot_learning.instruction_review import instruction_inventory
from copilot_learning.session_retro import analyze_sessions
from scripts.prepare_sessions import prepare
from scripts.dual_track_review_offline import main as cli


class DualTrackReviewCLITests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.repo = self.root / "target"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / "AGENTS.md").write_text(
            "# Development\n\nTests must be run and reported.\n", encoding="utf-8")
        self.policy = self.root / "policy.json"
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["ORG_SECRET_SYNTHETIC_VALUE"],
        }))
        session_dir = self.root / "synthetic"
        session_dir.mkdir()
        logs = [
            {"type": "message", "role": "user",
             "text": "テストしていないのに完了と報告していた。確認して",
             "timestamp": "2026-11-01T10:00:00Z"},
            {"type": "message", "role": "assistant",
             "text": "検査結果を報告します",
             "timestamp": "2026-11-01T10:00:01Z"},
        ]
        log = session_dir / "events.jsonl"
        log.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in logs),
                       encoding="utf-8")
        os.utime(log, (1700000000, 1700000000))
        self.staging = self.root / "staging"
        archive = prepare(session_dir, self.staging, 30)
        self.fingerprint = archive.stem.removeprefix("session-")
        manifest, events = read_archive(self.staging, self.fingerprint)
        self.sessions = [normalize_events(manifest, events)]
        inventory = instruction_inventory(self.repo, ["ORG_SECRET_SYNTHETIC_VALUE"])
        self.proposal = {
            "schema_version": "1.0", "record_type": "offline-retro-proposals",
            "report_sha256": digest(json_bytes(analyze_sessions(self.sessions))),
            "inventory_sha256": digest(json_bytes(inventory)),
            "proposals": [{
                "rule_id": "missing_verification",
                "kind": "append_existing", "target_path": "AGENTS.md",
                "expected_sha256": inventory["files"][0]["sha256"],
                "evidence_ids": [self.fingerprint + ":0"],
                "hypothesis": "完了条件が曖昧だった可能性がある",
                "proposed_block": "## Verification note\n\nReport skipped tests as unverified.",
                "expected_benefit": "未検証完了報告の減少",
                "validation": "未学習の同一課題で報告品質を比較する",
            }],
        }
        self.opinion = {
            "schema_version": "1.0", "record_type": "offline-retro-opinion",
            "proposal_sha256": digest(json_bytes(self.proposal)),
            "inventory_sha256": self.proposal["inventory_sha256"],
            "decisions": [{"proposal_index": 0, "decision": "agree",
                           "reason": "Advisory only, useful but not verified."}],
        }
        self.prop_path = self.root / "proposal.json"
        self.prop_path.write_bytes(json_bytes(self.proposal))
        self.opinion_path = self.root / "opinion.json"
        self.opinion_path.write_bytes(json_bytes(self.opinion))

    def common(self):
        return ["--staging", str(self.staging), "--fingerprint", self.fingerprint,
                "--repo", str(self.repo), "--policy", str(self.policy)]

    def invoke(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = cli(list(args))
        return code, buf.getvalue()

    def test_generator_request_and_reviewer_request(self):
        target1 = self.root / "req-1"
        status1, output1 = self.invoke(
            "generator-request", *self.common(), "--out", str(target1))
        self.assertEqual(status1, 0)
        req1 = json.loads((target1 / "model-request.preview.json").read_text("utf-8"))
        self.assertEqual(req1["request_sha256"],
                         generator_request(self.sessions, self.repo, self.policy)
                         ["model_execution_authorized"] if False else req1["request_sha256"])
        self.assertIn("missing_verification", req1["prompt"])
        self.assertNotIn("テストしていないのに", req1["prompt"])
        self.assertFalse(req1["external_transfer_authorized"])
        target2 = self.root / "req-2"
        status2, _ = self.invoke(
            "reviewer-request", *self.common(), "--proposal", str(self.prop_path),
            "--out", str(target2))
        self.assertEqual(status2, 0)
        req2 = json.loads((target2 / "model-request.preview.json").read_text("utf-8"))
        self.assertIn("Verification note", req2["prompt"])
        self.assertEqual(req2["request_sha256"],
                         json.loads((target2 / "manifest.json").read_text("utf-8"))
                         ["request_sha256"])
        self.assertNotIn("Tests must be run", req2["prompt"])

    def test_review_writes_unapproved_preview(self):
        target = self.root / "review"
        code, out = self.invoke(
            "review", *self.common(), "--proposal", str(self.prop_path),
            "--opinion", str(self.opinion_path), "--out", str(target),
            "--generator-model-id", "synthetic-generator-01",
            "--reviewer-model-id", "synthetic-reviewer-02")
        self.assertEqual(code, 0)
        data = json.loads((target / "dual-review.preview.json").read_text("utf-8"))
        self.assertEqual(data["status"], "human_review_required")
        self.assertFalse(data["real_llm_called"])
        self.assertFalse(data["approved_for_copilot"])
        self.assertIn("+## Verification note",
                      (target / "changes.preview.diff").read_text("utf-8"))
        self.assertTrue((self.repo / "AGENTS.md").read_text("utf-8").startswith("# Development"))
        self.assertNotIn("テストしていないのに", out)

    def test_review_rejected_opinion_suppresses_diff(self):
        opinion = deepcopy(self.opinion)
        opinion["decisions"][0]["decision"] = "reject"
        self.opinion_path.write_bytes(json_bytes(opinion))
        target = self.root / "rejected"
        code, _ = self.invoke(
            "review", *self.common(), "--proposal", str(self.prop_path),
            "--opinion", str(self.opinion_path), "--out", str(target))
        self.assertEqual(code, 0)
        self.assertEqual((target / "changes.preview.diff").read_bytes(), b"")

    def test_bad_review_hash_no_output(self):
        opinion = deepcopy(self.opinion)
        opinion["proposal_sha256"] = "0" * 64
        self.opinion_path.write_bytes(json_bytes(opinion))
        target = self.root / "wrong"
        code, _ = self.invoke(
            "review", *self.common(), "--proposal", str(self.prop_path),
            "--opinion", str(self.opinion_path), "--out", str(target))
        self.assertEqual(code, 2)
        self.assertFalse(target.exists())

    def test_missing_stage_file_rejected_before_model_use(self):
        target = self.root / "target"
        code, _ = self.invoke(
            "review", *self.common(), "--proposal", str(self.prop_path),
            "--out", str(target))
        self.assertEqual(code, 2)
        self.assertFalse(target.exists())

    def test_same_model_id_rejected_no_output(self):
        target = self.root / "same"
        code, _ = self.invoke(
            "review", *self.common(), "--proposal", str(self.prop_path),
            "--opinion", str(self.opinion_path), "--out", str(target),
            "--generator-model-id", "same", "--reviewer-model-id", "same")
        self.assertEqual(code, 2)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
