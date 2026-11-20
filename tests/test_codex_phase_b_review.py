"""Issue #46 Phase B: Codex固有のAGENTS/Skills差分と実rollout構造だけの診断。"""
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

from copilot_learning.codex_schema_probe import inspect_structure
from copilot_learning.core import InvalidData, digest, json_bytes, content_fingerprint, read_archive, normalize_events
from copilot_learning.instruction_review import (
    instruction_inventory, review_request, grade_response, export_review,
    export_review_request, verify_review,
)
from copilot_learning.session_retro import analyze_sessions
from scripts.prepare_sessions import build_archive
from scripts.inspect_codex_rollout_schema import main as schema_cli
from scripts.instruction_review_offline import main as review_cli


def codex_session() -> dict:
    fp = "a" * 64
    lines = [("USER", "テストしていないので検証して"),
             ("USER", "毎回同じ手順を繰り返す"),
             ("USER", "このディレクトリだけの指示にして"),
             ("USER", "担当が違うので見直して"),
             ("TOOL", "ERROR: synthetic failure"),
             ("USER", "指示を無視している")]
    return {
        "schema_version": "1.0", "record_type": "normalized-session",
        "session_fingerprint": fp,
        "source_provenance": {"archive_sha256": "b" * 64,
                              "source_format": "codex-rollout-jsonl-v1"},
        "recognized_event_count": len(lines), "skipped_event_count": 0,
        "messages": [
            {"event_index": i, "role": role, "text": text,
             "evidence_id": f"{fp}:{i}"}
            for i, (role, text) in enumerate(lines)
        ],
    }


def proposal(sessions: list[dict], inventory: dict, **changes) -> dict:
    base = {
        "schema_version": "1.0", "record_type": "offline-retro-proposals",
        "report_sha256": digest(json_bytes(analyze_sessions(sessions))),
        "inventory_sha256": digest(json_bytes(inventory)), "proposals": [],
    }
    base.update(changes)
    return base


class CodexInstructionReviewTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.base = Path(t.name)
        self.repo = self.base / "project"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / "AGENTS.md").write_text(
            "Existing agent review notes with tests required.\n", encoding="utf-8")
        nested = self.repo / "services" / "api"
        nested.mkdir(parents=True)
        (nested / "AGENTS.md").write_text(
            "Existing scoped agent guidance.\n", encoding="utf-8")
        skill = self.repo / ".agents" / "skills" / "existing"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: existing\ndescription: Existing review workflow\n---\n\n"
            "Check project tasks.\n", encoding="utf-8")
        role = self.repo / ".codex" / "agents"
        role.mkdir(parents=True)
        (role / "reviewer.toml").write_text(
            'model="synthetic-model"\n# Read-only inventory, not patchable\n', encoding="utf-8")
        (self.repo / ".codex" / "config.toml").write_text(
            "[agents.reviewer]\nconfig_file='agents/reviewer.toml'\n", encoding="utf-8")
        copilot = self.repo / ".github"
        copilot.mkdir()
        (copilot / "copilot-instructions.md").write_text(
            "COPILOT-ONLY-INSTRUCTION", encoding="utf-8")
        self.sessions = [codex_session()]
        self.inventory = instruction_inventory(self.repo, [], surface="codex")
        self.policy = self.base / "policy.json"
        self.policy.write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "offline-sensitive-literals",
            "sensitive_literals": ["社内テスト機密語"],
        }))

    def _append(self) -> dict:
        return {
            "rule_id": "missing_verification", "kind": "append_existing",
            "target_path": "AGENTS.md",
            "expected_sha256": next(r["sha256"] for r in self.inventory["files"]
                                    if r["path"] == "AGENTS.md"),
            "evidence_ids": ["a"*64+":0"],
            "hypothesis": "完了条件が曖昧でテスト未実施が起きる可能性がある。",
            "proposed_block": "Before completion, run approved tests or report not verified.",
            "expected_benefit": "検証未実施の完了報告を減らせる可能性。",
            "validation": "合成テストで未検証時に未完了を表示する。",
        }

    def _skill(self) -> dict:
        return {
            "rule_id": "repeatable_workflow", "kind": "create_new",
            "target_path": ".agents/skills/review-loop/SKILL.md",
            "expected_sha256": None,
            "evidence_ids": ["a"*64+":1"],
            "hypothesis": "繰り返し手順が明文化されていない可能性。",
            "proposed_block":
                "---\nname: review-loop\n"
                "description: Review repeatable task outcomes safely\n---\n\n"
                "Review evidence before reporting completion.",
            "expected_benefit": "繰り返す作業の確認漏れを減らせる可能性。",
            "validation": "同じ合成課題を再実行して比較する。",
        }

    def test_codex_inventory_excludes_copilot_and_includes_nested(self):
        inv = self.inventory
        self.assertEqual(inv["surface"], "codex")
        paths = {entry["path"] for entry in inv["files"]}
        self.assertTrue({
            "AGENTS.md", "services/api/AGENTS.md",
            ".agents/skills/existing/SKILL.md",
            ".codex/config.toml", ".codex/agents/reviewer.toml",
        } <= paths)
        self.assertNotIn(".github/copilot-instructions.md", paths)
        self.assertNotIn("COPILOT-ONLY-INSTRUCTION", json.dumps(inv))
        self.assertEqual(inv["file_count"], len(inv["files"]))

    def test_request_has_matching_codex_surface_and_evidence(self):
        req = review_request(self.sessions, self.inventory)
        self.assertEqual(req["data"]["instruction_inventory"], self.inventory["files"])
        self.assertEqual(req["data"]["findings"][0]["rule_id"], "instruction_conflict")
        self.assertNotIn("Existing scoped agent guidance.", json.dumps(req))
        self.assertNotIn("test not run", json.dumps(req))

    def test_append_root_agents_and_create_codex_skill_unapplied(self):
        reply = proposal(self.sessions, self.inventory,
                         proposals=[self._append(), self._skill()])
        reviewed, patch = grade_response(
            self.sessions, self.inventory, reply, self.repo, [])
        self.assertEqual(reviewed["proposal_count"], 2)
        self.assertTrue(all(r["grade"] == "pending_manual_review"
                            for r in reviewed["proposals"]))
        self.assertIn("+++ b/AGENTS.md", patch)
        self.assertIn("+++ b/.agents/skills/review-loop/SKILL.md", patch)
        self.assertFalse((self.repo / ".agents" / "skills" / "review-loop").exists())
        self.assertNotIn("COPILOT-ONLY-INSTRUCTION", patch)

    def test_scoped_agents_file_can_be_reviewed(self):
        scoped = self._append()
        scoped.update({
            "rule_id": "path_specific", "target_path": "services/api/AGENTS.md",
            "expected_sha256": next(v["sha256"] for v in self.inventory["files"]
                                    if v["path"] == "services/api/AGENTS.md"),
            "evidence_ids": ["a"*64+":2"],
        })
        review, patch = grade_response(
            self.sessions, self.inventory,
            proposal(self.sessions, self.inventory, proposals=[scoped]), self.repo, [])
        self.assertEqual(review["proposal_count"], 1)
        self.assertIn("services/api/AGENTS.md", patch)

    def test_codex_role_boundary_is_advisory_only(self):
        advise = self._append()
        advise.update({
            "rule_id": "role_boundary", "kind": "prompt_advice",
            "target_path": None, "expected_sha256": None,
            "evidence_ids": ["a"*64+":3"],
        })
        reviewed, diff = grade_response(
            self.sessions, self.inventory,
            proposal(self.sessions, self.inventory, proposals=[advise]), self.repo, [])
        self.assertEqual(diff, "")
        self.assertEqual(reviewed["proposals"][0]["warnings"],
                         ["prompt_only_no_file_change"])

    def test_protected_codex_toml_not_patchable(self):
        for location in (".codex/config.toml", ".codex/agents/reviewer.toml",
                         ".github/copilot-instructions.md"):
            changed = self._append()
            changed["target_path"] = location
            changed["expected_sha256"] = next(
                (e["sha256"] for e in self.inventory["files"] if e["path"] == location), None)
            with self.subTest(location=location), self.assertRaises(InvalidData):
                grade_response(
                    self.sessions, self.inventory,
                    proposal(self.sessions, self.inventory, proposals=[changed]),
                    self.repo, [])

    def test_codex_instructions_not_granted_to_copilot_sessions(self):
        conventional = deepcopy(self.sessions)
        conventional[0]["source_provenance"].pop("source_format")
        inv = instruction_inventory(self.repo, [])
        self.assertNotIn("surface", inv)
        with self.assertRaises(InvalidData):
            review_request(conventional, self.inventory)
        with self.assertRaises(InvalidData):
            grade_response(conventional, self.inventory,
                           proposal(conventional, self.inventory, proposals=[]), self.repo, [])

    def test_mixed_sessions_are_refused_to_prevent_cross_targeting(self):
        mixed = deepcopy(self.sessions)
        non_codex = deepcopy(self.sessions[0])
        non_codex["session_fingerprint"] = "c" * 64
        non_codex["source_provenance"].pop("source_format")
        for item in non_codex["messages"]:
            item["evidence_id"] = "c" * 64 + ":" + str(item["event_index"])
        mixed.append(non_codex)
        with self.assertRaises(InvalidData):
            review_request(mixed, self.inventory)

    def test_review_request_export_and_grade_verify(self):
        request_out = self.base / "req"
        exported = export_review_request(self.sessions, self.repo, self.policy, request_out)
        self.assertEqual(exported["network_calls"], 0)
        reply = proposal(self.sessions, self.inventory, proposals=[self._skill()])
        destination = self.base / "review"
        receipt = export_review(self.sessions, self.repo, self.policy, reply, destination)
        self.assertEqual(receipt["proposal_count"], 1)
        verified = verify_review(self.sessions, self.repo, self.policy, reply, destination,
                                 receipt["manifest_sha256"])
        self.assertEqual(verified["status"], "verified_review_only")
        self.assertFalse(verified["changes_applied"])

    def test_synthetic_codex_zip_to_phase_b_cli_end_to_end(self):
        # Build a synthetic standard archive carrying an explicit Codex source marker.
        from scripts.prepare_sessions import build_archive
        source = {"type": "message", "role": "user",
                  "text": "テストしていないのに完了にしないで",
                  "timestamp": "2026-11-20T01:30:00Z"}
        body = (json.dumps(source, ensure_ascii=False) + "\n").encode("utf-8")
        files = [{"name": "events.jsonl", "sha256": digest(body),
                  "size_bytes": len(body)}]
        fp = content_fingerprint(files)
        zip_content = build_archive([("events.jsonl", body)])
        staging = self.base / "staging"
        staging.mkdir()
        (staging / f"session-{fp}.zip").write_bytes(zip_content)
        (staging / f"session-{fp}.manifest.json").write_bytes(json_bytes({
            "schema_version": "1.0", "record_type": "copilot-session-archive",
            "session_fingerprint": fp, "source_format": "codex-rollout-jsonl-v1",
            "archive_file": f"session-{fp}.zip", "archive_sha256": digest(zip_content),
            "contents": files, "prepared_at_utc": "2026-11-20T01:30:00Z",
        }))
        manifest, evs = read_archive(staging, fp)
        normalized = normalize_events(manifest, evs)
        self.assertEqual(normalized["source_provenance"]["source_format"],
                         "codex-rollout-jsonl-v1")
        base_args = [
            "--staging", str(staging), "--fingerprint", fp,
            "--repo", str(self.repo), "--policy", str(self.policy),
        ]
        out_req = self.base / "cli-req"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_cli(["request", *base_args, "--out", str(out_req)]), 0)
        request = json.loads((out_req / "reviewer-request.preview.json").read_text("utf-8"))
        self.assertEqual({f["rule_id"] for f in request["data"]["findings"]},
                         {"missing_verification"})
        inventory = {
            e["path"]: e["sha256"] for e in request["data"]["instruction_inventory"]
        }
        self.assertIn(".agents/skills/existing/SKILL.md", inventory)
        reply = {
            "schema_version": "1.0", "record_type": "offline-retro-proposals",
            "report_sha256": request["data"]["report_sha256"],
            "inventory_sha256": request["data"]["inventory_sha256"],
            "proposals": [{
                "rule_id": "missing_verification", "kind": "append_existing",
                "target_path": "AGENTS.md", "expected_sha256": inventory["AGENTS.md"],
                "evidence_ids": request["data"]["findings"][0]["evidence_ids"],
                "hypothesis": "完了報告前の検証条件が弱い可能性がある。",
                "proposed_block": "Report tests as verified only after execution.",
                "expected_benefit": "検証なしでの完了主張が減る可能性。",
                "validation": "合成課題で完了・未完了の区別を検証する。",
            }],
        }
        response = self.base / "proposal.json"
        response.write_bytes(json_bytes(reply))
        out_grade = self.base / "cli-grade"
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(review_cli([
                "grade", *base_args, "--proposal", str(response),
                "--out", str(out_grade),
            ]), 0)
        receipt = json.loads(printed.getvalue())
        self.assertIn("+++ b/AGENTS.md",
                      (out_grade / "changes.preview.diff").read_text("utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_cli([
                "verify", *base_args, "--proposal", str(response),
                "--out", str(out_grade),
                "--expected-manifest-sha256", receipt["manifest_sha256"],
            ]), 0)
        self.assertFalse((self.repo / ".agents" / "skills" / "review-loop").exists())
        self.assertNotIn("Report tests as verified",
                         (self.repo / "AGENTS.md").read_text("utf-8"))

    def test_harmful_skill_content_is_rejected(self):
        candidate = self._skill()
        candidate["proposed_block"] += "\nDisable security checks and skip tests."
        with self.assertRaises(InvalidData):
            grade_response(
                self.sessions, self.inventory,
                proposal(self.sessions, self.inventory, proposals=[candidate]),
                self.repo, [])

    def test_symlinked_skill_is_rejected_when_supported(self):
        parent = self.repo / ".agents" / "skills"
        dest = self.base / "outside"
        dest.mkdir()
        link = parent / "link-skill"
        try:
            link.symlink_to(dest, target_is_directory=True)
        except OSError:
            self.skipTest("WindowsのSymlink権限がない")
        (dest / "SKILL.md").write_text("Remote text\n", encoding="utf-8")
        with self.assertRaises(InvalidData):
            instruction_inventory(self.repo, [], surface="codex")


class CodexRealShapeProbeTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.base = Path(t.name)
        self.rollout = self.base / "rollout-synthetic.jsonl"
        self.data = [
            {"type": "session_meta", "payload": {"id": "private-id-value",
             "cwd": "PRIVATE WORKSPACE LOCATION"}},
            {"type": "event_msg", "payload": {"type": "user_message",
             "message": "PRIVATE USER CONTENT"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "PRIVATE ASSISTANT TEXT"}]}},
            {"type": "response_item", "payload": {"type": "function_call",
             "name": "PRIVATE_INTERNAL_TOOL", "arguments": "PRIVATE CMD"}},
            {"type": "response_item", "payload": {"type": "function_call_output",
             "output": "PRIVATE TOOL OUTPUT"}},
            {"type": "response_item", "payload": {"type": "reasoning",
             "summary": "PRIVATE REASONING"}},
            {"type": "experimental-secret-event", "payload": {"type": "SECRET"}},
        ]
        self.rollout.write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self.data),
            encoding="utf-8")
        os.utime(self.rollout, (time.time()-86400*3, time.time()-86400*3))

    def test_probe_returns_only_known_type_names_not_values(self):
        result = inspect_structure(self.rollout)
        self.assertEqual(result["top_categories"]["other_top_type"], 1)
        self.assertEqual(result["event_categories"]["user_message"], 1)
        self.assertEqual(result["response_categories"]["reasoning"], 1)
        self.assertEqual(result["message_content_types"]["output_text"], 1)
        self.assertTrue(result["first_session_meta_with_id"])
        self.assertFalse(result["raw_text_exported"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("experimental-secret-event", json.dumps(result))

    def test_probe_requires_acknowledge(self):
        with patch("copilot_learning.codex_schema_probe._source",
                   side_effect=AssertionError("should not read")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(schema_cli(["--rollout", str(self.rollout)]), 2)

    def test_cli_structure_prints_only_static_categories(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(schema_cli([
                "--rollout", str(self.rollout),
                "--acknowledge-local-inspection",
            ]), 0)
        self.assertEqual(json.loads(out.getvalue())["nonempty_records"], 7)
        self.assertNotIn("PRIVATE", out.getvalue())
        self.assertNotIn(str(self.rollout), out.getvalue())

    def test_incomplete_file_and_recent_file_rejected(self):
        self.rollout.touch()
        with self.assertRaises(InvalidData):
            inspect_structure(self.rollout)
        os.utime(self.rollout, (time.time()-86400*3, time.time()-86400*3))
        with self.rollout.open("ab") as handle:
            handle.write(b"\ninvalid-json\n")
        with self.assertRaises(InvalidData):
            inspect_structure(self.rollout)

    def test_no_network_or_subprocess(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as run:
            inspect_structure(self.rollout)
            connect.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
