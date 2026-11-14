"""ネットワークも実際のCopilotデータも使わないローカル単体テスト。"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_sessions as collector  # noqa: E402


class PrepareSessionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "staging"
        self.session = self.source / "test-session-001"
        self.session.mkdir()
        self.event = self.session / "events.jsonl"
        self.event.write_text('{"type":"message","text":"テスト成功"}\n', encoding="utf-8")
        self.workspace = self.session / "workspace.yaml"
        self.workspace.write_text("name: sample\n", encoding="utf-8")
        self.make_old()

    def make_old(self):
        old = 1_700_000_000
        os.utime(self.event, (old, old))

    def test_archive_and_manifest_integrity(self):
        self.assertEqual(collector.main(["--source", str(self.source), "--session", self.session.name, "--out", str(self.output)]), 0)
        manifests = list(self.output.glob("*.manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        path = self.output / manifest["archive_file"]
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest["archive_sha256"])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(sorted(archive.namelist()), ["events.jsonl", "workspace.yaml"])
            self.assertIn("テスト成功", archive.read("events.jsonl").decode("utf-8"))
        self.assertEqual(manifest["review_status"], "requires_manual_approval")
        self.assertNotIn(self.session.name, path.name)

    def test_deterministic_archive_on_repeat(self):
        collector.main(["--source", str(self.source), "--all", "--out", str(self.output)])
        before = [file.read_bytes() for file in self.output.glob("*.zip")]
        collector.main(["--source", str(self.source), "--all", "--out", str(self.output)])
        self.assertEqual(before, [file.read_bytes() for file in self.output.glob("*.zip")])

    def test_suspected_secret_is_blocked(self):
        self.event.write_text('{"type":"message","value":"AKIAABCDEFGHIJKLMNOP"}\n', encoding="utf-8")
        self.make_old()
        self.assertEqual(collector.main(["--source", str(self.source), "--all", "--out", str(self.output)]), 1)
        self.assertEqual(list(self.output.glob("*.zip")), [])

    def test_recent_session_is_skipped(self):
        self.event.touch()
        self.assertEqual(collector.main(["--source", str(self.source), "--all", "--out", str(self.output)]), 1)

    def test_output_inside_repository_is_rejected(self):
        (self.root / ".git").mkdir()
        self.assertEqual(collector.main(["--source", str(self.source), "--all", "--out", str(self.root / "repo-staging")]), 2)
        self.assertEqual(list(self.root.rglob("*.zip")), [])

    def test_directory_traversal_is_rejected(self):
        self.assertEqual(collector.main(["--source", str(self.source), "--session", "../outside", "--out", str(self.output)]), 1)

    def test_invalid_jsonl_is_skipped(self):
        self.event.write_text("not json\n", encoding="utf-8")
        self.make_old()
        self.assertEqual(collector.main(["--source", str(self.source), "--all", "--out", str(self.output)]), 1)

    def test_list_only_does_not_create_files(self):
        self.assertEqual(collector.main(["--source", str(self.source), "--list"]), 0)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
