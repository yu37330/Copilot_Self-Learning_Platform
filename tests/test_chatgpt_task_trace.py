"""Issue #41: ChatGPT/Commander第3系統の匿名集計・欠落・安全性を合成データで検証。"""
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

from copilot_learning.chatgpt_task_trace import (
    evaluate_sources, summarize_chatgpt, summarize_desktop_history,
)
from copilot_learning.core import InvalidData, json_bytes
from scripts.analyze_chatgpt_desktop_offline import main as cli


def synthetic_chat() -> list[dict]:
    """サンプル会話の途中に別分岐を含める。activeな経路だけ評価する。"""
    def msg(role: str, text: str):
        return {"author": {"role": role}, "content": {"content_type": "text", "parts": [text]}}
    return [{
        "id": "synthetic-chat-id", "title": "PRIVATE TASK NAME MUST NOT LEAK",
        "current_node": "u3",
        "mapping": {
            "root": {"parent": None, "message": None},
            "u1": {"parent": "root", "message": msg("user", "テストしていないのに完了と言った")},
            "a2": {"parent": "u1", "message": msg("assistant", "PRIVATE ASSISTANT TEXT")},
            "u3": {"parent": "a2", "message": msg("user", "意図と違うのでやり直して")},
            "alternate": {"parent": "u1", "message": msg("user", "BRANCH_NOT_SELECTED 毎回同じ")},
        },
    }]


def synthetic_desktop() -> list[dict]:
    return [
        {"timestamp": "11/20/2026, 05:50:00", "toolName": "start_process",
         "arguments": {"command": "PRIVATE COMMAND secret should not leak"},
         "output": {"content": [{"type": "text", "text": "PRIVATE TOOL OUTPUT"}]},
         "duration": 2700},
        {"timestamp": "11/20/2026, 05:51:00", "toolName": "read_process_output",
         "arguments": {"pid": 42}, "output": {"status": "error",
             "content": [{"type": "text", "text": "[output omitted from history: 9000 bytes, over the 4096-byte cap]"}]}},
        {"timestamp": "11/20/2026, 05:51:10", "toolName": "read_process_output",
         "arguments": {"pid": 42}, "output": {}},
        {"timestamp": "11/20/2026, 05:51:20", "toolName": "read_process_output",
         "arguments": {"pid": 42}, "output": {}},
        {"timestamp": "11/20/2026, 05:51:30", "toolName": "write_file",
         "arguments": {"path": "PRIVATE FILEPATH"},
         "isError": True,
         "output": {"content": [{"type": "text", "text": "PRIVATE FAILURE PATH"}]}},
    ]


class TaskTraceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.chat = synthetic_chat()
        self.desktop = synthetic_desktop()
        self.chatpath = self.root / "conversations.json"
        self.desktoppath = self.root / "tool-history.jsonl"
        self.policy = self.root / "policy.json"
        self.out = self.root / "preview"
        self._write()
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["社内機密の目印"],
        }))

    def _write(self):
        self.chatpath.write_bytes(json_bytes(self.chat))
        self.desktoppath.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.desktop),
            encoding="utf-8")

    def _args(self):
        return ["--chatgpt-json", str(self.chatpath),
                "--desktop-history", str(self.desktoppath),
                "--policy", str(self.policy), "--out", str(self.out),
                "--acknowledge-local-analysis"]

    def test_selected_branch_only_and_correction_counts(self):
        report = summarize_chatgpt(self.chat, [])
        self.assertEqual(report["conversations"], 1)
        self.assertEqual(report["user_messages"], 2)
        self.assertEqual(report["assistant_messages"], 1)
        self.assertEqual(report["correction_signals"]["missing_verification"], 1)
        self.assertEqual(report["correction_signals"]["explicit_correction"], 1)
        self.assertNotIn("repeatable_workflow", report["correction_signals"])

    def test_desktop_indeterminate_is_not_success(self):
        report = summarize_desktop_history(self.desktop)
        self.assertEqual(report["calls"], 5)
        self.assertEqual(report["explicit_error"], 2)
        self.assertEqual(report["explicit_success"], 0)
        self.assertEqual(report["outcome_unknown"], 3)
        self.assertEqual(report["output_truncated"], 1)
        self.assertEqual(report["calls_by_tool"]["read_process_output"], 3)
        self.assertFalse(report["task_success_verified"])

    def test_no_raw_args_command_chat_or_output_in_report(self):
        report = evaluate_sources(
            self.chat, self.desktop, chat_source_sha256="a" * 64,
            desktop_source_sha256="b" * 64, policy_sha256="c" * 64, literals=[])
        as_text = json.dumps(report, ensure_ascii=False)
        for forbidden in ("PRIVATE", "意図と違う", "テストしていない", "secret should not leak",
                          "PRIVATE FILEPATH", "BRANCH_NOT_SELECTED", "synthetic-chat-id"):
            self.assertNotIn(forbidden, as_text)
        self.assertEqual(report["task_linking"], "unlinked_requires_explicit_task_id")
        self.assertEqual(report["cross_source_claims"], [])
        self.assertFalse(report["business_outcome_verified"])
        self.assertFalse(report["model_review_performed"])
        self.assertEqual(len(report["recommendations"]), 5)

    def test_repeated_polls_are_only_a_review_signal(self):
        report = evaluate_sources(None, self.desktop, chat_source_sha256=None,
                                  desktop_source_sha256="b"*64, policy_sha256="a"*64,
                                  literals=[])
        refs = [r["rule_id"] for r in report["recommendations"]]
        self.assertIn("polling_review", refs)
        self.assertTrue(all(r["assessment"] == "heuristic_review_only" for r in report["recommendations"]))

    def test_chat_only_and_desktop_only(self):
        for label, args in (("chat", ["--chatgpt-json", str(self.chatpath)]),
                            ("desktop", ["--desktop-history", str(self.desktoppath)])):
            output = self.root / ("out-" + label)
            with contextlib.redirect_stdout(io.StringIO()):
                result = cli(["analyze", *args, "--policy", str(self.policy),
                              "--out", str(output), "--acknowledge-local-analysis"])
            self.assertEqual(result, 0)
            self.assertTrue((output / "manifest.json").exists())

    def test_complete_cli_roundtrip_and_modified_preview(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(["analyze", *self._args()]), 0)
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(receipt["status"], "preview_written_not_applied")
        report_path = self.out / "chatgpt-task-improvement.preview.json"
        data = json.loads(report_path.read_text("utf-8"))
        self.assertEqual(data["desktop_commander"]["calls"], 5)
        self.assertEqual(data["chatgpt"]["user_messages"], 2)
        verify_args = ["verify", *self._args(), "--expected-manifest-sha256",
                       receipt["manifest_sha256"]]
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(verify_args), 0)
        report_path.chmod(0o600)
        report_path.write_text("TAMPERED", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(verify_args), 2)

    def test_source_change_is_rejected_on_verify(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(["analyze", *self._args()]), 0)
        receipt = json.loads(stdout.getvalue())
        self.desktop.append(self.desktop[0])
        self._write()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["verify", *self._args(), "--expected-manifest-sha256",
                                  receipt["manifest_sha256"]]), 2)

    def test_policy_changes_are_bound(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(["analyze", *self._args()]), 0)
        receipt = json.loads(stdout.getvalue())
        self.policy.write_bytes(json_bytes({"schema_version": "1.0",
            "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["社内機密の目印", "追加の機密辞書"]}))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["verify", *self._args(), "--expected-manifest-sha256",
                                  receipt["manifest_sha256"]]), 2)

    def test_missing_consent_does_not_read_inputs(self):
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("unexpected read")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli(["analyze", "--chatgpt-json", str(self.chatpath),
                                      "--policy", str(self.policy), "--out", str(self.out)]), 2)
        self.assertFalse(self.out.exists())

    def test_unrecognized_conversations_schema_rejected(self):
        for item in ({"messages": []}, [{"id": "i", "mapping": {}}],
                     [{"id": "i", "current_node": "lost", "mapping": {}}]):
            with self.subTest(item=item), self.assertRaises(InvalidData):
                summarize_chatgpt(item, [])

    def test_cycle_and_duplicate_conversation_rejected(self):
        cycle = deepcopy(self.chat)
        cycle[0]["mapping"]["root"]["parent"] = "u3"
        with self.assertRaises(InvalidData):
            summarize_chatgpt(cycle, [])
        with self.assertRaises(InvalidData):
            summarize_chatgpt(self.chat + self.chat, [])

    def test_sensitive_user_text_and_policy_rejected_without_echo(self):
        for text in ("test@example.org", "090-1234-5678",
                     "AKIAABCDEFGHIJKLMNOP", "社内機密の目印"):
            item = deepcopy(self.chat)
            item[0]["mapping"]["u1"]["message"]["content"]["parts"] = [text]
            with self.subTest(label=len(text)), self.assertRaises(InvalidData) as caught:
                summarize_chatgpt(item, ["社内機密の目印"])
            self.assertNotIn(text, str(caught.exception))

    def test_non_string_message_parts_do_not_become_text(self):
        item = deepcopy(self.chat)
        item[0]["mapping"]["u1"]["message"]["content"]["parts"] = [{"image": "SECRET"}]
        result = summarize_chatgpt(item, [])
        self.assertEqual(result["user_messages"], 1)
        self.assertEqual(result["skipped_nodes_on_selected_branches"], 1)

    def test_unknown_tool_name_is_not_exposed(self):
        dc = deepcopy(self.desktop)
        dc[0]["toolName"] = "PRIVATE_INTERNALLY_NAMED_ACTION"
        result = summarize_desktop_history(dc)
        self.assertEqual(result["unclassified_tool_names"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_invalid_history_rows_and_unhandled_export_fail_closed(self):
        for value in ([{"toolName": None}], [{"toolName": ""}], {"bad": "shape"}):
            with self.subTest(value=value), self.assertRaises(InvalidData):
                summarize_desktop_history(value)
        self.desktoppath.write_text('{"toolName":"start_process"}\nNOT_JSON', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["analyze", *self._args()]), 2)
        self.assertFalse(self.out.exists())

    def test_repo_output_input_and_existing_output_blocked(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["analyze", *self._args()[:-3], "--out", str(repo / "preview"),
                                  "--acknowledge-local-analysis"]), 2)
        self.assertFalse((repo / "preview").exists())
        self.out.mkdir()
        (self.out / "keep.txt").write_text("no change", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["analyze", *self._args()]), 2)
        self.assertEqual((self.out / "keep.txt").read_text("utf-8"), "no change")

    def test_no_network_subprocess_or_side_effect(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli(["analyze", *self._args()]), 0)
            connect.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
