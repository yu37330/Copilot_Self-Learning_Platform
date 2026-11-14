"""学習に使わない独立データでの適用・非適用判定を数えるオフライン評価器。

これは知識の選択品質のみ。Copilotの実作業成功率を検証したことにはならない。
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from .core import InvalidData, digest, json_bytes, check_sensitive, SHA_PATTERN

LABELS = {"applicable", "not_applicable"}
OUTCOMES = LABELS | {"needs_review"}
METHODS = ("A", "B", "C", "D")


def task_fingerprint(text: str) -> str:
    """意味の近さは判定できない。全半角・大文字・空白の表記揺れだけを統一する。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        raise InvalidData("評価タスク本文は1〜12000文字が必要です")
    check_sensitive(text)
    normal = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return digest(normal.encode("utf-8"))



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
    # 実データが無い初期PoCはケース本文なしで動けるが、リーク検査は「未完了」と明示する。
    training_fingerprints = dataset.get("training_task_sha256", [])
    if not isinstance(training_fingerprints, list) or any(
        not isinstance(x, str) or not SHA_PATTERN.fullmatch(x) for x in training_fingerprints
    ) or len(set(training_fingerprints)) != len(training_fingerprints):
        raise InvalidData("training_task_sha256は一意のSHA-256文字列配列です")
    ids: set[str] = set()
    case_fingerprints: set[str] = set()
    complete_fingerprint_check = "training_task_sha256" in dataset
    for case in cases:
        if not isinstance(case, dict) or case.get("expected") not in LABELS:
            raise InvalidData("ケースの正解ラベルが不正です")
        ident, group = case.get("case_id"), case.get("group_id")
        if not isinstance(ident, str) or not ident.strip() or ident in ids:
            raise InvalidData("case_idは一意の非空文字列です")
        if not isinstance(group, str) or not group.strip() or group in train_groups:
            raise InvalidData("学習側と評価側のグループが重複しています")
        if "task" in case:
            case_hash = task_fingerprint(case["task"])
            if case_hash in case_fingerprints or case_hash in training_fingerprints:
                raise InvalidData("評価タスクが訓練データまたは他の評価タスクと重複しています")
            if "task_sha256" in case and case["task_sha256"] != case_hash:
                raise InvalidData("評価タスクのfingerprintが本文と一致しません")
            case_fingerprints.add(case_hash)
        else:
            complete_fingerprint_check = False
            if "task_sha256" in case:
                raise InvalidData("task_sha256には検証対象のtask本文が必要です")
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
        positives = n - negatives
        # 棄権を正解に混ぜず、判定したケースの再現率と安全性を分けて示す。
        results[method] = {
            "status": "offline_label_evaluated", "n": n, **c,
            "accuracy": round(correct / n, 5),
            "coverage": round((n - c["abstain"]) / n, 5),
            "false_application_rate": round(c["fp"] / negatives, 5) if negatives else None,
            "false_rejection_rate": round(c["fn"] / positives, 5) if positives else None,
            "abstention_rate": round(c["abstain"] / n, 5),
            "positive_cases": positives, "negative_cases": negatives,
            "false_application_wilson_95": wilson_interval(c["fp"], negatives),
            "wilson_95": wilson_interval(correct, n),
            "real_copilot_success_verified": False,
        }
    return {
        "schema_version": "1.0", "evaluation_type": "offline_applicability_holdout",
        "dataset_sha256": digest(json_bytes(dataset)), "n_cases": len(cases),
        "leakage_checks": {
            "group_ids_disjoint": True,
            "task_fingerprint_checked_count": len(case_fingerprints),
            "train_task_fingerprint_count": len(training_fingerprints),
            "complete": complete_fingerprint_check,
            "limitations": "同義文の重複や別プロジェクトの同一障害は自動判定できません",
        },
        "label_source": dataset.get("label_source", "unspecified"),
        "results": results,
        "caveat": "適用/非適用ラベルの比較であり、Copilotの実タスク品質・コスト改善の実測ではありません",
    }
