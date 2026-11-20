"""Issue #46: Codexの公式rollout構造の合成E2E、秘匿、検証、停止境界。"""
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

from copilot_learning.codex_rollout import (
    PREVIEW_FILE, RECEIPT_FILE, _parse, import_rollout, verify_rollout,
)
from copilot_learning.core import InvalidData, digest, json_bytes, normalize_events, read_archive
from copilot_learning.session_retro import analyze_sessions
from scripts.import_codex_rollout_offline import main as cli
from scripts.probe_codex_sessions import main as probe_cli, probe
from scripts.session_retro_offline import main as retro_cli

TS = "2026-11-19T12:00:00Z"


def example_rollout() -> list[dict]:
    return [
        {"timestamp": TS, "type": "session_meta",
         "payload": {"id": "synthetic-1234567", "cwd": "PRIVATE WORKSPACE PATH",
                     "base_instructions": "NEVER EXPOSE PRIVATE INSTRUCTIONS"}},
        {"timestamp": TS, "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "テストしていないのに完了と言った"}]}},
        {"timestamp": TS, "type": "event_msg",
         "payload": {"type": "user_message", "kind": "plain",
                     "message": "テストしていないのに完了と言った"}},
        {"timestamp": TS, "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "結果を検証します"}]}},
        {"timestamp": TS, "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "arguments": '{"command":"PRIVATE COMMAND SECRET ABC"}'}},
        {"timestamp": TS, "type": "response_item",
         "payload": {"type": "function_call_output", "output": "PRIVATE TERMINAL OUTPUT"}},
        {"timestamp": TS, "type": "response_item",
         "payload": {"type": "reasoning", "summary": [
             {"type": "summary_text", "text": "PRIVATE REASONING"}]}},
        {"timestamp": TS, "type": "event_msg",
         "payload": {"type": "user_message", "kind": "plain",
                     "message": "意図と違うからやり直して"}},
        {"timestamp": TS, "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "channel": "final",
                     "content": [{"type": "output_text", "text": "もう一度確認します"}]}},
        {"timestamp": TS, "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "channel": "analysis",
                     "content": [{"type": "output_text", "text": "PRIVATE INTERNAL ANALYSIS"}]}},
        {"timestamp": TS, "type": "event_msg",
         "payload": {"type": "turn_aborted", "reason": "synthetic"}},
        {"timestamp": TS, "type": "unknown_new_record",
         "payload": {"content": "PRIVATE UNKNOWN RECORD"}},
    ]


def encoded(events: list[dict]) -> bytes:
    return "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n"
                   for e in events).encode("utf-8")


class CodexRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rollout = self.root / "rollout-2026-11-19-synthetic.jsonl"
        self.events = example_rollout()
        self.policy = self.root / "policy.json"
        self.out = self.root / "staging"
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["組織内の秘密の単語"],
        }))
        self._write_rollout()

    def _write_rollout(self):
        self.rollout.write_bytes(encoded(self.events))
        os.utime(self.rollout, (time.time() - 900, time.time() - 900))

    def _args(self, action="import"):
        return [action, "--rollout", str(self.rollout), "--policy", str(self.policy),
                "--out", str(self.out), "--acknowledge-private-local-import"]

    def test_native_event_and_response_items_are_deduplicated(self):
        events, stats, sid = _parse(encoded(self.events), ["組織内の秘密の単語"])
        self.assertEqual(sid, "synthetic-1234567")
        self.assertEqual(stats["native_user_message_count"], 2)
        self.assertEqual(stats["assistant_output_text_count"], 2)
        self.assertEqual(stats["recognized_message_count"], 4)
        self.assertEqual(stats["ignored_event_categories"]["duplicate_user_response_items"], 1)
        self.assertEqual(stats["tool_calls_by_category"]["shell_tool"], 1)
        self.assertEqual(stats["tool_results_not_exported"], 1)
        self.assertEqual(stats["agent_error_or_interruption_signal_count"], 1)
        self.assertEqual(stats["unsupported_categories"]["unknown_top_type"], 1)
        self.assertEqual(len(events), 5)
        self.assertEqual(events[-1]["type"], "codex.unrecognized_records")

    def test_import_produces_verified_standard_archive_and_safe_preview(self):
        receipt = import_rollout(self.rollout, self.policy, self.out)
        self.assertEqual(receipt["recognized_message_count"], 4)
        fp = receipt["session_fingerprint"]
        self.assertEqual({x.name for x in self.out.iterdir()}, {
            f"session-{fp}.zip", f"session-{fp}.manifest.json",
            PREVIEW_FILE, RECEIPT_FILE,
        })
        manifest, saved_events = read_archive(self.out, fp)
        self.assertEqual(manifest["source_rollout_sha256"], digest(encoded(self.events)))
        normalized = normalize_events(manifest, saved_events)
        self.assertEqual(normalized["recognized_event_count"], 4)
        self.assertEqual(normalized["skipped_event_count"], 1)
        retro = analyze_sessions([normalized])
        self.assertEqual({f["rule_id"] for f in retro["findings"]},
                         {"missing_verification", "explicit_correction"})
        self.assertEqual(retro["coverage"], "partial_unrecognized_events")
        preview = json.loads((self.out / PREVIEW_FILE).read_text("utf-8"))
        self.assertFalse(preview["completion_verified"])
        self.assertFalse(preview["real_codex_execution_verified"])
        self.assertEqual(preview["provenance"]["tool_calls_by_category"]["shell_tool"], 1)
        combined = (self.out / PREVIEW_FILE).read_text("utf-8") + json.dumps(saved_events)
        for secret in ("PRIVATE", "synthetic-1234567", "PRIVATE WORKSPACE PATH",
                       "PRIVATE COMMAND", "PRIVATE TERMINAL OUTPUT"):
            self.assertNotIn(secret, combined)

    def test_session_retro_cli_accepts_codex_sanitized_archive(self):
        receipt = import_rollout(self.rollout, self.policy, self.out)
        destination = self.root / "phase-a-preview"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(retro_cli([
                "--staging", str(self.out),
                "--fingerprint", receipt["session_fingerprint"],
                "--out", str(destination),
            ]), 0)
        report = json.loads((destination / "retrospective.preview.json").read_text("utf-8"))
        self.assertEqual(report["finding_count"], 2)
        self.assertEqual(report["coverage"], "partial_unrecognized_events")
        self.assertNotIn("PRIVATE", buf.getvalue())

    def test_import_verify_and_second_identical_archive(self):
        saved = import_rollout(self.rollout, self.policy, self.out)
        verified = verify_rollout(self.rollout, self.policy, self.out,
                                  expected_receipt_sha256=saved["receipt_sha256"])
        self.assertEqual(verified["status"], "verified_offline_codex_import")
        second = self.root / "second"
        saved_again = import_rollout(self.rollout, self.policy, second)
        self.assertEqual(saved_again["session_fingerprint"], saved["session_fingerprint"])
        self.assertEqual(
            (self.out / f"session-{saved['session_fingerprint']}.zip").read_bytes(),
            (second / f"session-{saved['session_fingerprint']}.zip").read_bytes())

    def test_modified_rollout_refuses_verification(self):
        saved = import_rollout(self.rollout, self.policy, self.out)
        self.events[7]["payload"]["message"] = "何度もやり直して"
        self._write_rollout()
        with self.assertRaises(InvalidData):
            verify_rollout(self.rollout, self.policy, self.out,
                           expected_receipt_sha256=saved["receipt_sha256"])

    def test_modified_policy_refuses_verification(self):
        saved = import_rollout(self.rollout, self.policy, self.out)
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["組織内の秘密の単語", "追加の機密語"],
        }))
        with self.assertRaises(InvalidData):
            verify_rollout(self.rollout, self.policy, self.out,
                           expected_receipt_sha256=saved["receipt_sha256"])

    def test_modified_preview_refuses_verification(self):
        saved = import_rollout(self.rollout, self.policy, self.out)
        file = self.out / PREVIEW_FILE
        file.chmod(0o600)
        file.write_bytes(b"TAMPERED")
        with self.assertRaises(InvalidData):
            verify_rollout(self.rollout, self.policy, self.out,
                           expected_receipt_sha256=saved["receipt_sha256"])

    def test_older_source_and_active_source(self):
        self.rollout.touch()
        with self.assertRaises(InvalidData):
            import_rollout(self.rollout, self.policy, self.out)
        self.assertFalse(self.out.exists())

    def test_missing_session_meta_fails(self):
        self.events.pop(0)
        self._write_rollout()
        with self.assertRaises(InvalidData):
            import_rollout(self.rollout, self.policy, self.out)

    def test_synth_unknown_or_missing_metadata_fails(self):
        for sid in (None, "", "x"):
            records = deepcopy(example_rollout())
            records[0]["payload"]["id"] = sid
            with self.subTest(sid=sid), self.assertRaises(InvalidData):
                _parse(encoded(records), [])

    def test_multiple_metadata_fails(self):
        self.events.insert(1, deepcopy(self.events[0]))
        self._write_rollout()
        with self.assertRaises(InvalidData):
            import_rollout(self.rollout, self.policy, self.out)

    def test_secret_user_message_fails_without_echo(self):
        for value in ("090-1234-5678", "test@example.org",
                      "AKIAABCDEFGHIJKLMNOP", "組織内の秘密の単語"):
            self.events = example_rollout()
            self.events[2]["payload"]["message"] = "修正して" + value
            self._write_rollout()
            with self.subTest(reason=value), self.assertRaises(InvalidData) as caught:
                import_rollout(self.rollout, self.policy, self.out)
            self.assertNotIn(value, str(caught.exception))
            self.assertFalse(self.out.exists())

    def test_fallback_user_response_item_when_event_msg_missing(self):
        self.events = [e for e in example_rollout()
                       if not (e["type"] == "event_msg" and e["payload"]["type"] == "user_message")]
        self._write_rollout()
        result = import_rollout(self.rollout, self.policy, self.out)
        prev = json.loads((self.out / PREVIEW_FILE).read_text("utf-8"))
        self.assertEqual(result["recognized_message_count"], 3)
        self.assertEqual(prev["provenance"]["fallback_user_response_count"], 1)

    def test_multimodal_part_excluded_with_partial_coverage(self):
        self.events[3]["payload"]["content"] = [{"type": "output_text", "text": "okay"},
                                                  {"type": "tool", "text": "SECRET"}]
        self._write_rollout()
        result = import_rollout(self.rollout, self.policy, self.out)
        preview = json.loads((self.out / PREVIEW_FILE).read_text("utf-8"))
        self.assertEqual(preview["provenance"]["recognized_message_count"], 3)
        self.assertEqual(preview["provenance"]["unsupported_categories"]["unsupported_message_content"], 1)
        self.assertEqual(preview["retrospective"]["coverage"], "partial_unrecognized_events")

    def test_codex_instruction_targets_are_not_copilot_paths(self):
        self.events.extend([
            {"timestamp": TS, "type": "event_msg",
             "payload": {"type": "user_message", "kind": "plain",
                         "message": "指示を無視している"}},
            {"timestamp": TS, "type": "event_msg",
             "payload": {"type": "user_message", "kind": "plain",
                         "message": "このディレクトリだけ設定を変えて"}},
            {"timestamp": TS, "type": "event_msg",
             "payload": {"type": "user_message", "kind": "plain",
                         "message": "毎回同じ手順になる"}},
        ])
        self._write_rollout()
        import_rollout(self.rollout, self.policy, self.out)
        preview = json.loads((self.out / PREVIEW_FILE).read_text("utf-8"))
        retro = preview["retrospective"]
        self.assertEqual(retro["target_surface"], "codex")
        targets = {item["rule_id"]: item["target"] for item in retro["findings"]}
        self.assertEqual(targets["instruction_conflict"], "AGENTS.md")
        self.assertEqual(targets["path_specific"], "path/to/AGENTS.md")
        self.assertEqual(targets["repeatable_workflow"], ".agents/skills/NAME/SKILL.md")
        self.assertNotIn("copilot-instructions.md", json.dumps(retro))

    def test_unknown_tool_name_is_not_emitted(self):
        self.events[4]["payload"]["name"] = "PRIVATE-INTERNAL-NAME"
        self._write_rollout()
        import_rollout(self.rollout, self.policy, self.out)
        data = (self.out / PREVIEW_FILE).read_text("utf-8")
        self.assertIn("other_tool", data)
        self.assertNotIn("PRIVATE-INTERNAL-NAME", data)

    def test_partially_missing_native_user_event_is_preserved(self):
        self.events.insert(4, {
            "timestamp": TS, "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "追加の依頼です"}]},
        })
        self._write_rollout()
        import_rollout(self.rollout, self.policy, self.out)
        preview = json.loads((self.out / PREVIEW_FILE).read_text("utf-8"))
        self.assertEqual(preview["provenance"]["recognized_message_count"], 5)
        self.assertEqual(preview["provenance"]["fallback_user_response_count"], 1)
        self.assertEqual(preview["provenance"]["ignored_event_categories"]["duplicate_user_response_items"], 1)
        self.assertEqual(preview["provenance"]["unsupported_categories"]["user_event_msg_missing_somewhere"], 1)

    def test_sensitive_assistant_output_is_rejected_without_leak(self):
        self.events[3]["payload"]["content"][0]["text"] = "これは秘密の連絡先test@example.org"
        self._write_rollout()
        with self.assertRaises(InvalidData) as caught:
            import_rollout(self.rollout, self.policy, self.out)
        self.assertNotIn("test@example.org", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_codex_home_is_not_an_output_directory(self):
        with self.assertRaises(InvalidData):
            import_rollout(self.rollout, self.policy,
                           self.root / ".codex" / "sessions" / "generated")
        self.assertFalse((self.root / ".codex").exists())

    def test_invalid_timestamp_is_rejected(self):
        self.events[2]["timestamp"] = "2026-11-19T14:00:01"
        self._write_rollout()
        with self.assertRaises(InvalidData):
            import_rollout(self.rollout, self.policy, self.out)

    def test_probe_does_not_read_file_contents_or_show_paths(self):
        folder = self.root / "sessions" / "2026" / "11" / "19"
        folder.mkdir(parents=True)
        target = folder / "rollout-PRIVATE-SESSION-ID.jsonl"
        target.write_text("PRIVATE CONTENT", encoding="utf-8")
        buf = io.StringIO()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("cannot read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("cannot read")), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(probe_cli(["--root", str(self.root / "sessions")]), 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["rollout_count"], 1)
        self.assertFalse(report["content_read"])
        self.assertNotIn("PRIVATE", buf.getvalue())

    def test_no_consent_means_no_file_read(self):
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("read forbidden")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli(["import", "--rollout", str(self.rollout),
                    "--policy", str(self.policy), "--out", str(self.out)]), 2)
        self.assertFalse(self.out.exists())

    def test_cli_import_verify_and_output_reuse_rejected(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli([*self._args()]), 0)
        info = json.loads(stdout.getvalue())
        self.assertNotIn("PRIVATE", stdout.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["verify", *self._args()[1:],
                                  "--expected-receipt-sha256", info["receipt_sha256"]]), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli([*self._args()]), 2)

    def test_git_repository_output_and_input_rejected(self):
        repo = self.root / "git"
        repo.mkdir()
        (repo / ".git").mkdir()
        with self.assertRaises(InvalidData):
            import_rollout(self.rollout, self.policy, repo / "stage")
        source = repo / self.rollout.name
        source.write_bytes(self.rollout.read_bytes())
        os.utime(source, (time.time() - 900, time.time() - 900))
        with self.assertRaises(InvalidData):
            import_rollout(source, self.policy, self.out)

    def test_communication_and_subprocess_never_called(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as run:
            import_rollout(self.rollout, self.policy, self.out)
            connect.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
