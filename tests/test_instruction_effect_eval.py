"""Issue #31 Phase C: 未学習ペア比較の欠測・漏えい・出典検査を合成テストで確認。"""
from __future__ import annotations

import contextlib
import io
import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.evals import task_fingerprint
from copilot_learning.instruction_effect_eval import (
    blank_observations, evaluate_instruction_effect, validate_dataset,
)
from scripts.evaluate_instruction_effect_offline import main as cli


def sample_dataset() -> dict:
    tasks = ["Synthetic Python lint task alpha", "Synthetic review case beta",
             "Synthetic agent workflow gamma"]
    return {
        "schema_version": "1.0",
        "record_type": "offline-instruction-effect-holdout",
        "label_source": "synthetic",
        "proposal_review_sha256": "1" * 64,
        "evaluation_protocol_sha256": "2" * 64,
        "instruction_snapshots": {"baseline": "3" * 64, "candidate": "4" * 64},
        "training_group_ids": ["train-series"],
        "training_task_sha256": [task_fingerprint("Separate training task")],
        "cases": [
            {"case_id": f"eval-{i}", "group_id": f"holdout-{i}", "task": task,
             "task_sha256": task_fingerprint(task),
             "family": ["troubleshooting", "session_resumption", "negative_scope"][i]}
            for i, task in enumerate(tasks)
        ],
    }


def sample_submission(dataset: dict) -> dict:
    profile = {"model_id": "synthetic.v1", "budget_tokens": 8000,
               "tool_policy": "read_only", "sampling_seed": 17}
    pairs = {
        "baseline": ["success", "failure", "success"],
        "candidate": ["success", "success", "failure"],
    }
    arms = {}
    for arm, statuses in pairs.items():
        cases = {}
        for i, status in enumerate(statuses):
            case_id = f"eval-{i}"
            cases[case_id] = {
                "run_id": f"{arm}-{i}",
                "trace_sha256": digest(json_bytes(["trace", arm, case_id])),
                "outcome": status,
                "reviewer_sha256": "6" * 64,
                "review_receipt_sha256": digest(json_bytes(["review", arm, case_id])),
                "tool_calls": (10 - i * 2) if arm == "baseline" else (7 - i * 2),
                "retry_count": 1 if arm == "baseline" else 0,
                "input_tokens": 200 + i,
                "output_tokens": 100 + i,
                "latency_ms": 2000 + i * 100,
                "cost_usd": 0.11 + 0.01 * i if arm == "baseline" else 0.07 + 0.01 * i,
            }
        arms[arm] = {
            "source_kind": "synthetic",
            "instruction_sha256": dataset["instruction_snapshots"][arm],
            # 比較条件を独立した辞書にし、片方のテスト変更が他方へ波及しないようにする。
            "profile": deepcopy(profile),
            "executor_sha256": "5" * 64,
            "cases": cases,
        }
    return {
        "schema_version": "1.0",
        "record_type": "offline-instruction-effect-observations",
        "dataset_sha256": digest(json_bytes(dataset)),
        "arms": arms,
    }


class InstructionEffectTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = sample_dataset()
        self.obs = sample_submission(self.dataset)

    def evaluate(self, obs=None, dataset=None):
        return evaluate_instruction_effect(
            dataset if dataset is not None else self.dataset,
            obs if obs is not None else self.obs)

    def test_valid_dataset_has_disjoint_groups_and_hashes(self):
        res = validate_dataset(self.dataset)
        self.assertEqual(res["case_count"], 3)
        self.assertTrue(res["leakage_checks"]["task_hash_disjoint"])
        self.assertFalse(res["leakage_checks"]["semantic_paraphrase_leakage_excluded"])
        self.assertEqual(res["family_coverage"]["negative_scope"], 1)

    def test_complete_paired_metrics_and_mcnemar(self):
        res = self.evaluate()
        self.assertEqual(res["comparison_status"], "submitted_not_independently_verified")
        self.assertEqual(res["paired_comparison"]["both_success"], 1)
        self.assertEqual(res["paired_comparison"]["both_failure"], 0)
        self.assertEqual(res["paired_comparison"]["baseline_only_success"], 1)
        self.assertEqual(res["paired_comparison"]["candidate_only_success"], 1)
        self.assertEqual(res["paired_comparison"]["conclusive_paired_n"], 3)
        self.assertEqual(res["paired_comparison"]["delta_success_rate"], 0)
        self.assertEqual(res["paired_comparison"]["mcnemar_exact_two_sided_p"], 1)
        self.assertEqual(res["arms"]["baseline"]["successes"], 2)
        self.assertEqual(res["arms"]["candidate"]["successes"], 2)
        self.assertEqual(res["paired_comparison"]["delta_metrics_candidate_minus_baseline"]["tool_calls"]["mean_delta"], -3)
        self.assertTrue(res["paired_comparison"]["small_sample_warning"])
        self.assertFalse(res["real_copilot_execution_verified"])
        self.assertFalse(res["instruction_snapshot_installation_verified"])
        self.assertFalse(res["causal_improvement_established"])
        self.assertEqual(res["network_calls"], 0)
        self.assertNotIn("Synthetic", json.dumps(res))

    def test_not_executed_is_null_not_failed(self):
        empty = blank_observations(self.dataset)
        res = self.evaluate(empty)
        self.assertEqual(res["comparison_status"], "not_executed")
        self.assertIsNone(res["paired_comparison"])
        self.assertIsNone(res["arms"]["baseline"]["n"])
        self.assertIsNone(res["arms"]["candidate"]["success_rate"])

    def test_one_arm_missing_is_null(self):
        value = deepcopy(self.obs)
        value["arms"]["candidate"] = None
        res = self.evaluate(value)
        self.assertEqual(res["comparison_status"], "one_arm_missing")
        self.assertIsNone(res["paired_comparison"])
        self.assertIsNone(res["arms"]["candidate"]["n"])
        self.assertEqual(res["arms"]["baseline"]["n"], 3)

    def test_inconclusive_is_not_silently_scored_as_failure(self):
        value = deepcopy(self.obs)
        row = value["arms"]["candidate"]["cases"]["eval-1"]
        row["outcome"] = "inconclusive"
        row["reviewer_sha256"] = row["review_receipt_sha256"] = None
        res = self.evaluate(value)
        self.assertEqual(res["paired_comparison"]["inconclusive_pairs"], 1)
        self.assertEqual(res["paired_comparison"]["conclusive_paired_n"], 2)
        self.assertEqual(res["arms"]["candidate"]["inconclusive"], 1)
        self.assertEqual(res["arms"]["candidate"]["success_rate"], 0.5)
        self.assertFalse(res["paired_comparison"]["complete_outcome_coverage"])

    def test_metric_null_stays_null_with_explicit_coverage(self):
        value = deepcopy(self.obs)
        value["arms"]["candidate"]["cases"]["eval-2"]["cost_usd"] = None
        res = self.evaluate(value)
        metric = res["arms"]["candidate"]["metrics"]["cost_usd"]
        self.assertEqual(metric["n_measured"], 2)
        self.assertIsNone(metric["total"])
        self.assertIsNone(metric["mean"])
        pair = res["paired_comparison"]["delta_metrics_candidate_minus_baseline"]["cost_usd"]
        self.assertEqual(pair["n_paired_observed"], 2)
        self.assertIsNotNone(pair["mean_delta"])

    def test_instruction_and_proposal_hashes_are_in_report(self):
        res = self.evaluate()
        self.assertEqual(res["instruction_snapshots"], self.dataset["instruction_snapshots"])
        self.assertEqual(res["proposal_review_sha256"], "1" * 64)
        self.assertEqual(res["evaluation_protocol_sha256"], "2" * 64)

    def test_dataset_train_group_and_duplicate_case_blocked(self):
        for mode in ("group", "task", "case_id", "hash"):
            altered = deepcopy(self.dataset)
            if mode == "group":
                altered["cases"][0]["group_id"] = altered["training_group_ids"][0]
            elif mode == "task":
                altered["cases"][0]["task"] = "Separate training task"
                altered["cases"][0]["task_sha256"] = task_fingerprint("Separate training task")
            elif mode == "case_id":
                altered["cases"][1]["case_id"] = "eval-0"
            else:
                altered["cases"][1]["task_sha256"] = "a" * 64
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                validate_dataset(altered)

    def test_dataset_hash_and_label_guards(self):
        for mode in ("same_revision", "bad_label", "case_count", "blank_groups", "unknown_family"):
            altered = deepcopy(self.dataset)
            if mode == "same_revision":
                altered["instruction_snapshots"]["candidate"] = altered["instruction_snapshots"]["baseline"]
            elif mode == "bad_label":
                altered["label_source"] = "real_verified"
            elif mode == "case_count":
                altered["cases"] = []
            elif mode == "blank_groups":
                altered["training_group_ids"] = []
            else:
                altered["cases"][0]["family"] = "other"
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                validate_dataset(altered)

    def test_sensitive_task_is_rejected_without_echo(self):
        for text in ("test email x@example.org", "Synthetic phone 090-1234-5678",
                     "Synthetic private organization label 取り扱い注意"):
            altered = deepcopy(self.dataset)
            altered["cases"][0]["task"] = text
            altered["cases"][0]["task_sha256"] = task_fingerprint(text)
            literals = ["取り扱い注意"] if "取り扱い注意" in text else []
            with self.subTest(text=text), self.assertRaises(InvalidData) as caught:
                validate_dataset(altered, sensitive_literals=literals)
            self.assertNotIn(text, str(caught.exception))

    def test_submission_wrong_schema_or_sha_blocked(self):
        for mode in ("wrong_dataset", "extra", "missing_arm"):
            altered = deepcopy(self.obs)
            if mode == "wrong_dataset":
                altered["dataset_sha256"] = "0" * 64
            elif mode == "extra":
                altered["unexpected"] = "yes"
            else:
                del altered["arms"]["candidate"]
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                self.evaluate(altered)

    def test_different_profiles_blocked(self):
        for name, value in (("model_id", "different_model"), ("budget_tokens", 500),
                            ("tool_policy", "full_access"), ("sampling_seed", 42)):
            altered = deepcopy(self.obs)
            altered["arms"]["candidate"]["profile"][name] = value
            with self.subTest(name=name), self.assertRaises(InvalidData):
                self.evaluate(altered)

    def test_wrong_instruction_revision_and_kind(self):
        for mode in ("sha", "kind"):
            altered = deepcopy(self.obs)
            if mode == "sha":
                altered["arms"]["candidate"]["instruction_sha256"] = "0" * 64
            else:
                altered["arms"]["candidate"]["source_kind"] = "imported_replay"
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                self.evaluate(altered)

    def test_run_and_trace_reused_between_arms_blocked(self):
        for key in ("run_id", "trace_sha256"):
            altered = deepcopy(self.obs)
            altered["arms"]["candidate"]["cases"]["eval-0"][key] = altered["arms"]["baseline"]["cases"]["eval-0"][key]
            with self.subTest(key=key), self.assertRaises(InvalidData):
                self.evaluate(altered)

    def test_missing_case_and_extra_case_are_rejected(self):
        for mode in ("missing", "extra"):
            altered = deepcopy(self.obs)
            if mode == "missing":
                del altered["arms"]["baseline"]["cases"]["eval-0"]
            else:
                altered["arms"]["baseline"]["cases"]["extra"] = deepcopy(
                    altered["arms"]["baseline"]["cases"]["eval-0"])
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                self.evaluate(altered)

    def test_review_from_same_executor_or_missing_evidence_rejected(self):
        for mode in ("same_reviewer", "missing_review", "missing_reviewer"):
            altered = deepcopy(self.obs)
            item = altered["arms"]["candidate"]["cases"]["eval-0"]
            if mode == "same_reviewer":
                item["reviewer_sha256"] = altered["arms"]["candidate"]["executor_sha256"]
            elif mode == "missing_review":
                item["review_receipt_sha256"] = None
            else:
                item["reviewer_sha256"] = None
            with self.subTest(mode=mode), self.assertRaises(InvalidData):
                self.evaluate(altered)

    def test_invalid_metric_type_range_or_nan_rejected(self):
        for val in (True, -1, 1.2, 10**12, "3"):
            altered = deepcopy(self.obs)
            altered["arms"]["baseline"]["cases"]["eval-0"]["tool_calls"] = val
            with self.subTest(val=val), self.assertRaises(InvalidData):
                self.evaluate(altered)
        for val in (float("nan"), float("inf"), -0.1, True):
            altered = deepcopy(self.obs)
            altered["arms"]["baseline"]["cases"]["eval-0"]["cost_usd"] = val
            with self.subTest(val=val), self.assertRaises(InvalidData):
                self.evaluate(altered)

    def test_mcnemar_one_sided_improvement_keeps_small_sample_warning(self):
        altered = deepcopy(self.obs)
        for case in altered["arms"]["baseline"]["cases"].values():
            case["outcome"] = "failure"
        for case in altered["arms"]["candidate"]["cases"].values():
            case["outcome"] = "success"
        res = self.evaluate(altered)
        self.assertEqual(res["paired_comparison"]["candidate_only_success"], 3)
        self.assertEqual(res["paired_comparison"]["delta_success_rate"], 1.0)
        self.assertEqual(res["paired_comparison"]["mcnemar_exact_two_sided_p"], 0.25)
        self.assertFalse(res["causal_improvement_established"])

    def test_cli_blank_evaluate_and_no_overwrite(self):
        dataset_path = self.root / "dataset.json"
        dataset_path.write_bytes(json_bytes(self.dataset))
        output_blank = self.root / "blank.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["blank", "--dataset", str(dataset_path),
                                  "--out", str(output_blank)]), 0)
        self.assertIsNone(json.loads(output_blank.read_text("utf-8"))["arms"]["baseline"])
        observation_path = self.root / "obs.json"
        observation_path.write_bytes(json_bytes(self.obs))
        output_report = self.root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["evaluate", "--dataset", str(dataset_path),
                                  "--observations", str(observation_path),
                                  "--out", str(output_report)]), 0)
        self.assertFalse(json.loads(output_report.read_text("utf-8"))["real_copilot_execution_verified"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["evaluate", "--dataset", str(dataset_path),
                                  "--observations", str(observation_path),
                                  "--out", str(output_report)]), 2)

    def test_cli_real_data_policy_required_and_repo_output_blocked(self):
        dataset_path = self.root / "dataset.json"
        altered = deepcopy(self.dataset)
        altered["label_source"] = "human_verified"
        dataset_path.write_bytes(json_bytes(altered))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["blank", "--dataset", str(dataset_path),
                                  "--out", str(self.root / "blank.json")]), 2)
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        public_path = repo / "output.json"
        dataset_path.write_bytes(json_bytes(self.dataset))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli(["blank", "--dataset", str(dataset_path),
                                  "--out", str(public_path)]), 2)
        self.assertFalse(public_path.exists())

    def test_no_network_or_subprocess_or_model(self):
        with patch("socket.socket.connect") as connect, patch("subprocess.run") as run:
            self.evaluate()
            blank_observations(self.dataset)
            connect.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
