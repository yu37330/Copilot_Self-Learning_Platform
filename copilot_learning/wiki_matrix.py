"""既存13条件にWikiなし/Memory型/原本型を直交追加する。計画のみ・通信なし。"""
from __future__ import annotations

import copy
from typing import Any
from .core import InvalidData, SHA_PATTERN, digest, json_bytes

ARMS = {"D", "A0", "A1", "A2", "A3", "A4", "A5", "S", "Q", "U", "B", "C0", "C1"}
WIKI_MODES = ("W0", "WM", "WO")


def build_wiki_matrix(base: dict[str, Any]) -> dict[str, Any]:
    """PR #20のexperiment.jsonから39条件を作る。D+Wikiは知識なしではない。"""
    def fail():
        raise InvalidData("Wiki比較の基礎となる未実行の実験計画が不正です")
    if (not isinstance(base, dict) or base.get("record_type") != "offline-experiment-suite-plan"
            or base.get("network_calls") != 0 or base.get("external_transfer_authorized") is not False
            or base.get("real_quality_verified") is not False
            or not isinstance(base.get("arms"), dict) or set(base["arms"]) != ARMS):
        fail()
    config = {k: base.get(k) for k in ("version", "project", "common", "arms")}
    if digest(json_bytes(config)) != base.get("suite_id"):
        fail()
    common = base["common"]
    if not isinstance(common, dict) or common.get("evaluation_learning_enabled") is not False:
        fail()
    for key in ("training_snapshot_sha256", "dataset_sha256"):
        if not isinstance(common.get(key), str) or not SHA_PATTERN.fullmatch(common[key]):
            fail()
    if type(common.get("repetitions")) is not int or not 1 <= common["repetitions"] <= 20:
        fail()
    for key in ("retrieved_context_budget_tokens", "retrieval_top_k_total"):
        if type(common.get(key)) is not int or common[key] < 1:
            fail()
    for arm in base["arms"].values():
        if (not isinstance(arm, dict) or arm.get("status") != "not_executed"
                or arm.get("metrics") is not None or arm.get("cost_usd") is not None):
            fail()
    original_jobs = base.get("jobs")
    if not isinstance(original_jobs, list) or not 1 <= len(original_jobs) <= 260000:
        fail()
    seen, cases, per_arm = set(), {}, {}
    for job in original_jobs:
        if not isinstance(job, dict):
            fail()
        arm, repeat, cid = (job.get(k) for k in ("arm", "repeat", "case_id"))
        if (arm not in ARMS or type(repeat) is not int or not 0 <= repeat < common["repetitions"]
                or not isinstance(cid, str) or not cid or len(cid) > 80
                or job.get("status") != "not_executed" or job.get("result") is not None):
            fail()
        if (arm, repeat, cid) in seen:
            fail()
        seen.add((arm, repeat, cid))
        expected_id = digest(json_bytes([base["suite_id"], arm, repeat, cid]))[:32]
        if job.get("job_id") != expected_id:
            fail()
        task_sha = job.get("task_sha256")
        if not isinstance(task_sha, str) or not SHA_PATTERN.fullmatch(task_sha):
            fail()
        if cid in cases and cases[cid] != task_sha:
            fail()
        cases[cid] = task_sha
        per_arm.setdefault((arm, repeat), set()).add(cid)
    if any(per_arm.get((arm, r)) != set(cases) for arm in ARMS for r in range(common["repetitions"])):
        fail()
    profile = {
        "W0": {"source": "no_wiki", "editor": None},
        "WM": {"source": "separate_frozen_agentcore_export", "strategies": ["semantic", "episodic"],
               "editor": "common_wiki_compiler", "raw_evidence_verified": False},
        "WO": {"source": "same_approved_training_documents", "editor": "common_wiki_compiler",
               "agentcore_memory_used": False},
    }
    suite_id = digest(json_bytes({"base": base["suite_id"], "wiki_profiles": profile}))
    jobs = []
    for job in original_jobs:
        for mode in WIKI_MODES:
            # Wikiは下流方式ごとに再抽出しない。同じ反復内では確定版を共用する設計。
            key = None if mode == "W0" else digest(json_bytes([suite_id, mode, job["repeat"]]))
            jobs.append({"job_id": digest(json_bytes([suite_id, job["job_id"], mode]))[:32],
                         "base_job_id": job["job_id"], "learning_arm": job["arm"],
                         "wiki_mode": mode, "repeat": job["repeat"], "case_id": job["case_id"],
                         "task_sha256": job["task_sha256"], "wiki_snapshot_key": key,
                         "true_no_knowledge_control": job["arm"] == "D" and mode == "W0",
                         "status": "not_executed", "result": None})
    return {"schema_version": "1.0", "record_type": "offline-wiki-comparison-plan",
            "suite_id": suite_id, "base_suite_id": base["suite_id"], "wiki_profiles": profile,
            "condition_count": len(ARMS) * len(WIKI_MODES), "jobs": jobs,
            "common": copy.deepcopy(common),
            "budget_rule": "direct_memory_plus_wiki_share_existing_total_context_and_top_k",
            "wiki_refresh_during_evaluation": False,
            "construction_cost_rule": "report_wiki_construction_and_updates_separately_from_queries",
            "observations": {arm + "+" + mode: None for arm in sorted(ARMS) for mode in WIKI_MODES},
            "live_execution_ready": False, "external_transfer_authorized": False,
            "network_calls": 0, "real_quality_verified": False}
