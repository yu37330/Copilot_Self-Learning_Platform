"""#41: 明示Task IDによるChatGPT/Commander参照紐付けの合成回帰検査。"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.task_trace_binding import bind_explicit_tasks, index_sources
from scripts.bind_chatgpt_task_trace_offline import main as cli


def sample_chat() -> list[dict]:
    def msg(role: str, value: str):
        return {"author": {"role": role}, "content": {"parts": [value]}}
    return [{
        "id": "PRIVATE-CHAT-ID-1",
        "title": "PRIVATE CHAT TITLE",
        "current_node": "u2",
        "mapping": {
            "root": {"parent": None, "message": None},
            "u1": {"parent": "root", "message": msg("user", "テストしていないのに完了と言った")},
            "a1": {"parent": "u1", "message": msg("assistant", "PRIVATE ASSISTANT CONTENT")},
            "u2": {"parent": "a1", "message": msg("user", "意図と違うのでやり直して")},
            "other": {"parent": "u1", "message": msg("user", "別分岐")},
        },
    }]


def sample_calls() -> list[dict]:
    return [
        {"timestamp": "2026-11-20T10:00:00+09:00", "toolName": "start_process",
         "arguments": {"command": "PRIVATE CMD DO NOT COPY"},
         "output": {"isError": True, "content": [{"text": "PRIVATE ERROR"}]}},
        {"timestamp": "2026-11-20T10:01:00+09:00", "toolName": "read_process_output",
         "arguments": {"pid": 1122},
         "output": {"content": [{"text": "PRIVATE STDOUT"}]}},
        {"timestamp": "2026-11-20T10:02:00+09:00", "toolName": "write_file",
         "arguments": {"path": "PRIVATE-WORKSPACE-PATH"},
         "output": {"status": "success"}},
        {"timestamp": "2026-11-20T10:03:00+09:00", "toolName": "read_process_output",
         "arguments": {"pid": 1122}, "output": {}},
    ]


class ExplicitTaskBindingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.chat = sample_chat()
        self.calls = sample_calls()
        self.chat_path = self.root / "chat.json"
        self.desktop_path = self.root / "history.jsonl"
        self.policy_path = self.root / "policy.json"
        self.binding_path = self.root / "task-bindings.json"
        self.out = self.root / "result"
        self.policy_path.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["社内専用テスト語"],
        }))
        self._write_sources()
        self.index = index_sources(self.chat, self.calls, **self._params())
        self.binding = {
            "schema_version": "1.0",
            "record_type": "explicit-chatgpt-desktop-task-bindings",
            "source_sha256": self.index["source_sha256"],
            "tasks": [{
                "task_id": "task-review-001",
                "conversation_ref": self.index["conversations"][0]["conversation_ref"],
                "desktop_calls": [
                    {"index": x["index"], "call_sha256": x["call_sha256"]}
                    for x in self.index["desktop_calls"][:2]
                ],
                "completion_claim": "reported_complete",
            }],
        }
        self.binding_path.write_bytes(json_bytes(self.binding))

    def _write_sources(self):
        self.chat_path.write_bytes(json_bytes(self.chat))
        self.desktop_path.write_text("".join(
            json.dumps(v, ensure_ascii=False, sort_keys=True) + "\n" for v in self.calls),
            encoding="utf-8")

    def _params(self):
        return {"chat_sha256": digest(self.chat_path.read_bytes()),
                "desktop_sha256": digest(self.desktop_path.read_bytes()),
                "literals": ["社内専用テスト語"]}

    def _args(self):
        return ["--chatgpt-json", str(self.chat_path),
                "--desktop-history", str(self.desktop_path),
                "--policy", str(self.policy_path),
                "--out", str(self.out), "--acknowledge-local-analysis"]

    def test_index_shows_only_pseudonymous_refs(self):
        index = self.index
        self.assertEqual(len(index["conversations"]), 1)
        self.assertEqual(len(index["desktop_calls"]), 4)
        dump = json.dumps(index, ensure_ascii=False)
        for term in ("PRIVATE", "テストしていない", "意図と違う", "1122", "CMD"):
            self.assertNotIn(term, dump)
        self.assertEqual(index["task_linking"], "unlinked_index_only")
        self.assertFalse(index["business_outcome_verified"])

    def test_bind_exact_hash_and_indices_without_timestamps(self):
        review = bind_explicit_tasks(self.chat, self.calls, self.binding, **self._params())
        self.assertEqual(review["task_count"], 1)
        self.assertEqual(review["bound_desktop_call_count"], 2)
        self.assertEqual(review["unlinked_calls"], 2)
        task = review["tasks"][0]
        self.assertEqual(task["correction_signals"]["missing_verification"], 1)
        self.assertEqual(task["correction_signals"]["explicit_correction"], 1)
        self.assertEqual(task["desktop_outcomes"]["explicit_error"], 1)
        self.assertEqual(task["desktop_outcomes"]["unknown"], 1)
        self.assertEqual(task["completion_verification"],
                         "not_verified_requires_independent_artifact")
        self.assertTrue(task["reported_complete"])
        self.assertFalse(review["task_completion_verified"])
        self.assertFalse(review["external_evidence_checked"])
        self.assertEqual(review["task_linking"], "explicit_source_hash_and_index_only")
        self.assertNotIn("PRIVATE", json.dumps(review))

    def test_complete_index_bind_verify_cli_roundtrip(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(["index", *self._args()]), 0)
        self.assertTrue((self.out / "task-trace.index.json").exists())
        self.out = self.root / "review"
        args = ["--binding", str(self.binding_path), *self._args()]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(["bind", *args]), 0)
        receipt = json.loads(stdout.getvalue())
        review = json.loads((self.out / "task-trace.review.json").read_text("utf-8"))
        self.assertFalse(review["task_completion_verified"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["verify", *args,
                                  "--expected-manifest-sha256", receipt["manifest_sha256"]]), 0)
        self.assertNotIn("PRIVATE", stdout.getvalue())

    def test_duplicate_selected_call_rejected(self):
        changed = deepcopy(self.binding)
        changed["tasks"][0]["desktop_calls"].append(changed["tasks"][0]["desktop_calls"][0])
        with self.assertRaises(InvalidData):
            bind_explicit_tasks(self.chat, self.calls, changed, **self._params())

    def test_another_task_cannot_reuse_conversation(self):
        changed = deepcopy(self.binding)
        another = deepcopy(changed["tasks"][0])
        another["task_id"] = "another-task-002"
        another["desktop_calls"] = [self.index["desktop_calls"][2]]
        changed["tasks"].append(another)
        with self.assertRaises(InvalidData):
            bind_explicit_tasks(self.chat, self.calls, changed, **self._params())

    def test_wrong_call_index_and_sha_rejected(self):
        for mode in ("index", "sha"):
            changed = deepcopy(self.binding)
            target = changed["tasks"][0]["desktop_calls"][0]
            if mode == "index":
                target["index"] = 999
            else:
                target["call_sha256"] = "0" * 64
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                bind_explicit_tasks(self.chat, self.calls, changed, **self._params())

    def test_wrong_conversation_ref_or_task_id_rejected(self):
        for mode in ("ref", "id"):
            changed = deepcopy(self.binding)
            item = changed["tasks"][0]
            if mode == "ref":
                item["conversation_ref"] = "0" * 64
            else:
                item["task_id"] = "Private task with spaces"
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                bind_explicit_tasks(self.chat, self.calls, changed, **self._params())

    def test_changed_source_or_manifest_sha_rejected(self):
        for mode in ("chat", "desktop"):
            changed = deepcopy(self.binding)
            changed["source_sha256"][mode] = "0" * 64
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                bind_explicit_tasks(self.chat, self.calls, changed, **self._params())

    def test_unknown_or_forged_completion_status_rejected(self):
        changed = deepcopy(self.binding)
        changed["tasks"][0]["completion_claim"] = "verified_success"
        with self.assertRaises(InvalidData):
            bind_explicit_tasks(self.chat, self.calls, changed, **self._params())
        changed = deepcopy(self.binding)
        changed["tasks"][0]["verified"] = True
        with self.assertRaises(InvalidData):
            bind_explicit_tasks(self.chat, self.calls, changed, **self._params())

    def test_source_change_blocks_verify(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(["bind", "--binding", str(self.binding_path), *self._args()]), 0)
        token = json.loads(stdout.getvalue())["manifest_sha256"]
        self.calls[0]["arguments"]["command"] += " CHANGED"
        self._write_sources()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["verify", "--binding", str(self.binding_path), *self._args(),
                                  "--expected-manifest-sha256", token]), 2)

    def test_policy_change_blocks_verify(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(["bind", "--binding", str(self.binding_path), *self._args()]), 0)
        token = json.loads(stdout.getvalue())["manifest_sha256"]
        self.policy_path.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["社内専用テスト語", "追加の秘密語"],
        }))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["verify", "--binding", str(self.binding_path), *self._args(),
                                  "--expected-manifest-sha256", token]), 2)

    def test_tampered_output_rejected(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli(["bind", "--binding", str(self.binding_path), *self._args()]), 0)
        token = json.loads(stdout.getvalue())["manifest_sha256"]
        changed = self.out / "task-trace.review.json"
        changed.chmod(0o600)
        changed.write_text("TAMPERED", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["verify", "--binding", str(self.binding_path), *self._args(),
                                  "--expected-manifest-sha256", token]), 2)

    def test_missing_consent_never_reads_source(self):
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("unexpected read")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli(["bind", "--binding", str(self.binding_path),
                                      "--chatgpt-json", str(self.chat_path),
                                      "--desktop-history", str(self.desktop_path),
                                      "--policy", str(self.policy_path),
                                      "--out", str(self.out)]), 2)
        self.assertFalse(self.out.exists())

    def test_source_content_mismatch_rejected_even_with_claim(self):
        changed = deepcopy(self.chat)
        changed[0]["id"] = "FALSIFIED-ID"
        with self.assertRaises(InvalidData):
            bind_explicit_tasks(changed, self.calls, self.binding, **self._params())

    def test_sensitive_chat_data_rejected(self):
        changed = deepcopy(self.chat)
        changed[0]["mapping"]["u2"]["message"]["content"]["parts"] = ["090-1234-5678"]
        with self.assertRaises(InvalidData):
            index_sources(changed, self.calls, **self._params())

    def test_git_or_existing_destination_rejected(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["index", *self._args()[:-3], "--out", str(repo / "private"),
                                  "--acknowledge-local-analysis"]), 2)
        self.assertFalse((repo / "private").exists())
        self.out.mkdir()
        (self.out / "existing.txt").write_text("KEEP", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["index", *self._args()]), 2)
        self.assertEqual((self.out / "existing.txt").read_text("utf-8"), "KEEP")

    def test_no_network_or_subprocess(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli(["index", *self._args()]), 0)
            connect.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
