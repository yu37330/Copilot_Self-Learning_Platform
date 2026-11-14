"""全比較構成の一括準備を合成データで検査。実モデルの性能試験ではない。"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.evals import task_fingerprint
from copilot_learning.experiment_suite import (ARM_SPECS, FAMILIES, build_experiment_suite,
                                                export_experiment_suite)
from scripts.prepare_experiment_suite import main as cli


class ExperimentSuiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dataset = {
            "label_source": "synthetic", "training_group_ids": ["train-1"],
            "training_task_sha256": [], "cases": [
                {"case_id": f"case-{i}", "group_id": f"test-{i}", "family": family,
                 "task": f"合成検証タスク{i}は領域{family}を対象にします。",
                 "expected": "applicable" if i % 2 == 0 else "not_applicable"}
                for i, family in enumerate(FAMILIES)
            ],
        }
        self.params = {"project": "synthetic", "training_snapshot_sha256": "a" * 64,
                       "model_id": "synthetic-model"}
        self.out = self.root / "準備 結果"

    def build(self, **changes):
        return build_experiment_suite(copy.deepcopy(self.dataset), **{**self.params, **changes})

    def test_all_thirteen_arms_prepared_not_executed(self):
        plan = self.build()
        self.assertEqual(len(plan["arms"]), 13)
        self.assertEqual(set(plan["arms"]), {x[0] for x in ARM_SPECS})
        self.assertTrue(plan["readiness"]["all_profiles_prepared"])
        self.assertFalse(plan["readiness"]["live_execution_ready"])
        self.assertFalse(plan["external_transfer_authorized"])
        for arm in plan["arms"].values():
            self.assertEqual(arm["status"], "not_executed")
            self.assertIsNone(arm["metrics"])
            self.assertIsNone(arm["cost_usd"])

    def test_all_native_strategies_and_combinations(self):
        arms = self.build()["arms"]
        self.assertEqual(arms["A0"]["strategies"], ["episodic"])
        self.assertEqual(arms["A2"]["strategies"], ["episodic", "semantic"])
        self.assertEqual(len(arms["A5"]["strategies"]), 4)
        for ident, strategy in (("S", "semantic"), ("Q", "summary"), ("U", "user_preference")):
            self.assertEqual(arms[ident]["strategies"], [strategy])

    def test_hybrid_reuses_same_source_snapshot_not_new_memory(self):
        plan = self.build(repetitions=2)
        for hybrid, source in (("C0", "A0"), ("C1", "A1")):
            self.assertEqual(plan["arms"][hybrid]["resource_creation"], "none")
            for h in (j for j in plan["jobs"] if j["arm"] == hybrid):
                a = next(j for j in plan["jobs"] if j["arm"] == source and
                         j["repeat"] == h["repeat"] and j["case_id"] == h["case_id"])
                self.assertEqual(h["knowledge_isolation_key"], a["knowledge_isolation_key"])
                self.assertTrue(h["snapshot_reuse_required"])

    def test_replicates_and_native_arms_are_isolated(self):
        jobs = self.build(repetitions=2)["jobs"]
        ids = {j["job_id"] for j in jobs}
        self.assertEqual(len(ids), 13 * len(FAMILIES) * 2)
        a = [j for j in jobs if j["arm"] in {"A0", "A1"} and j["case_id"] == "case-0"]
        self.assertEqual(len({j["knowledge_isolation_key"] for j in a}), 4)

    def test_control_has_no_knowledge(self):
        plan = self.build()
        self.assertEqual(plan["arms"]["D"]["kind"], "no_knowledge")
        self.assertTrue(all(j["knowledge_isolation_key"] is None for j in plan["jobs"] if j["arm"] == "D"))

    def test_tasks_and_labels_do_not_leak_into_plan(self):
        plan = self.build()
        text = json.dumps(plan, ensure_ascii=False)
        for case in self.dataset["cases"]:
            self.assertNotIn(case["task"], text)
        self.assertNotIn('"expected"', text)
        self.assertNotIn('"group_id"', text)

    def test_eval_learning_disabled_and_budget_is_global(self):
        p = self.build(context_budget_tokens=1234, top_k_total=4)
        self.assertFalse(p["common"]["evaluation_learning_enabled"])
        self.assertEqual(p["common"]["retrieved_context_budget_tokens"], 1234)
        self.assertEqual(p["common"]["retrieval_top_k_total"], 4)
        self.assertIn("native_hidden_model", p["cost_contract"])

    def test_identical_inputs_are_deterministic(self):
        self.assertEqual(json_bytes(self.build()), json_bytes(self.build()))

    def test_model_input_or_budget_changes_suite_id(self):
        original = self.build()["suite_id"]
        for change in ({"model_id": "other"}, {"training_snapshot_sha256": "b" * 64},
                       {"top_k_total": 7}, {"context_budget_tokens": 1500}):
            with self.subTest(change=change):
                self.assertNotEqual(original, self.build(**change)["suite_id"])

    def test_leaked_training_group_rejected(self):
        self.dataset["cases"][0]["group_id"] = "train-1"
        with self.assertRaises(InvalidData): self.build()

    def test_leaked_training_task_rejected(self):
        self.dataset["training_task_sha256"] = [task_fingerprint(self.dataset["cases"][0]["task"])]
        with self.assertRaises(InvalidData): self.build()

    def test_incomplete_fingerprint_checks_rejected(self):
        del self.dataset["training_task_sha256"]
        with self.assertRaises(InvalidData): self.build()

    def test_unknown_family_or_unsafe_case_id_rejected(self):
        for field, value in (("family", "unknown"), ("case_id", "../../task")):
            with self.subTest(field=field):
                old = self.dataset["cases"][0][field]
                self.dataset["cases"][0][field] = value
                with self.assertRaises(InvalidData): self.build()
                self.dataset["cases"][0][field] = old

    def test_missing_families_reported_without_skipping_arms(self):
        self.dataset["cases"] = self.dataset["cases"][:1]
        p = self.build()
        self.assertEqual(len(p["readiness"]["missing_case_families"]), 4)
        self.assertEqual(len(p["arms"]), 13)

    def test_invalid_or_boolean_bounds_rejected(self):
        for change in ({"repetitions": True}, {"repetitions": 21}, {"top_k_total": 0},
                       {"context_budget_tokens": 9000}, {"context_budget_tokens": False},
                       {"training_snapshot_sha256": "bad"}, {"project": "../private"}):
            with self.subTest(change=change), self.assertRaises(InvalidData): self.build(**change)

    def test_export_with_zero_external_calls_and_hash_manifest(self):
        p = self.build()
        with patch("subprocess.run") as run, patch("socket.socket.connect") as net, \
                patch("copilot_learning.aws_bridge.aws_command") as aws:
            result = export_experiment_suite(p, self.out)
            run.assert_not_called(); net.assert_not_called(); aws.assert_not_called()
        manifest = json.loads((self.out / "manifest.json").read_text("utf-8"))
        self.assertEqual(result["manifest_sha256"], digest((self.out / "manifest.json").read_bytes()))
        for name, info in manifest["files"].items():
            self.assertEqual(info["sha256"], digest((self.out / name).read_bytes()))
        blank = json.loads((self.out / "observations.blank.json").read_text("utf-8"))
        self.assertTrue(all(x is None for x in blank["arms"].values()))
        self.assertEqual(set(manifest["files"]), {"experiment.json", "memory_profiles.design.json",
                                                "jobs.json", "observations.blank.json"})

    def test_existing_output_not_overwritten(self):
        self.out.mkdir(); (self.out / "mine").write_text("keep")
        with self.assertRaises(InvalidData): export_experiment_suite(self.build(), self.out)
        self.assertEqual((self.out / "mine").read_text(), "keep")

    def test_repo_and_autoload_output_rejected(self):
        for name in (".github", ".agents", ".CoPiLoT"):
            with self.subTest(name=name), self.assertRaises(InvalidData):
                export_experiment_suite(self.build(), self.root / name / "plans")
        repo = self.root / "repo"; (repo / ".git").mkdir(parents=True)
        with self.assertRaises(InvalidData): export_experiment_suite(self.build(), repo / "plans")

    def test_symlink_output_rejected(self):
        link = self.root / "linked"
        try: link.symlink_to(self.root, target_is_directory=True)
        except OSError: self.skipTest("この環境ではシンボリックリンクを作成できません")
        with self.assertRaises(InvalidData): export_experiment_suite(self.build(), link / "plans")

    def test_partial_write_is_cleaned(self):
        with patch("copilot_learning.experiment_suite.os.fsync", side_effect=OSError("mock failure")):
            with self.assertRaises(OSError): export_experiment_suite(self.build(), self.out)
        self.assertFalse(self.out.exists())

    def test_cli_non_ascii_path_and_fixed_error(self):
        path = self.root / "評価 入力.json"; path.write_bytes(json_bytes(self.dataset))
        args = ["--dataset", str(path), "--project", "test", "--training-snapshot-sha256", "a" * 64,
                "--model-id", "synthetic-model", "--out", str(self.out)]
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(cli(args), 0)
        self.assertEqual(json.loads(stdout.getvalue())["arm_count"], 13)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(cli(args), 2)
        self.assertNotIn(str(self.out), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
