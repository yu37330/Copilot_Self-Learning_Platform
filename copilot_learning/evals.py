"""学習に使わない独立データでの適用・非適用判定を数えるオフライン評価器。

これは知識の選択品質のみ。Copilotの実作業成功率を検証したことにはならない。
"""
from __future__ import annotations

import math
from typing import Any

from .core import InvalidData, digest, json_bytes

LABELS = {"applicable", "not_applicable"}
OUTCOMES = LABELS | {"needs_review"}
METHODS = ("A", "B", "C", "D")


def wilson_interval(correct: int, total: int, z: float = 1.96) -> list[float] | None:
    if total == 0:
        return None
    p = correct / total
    d = 1 + z * z / total
    mid = (p + z * z / (2 * total)) / d
    margin = z / d * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return [round(max(0.0, mid - margin), 5), round(min(1.0, mid + margin), 5)]


def evaluate_holdout(dataset: dict[str, Any], predictions: dict[str, dict[str, str] | None]) -> dict[str, Any]:
    """同一の正解ラベル集合を比較。未実行メソッドはNoneとし0%と混同しない。"""
    cases = dataset.get("cases")
    train_groups = dataset.get("training_group_ids")
    if not isinstance(cases, list) or not cases or not isinstance(train_groups, list) or not all(
        isinstance(x, str) and x.strip() for x in train_groups
    ):
        raise InvalidData("casesとtraining_group_idsが必要です")
    if len(cases) > 10000 or len(set(train_groups)) != len(train_groups):
        raise InvalidData("データ件数/訓練グループが不正です")
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or case.get("expected") not in LABELS:
            raise InvalidData("ケースの正解ラベルが不正です")
        ident, group = case.get("case_id"), case.get("group_id")
        if not isinstance(ident, str) or not ident.strip() or ident in ids:
            raise InvalidData("case_idは一意の非空文字列です")
        if not isinstance(group, str) or not group.strip() or group in train_groups:
            raise InvalidData("学習側と評価側のグループが重複しています")
        ids.add(ident)
    if not isinstance(predictions, dict) or set(predictions) - set(METHODS):
        raise InvalidData("方式はA/B/C/Dのみです")
    results: dict[str, Any] = {}
    for method in METHODS:
        votes = predictions.get(method)
        if votes is None:
            results[method] = {"status": "not_executed", "n": None, "accuracy": None,
                               "false_application_rate": None, "wilson_95": None}
            continue
        if not isinstance(votes, dict) or set(votes) != ids or any(v not in OUTCOMES for v in votes.values()):
            raise InvalidData(f"{method}: 評価ケースと予測が一致しません")
        c = {"tp": 0, "tn": 0, "fp": 0, "fn": 0, "abstain": 0}
        for case in cases:
            expected, actual = case["expected"], votes[case["case_id"]]
            if actual == "needs_review":
                c["abstain"] += 1
            elif expected == "applicable":
                c["tp" if actual == "applicable" else "fn"] += 1
            else:
                c["tn" if actual == "not_applicable" else "fp"] += 1
        n = len(cases)
        correct = c["tp"] + c["tn"]
        negatives = sum(case["expected"] == "not_applicable" for case in cases)
        results[method] = {
            "status": "offline_label_evaluated", "n": n, **c,
            "accuracy": round(correct / n, 5),
            "coverage": round((n - c["abstain"]) / n, 5),
            "false_application_rate": round(c["fp"] / negatives, 5) if negatives else None,
            "wilson_95": wilson_interval(correct, n),
            "real_copilot_success_verified": False,
        }
    return {
        "schema_version": "1.0", "evaluation_type": "offline_applicability_holdout",
        "dataset_sha256": digest(json_bytes(dataset)), "n_cases": len(cases),
        "label_source": dataset.get("label_source", "unspecified"),
        "results": results,
        "caveat": "適用/非適用ラベルの比較であり、Copilotの実タスク品質・コスト改善の実測ではありません",
    }
