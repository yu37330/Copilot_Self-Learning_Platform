"""Issue #31: 合成セッションによる振り返り・安全性の回帰検査。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, normalize_events
from copilot_learning.session_retro import (
    MANIFEST_FILE, MARKDOWN_FILE, REPORT_FILE, analyze_sessions,
    export_preview, prepare_preview, verify_preview,
)
from scripts.prepare_sessions import prepare
from scripts.session_retro_offline import main as cli


def session(fp: str, items: list[tuple[str, str]], *, skipped: int = 0) -> dict:
    messages = [
        {"role": role, "text": content, "event_index": i, "evidence_id": f"{fp}:{i}"}
        for i, (role, content) in enumerate(items)
    ]
    return {
        "schema_version": "1.0", "record_type": "normalized-session",
        "session_fingerprint": fp, "source_provenance": {"archive_sha256": "9" * 64},
        "recognized_event_count": len(messages), "skipped_event_count": skipped,
        "messages": messages,
    }


class SessionRetroTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.output = self.root / "review"
        self.first = session("a" * 64, [
            ("USER", "意図と違う。やり直して"),
            ("ASSISTANT", "確認します"),
            ("TOOL", "ERROR: module not found"),
            ("USER", "テストしていないのに完了にしないで"),
        ])
        self.second = session("b" * 64, [
            ("USER", "毎回同じ手順になるので整理したい"),
            ("USER", "担当が違うので別の担当にして"),
            ("USER", "このディレクトリだけルールを変えたい"),
            ("USER", "指示を無視している"),
            ("USER", "not what I asked"),
        ])

    def test_cross_session_classification_and_evidence(self):
        report = analyze_sessions([self.second, self.first])
        self.assertEqual(report["finding_count"], 7)
        found = {item["rule_id"]: item for item in report["findings"]}
        self.assertEqual(found["explicit_correction"]["target"], "prompt")
        self.assertEqual(found["explicit_correction"]["session_count"], 2)
        self.assertEqual(found["explicit_correction"]["signal_count"], 2)
        self.assertEqual(found["tool_failure"]["target"], "test_or_skill")
        self.assertEqual(found["missing_verification"]["target"], "AGENTS.md")
        self.assertEqual(found["role_boundary"]["target"], ".github/agents/NAME.agent.md")
        self.assertEqual(found["repeatable_workflow"]["target"], ".github/skills/NAME/SKILL.md")
        self.assertEqual(found["path_specific"]["target"], ".github/instructions/NAME.instructions.md")
        self.assertEqual(found["instruction_conflict"]["target"], ".github/copilot-instructions.md")
        self.assertEqual(report["source_sessions"][0]["session_fingerprint"], "a" * 64)
        self.assertIsNone(report["causal_conclusion"])
        self.assertIsNone(report["measured_improvement"])
        self.assertFalse(report["approved_for_copilot"])

    def test_assistant_and_tool_text_not_misclassified_as_user_correction(self):
        report = analyze_sessions([session("a" * 64, [
            ("ASSISTANT", "指示を無視しているという表現は禁物"),
            ("TOOL", "The README says: やり直して"),
            ("USER", "通常の追加実装をしてください"),
        ])])
        self.assertEqual(report["finding_count"], 0)

    def test_tool_failure_requires_tool_role(self):
        report = analyze_sessions([session("a" * 64, [
            ("ASSISTANT", "次のドキュメントは ERROR: という例を含みます"),
            ("TOOL", "通常の文字列にError:が含まれます"),
        ])])
        self.assertEqual(report["finding_count"], 0)
        report = analyze_sessions([session("a" * 64, [("TOOL", "exit code 1")])])
        self.assertEqual(report["findings"][0]["rule_id"], "tool_failure")

    def test_incomplete_coverage_is_not_success(self):
        result = analyze_sessions([session("a" * 64, [("USER", "通常のタスク")], skipped=3)])
        self.assertEqual(result["coverage"], "partial_unrecognized_events")
        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(result["status"], "manual_review_required")

    def test_deterministic_order_and_preview(self):
        manifest1, data1, md1 = prepare_preview([self.second, self.first])
        manifest2, data2, md2 = prepare_preview([self.first, self.second])
        self.assertEqual((manifest1, data1, md1), (manifest2, data2, md2))
        self.assertEqual(manifest1["status"], "not_installed_manual_review_required")

    def test_no_session_text_in_preview(self):
        receipt = export_preview([self.first, self.second], self.output)
        self.assertEqual(receipt["status"], "preview_written_not_installed")
        self.assertEqual(
            {p.name for p in self.output.iterdir()},
            {REPORT_FILE, MARKDOWN_FILE, MANIFEST_FILE},
        )
        self.assertEqual(verify_preview([self.second, self.first], self.output, receipt["manifest_sha256"])
                         ["status"], "verified_preview_only")
        dump = (self.output / REPORT_FILE).read_text("utf-8") + (self.output / MARKDOWN_FILE).read_text("utf-8")
        self.assertNotIn("意図と違う。やり直して", dump)
        self.assertNotIn("module not found", dump)
        self.assertNotIn("このディレクトリだけルールを変えたい", dump)
        self.assertFalse((self.output / "SKILL.md").exists())
        self.assertFalse((self.output / "AGENTS.md").exists())

    def test_no_existing_file_overwrite(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("important", encoding="utf-8")
        with self.assertRaises(InvalidData):
            export_preview([self.first], self.output)
        self.assertEqual(marker.read_text("utf-8"), "important")

    def test_modification_and_changed_input_detected(self):
        receipt = export_preview([self.first], self.output)
        path = self.output / MARKDOWN_FILE
        path.chmod(0o600)
        path.write_text("改変", encoding="utf-8")
        with self.assertRaises(InvalidData):
            verify_preview([self.first], self.output, receipt["manifest_sha256"])
        with self.assertRaises(InvalidData):
            verify_preview([self.first], self.output, "f" * 64)

    def test_git_autodiscovery_and_symlink_paths_blocked(self):
        (self.root / "repo" / ".git").mkdir(parents=True)
        for path in (self.root / "repo" / "retro", self.root / ".github" / "skills" / "retro",
                     self.root / ".agents" / "retro", self.root / ".copilot" / "retro"):
            with self.subTest(path=path), self.assertRaises(InvalidData):
                export_preview([self.first], path)
        link = self.root / "link"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(InvalidData):
            export_preview([self.first], link / "retro")

    def test_bad_source_duplicate_and_forged_evidence(self):
        with self.assertRaises(InvalidData):
            analyze_sessions([self.first, self.first])
        tampered = session("c" * 64, [("USER", "やり直して")])
        tampered["messages"][0]["evidence_id"] = "a" * 64 + ":0"
        with self.assertRaises(InvalidData):
            analyze_sessions([tampered])
        broken = session("d" * 64, [("USER", "やり直して")])
        broken["source_provenance"]["archive_sha256"] = "none"
        with self.assertRaises(InvalidData):
            analyze_sessions([broken])

    def test_input_secret_never_echoed(self):
        private = session("a" * 64, [("USER", "AKIAABCDEFGHIJKLMNOP")])
        with self.assertRaises(InvalidData) as caught:
            export_preview([private], self.output)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_cap_limits(self):
        with self.assertRaises(InvalidData):
            analyze_sessions([session(f"{i:064x}", []) for i in range(33)])
        large = session("a" * 64, [("USER", "やり直して")] * 15001)
        with self.assertRaises(InvalidData):
            analyze_sessions([large])

    def test_network_and_subprocess_are_not_used(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as subprocess:
            receipt = export_preview([self.first], self.output)
            verify_preview([self.first], self.output, receipt["manifest_sha256"])
            connect.assert_not_called()
            subprocess.assert_not_called()

    def test_cli_from_actual_prepared_synthetic_zip(self):
        source = self.root / "synthetic-session"
        source.mkdir()
        event = source / "events.jsonl"
        event.write_text(
            json.dumps({"type": "message", "role": "user", "text": "意図と違う、やり直して",
                        "timestamp": "2026-11-18T11:00:00Z"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        old = 1700000000
        os.utime(event, (old, old))
        staging = self.root / "staging"
        archive = prepare(source, staging, minimum_age_minutes=30)
        fp = archive.stem.removeprefix("session-")
        self.assertEqual(cli(["--staging", str(staging), "--fingerprint", fp, "--out", str(self.output)]), 0)
        meta = json.loads((self.output / MANIFEST_FILE).read_text("utf-8"))
        from copilot_learning.core import digest, json_bytes
        self.assertEqual(
            cli(["--staging", str(staging), "--fingerprint", fp, "--out", str(self.output),
                 "--verify", "--expected-manifest-sha256", digest(json_bytes(meta))]), 0)
        self.assertEqual(json.loads((self.output / REPORT_FILE).read_text("utf-8"))["finding_count"], 1)


if __name__ == "__main__":
    unittest.main()
