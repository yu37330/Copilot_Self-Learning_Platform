"""Issue #46: Codex Task ID / JUnit report / GitHub locator の匿名出典検査。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from copilot_learning.codex_task_evidence import (
    _test_xml, evaluate_task, inspect_sources,
)
from copilot_learning.core import InvalidData, json_bytes
from scripts.review_codex_task_evidence_offline import main as cli


def synthetic_rollout() -> list[dict]:
    stamp = "2026-11-19T12:00:00Z"
    return [
        {"type": "session_meta", "timestamp": stamp, "payload": {
            "id": "private-codex-session", "cwd": "PRIVATE WORKSPACE PATH"}},
        {"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "task_started", "turn_id": "PRIVATE TURN"}},
        {"type": "response_item", "timestamp": stamp, "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "PRIVATE USER MESSAGE"}]}},
        {"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "exec_command_end", "exit_code": 1,
            "command": ["PRIVATE COMMAND"], "aggregated_output": "PRIVATE FAILURE LOG"}},
        {"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 120, "output_tokens": 50,
                "cached_input_tokens": 20, "reasoning_output_tokens": 10,
                "total_tokens": 170}}}},
        {"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "task_complete", "turn_id": "PRIVATE TURN",
            "last_agent_message": "PRIVATE REPORTED SUCCESS"}},
    ]


def sample_junit() -> bytes:
    return b"""<?xml version="1.0"?>
<testsuite name="PRIVATE INTERNAL SUITE" tests="3" failures="0" errors="0" skipped="1">
  <testcase name="PRIVATE integration A"/>
  <testcase name="PRIVATE integration B"/>
  <testcase name="PRIVATE slow test"><skipped message="PRIVATE skip details"/></testcase>
</testsuite>
"""


class CodexTaskEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.rollout = self.root / "rollout-synthetic.jsonl"
        self.xml = self.root / "private-junit.xml"
        self.policy = self.root / "policy.json"
        self.binding_file = self.root / "task-binding.json"
        self.out = self.root / "out"
        self.rollout.write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in synthetic_rollout()),
            encoding="utf-8")
        os.utime(self.rollout, (time.time()-3*86400, time.time()-3*86400))
        self.xml.write_bytes(sample_junit())
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["社内秘密の文字列"],
        }))
        self.index = inspect_sources(self.rollout, self.xml, self.policy)
        self.binding = {
            "schema_version": "1.0",
            "record_type": "offline-codex-task-evidence-binding",
            "task_id": "codex-review-123",
            "rollout_sha256": self.index["rollout_sha256"],
            "test_report_sha256": self.index["test_report_sha256"],
            "github": {"repository_full_name": "example-org/example-repo",
                       "pr_number": 246},
            "completion_claim": "reported_complete",
        }
        self.binding_file.write_bytes(json_bytes(self.binding))

    def _args(self):
        return [
            "--rollout", str(self.rollout), "--test-report", str(self.xml),
            "--policy", str(self.policy), "--out", str(self.out),
            "--acknowledge-local-analysis",
        ]

    def test_source_index_only_exposes_safe_metrics(self):
        index = self.index
        self.assertEqual(index["test_report"]["testcases_declared_and_parsed"], 3)
        self.assertEqual(index["test_report"]["skipped_case_count"], 1)
        self.assertEqual(index["rollout_metadata"]["event_telemetry"]
                         ["exec_command_end_nonzero_count"], 1)
        self.assertEqual(index["rollout_metadata"]["event_telemetry"]
                         ["latest_cumulative_token_usage"]["total_tokens"], 170)
        self.assertEqual(len(index["rollout_sha256"]), 64)
        for forbidden in ("PRIVATE", "example-repo", "codex-review-123",
                          "integration B", "secret"):
            self.assertNotIn(forbidden, json.dumps(index, ensure_ascii=False))

    def test_test_report_syntax_only_never_elevates_business_result(self):
        review = evaluate_task(self.index, self.binding)
        self.assertEqual(review["test_status"],
                         "test_report_clean_but_executor_not_verified")
        self.assertFalse(review["business_completion_verified"])
        self.assertTrue(review["completion_claimed"])
        self.assertFalse(review["test_result"]["test_process_execution_verified"])
        self.assertFalse(review["github"]["pr_merge_verified"])
        self.assertEqual(review["github"]["provider_state"],
                         "not_checked_requires_live_provider_lookup")
        self.assertEqual(review["rollout"]["anomaly_candidates"][0]
                         ["source_lines"], [4])
        self.assertNotIn("example-repo", json.dumps(review))
        self.assertNotIn("PRIVATE", json.dumps(review))

    def test_cli_index_review_verify_roundtrip(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli(["index", *self._args()]), 0)
        self.assertTrue((self.out / "codex-task-sources.index.json").is_file())
        self.out = self.root / "review"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli(["review", "--binding", str(self.binding_file),
                                  *self._args()]), 0)
        receipt = json.loads(output.getvalue())
        self.assertFalse(json.loads(
            (self.out / "codex-task-evidence.preview.json").read_text("utf-8")
        )["business_completion_verified"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["verify", "--binding", str(self.binding_file),
                                  *self._args(), "--expected-manifest-sha256",
                                  receipt["manifest_sha256"]]), 0)
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_test_report_failure_is_not_zero(self):
        self.xml.write_bytes(b'<testsuite tests="2" failures="1" errors="0" skipped="0">'
                             b'<testcase name="PRIVATE"><failure>PRIVATE LOG</failure></testcase>'
                             b'<testcase name="PRIVATE2"/></testsuite>')
        index = inspect_sources(self.rollout, self.xml, self.policy)
        binding = deepcopy(self.binding)
        binding["test_report_sha256"] = index["test_report_sha256"]
        review = evaluate_task(index, binding)
        self.assertEqual(review["test_status"], "test_report_contains_failures")
        self.assertEqual(review["test_result"]["failed_case_count"], 1)
        self.assertNotIn("PRIVATE", json.dumps(review))

    def test_not_present_test_report_stays_unknown(self):
        index = inspect_sources(self.rollout, None, self.policy)
        binding = deepcopy(self.binding)
        binding["test_report_sha256"] = None
        review = evaluate_task(index, binding)
        self.assertEqual(review["test_status"], "test_report_missing")
        self.assertIsNone(review["test_result"])
        self.assertFalse(review["business_completion_verified"])

    def test_test_xml_rejects_entities_doctype_and_malformed(self):
        for raw in (b"<!DOCTYPE foo><testsuite/>",
                    b"<testsuite><testcase>",
                    b"<not-junit><testcase/></not-junit>",
                    b'<testsuite tests="2"><testcase/></testsuite>',
                    b'<testsuite><testcase><error/><failure/></testcase></testsuite>'):
            with self.subTest(size=len(raw)), self.assertRaises(InvalidData):
                _test_xml(raw)

    def test_report_checks_declared_counts(self):
        report = _test_xml(sample_junit())
        self.assertEqual(report["testcases_declared_and_parsed"], 3)
        self.assertEqual(report["cases_without_fail_error_skip"], 2)
        self.assertTrue(report["report_indicates_zero_failures"])
        self.assertFalse(report["test_executor_identity_verified"])

    def test_untrusted_github_pr_reference_never_marked_merged(self):
        for change in (
            {"repository_full_name": "example-org/example-repo", "pr_number": 246},
            None,
        ):
            binding = deepcopy(self.binding)
            binding["github"] = change
            report = evaluate_task(self.index, binding)
            self.assertFalse(report["github"]["pr_merge_verified"])
            self.assertFalse(report["business_completion_verified"])

    def test_invalid_github_reference_and_task_id_rejected(self):
        invalid = [
            {"github": {"repository_full_name": "../bad", "pr_number": 1}},
            {"github": {"repository_full_name": "test/repo", "pr_number": True}},
            {"github": {"repository_full_name": "test/repo", "pr_number": -1}},
            {"github": {"repository_full_name": "test/repo", "pr_number": "1"}},
            {"task_id": "PRIVATE invalid task with spaces"},
            {"completion_claim": "verified_success"},
            {"fake_status": "verified_success"},
        ]
        for change in invalid:
            binding = deepcopy(self.binding)
            binding.update(change)
            with self.subTest(change=change), self.assertRaises(InvalidData):
                evaluate_task(self.index, binding)

    def test_source_changed_rejected_even_if_task_claim_complete(self):
        binding = deepcopy(self.binding)
        binding["rollout_sha256"] = "0"*64
        with self.assertRaises(InvalidData):
            evaluate_task(self.index, binding)

    def test_xml_source_change_breaks_verify(self):
        result = io.StringIO()
        with contextlib.redirect_stdout(result):
            self.assertEqual(cli(["review", "--binding", str(self.binding_file),
                                  *self._args()]), 0)
        sha = json.loads(result.getvalue())["manifest_sha256"]
        self.xml.write_bytes(b'<testsuite tests="1"><testcase/></testsuite>')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["verify", "--binding", str(self.binding_file),
                                  *self._args(), "--expected-manifest-sha256", sha]), 2)

    def test_binding_mutation_breaks_verify(self):
        result = io.StringIO()
        with contextlib.redirect_stdout(result):
            self.assertEqual(cli(["review", "--binding", str(self.binding_file),
                                  *self._args()]), 0)
        sha = json.loads(result.getvalue())["manifest_sha256"]
        mutated = deepcopy(self.binding)
        mutated["task_id"] = "codex-review-999"
        self.binding_file.write_bytes(json_bytes(mutated))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["verify", "--binding", str(self.binding_file),
                                  *self._args(), "--expected-manifest-sha256", sha]), 2)

    def test_tampered_preview_rejected(self):
        result = io.StringIO()
        with contextlib.redirect_stdout(result):
            self.assertEqual(cli(["review", "--binding", str(self.binding_file),
                                  *self._args()]), 0)
        sha = json.loads(result.getvalue())["manifest_sha256"]
        file = self.out / "codex-task-evidence.preview.json"
        file.chmod(0o600)
        file.write_text("TAMPERED", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["verify", "--binding", str(self.binding_file),
                                  *self._args(), "--expected-manifest-sha256", sha]), 2)

    def test_missing_acknowledgement_does_not_read_source(self):
        with patch("copilot_learning.codex_schema_probe._source",
                   side_effect=AssertionError("read forbidden")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli(["index", *self._args()[:-1]]), 2)
        self.assertFalse(self.out.exists())

    def test_repo_output_and_private_xml_input_rejected(self):
        inside = self.root / "repo"
        inside.mkdir()
        (inside / ".git").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["index", *self._args()[:-3],
                                  "--out", str(inside / "analysis"),
                                  "--acknowledge-local-analysis"]), 2)
        # A JUnit file inside a Git repo must not be accepted for private analysis.
        other = inside / "junit.xml"
        other.write_bytes(sample_junit())
        with self.assertRaises(InvalidData):
            inspect_sources(self.rollout, other, self.policy)

    def test_existing_output_not_overwritten(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("KEEP", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["index", *self._args()]), 2)
        self.assertEqual((self.out / "keep.txt").read_text("utf-8"), "KEEP")

    def test_network_and_subprocess_never_called(self):
        with patch("socket.socket.connect") as conn, patch("subprocess.run") as run:
            report = evaluate_task(self.index, self.binding)
            self.assertFalse(report["independent_acceptance_verified"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli(["index", *self._args()]), 0)
            conn.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
