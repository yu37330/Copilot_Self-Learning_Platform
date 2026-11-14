"""比較構成を最初から一括準備する。計画のみでAWS・モデルは実行しない。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .core import InvalidData, SHA_PATTERN, assert_external_output, digest, json_bytes
from .evals import evaluate_holdout, task_fingerprint
from .paired_experiment import _config

ROOT = Path(__file__).resolve().parents[1]
VERSION = "full-matrix-plan-1"
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
FAMILIES = ("troubleshooting", "project_facts", "session_resumption", "preferences", "negative_scope")
# 設定を全部同時に有効化するのではなく、各設定を別の比較条件として用意する。
ARM_SPECS = (
    ("D", (), "none", None),
    ("A0", ("episodic",), "aws_default", None),
    ("A1", ("episodic",), "coding_override_draft_v1", None),
    ("A2", ("episodic", "semantic"), "aws_default", None),
    ("A3", ("episodic", "summary"), "aws_default", None),
    ("A4", ("episodic", "user_preference"), "aws_default", None),
    ("A5", ("episodic", "semantic", "summary", "user_preference"), "aws_default", None),
    ("S", ("semantic",), "aws_default", None),
    ("Q", ("summary",), "aws_default", None),
    ("U", ("user_preference",), "aws_default", None),
    ("B", (), "agentrun_inspired", None),
    ("C0", (), "independent_grade", "A0"),
    ("C1", (), "independent_grade", "A1"),
)
NAMESPACES = {
    "episodes": "/strategy/{memoryStrategyId}/actor/{actorId}/session/{sessionId}/",
    "reflections": "/strategy/{memoryStrategyId}/actor/{actorId}/",
    "semantic": "/facts/{memoryStrategyId}/actor/{actorId}/",
    "summary": "/summary/{memoryStrategyId}/actor/{actorId}/session/{sessionId}/",
    "user_preference": "/preferences/{memoryStrategyId}/actor/{actorId}/",
}
OVERRIDE_GUIDANCE = [
    "観測結果・ユーザー確認済み事実・推測を分け、成功の自己申告をテスト成功の証拠にしない。",
    "環境・版・適用条件・適用除外・反証を残し、元データにない根拠IDを生成しない。",
    "標準指示を確認して編集し、出力スキーマや統合操作名を変更しない。",
]


def _integer(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def build_experiment_suite(dataset: dict[str, Any], *, project: str,
                           training_snapshot_sha256: str, model_id: str,
                           budget_tokens: int = 8000, context_budget_tokens: int = 2000,
                           top_k_total: int = 6, repetitions: int = 1) -> dict[str, Any]:
    """共通入力を固定した13条件の準備計画。正解ラベルやタスク本文は計画へ含めない。"""
    if (not isinstance(project, str) or not TOKEN.fullmatch(project)
            or not isinstance(training_snapshot_sha256, str)
            or not SHA_PATTERN.fullmatch(training_snapshot_sha256)):
        raise InvalidData("実験識別子または訓練スナップショットSHAが不正です")
    if not isinstance(dataset, dict) or dataset.get("label_source") not in {"synthetic", "human_verified"}:
        raise InvalidData("評価データと正解ラベルの出所が必要です")
    profile = _config({"model_id": model_id, "budget_tokens": budget_tokens,
                       "tool_policy": "offline_review_only"})
    if (not _integer(context_budget_tokens, 1, budget_tokens)
            or not _integer(top_k_total, 1, 100) or not _integer(repetitions, 1, 20)):
        raise InvalidData("共通文脈予算・検索上限・反復数が不正です")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not (1 <= len(cases) <= 1000):
        raise InvalidData("評価ケース数は1〜1000件が必要です")
    check = evaluate_holdout(dataset, {})
    if not check["leakage_checks"]["complete"]:
        raise InvalidData("学習と評価の本文ハッシュ・グループ確認が必要です")
    for case in cases:
        if not TOKEN.fullmatch(case["case_id"]) or case.get("family") not in FAMILIES:
            raise InvalidData("評価ケースIDまたは評価領域が不正です")
    covered = {case["family"] for case in cases}
    common = {
        "downstream_profile": profile,
        "retrieved_context_budget_tokens": context_budget_tokens,
        "retrieval_top_k_total": top_k_total,
        "evaluation_learning_enabled": False,
        "training_snapshot_sha256": training_snapshot_sha256,
        "dataset_sha256": digest(json_bytes(dataset)),
        "repetitions": repetitions,
    }
    arms = {}
    for ident, strategies, prompt, source in ARM_SPECS:
        kind = ("no_knowledge" if ident == "D" else "hybrid" if source
                else "independent" if ident == "B" else "managed_memory")
        arms[ident] = {
            "kind": kind, "strategies": list(strategies), "prompt_profile": prompt,
            "source_arm": source,
            "requires_identical_source_export": source is not None,
            "resource_creation": "none" if not strategies else "separate_per_project_arm_repeat",
            "namespace_templates": NAMESPACES if strategies else {},
            "retention_proposal": {"short_term_days": 30, "long_term_days": 90} if strategies else None,
            "override_guidance": OVERRIDE_GUIDANCE if ident == "A1" else [],
            "aws_request_status": "design_not_api_request" if strategies else "not_applicable",
            "status": "not_executed", "knowledge_revision_sha256": None,
            "metrics": None, "cost_usd": None,
        }
    config = {"version": VERSION, "project": project, "common": common, "arms": arms}
    suite_id = digest(json_bytes(config))
    jobs = []
    # 反復ごとにMemory/知識版を分離する。評価中の追学習は許可しない。
    for repeat in range(repetitions):
        for ident, _, _, source in ARM_SPECS:
            isolation = digest(json_bytes([suite_id, source or ident, repeat]))[:24]
            for case in sorted(cases, key=lambda row: row["case_id"]):
                jobs.append({
                    "job_id": digest(json_bytes([suite_id, ident, repeat, case["case_id"]]))[:32],
                    "arm": ident, "repeat": repeat, "case_id": case["case_id"],
                    "task_sha256": task_fingerprint(case["task"]), "family": case["family"],
                    "knowledge_isolation_key": None if ident == "D" else isolation,
                    "snapshot_from_arm": source, "snapshot_reuse_required": source is not None,
                    "status": "not_executed", "result": None,
                })
    return {
        "schema_version": "1.0", "record_type": "offline-experiment-suite-plan",
        "suite_id": suite_id, **config, "jobs": jobs,
        "comparisons": [
            {"left": "A0", "right": target, "question": question}
            for target, question in (("A1", "prompt_and_model_override"), ("A2", "semantic_addition"),
                                     ("A3", "summary_addition"), ("A4", "preference_addition"),
                                     ("A5", "all_strategies"), ("B", "independent_engine"),
                                     ("C0", "grade_only_same_extraction"), ("D", "memory_effect"))
        ] + [{"left": "A1", "right": "C1", "question": "grade_only_same_extraction"}],
        "family_coverage": {family: family in covered for family in FAMILIES},
        "readiness": {"all_profiles_prepared": True, "live_execution_ready": False,
                      "training_snapshot_digest_verified": False,
                      "missing_case_families": sorted(set(FAMILIES) - covered),
                      "remaining": ["aws_adapters_and_api_validation", "override_prompt_completion",
                                    "extraction_completion_and_snapshot_freeze", "case_executor_and_trace_capture",
                                    "source_snapshot_identity_enforcement", "organization_security_approval"]},
        "cost_contract": {"include": ["extraction", "consolidation", "reflection", "grade", "retrieval",
                                        "downstream_inference", "tool_calls"],
                          "shared_extraction": "report_standalone_total_and_incremental_grade_separately",
                          "native_hidden_model": "not_assumed_equal_to_override_model"},
        "external_transfer_authorized": False, "network_calls": 0,
        "real_quality_verified": False,
    }


def export_experiment_suite(plan: dict[str, Any], destination: Path) -> dict[str, Any]:
    """新規フォルダへ計画を出すだけ。既存出力と自動読込先には書き込まない。"""
    if (not isinstance(plan, dict) or plan.get("record_type") != "offline-experiment-suite-plan"
            or plan.get("external_transfer_authorized") is not False or plan.get("network_calls") != 0):
        raise InvalidData("オフラインの実験準備計画が必要です")
    config = {key: plan[key] for key in ("version", "project", "common", "arms")}
    if (digest(json_bytes(config)) != plan.get("suite_id")
            or set(plan["arms"]) != {item[0] for item in ARM_SPECS}
            or plan.get("readiness", {}).get("live_execution_ready") is not False
            or plan.get("real_quality_verified") is not False
            or any(arm.get("status") != "not_executed" or arm.get("metrics") is not None
                   or arm.get("cost_usd") is not None for arm in plan["arms"].values())
            or any(job.get("status") != "not_executed" or job.get("result") is not None
                   for job in plan.get("jobs", []))):
        raise InvalidData("準備計画の設定・状態・結果が変更されています")
    out = Path(os.path.abspath(Path(destination).expanduser()))
    for part in (out, *out.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise InvalidData("リンクまたはジャンクション経由の出力はできません")
    assert_external_output(out, ROOT)
    if out.exists() or any(p.rstrip(" .").casefold() in {".git", ".github", ".copilot", ".agents", ".claude"} for p in out.parts):
        raise InvalidData("既存フォルダ・自動読込先への出力は禁止です")
    payloads = {
        "experiment.json": json_bytes(plan),
        "memory_profiles.design.json": json_bytes({"record_type": "design_not_api_request", "arms": plan["arms"]}),
        "jobs.json": json_bytes(plan["jobs"]),
        "observations.blank.json": json_bytes({"suite_id": plan["suite_id"], "arms": {k: None for k in plan["arms"]}}),
    }
    manifest = {"schema_version": "1.0", "record_type": "offline-experiment-preparation",
                "suite_id": plan["suite_id"],
                "files": {k: {"sha256": digest(v), "size_bytes": len(v)} for k, v in payloads.items()},
                "external_transfer_authorized": False, "network_calls": 0}
    payloads["manifest.json"] = json_bytes(manifest)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(mode=0o700)
    created = []
    try:
        for name, body in payloads.items():
            target = out / name
            with target.open("xb") as handle:
                created.append(target)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o600)
    except BaseException:
        for target in reversed(created):
            target.unlink()
        out.rmdir()
        raise
    return {"status": "all_profiles_prepared_not_executed", "arm_count": len(plan["arms"]),
            "job_count": len(plan["jobs"]), "manifest_sha256": digest(json_bytes(manifest)),
            "live_execution_ready": False, "network_calls": 0}
