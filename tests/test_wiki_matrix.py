"""Wiki直交比較の準備だけを検証。実モデルの結果を作らない。"""
import copy
import unittest
from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.wiki_matrix import ARMS, build_wiki_matrix


def base_plan():
    common = {"evaluation_learning_enabled": False, "training_snapshot_sha256": "a"*64,
              "dataset_sha256": "b"*64, "repetitions": 2,
              "retrieved_context_budget_tokens": 2000, "retrieval_top_k_total": 6,
              "downstream_profile": {"model_id": "synthetic", "budget_tokens": 8000,
                                     "tool_policy": "offline_review_only"}}
    config = {"version": "full-matrix-plan-1", "project": "sample", "common": common,
              "arms": {arm: {"status": "not_executed", "metrics": None, "cost_usd": None}
                       for arm in sorted(ARMS)}}
    sid = digest(json_bytes(config))
    jobs = [{"job_id": digest(json_bytes([sid, arm, repeat, case]))[:32], "arm": arm,
             "repeat": repeat, "case_id": case, "task_sha256": digest(case.encode()),
             "status": "not_executed", "result": None}
            for repeat in range(2) for arm in sorted(ARMS) for case in ("positive", "negative")]
    return {**config, "record_type": "offline-experiment-suite-plan", "suite_id": sid,
            "jobs": jobs, "network_calls": 0, "external_transfer_authorized": False,
            "real_quality_verified": False}


class WikiMatrixTests(unittest.TestCase):
    def test_all_thirty_nine_conditions_prepared(self):
        base = base_plan()
        before = copy.deepcopy(base)
        plan = build_wiki_matrix(base)
        self.assertEqual(plan["condition_count"], 39)
        self.assertEqual(len(plan["jobs"]), 156)
        self.assertEqual(len({j["job_id"] for j in plan["jobs"]}), 156)
        self.assertEqual(base, before)
        self.assertTrue(all(value is None for value in plan["observations"].values()))
        self.assertFalse(plan["live_execution_ready"])

    def test_only_d_without_wiki_is_no_knowledge_control(self):
        plan = build_wiki_matrix(base_plan())
        for job in plan["jobs"]:
            self.assertEqual(job["true_no_knowledge_control"],
                             job["learning_arm"] == "D" and job["wiki_mode"] == "W0")

    def test_snapshot_shared_within_repeat_but_not_across_repeats(self):
        jobs = build_wiki_matrix(base_plan())["jobs"]
        for mode in ("WM", "WO"):
            keys = [{j["wiki_snapshot_key"] for j in jobs if j["repeat"] == r and j["wiki_mode"] == mode}
                    for r in (0, 1)]
            self.assertEqual(len(keys[0]), 1)
            self.assertTrue(keys[0].isdisjoint(keys[1]))

    def test_total_budget_and_learning_freeze_unchanged(self):
        plan = build_wiki_matrix(base_plan())
        self.assertEqual(plan["common"]["retrieved_context_budget_tokens"], 2000)
        self.assertEqual(plan["common"]["retrieval_top_k_total"], 6)
        self.assertFalse(plan["wiki_refresh_during_evaluation"])
        self.assertFalse(plan["common"]["evaluation_learning_enabled"])

    def test_configuration_digest_tampering(self):
        plan = base_plan()
        plan["common"]["retrieval_top_k_total"] = 10
        with self.assertRaises(InvalidData): build_wiki_matrix(plan)

    def test_missing_arm_rejected(self):
        plan = base_plan()
        del plan["arms"]["A5"]
        with self.assertRaises(InvalidData): build_wiki_matrix(plan)

    def test_observed_result_not_used_as_plan(self):
        plan = base_plan()
        plan["jobs"][0]["result"] = {"accuracy": 1}
        with self.assertRaises(InvalidData): build_wiki_matrix(plan)

    def test_missing_case_and_duplicate_job_rejected(self):
        plan = base_plan()
        del plan["jobs"][0]
        with self.assertRaises(InvalidData): build_wiki_matrix(plan)
        plan = base_plan()
        plan["jobs"].append(copy.deepcopy(plan["jobs"][0]))
        with self.assertRaises(InvalidData): build_wiki_matrix(plan)

    def test_task_and_id_tampering(self):
        for key, value in (("task_sha256", "c"*64), ("job_id", "forged"), ("repeat", True)):
            plan = base_plan()
            plan["jobs"][0][key] = value
            with self.subTest(key=key), self.assertRaises(InvalidData): build_wiki_matrix(plan)

    def test_no_aws_permission_or_fake_success(self):
        plan = base_plan()
        plan["external_transfer_authorized"] = True
        with self.assertRaises(InvalidData): build_wiki_matrix(plan)


if __name__ == "__main__":
    unittest.main()
