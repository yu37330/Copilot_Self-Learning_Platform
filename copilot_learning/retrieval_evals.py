"""active Note検索の品質を合成Hold-outで測る。再利用・実行の成功を主張しない。"""
from __future__ import annotations

from typing import Any

from .core import InvalidData, digest, json_bytes
from .evals import task_fingerprint
from .knowledge_store import KnowledgeStore
from .retrieval import search_active


def evaluate_retrieval(store: KnowledgeStore, dataset: dict[str, Any], *, top_k: int = 5) -> dict[str, Any]:
    """正例Hit/MRRと不適切知識の検索露出を分けて集計する。

    結果は候補の検索品質であり、Copilotへの適用も意味的妥当性も証明しない。
    """
    if not isinstance(top_k, int) or not 1 <= top_k <= 25:
        raise InvalidData("top_kは1〜25の整数です")
    cases = dataset.get("cases") if isinstance(dataset, dict) else None
    train_groups = dataset.get("training_group_ids", []) if isinstance(dataset, dict) else []
    if (not isinstance(cases, list) or not 1 <= len(cases) <= 10000 or
            not isinstance(train_groups, list) or any(not isinstance(g, str) or not g.strip() for g in train_groups) or
            len(set(train_groups)) != len(train_groups)):
        raise InvalidData("ケースと学習グループが不正です")
    active = store.active()
    active_by_id = {item["knowledge_id"]: item for item in active}
    ids: set[str] = set()
    task_ids: set[str] = set()
    hits = 0
    reciprocal_rank = 0.0
    expected_count = 0
    wrong_exposures = 0
    negatives_with_results = 0
    positive_cases = 0
    negative_cases = 0
    details = []
    for case in cases:
        if not isinstance(case, dict):
            raise InvalidData("評価ケースはJSONオブジェクトが必要です")
        case_id, group, task = case.get("case_id"), case.get("group_id"), case.get("task")
        if (not isinstance(case_id, str) or not case_id.strip() or case_id in ids or
                not isinstance(group, str) or not group.strip() or group in train_groups):
            raise InvalidData("ケースIDまたはtrain/testのgroup分離が不正です")
        ids.add(case_id)
        fp = task_fingerprint(task)
        if fp in task_ids:
            raise InvalidData("Hold-outに同一タスクが重複しています")
        task_ids.add(fp)
        relevant, banned = case.get("relevant_ids"), case.get("forbidden_ids", [])
        if (not isinstance(relevant, list) or not isinstance(banned, list) or
                not all(isinstance(v, str) for v in relevant + banned) or
                len(set(relevant)) != len(relevant) or len(set(banned)) != len(banned) or
                set(relevant) & set(banned)):
            raise InvalidData("関連・不適用の知識IDラベルが不正です")
        if any(id_ not in active_by_id for id_ in relevant + banned):
            raise InvalidData("評価ラベルがactiveでない、または存在しない知識を参照しています")
        paths = case.get("changed_paths", [])
        predictions = search_active(store, task, limit=top_k, changed_paths=paths)
        predicted_ids = [item["knowledge_id"] for item in predictions]
        relevant_ids = set(relevant)
        matched = relevant_ids & set(predicted_ids)
        ranks = [idx + 1 for idx, value in enumerate(predicted_ids) if value in relevant_ids]
        if relevant_ids:
            positive_cases += 1
            hits += bool(matched)
            reciprocal_rank += 1.0 / min(ranks) if ranks else 0.0
            expected_count += len(relevant_ids)
        else:
            negative_cases += 1
            negatives_with_results += bool(predicted_ids)
        exposed_wrong = len(set(banned) & set(predicted_ids))
        wrong_exposures += bool(exposed_wrong)
        details.append({
            "case_id": case_id,
            "relevant_total": len(relevant_ids), "relevant_found": len(matched),
            "reciprocal_rank": round(1.0 / min(ranks), 5) if ranks else 0.0,
            "forbidden_exposed": exposed_wrong,
            "retrieved": [{"knowledge_id": p["knowledge_id"], "applicability": p["applicability"]} for p in predictions],
        })
    revision = [(x["knowledge_id"], x["revision"], x["candidate_sha256"]) for x in active]
    return {
        "schema_version": "1.0", "evaluation_type": "offline_retrieval_holdout",
        "dataset_sha256": digest(json_bytes(dataset)),
        "knowledge_revision_sha256": digest(json_bytes(revision)),
        "active_knowledge_count": len(active), "top_k": top_k,
        "n_cases": len(cases), "n_positive": positive_cases, "n_negative": negative_cases,
        "hit_rate_at_k": round(hits / positive_cases, 5) if positive_cases else None,
        "mrr_at_k": round(reciprocal_rank / positive_cases, 5) if positive_cases else None,
        "negative_case_exposure_rate": round(negatives_with_results / negative_cases, 5) if negative_cases else None,
        "forbidden_knowledge_exposure_rate": round(wrong_exposures / len(cases), 5),
        "details": details,
        "real_copilot_improvement_verified": False,
        "caveat": "これは検索候補の合成オフライン評価です。意味的適用性・実タスク成功率は別途検証が必要です。",
    }
