"""#10: A/B/C/D同一条件のオフライン観測データを比較する（実性能の証明ではない）。"""
from __future__ import annotations

import re
from typing import Any

from .core import InvalidData, SHA_PATTERN, digest, json_bytes
from .evals import METHODS, OUTCOMES, evaluate_holdout

MAX_CASES = 1000
SOURCES = {"synthetic", "imported_replay"}


def _config(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict)
            or set(value) != {"model_id", "budget_tokens", "tool_policy"}
            or not isinstance(value["model_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value["model_id"])
            or not isinstance(value["budget_tokens"], int) or isinstance(value["budget_tokens"], bool)
            or not (1 <= value["budget_tokens"] <= 10_000_000)
            or not isinstance(value["tool_policy"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value["tool_policy"])):
        raise InvalidData("モデル/予算/ツール権限の共通設定が不正です")
    return value


def evaluate_arms(dataset: dict[str, Any], submission: dict[str, Any]) -> dict[str, Any]:
    """提出済み結果の整合性だけ検証し、テキストは出力しない。"""
    if not isinstance(dataset, dict) or not isinstance(submission, dict):
        raise InvalidData("入力はJSONオブジェクトが必要です")
    cases = dataset.get("cases")
    if (not isinstance(cases, list) or not (1 <= len(cases) <= MAX_CASES)
            or dataset.get("label_source") not in {"synthetic", "human_verified"}
            or "training_task_sha256" not in dataset
            or any(not isinstance(c, dict) or "task" not in c for c in cases)):
        raise InvalidData("独立検証可能なHold-outデータと正解出所が必要です")
    if (submission.get("schema_version") != "1.0"
            or submission.get("record_type") != "offline-paired-observations"
            or submission.get("dataset_sha256") != digest(json_bytes(dataset))
            or set(submission) != {"schema_version", "record_type", "dataset_sha256", "arms"}):
        raise InvalidData("提出結果と評価データのダイジェストが異なります")
    # datasetの型・グループ・タスク重複は、方式の処理より前に検証する。
    checked_input = evaluate_holdout(dataset, {})
    if not checked_input["leakage_checks"]["complete"]:
        raise InvalidData("学習と評価のfingerprint確認が未完了です")
    arms = submission.get("arms")
    if not isinstance(arms, dict) or set(arms) != set(METHODS):
        raise InvalidData("A/B/C/Dの4方式を、未実行ならnullとして明示してください")
    expected_ids = {c["case_id"] for c in cases}
    if len(expected_ids) != len(cases):
        raise InvalidData("重複したcase_idがあります")
    predictions: dict[str, dict[str, str] | None] = {}
    profiles: dict[str, dict[str, Any]] = {}
    source_kinds = {}
    receipts = set()
    summary = {}
    for method in METHODS:
        arm = arms[method]
        if arm is None:
            predictions[method] = None
            summary[method] = {"status": "not_executed", "source_kind": None,
                               "cost_usd": None, "cost_per_case_usd": None}
            continue
        if (not isinstance(arm, dict)
                or set(arm) != {"source_kind", "profile", "knowledge_revision_sha256", "cases"}
                or not isinstance(arm["source_kind"], str)
                or arm["source_kind"] not in SOURCES):
            raise InvalidData(f"{method}の観測形式が不正です")
        profile = _config(arm["profile"])
        profiles[method] = profile
        source_kinds[method] = arm["source_kind"]
        rev = arm["knowledge_revision_sha256"]
        if (method == "D" and rev is not None) or (method != "D" and (not isinstance(rev, str) or not SHA_PATTERN.fullmatch(rev))):
            raise InvalidData(f"{method}の知識リビジョンが不正です（Dはnull）")
        observations = arm["cases"]
        if not isinstance(observations, dict) or set(observations) != expected_ids:
            raise InvalidData(f"{method}: 比較ケースが揃っていません")
        votes, total_cost, cost_complete = {}, 0.0, True
        for case_id, obs in observations.items():
            if not isinstance(obs, dict) or set(obs) != {"prediction", "trace_sha256", "run_id", "cost_usd"}:
                raise InvalidData(f"{method}: 個別観測形式が不正です")
            if obs["prediction"] not in OUTCOMES or not isinstance(obs["trace_sha256"], str) or not SHA_PATTERN.fullmatch(obs["trace_sha256"]):
                raise InvalidData(f"{method}: 予測または実行証跡が不正です")
            run_id = obs["run_id"]
            if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", run_id) or run_id in receipts:
                raise InvalidData("実験のrun_idが不正または他方式と重複しています")
            receipts.add(run_id)
            cost = obs["cost_usd"]
            if cost is None:
                cost_complete = False
            elif isinstance(cost, bool) or not isinstance(cost, (int, float)) or not (0 <= cost < 1_000_000):
                raise InvalidData("費用は非負の有限数値またはnullで指定してください")
            else:
                total_cost += cost
            votes[case_id] = obs["prediction"]
        predictions[method] = votes
        summary[method] = {
            "status": "submitted_offline", "source_kind": arm["source_kind"],
            "knowledge_revision_sha256": rev,
            "profile_sha256": digest(json_bytes(profile)),
            "cost_usd": round(total_cost, 6) if cost_complete else None,
            "cost_per_case_usd": round(total_cost / len(cases), 6) if cost_complete else None,
        }
    if len({digest(json_bytes(v)) for v in profiles.values()}) > 1:
        raise InvalidData("方式間でモデル・トークン予算・ツール権限が一致しません")
    heldout = evaluate_holdout(dataset, predictions)
    if not heldout["leakage_checks"]["complete"]:
        raise InvalidData("学習データとのfingerprint比較が未完了です")
    pairs = {}
    for i, left in enumerate(METHODS):
        for right in METHODS[i+1:]:
            if predictions[left] is None or predictions[right] is None:
                pairs[f"{left}_vs_{right}"] = None
                continue
            expected = {c["case_id"]:c["expected"] for c in cases}
            left_only=right_only=ties_correct=ties_incorrect=0
            for cid, answer in expected.items():
                lc = predictions[left][cid] == answer
                rc = predictions[right][cid] == answer
                if lc and rc: ties_correct += 1
                elif lc: left_only += 1
                elif rc: right_only += 1
                else: ties_incorrect += 1
            pairs[f"{left}_vs_{right}"] = {
                "left_only_correct": left_only, "right_only_correct": right_only,
                "both_correct": ties_correct, "both_incorrect_or_abstained": ties_incorrect,
                "delta_accuracy": round((left_only - right_only)/len(cases), 5),
            }
    return {
        "schema_version": "1.0", "record_type": "offline-paired-comparison",
        "dataset_sha256": digest(json_bytes(dataset)),
        "observations_sha256": digest(json_bytes(submission)),
        "label_source": dataset["label_source"],
        "paired_sample_size": len(cases), "common_profile_sha256": digest(json_bytes(next(iter(profiles.values())))) if profiles else None,
        "arms": {k: {**summary[k], "metrics": heldout["results"][k]} for k in METHODS},
        "paired_differences": pairs,
        "leakage_checks": heldout["leakage_checks"],
        "all_four_observed": all(predictions[k] is not None for k in METHODS),
        "real_llm_quality_verified": False,
        "real_copilot_improvement_verified": False,
        "network_calls": 0,
        "limitations": "事後提出された観測値の整合性検査。実モデル・Copilotの真実性は外部検収が必要。",
    }
