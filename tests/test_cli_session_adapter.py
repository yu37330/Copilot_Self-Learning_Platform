"""Issue #36: CLIネイティブログの正規化・安全な取込・メタデータ診断テスト。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.cli_session_adapter import VERSION, normalize_native_cli_events
from copilot_learning.core import InvalidData, normalize_events, read_archive
from copilot_learning.session_retro import analyze_sessions, export_preview, verify_preview
from scripts.prepare_sessions import prepare
from scripts.probe_session_sources import main as probe_cli


def raw_events() -> list[dict]:
    return [
        {"type": "session.start", "timestamp": "2026-11-19T05:00:00Z",
         "data": {"sessionId": "synthetic-session"}},
        {"type": "user.message", "timestamp": "2026-11-19T05:00:01Z",
         "data": {"content": "意図と違うので、やり直して"}},
        {"type": "assistant.turn_start", "timestamp": "2026-11-19T05:00:02Z",
         "data": {"turnId": "turn-1"}},
        {"type": "assistant.message", "timestamp": "2026-11-19T05:00:03Z",
         "data": {"content": "結果を確認します"}},
        {"type": "tool.execution_start", "timestamp": "2026-11-19T05:00:04Z",
         "data": {"toolCallId": "tool-1", "toolName": "synthetic-tool"}},
        {"type": "tool.execution_complete", "timestamp": "2026-11-19T05:00:05Z",
         "data": {"toolCallId": "tool-1", "success": False,
                  "error": "synthetic private path that must never appear in output"}},
        {"type": "tool.execution_complete", "timestamp": "2026-11-19T05:00:06Z",
         "data": {"toolCallId": "tool-2", "success": True,
                  "result": "synthetic private tool output not for downstream learning"}},
        {"type": "session.shutdown", "timestamp": "2026-11-19T05:00:07Z",
         "data": {"reason": "synthetic"}},
    ]


def synthetic_manifest():
    return {"session_fingerprint": "a" * 64,
            "archive_sha256": "b" * 64,
            "_snapshot_manifest_sha256": "c" * 64,
            "prepared_at_utc": "2026-11-19T05:00:00Z"}


class CliSessionAdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = synthetic_manifest()
        self.events = raw_events()

    def test_cli_event_envelope_dispatch_and_exact_evidence_index(self):
        result = normalize_events(self.manifest, self.events)
        self.assertEqual(result["normalizer_version"], VERSION)
        self.assertEqual([x["role"] for x in result["messages"]], ["USER", "ASSISTANT", "TOOL"])
        self.assertEqual([x["event_index"] for x in result["messages"]], [1, 3, 5])
        self.assertEqual(result["messages"][0]["evidence_id"], "a" * 64 + ":1")
        self.assertEqual(result["messages"][1]["timestamp"], "2026-11-19T05:00:03Z")
        self.assertEqual(result["ignored_known_event_count"], 5)
        self.assertEqual(result["skipped_event_count"], 0)
        self.assertEqual(result["source_provenance"]["archive_sha256"], "b" * 64)

    def test_tool_output_is_redacted_and_error_signal_is_counted(self):
        result = normalize_events(self.manifest, self.events)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("synthetic private", serialized)
        self.assertEqual(result["messages"][-1]["text"],
                         "ERROR: Copilot tool execution reported failure")
        retro = analyze_sessions([result])
        kind = {x["rule_id"] for x in retro["findings"]}
        self.assertIn("tool_failure", kind)
        self.assertIn("explicit_correction", kind)

    def test_unknown_event_not_converted_to_a_message(self):
        events = self.events + [{"type": "experimental.new-kind",
                                 "timestamp": "2026-11-19T05:00:09Z",
                                 "data": {"content": "fake user message"}}]
        result = normalize_events(self.manifest, events)
        self.assertEqual(result["skipped_event_count"], 1)
        self.assertEqual(result["skipped_details"], [
            {"event_index": len(events) - 1, "reason": "unsupported_type"}])
        self.assertEqual(result["format_status"], "partial_unrecognized_events")
        self.assertNotIn("fake user message", json.dumps(result))

    def test_missing_text_does_not_become_fake_content(self):
        events = self.events + [{"type": "assistant.message",
                                 "timestamp": "2026-11-19T05:00:09Z",
                                 "data": {"content": {"untrusted": "do not stringify"}}}]
        result = normalize_events(self.manifest, events)
        self.assertEqual(result["skipped_event_count"], 1)
        self.assertNotIn("do not stringify", json.dumps(result))

    def test_mixed_legacy_and_native_is_rejected(self):
        events = self.events + [{"type": "message", "role": "user", "text": "legacy"}]
        with self.assertRaises(InvalidData):
            normalize_events(self.manifest, events)

    def test_missing_structured_data_rejected(self):
        events = self.events.copy()
        events[1] = {"type": "user.message", "content": "unsafe"}
        with self.assertRaises(InvalidData):
            normalize_events(self.manifest, events)

    def test_invalid_or_missing_timestamp_is_rejected(self):
        for value in (None, "2026-11-19T05:00:00", "invalid", 123):
            events = raw_events()
            events[1] = {**events[1], "timestamp": value}
            with self.subTest(value=value), self.assertRaises(InvalidData):
                normalize_events(self.manifest, events)

    def test_missing_user_or_only_telemetry_rejected(self):
        events = [e for e in self.events if e["type"] != "user.message"]
        with self.assertRaises(InvalidData):
            normalize_events(self.manifest, events)
        with self.assertRaises(InvalidData):
            normalize_events(self.manifest, [self.events[0], self.events[-1]])

    def test_missing_tool_status_is_not_synthetic_success(self):
        events = raw_events()
        events[5]["data"].pop("success")
        res = normalize_events(self.manifest, events)
        self.assertEqual(res["skipped_event_count"], 1)
        self.assertEqual(res["skipped_details"][0]["reason"], "tool_result_status_missing")

    def test_secret_text_is_rejected_without_printing_it(self):
        events = raw_events()
        events[1]["data"]["content"] = "AKIAABCDEFGHIJKLMNOP"
        with self.assertRaises(InvalidData) as exc:
            normalize_events(self.manifest, events)
        self.assertNotIn(events[1]["data"]["content"], str(exc.exception))

    def test_timestamp_offset_converted_to_utc(self):
        events = raw_events()
        events[1]["timestamp"] = "2026-11-19T14:00:01+09:00"
        result = normalize_native_cli_events(self.manifest, events)
        self.assertEqual(result["messages"][0]["timestamp"], "2026-11-19T05:00:01Z")

    def test_legacy_format_keeps_existing_behavior(self):
        manifest = synthetic_manifest()
        older = [{"type": "message", "role": "user", "text": "合成データ",
                  "timestamp": "2026-11-19T05:00:01Z"}]
        result = normalize_events(manifest, older)
        self.assertEqual(result["normalizer_version"], "allowlist-1.1")
        self.assertEqual(result["messages"][0]["text"], "合成データ")

    def test_complete_synthetic_zip_archive_to_retro_preview(self):
        session = self.root / "native-session"
        session.mkdir()
        log = session / "events.jsonl"
        log.write_text("\n".join(json.dumps(event, ensure_ascii=False)
                                 for event in raw_events()) + "\n", encoding="utf-8")
        os.utime(log, (1700000000, 1700000000))
        staging = self.root / "staging"
        archive = prepare(session, staging, 30)
        fingerprint = archive.stem.removeprefix("session-")
        manifest, stored = read_archive(staging, fingerprint)
        normalized = normalize_events(manifest, stored)
        self.assertEqual(normalized["recognized_event_count"], 3)
        out = self.root / "preview"
        receipt = export_preview([normalized], out)
        verified = verify_preview([normalized], out, receipt["manifest_sha256"])
        self.assertEqual(verified["status"], "verified_preview_only")
        preview = (out / "retrospective.preview.json").read_text("utf-8")
        self.assertNotIn("synthetic private", preview)

    def test_probe_only_counts_files_never_reads_content(self):
        root = self.root / "session-state"
        one = root / "private-session-name"
        one.mkdir(parents=True)
        (one / "events.jsonl").write_text("PRIVATE AND NOT READ", encoding="utf-8")
        exports = self.root / "vscode-exports"
        exports.mkdir()
        (exports / "chat.json").write_text("PRIVATE CONTENT", encoding="utf-8")
        buffer = io.StringIO()
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("must not read")), \
                contextlib.redirect_stdout(buffer):
            code = probe_cli(["--cli-root", str(root), "--vscode-export-dir", str(exports)])
        self.assertEqual(code, 0)
        record = json.loads(buffer.getvalue())
        self.assertEqual(record["cli"]["directory_count"], 1)
        self.assertEqual(record["cli"]["event_logs"], 1)
        self.assertEqual(record["vscode_export"]["json_files"], 1)
        self.assertFalse(record["session_content_read"])
        self.assertFalse(record["vscode_export_parsed"])
        self.assertNotIn("PRIVATE", buffer.getvalue())
        self.assertNotIn("private-session-name", buffer.getvalue())

    def test_probe_missing_source_and_symlink_are_safe(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(probe_cli(["--cli-root", str(self.root / "missing")]), 0)
        self.assertEqual(json.loads(buffer.getvalue())["cli"]["status"], "absent")
        source = self.root / "source"
        source.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(source, target_is_directory=True)
        except OSError:
            return
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(probe_cli(["--cli-root", str(link)]), 0)
        self.assertEqual(json.loads(buffer.getvalue())["cli"]["status"], "untrusted_path")


if __name__ == "__main__":
    unittest.main()
