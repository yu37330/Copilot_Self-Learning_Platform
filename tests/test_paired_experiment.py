"""A/B/C/Dの入力固定と欠落ケース、モックなしでも再現可能な比較テスト。"""
import copy
import unittest

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.paired_experiment import evaluate_arms


class PairedExperimentTests(unittest.TestCase):
    def setUp(self):
        self.dataset={"label_source":"synthetic", "training_group_ids":["train-1"],"training_task_sha256":[],
                      "cases":[{"case_id":"case1", "group_id":"g1", "task":"Python 仮想環境を確認", "expected":"applicable"},
                               {"case_id":"case2", "group_id":"g2", "task":"Python APIの仕様が変化", "expected":"not_applicable"},
                               {"case_id":"case3", "group_id":"g3", "task":"Python パッケージ衝突", "expected":"applicable"}]}
        self.profile={"model_id":"test-model", "budget_tokens":1024, "tool_policy":"read-only"}
        self.submission={"schema_version":"1.0","record_type":"offline-paired-observations",
                         "dataset_sha256":digest(json_bytes(self.dataset)),"arms":{}}
        variants={"A":["applicable","not_applicable","applicable"],
                  "B":["applicable","applicable","applicable"],
                  "C":["applicable","not_applicable","needs_review"],
                  "D":["needs_review","not_applicable","needs_review"]}
        for method, pred in variants.items():
            self.submission["arms"][method]={
                "source_kind":"synthetic", "profile":dict(self.profile),
                "knowledge_revision_sha256":None if method=="D" else method.lower()*64,
                "cases":{case["case_id"]:{"prediction":pred[i], "trace_sha256":str(i+1)*64,
                                            "run_id":f"{method}-{i}","cost_usd":.05}
                         for i,case in enumerate(self.dataset["cases"])}
            }

    def check(self, dataset=None, submission=None):
        return evaluate_arms(dataset if dataset is not None else self.dataset,
                             submission if submission is not None else self.submission)

    def test_paired_metrics_and_no_real_quality_claim(self):
        r=self.check()
        self.assertTrue(r["all_four_observed"])
        self.assertEqual(r["arms"]["A"]["metrics"]["accuracy"],1.0)
        self.assertEqual(r["arms"]["B"]["metrics"]["fp"],1)
        self.assertEqual(r["arms"]["C"]["metrics"]["abstain"],1)
        self.assertEqual(r["paired_differences"]["A_vs_B"]["left_only_correct"],1)
        self.assertAlmostEqual(r["paired_differences"]["A_vs_B"]["delta_accuracy"],1/3,places=4)
        self.assertFalse(r["real_llm_quality_verified"])
        self.assertFalse(r["real_copilot_improvement_verified"])
        self.assertEqual(r["network_calls"],0)
        self.assertEqual(r["arms"]["A"]["cost_per_case_usd"],.05)
        self.assertNotIn("仮想環境",str(r))

    def test_unexecuted_distinct_from_zero(self):
        self.submission["arms"]["C"]=None
        r=self.check()
        self.assertFalse(r["all_four_observed"])
        self.assertIsNone(r["arms"]["C"]["metrics"]["accuracy"])
        self.assertIsNone(r["paired_differences"]["A_vs_C"])

    def test_training_group_leakage_fails(self):
        self.dataset["training_group_ids"]=["g1"]
        self.submission["dataset_sha256"]=digest(json_bytes(self.dataset))
        with self.assertRaises(InvalidData): self.check()

    def test_dataset_change_fails(self):
        self.dataset["cases"][0]["expected"]="not_applicable"
        with self.assertRaisesRegex(InvalidData,"ダイジェスト"):
            self.check()

    def test_missing_case_fails(self):
        del self.submission["arms"]["A"]["cases"]["case3"]
        with self.assertRaisesRegex(InvalidData,"揃っていません"):
            self.check()

    def test_mismatched_model_fails(self):
        self.submission["arms"]["B"]["profile"]["model_id"]="stronger-model"
        with self.assertRaisesRegex(InvalidData,"一致しません"):
            self.check()

    def test_mismatched_budget_fails(self):
        self.submission["arms"]["A"]["profile"]["budget_tokens"]=100
        with self.assertRaisesRegex(InvalidData,"一致しません"):
            self.check()

    def test_duplicate_run_id_fails(self):
        self.submission["arms"]["A"]["cases"]["case1"]["run_id"]="B-0"
        with self.assertRaisesRegex(InvalidData,"重複"):
            self.check()

    def test_nan_cost_and_bad_prediction_fail(self):
        self.submission["arms"]["B"]["cases"]["case2"]["cost_usd"]=float("nan")
        with self.assertRaises(InvalidData): self.check()
        self.submission["arms"]["B"]["cases"]["case2"]["cost_usd"]=.1
        self.submission["arms"]["B"]["cases"]["case2"]["prediction"]="success"
        with self.assertRaises(InvalidData): self.check()

    def test_missing_arm_section_fails_closed(self):
        del self.submission["arms"]
        with self.assertRaises(InvalidData): self.check()

    def test_source_kind_wrong_type_fails_closed(self):
        self.submission["arms"]["A"]["source_kind"]=["synthetic"]
        with self.assertRaises(InvalidData): self.check()

    def test_missing_case_id_fails_closed(self):
        del self.dataset["cases"][0]["case_id"]
        self.submission["dataset_sha256"]=digest(json_bytes(self.dataset))
        with self.assertRaises(InvalidData): self.check()

    def test_control_arm_must_have_no_knowledge(self):
        self.submission["arms"]["D"]["knowledge_revision_sha256"]="a"*64
        with self.assertRaises(InvalidData): self.check()

    def test_same_sample_required(self):
        self.submission["arms"]["A"]["cases"]["new_case"]={
            "prediction":"applicable", "run_id":"new", "trace_sha256":"b"*64,"cost_usd":0}
        with self.assertRaises(InvalidData):self.check()

    def test_reproducibility(self):
        self.assertEqual(json_bytes(self.check()),json_bytes(self.check()))
        self.assertEqual(self.check()["observations_sha256"],digest(json_bytes(self.submission)))


if __name__=="__main__":unittest.main()
