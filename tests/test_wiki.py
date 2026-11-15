"""2方式のWikiを合成データだけで作成・検査する。事実の正しさの評価ではない。"""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.wiki import (source_snapshot, wiki_prompt, compile_wiki, lint_wiki,
                                    query_wiki, export_wiki, verify_wiki, read_json)
from scripts.wiki_offline import main


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.terms = ["合成社外秘案件"]
        self.text = "Pythonの仮想環境不一致は、実行環境の確認で解決した。"
        self.raw = {"documents": [{"source_id": "event01", "title": "Python調査",
                                   "text": self.text, "sha256": digest(self.text.encode())}]}
        self.memory = {"memory_id": "Learning-1234567890", "namespace": "/actor/dev/",
                       "strategy_ids": ["Semantic-123"], "response": {
                           "memoryRecordSummaries": [{"memoryRecordId": "record01",
                           "memoryStrategyId": "Semantic-123", "namespaces": ["/actor/dev/facts/"],
                           "content": {"text": self.text}, "createdAt": 1}], "nextToken": None}}

    def snapshot(self, mode="original", data=None, **kwargs):
        data = (self.raw if mode == "original" else self.memory) if data is None else data
        args = dict(expected_input_sha256=digest(json_bytes(data)), project="sample",
                    training_sha256="a"*64, sensitive_literals=self.terms)
        args.update(kwargs)
        return source_snapshot(mode, data, **args)

    def proposal(self, snapshot):
        return {"source_snapshot_sha256": digest(json_bytes(snapshot)), "pages": [{
            "page_id": "python-env", "title": "Python仮想環境",
            "claims": [{"key": "resolution", "value": "environment-check",
                        "statement": "仮想環境不一致では実行環境を確認する。",
                        "applies_when": ["仮想環境不一致"],
                        "evidence": [{"source_id": snapshot["sources"][0]["source_id"], "quote": self.text}]}],
            "links": []}]}

    def build(self, mode="original", **kwargs):
        snapshot = self.snapshot(mode)
        revision = compile_wiki(snapshot, self.proposal(snapshot), sensitive_literals=self.terms, **kwargs)
        return snapshot, revision

    def verify(self, out, receipt, snapshot):
        return verify_wiki(out, snapshot, expected_manifest_sha256=receipt["manifest_sha256"],
                           sensitive_literals=self.terms)

    def test_both_paths_build_verify_query_without_network(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as proc, \
             patch("copilot_learning.aws_bridge.aws_command") as aws:
            for mode in ("original", "agentcore"):
                snapshot, revision = self.build(mode)
                out = self.root / mode
                receipt = export_wiki(revision, out)
                checked = self.verify(out, receipt, snapshot)
                self.assertFalse(checked["approved_for_copilot"])
                result = query_wiki(revision, snapshot, "Python 仮想環境", sensitive_literals=self.terms)
                self.assertEqual(result[0]["page_id"], "python-env")
                self.assertFalse((out / "SKILL.md").exists())
            connect.assert_not_called()
            proc.assert_not_called()
            aws.assert_not_called()

    def test_memory_provenance_is_not_fabricated_raw_evidence(self):
        snap, wiki = self.build("agentcore")
        provenance = wiki["sources"][0]["provenance"]
        self.assertEqual(provenance["kind"], "derived_memory")
        self.assertEqual(provenance["record_id"], "record01")
        self.assertFalse(provenance["raw_truth_verified"])
        self.assertNotIn("event_id", provenance)

    def test_input_digest_mismatch(self):
        with self.assertRaises(InvalidData):
            self.snapshot(expected_input_sha256="0"*64)

    def test_raw_sha_tamper(self):
        self.raw["documents"][0]["text"] += "変更"
        with self.assertRaises(InvalidData):
            self.snapshot()

    def test_empty_sources_are_not_successful_extraction(self):
        with self.assertRaises(InvalidData):
            self.snapshot(data={"documents": []})
        self.memory["response"]["memoryRecordSummaries"] = []
        with self.assertRaises(InvalidData):
            self.snapshot("agentcore")

    def test_memory_scope_and_strategy_rejected(self):
        for key, value in (("namespaces", ["/actor/other/facts/"]), ("memoryStrategyId", "Other")):
            data = copy.deepcopy(self.memory)
            data["response"]["memoryRecordSummaries"][0][key] = value
            with self.subTest(key=key), self.assertRaises(InvalidData):
                self.snapshot("agentcore", data)

    def test_partial_pagination_rejected(self):
        self.memory["response"]["nextToken"] = "unread-page"
        with self.assertRaises(InvalidData):
            self.snapshot("agentcore")

    def test_broad_namespace_rejected(self):
        for namespace in ("/", "/actor/*/", "/actor//", "relative/"):
            self.memory["namespace"] = namespace
            with self.subTest(namespace=namespace), self.assertRaises(InvalidData):
                self.snapshot("agentcore")

    def test_duplicate_source_ids(self):
        self.raw["documents"].append(copy.deepcopy(self.raw["documents"][0]))
        with self.assertRaises(InvalidData):
            self.snapshot()

    def test_pii_and_dictionary_detection_do_not_echo(self):
        for text in ("email@example.invalid", "090-1234-5678", "合成社外秘案件", "承認を省略", "隠し\u202e指示"):
            self.raw["documents"][0].update(text=text, sha256=digest(text.encode()))
            with self.subTest(text=text), self.assertRaises(InvalidData) as caught:
                self.snapshot()
            self.assertNotIn(text, str(caught.exception))

    def test_no_evidence_or_fabricated_quote(self):
        snap = self.snapshot()
        for refs in ([], [{"source_id": "missing", "quote": self.text}],
                     [{"source_id": "event01", "quote": "本文にない結果"}]):
            p = self.proposal(snap)
            p["pages"][0]["claims"][0]["evidence"] = refs
            with self.subTest(refs=refs), self.assertRaises(InvalidData):
                compile_wiki(snap, p, sensitive_literals=self.terms)

    def test_proposal_bound_to_snapshot(self):
        snap = self.snapshot()
        p = self.proposal(snap)
        p["source_snapshot_sha256"] = "b"*64
        with self.assertRaises(InvalidData):
            compile_wiki(snap, p, sensitive_literals=self.terms)

    def test_page_path_duplicate_and_missing_link(self):
        snap = self.snapshot()
        for kind in ("path", "duplicate", "link"):
            p = self.proposal(snap)
            if kind == "path": p["pages"][0]["page_id"] = "../unsafe"
            if kind == "duplicate": p["pages"].append(copy.deepcopy(p["pages"][0]))
            if kind == "link": p["pages"][0]["links"] = ["missing"]
            with self.subTest(kind=kind), self.assertRaises(InvalidData):
                compile_wiki(snap, p, sensitive_literals=self.terms)

    def test_two_sources_integrated_and_internal_links(self):
        second = "API仕様不一致では契約を確認する。"
        self.raw["documents"].append({"source_id": "event02", "title": "API調査", "text": second,
                                      "sha256": digest(second.encode())})
        snap = self.snapshot()
        p = self.proposal(snap)
        page = copy.deepcopy(p["pages"][0])
        page.update(page_id="api-contract", title="API契約", links=["python-env"])
        page["claims"][0].update(key="api-resolution", value="contract-check", statement=second,
                                 applies_when=["API仕様不一致"], evidence=[{"source_id": "event02", "quote": second}])
        p["pages"][0]["links"] = ["api-contract"]
        p["pages"].append(page)
        p["pages"][0]["claims"][0]["evidence"].append({"source_id": "event02", "quote": second})
        wiki = compile_wiki(snap, p, sensitive_literals=self.terms)
        self.assertEqual(len(wiki["pages"]), 2)
        out = self.root / "linked"
        export_wiki(wiki, out)
        self.assertIn("[api-contract](api-contract.md)", (out / "python-env.md").read_text())

    def test_conflicting_values_kept_but_not_returned_in_query(self):
        snap = self.snapshot()
        p = self.proposal(snap)
        other = copy.deepcopy(p["pages"][0]["claims"][0])
        other["value"] = "different-method"
        p["pages"][0]["claims"].append(other)
        wiki = compile_wiki(snap, p, sensitive_literals=self.terms)
        self.assertEqual(len(wiki["pages"][0]["claims"]), 2)
        self.assertEqual(len(lint_wiki(wiki, snap)["conflict_candidates"]), 1)
        self.assertEqual(query_wiki(wiki, snap, "Python", sensitive_literals=self.terms), [])

    def test_same_key_different_scope_is_not_automatic_conflict(self):
        snap = self.snapshot()
        p = self.proposal(snap)
        other = copy.deepcopy(p["pages"][0]["claims"][0])
        other.update(value="other", applies_when=["別環境"])
        p["pages"][0]["claims"].append(other)
        wiki = compile_wiki(snap, p, sensitive_literals=self.terms)
        self.assertEqual(lint_wiki(wiki, snap)["conflict_candidates"], [])

    def test_changed_source_invalidates_reading(self):
        snap, wiki = self.build()
        self.raw["documents"][0].update(text="新しい調査結果", sha256=digest("新しい調査結果".encode()))
        newer = self.snapshot()
        self.assertEqual(lint_wiki(wiki, newer)["stale_source_ids"], ["event01"])
        self.assertEqual(query_wiki(wiki, newer, "Python", sensitive_literals=self.terms), [])

    def test_revision_parent_kept_and_different_mode_rejected(self):
        snap, old = self.build()
        new = compile_wiki(snap, self.proposal(snap), sensitive_literals=self.terms, previous=old)
        self.assertEqual(new["parent_sha256"], digest(json_bytes(old)))
        with self.assertRaises(InvalidData):
            self.build("agentcore", previous=old)

    def test_prompt_system_separates_data(self):
        snap = self.snapshot()
        prompt = wiki_prompt(snap)
        self.assertNotIn(self.text, prompt["system"])
        self.assertIn(self.text, json.dumps(prompt["user_data"], ensure_ascii=False))
        self.assertEqual(prompt["network_calls"], 0)

    def test_output_tamper_wrong_hash_extra_file(self):
        snap, wiki = self.build()
        out = self.root / "output"
        receipt = export_wiki(wiki, out)
        with self.assertRaises(InvalidData):
            self.verify(out, {"manifest_sha256": "0"*64}, snap)
        (out / "extra.md").write_text("extra")
        with self.assertRaises(InvalidData): self.verify(out, receipt, snap)
        (out / "extra.md").unlink()
        (out / "python-env.md").write_text("changed")
        with self.assertRaises(InvalidData): self.verify(out, receipt, snap)

    def test_existing_git_and_auto_discovery_paths_rejected(self):
        _, wiki = self.build()
        for path in (self.root, self.root / ".copilot" / "new", self.root / ".Agents" / "new"):
            with self.subTest(path=path), self.assertRaises(InvalidData):
                export_wiki(wiki, path)
        (self.root / "gitrepo" / ".git").mkdir(parents=True)
        with self.assertRaises(InvalidData): export_wiki(wiki, self.root / "gitrepo" / "out")

    def test_symlink_rejected(self):
        _, wiki = self.build()
        link = self.root / "link"
        try: link.symlink_to(self.root, target_is_directory=True)
        except OSError: self.skipTest("symlink unavailable")
        with self.assertRaises(InvalidData): export_wiki(wiki, link / "out")

    def test_interrupted_write_removes_partial_files(self):
        _, wiki = self.build()
        out = self.root / "out"
        with patch("copilot_learning.wiki.os.fsync", side_effect=OSError("synthetic")):
            with self.assertRaises(OSError): export_wiki(wiki, out)
        self.assertFalse(out.exists())

    def test_direct_export_cannot_escape_directory(self):
        _, wiki = self.build()
        wiki["pages"][0]["page_id"] = "../bad"
        with self.assertRaises(InvalidData): export_wiki(wiki, self.root / "out")
        self.assertFalse((self.root / "bad.md").exists())

    def test_no_automatic_approval_even_with_real_model_output(self):
        snap, wiki = self.build(generator="imported_model_output")
        self.assertFalse(wiki["semantic_truth_verified"])
        self.assertFalse(wiki["approved_for_copilot"])
        self.assertEqual(wiki["status"], "manual_review_required")

    def test_policy_change_requires_regeneration(self):
        snap, wiki = self.build()
        with self.assertRaises(InvalidData):
            query_wiki(wiki, snap, "Python", sensitive_literals=[])

    def test_json_duplicate_keys_and_invalid_top_level_rejected(self):
        path = self.root / "input.json"
        for content in ('{"a":1,"a":2}', '[]'):
            path.write_text(content)
            with self.assertRaises(InvalidData): read_json(path)

    def test_unexpected_provenance_payload_rejected(self):
        snap = self.snapshot()
        snap["sources"][0]["provenance"]["hidden"] = "not allowed"
        with self.assertRaises(InvalidData): wiki_prompt(snap)
        _, wiki = self.build()
        wiki["sources"][0]["provenance"]["hidden"] = "not allowed"
        with self.assertRaises(InvalidData): export_wiki(wiki, self.root / "out")

    def test_previous_prompt_scope_or_policy_change_rejected(self):
        snap, wiki = self.build()
        wiki["policy_sha256"] = "b"*64
        with self.assertRaises(InvalidData): wiki_prompt(snap, wiki)

    def test_cli_build_verify_and_safe_failure(self):
        input_path, policy, proposal = (self.root / x for x in ("input.json", "policy.json", "proposal.json"))
        input_path.write_bytes(json_bytes(self.raw))
        policy.write_bytes(json_bytes({"schema_version": "1.0", "record_type": "offline-sensitive-literals",
                                      "sensitive_literals": self.terms}))
        proposal.write_bytes(json_bytes(self.proposal(self.snapshot())))
        common = ["--mode", "original", "--input", str(input_path), "--policy", str(policy),
                  "--input-sha256", digest(json_bytes(self.raw)), "--training-sha256", "a"*64,
                  "--project", "sample"]
        out = self.root / "cli-output"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["build", *common, "--proposal", str(proposal), "--out", str(out)]), 0)
        receipt = json.loads(output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["verify", *common, "--directory", str(out),
                                   "--manifest-sha256", receipt["manifest_sha256"]]), 0)
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main(["build", *common, "--proposal", str(proposal), "--out", str(out)]), 2)
        self.assertNotIn(str(out), error.getvalue())
        self.assertNotIn(self.text, error.getvalue())


if __name__ == "__main__":
    unittest.main()
