"""Issue #36: VS Code公式ChatModel.toExport型の限定取込を合成データで検証。"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, normalize_events, read_archive, json_bytes
from copilot_learning.vscode_export_adapter import (
    RECEIPT_FILE, build_conversion, export_import, prepare_import, verify_import,
)
from copilot_learning.session_retro import export_preview, analyze_sessions
from scripts.import_vscode_export_offline import main as cli
from scripts.session_retro_offline import main as retro_cli


def synthetic_export() -> dict:
    return {
        "responderUsername": "Synthetic Copilot",
        "initialLocation": "panel",
        "requests": [
            {
                "requestId": "synthetic-request-1",
                "timestamp": 1795000000000,
                "responseTimestamp": 1795000002000,
                "message": {"text": "意図と違うのでやり直して",
                            "parts": [{"kind": "text", "text": "意図と違うのでやり直して"}]},
                "response": [
                    {"value": "テスト結果を確認します。", "supportHtml": False},
                    {"kind": "toolInvocationSerialized",
                     "invocationMessage": {"value": "running private tool"},
                     "resultDetails": {"output": "PRIVATE TOOL OUTPUT MUST NOT APPEAR"}},
                    {"value": "確認は必要です。"},
                ],
                "agent": {"id": "synthetic-agent"},
                "result": {"metadata": {
                    "renderedGlobalContext": [{"text": "PRIVATE GLOBAL CONTEXT MUST NOT APPEAR"}]}},
            },
            {
                "requestId": "synthetic-request-2",
                "timestamp": 1795000010000,
                "message": {"text": "テストしていないのに完了と報告した"},
                "response": [{"value": "実行したテストを整理します"}],
            },
        ],
    }


class VSCodeExportAdapterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.policy = self.root / "policy.json"
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0",
            "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["内部機密テスト語"],
        }))
        self.export = self.root / "chat.json"
        self.document = synthetic_export()
        self.export.write_bytes(json_bytes(self.document))
        self.out = self.root / "staging"

    def _rewrite(self, doc: dict):
        self.export.write_bytes(json_bytes(doc))

    def test_official_shape_convert_to_verified_archive(self):
        result = export_import(self.export, self.policy, self.out)
        self.assertEqual(result["network_calls"], 0)
        self.assertFalse(result["approved_for_copilot"])
        fp = result["session_fingerprint"]
        self.assertEqual(set(p.name for p in self.out.iterdir()), {
            f"session-{fp}.zip", f"session-{fp}.manifest.json", RECEIPT_FILE})
        manifest, raw_events = read_archive(self.out, fp)
        self.assertEqual(manifest["source_export_sha256"], digest(self.export.read_bytes()))
        self.assertEqual(manifest["source_export_format"], "vscode-chat-export-model-toExport")
        self.assertIsNone(manifest["source_session_id_sha256"])
        self.assertEqual(manifest["source_session_id_status"], "not_in_export_format")
        self.assertEqual([ev.get("type") for ev in raw_events].count("message"), 5)
        session = normalize_events(manifest, raw_events)
        self.assertEqual(session["recognized_event_count"], 5)
        self.assertEqual(session["skipped_event_count"], 1)
        self.assertEqual(session["messages"][0]["timestamp_source"], "vscode_request_timestamp")
        self.assertEqual(session["messages"][-1]["timestamp_source"], "vscode_request_timestamp_fallback")
        self.assertEqual(session["timestamp_fallback_count"], 1)
        self.assertEqual(session["format_status"], "recognized")
        self.assertEqual(result["skipped_event_count"], 1)

    def test_explicit_source_map_preserved_and_timestamp_not_invented(self):
        receipt, archive, manifest, body = prepare_import(self.export, self.policy)
        self.assertEqual(receipt["source_request_count"], 2)
        self.assertEqual(receipt["converted_event_count"], 6)
        self.assertEqual(receipt["conversion_skipped_reasons"], {"unsupported_response_part": 1})
        self.assertEqual(receipt["source_event_index_map"][0]["request_index"], 0)
        self.assertEqual(receipt["source_event_index_map"][1]["response_part_index"], 0)
        self.assertEqual(receipt["source_event_index_map"][3]["response_part_index"], 2)
        self.assertEqual(receipt["response_time_fallback_request_count"], 1)
        self.assertEqual(receipt["source_event_index_map"][5]["timestamp_basis"],
                         "request_timestamp_fallback")
        self.assertEqual(receipt["status"], "partial_requires_manual_review")
        self.assertNotIn("PRIVATE TOOL", body.decode("utf-8"))
        self.assertNotIn("GLOBAL CONTEXT", body.decode("utf-8"))

    def test_tool_output_and_context_never_in_downstream_messages(self):
        result = export_import(self.export, self.policy, self.out)
        fp = result["session_fingerprint"]
        manifest, events = read_archive(self.out, fp)
        session = normalize_events(manifest, events)
        payload = json.dumps(session, ensure_ascii=False)
        self.assertNotIn("PRIVATE TOOL OUTPUT", payload)
        self.assertNotIn("PRIVATE GLOBAL CONTEXT", payload)
        retro = analyze_sessions([session])
        self.assertEqual({f["rule_id"] for f in retro["findings"]},
                         {"explicit_correction", "missing_verification"})
        self.assertEqual(retro["coverage"], "partial_unrecognized_events")
        preview = self.root / "preview"
        export_preview([session], preview)
        self.assertFalse((preview / "SKILL.md").exists())

    def test_deterministic_export_and_independent_verify(self):
        first = export_import(self.export, self.policy, self.out)
        original = verify_import(self.export, self.policy, self.out, first["receipt_sha256"])
        self.assertEqual(original["status"], "verified_offline_import_only")
        second_out = self.root / "second"
        second = export_import(self.export, self.policy, second_out)
        self.assertEqual(first, second)
        self.assertEqual((self.out / RECEIPT_FILE).read_bytes(),
                         (second_out / RECEIPT_FILE).read_bytes())
        self.assertEqual((self.out / "session-{}.zip".format(first["session_fingerprint"])).read_bytes(),
                         (second_out / "session-{}.zip".format(first["session_fingerprint"])).read_bytes())

    def test_modified_input_and_output_fail_verification(self):
        receipt = export_import(self.export, self.policy, self.out)
        copy = deepcopy(self.document)
        copy["requests"][0]["message"]["text"] = "やり直して、別の問題です"
        self._rewrite(copy)
        with self.assertRaises(InvalidData):
            verify_import(self.export, self.policy, self.out, receipt["receipt_sha256"])
        self._rewrite(self.document)
        archive = self.out / "session-{}.zip".format(receipt["session_fingerprint"])
        archive.chmod(0o600)
        archive.write_bytes(b"tampered")
        with self.assertRaises(InvalidData):
            verify_import(self.export, self.policy, self.out, receipt["receipt_sha256"])

    def test_changed_policy_requires_new_receipt(self):
        receipt = export_import(self.export, self.policy, self.out)
        policy = {"schema_version": "1.0", "record_type": "offline-sensitive-literals",
                  "sensitive_literals": ["内部機密テスト語", "追加秘密語"]}
        self.policy.write_bytes(json_bytes(policy))
        with self.assertRaises(InvalidData):
            verify_import(self.export, self.policy, self.out, receipt["receipt_sha256"])

    def test_unknown_response_or_missing_response_stays_unrecognized(self):
        doc = deepcopy(self.document)
        doc["requests"][1]["response"] = [{"kind": "otherResponse", "value": "DO NOT IMPORT"}]
        self._rewrite(doc)
        result = export_import(self.export, self.policy, self.out)
        fp = result["session_fingerprint"]
        manifest, events = read_archive(self.out, fp)
        normalized = normalize_events(manifest, events)
        self.assertEqual(normalized["skipped_event_count"], 2)
        self.assertNotIn("DO NOT IMPORT", json.dumps(normalized))
        self.assertNotIn("DO NOT IMPORT", (self.out / RECEIPT_FILE).read_text("utf-8"))

    def test_hidden_or_system_request_not_released(self):
        doc = deepcopy(self.document)
        doc["requests"][0]["hiddenFromTranscript"] = True
        self._rewrite(doc)
        result = export_import(self.export, self.policy, self.out)
        fp = result["session_fingerprint"]
        manifest, events = read_archive(self.out, fp)
        session = normalize_events(manifest, events)
        self.assertEqual(session["messages"][0]["text"], "テストしていないのに完了と報告した")
        self.assertEqual(session["skipped_event_count"], 1)
        self.assertNotIn("意図と違う", json.dumps(session, ensure_ascii=False))

    def test_empty_or_unknown_schema_rejected(self):
        for doc in ({}, {"responderUsername": "test", "requests": []},
                    {"responderUsername": "test", "requests": [{}]},
                    {"requests": [{"message": {"text": "hi"}, "timestamp": 1795000010000}]}):
            self._rewrite(doc)
            with self.subTest(doc=sorted(doc)), self.assertRaises(InvalidData):
                export_import(self.export, self.policy, self.out)
            self.assertFalse(self.out.exists())

    def test_legacy_message_string_is_allowed(self):
        doc = deepcopy(self.document)
        doc["requests"][0]["message"] = "意図と違うのでやり直して"
        self._rewrite(doc)
        result = export_import(self.export, self.policy, self.out)
        manifest, events = read_archive(self.out, result["session_fingerprint"])
        self.assertEqual(events[0]["text"], "意図と違うのでやり直して")

    def test_known_sensitive_text_rejected_without_echo(self):
        for i, sensitive in enumerate(("test@example.org", "090-1234-5678",
                                       "AKIAABCDEFGHIJKLMNOP", "内部機密テスト語")):
            doc = deepcopy(self.document)
            doc["requests"][0]["message"]["text"] += sensitive
            self._rewrite(doc)
            target = self.root / f"blocked-{i}"
            with self.subTest(sensitive_type=i):
                with self.assertRaises(InvalidData) as caught:
                    export_import(self.export, self.policy, target)
                self.assertNotIn(sensitive, str(caught.exception))
                self.assertFalse(target.exists())

    def test_unrecognized_source_without_valid_timestamp_rejected(self):
        doc = deepcopy(self.document)
        for req in doc["requests"]:
            req.pop("timestamp", None)
        self._rewrite(doc)
        with self.assertRaises(InvalidData):
            export_import(self.export, self.policy, self.out)

    def test_output_is_never_written_into_git_or_auto_discovery(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        for target in (repo / "result", self.root / ".github" / "skills" / "retro",
                       self.root / ".agents" / "retro"):
            with self.subTest(target=target), self.assertRaises(InvalidData):
                export_import(self.export, self.policy, target)
        self.assertFalse((repo / "result").exists())

    def test_input_in_git_workspace_rejected(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        source = repo / "copilot-export.json"
        source.write_bytes(self.export.read_bytes())
        with self.assertRaises(InvalidData):
            export_import(source, self.policy, self.out)

    def test_cli_requires_acknowledgement(self):
        outbuf = io.StringIO()
        with contextlib.redirect_stderr(outbuf):
            self.assertEqual(cli(["import", "--export", str(self.export),
                                  "--policy", str(self.policy), "--out", str(self.out)]), 2)
        self.assertFalse(self.out.exists())
        self.assertNotIn("意図と違う", outbuf.getvalue())

    def test_cli_import_verify_roundtrip(self):
        command = ["--export", str(self.export), "--policy", str(self.policy),
                   "--out", str(self.out), "--acknowledge-local-import"]
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            self.assertEqual(cli(["import", *command]), 0)
        receipt = json.loads(console.getvalue())
        self.assertIn("receipt_sha256", receipt)
        self.assertNotIn("意図と違う", console.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["verify", *command,
                                  "--expected-receipt-sha256", receipt["receipt_sha256"]]), 0)

    def test_imported_zip_is_accepted_by_existing_retro_cli(self):
        receipt = export_import(self.export, self.policy, self.out)
        retro_out = self.root / "retro-cli-preview"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(retro_cli([
                "--staging", str(self.out),
                "--fingerprint", receipt["session_fingerprint"],
                "--out", str(retro_out),
            ]), 0)
        report = json.loads((retro_out / "retrospective.preview.json").read_text("utf-8"))
        self.assertEqual({item["rule_id"] for item in report["findings"]},
                         {"explicit_correction", "missing_verification"})
        self.assertEqual(report["coverage"], "partial_unrecognized_events")
        self.assertNotIn("PRIVATE", stdout.getvalue())

    def test_symlink_input_rejected(self):
        link = self.root / "linked.json"
        try:
            link.symlink_to(self.export)
        except OSError:
            self.skipTest("Windowsのリンク作成権限がない")
        with self.assertRaises(InvalidData):
            export_import(link, self.policy, self.out)

    def test_offline_no_network_or_subprocess(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as run:
            result = export_import(self.export, self.policy, self.out)
            verify_import(self.export, self.policy, self.out, result["receipt_sha256"])
            connect.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
