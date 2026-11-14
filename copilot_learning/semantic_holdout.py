"""#7: 独立の未学習タスクで明示条件と証拠付き意味レビューを公平に比較する。

LLM推論は行わず、外部モデル由来と主張する回答をただちに信頼しない。
"""
from __future__ import annotations

from typing import Any
import re

from .applicability import evaluate_applicability
from .core import InvalidData, digest, json_bytes
from .evals import evaluate_holdout
from .learning import candidate_sha256
from .semantic_gate import task_hash, verify_semantic_review


def evaluate_semantic_holdout(dataset: dict[str, Any]) -> dict[str, Any]:
    """結果にはユーザーのタスクや候補本文・レビュー自由記述を含めない。"""
    if not isinstance(dataset, dict) or dataset.get("schema_version") != "1.0":
        raise InvalidData("未学習評価セットのバージョンが不正です")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 500:
        raise InvalidData("ケースは1〜500件が必要です")
    if dataset.get("label_source") not in {"synthetic", "human_verified"}:
        raise InvalidData("ラベルの出所をsynthetic/human_verifiedから選んでください")
    if (not isinstance(dataset.get("training_group_ids"), list)
            or not isinstance(dataset.get("training_task_sha256"), list)):
        raise InvalidData("学習データのグループIDと本文ハッシュが必要です")
    stripped_cases: list[dict[str, str]] = []
    literal: dict[str, str] = {}
    evidence: dict[str, str] = {}
    summaries: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("candidate"), dict):
            raise InvalidData("candidateが必要です")
        cid, task = case.get("case_id"), case.get("task")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", cid):
            raise InvalidData("case_idは英数字80文字以内で指定してください")
        baseline = evaluate_applicability(case["candidate"], task)
        review = case.get("review")
        if review is None:
            decision = "needs_review"
            reason = "review_not_supplied"
        else:
            result = verify_semantic_review(case["candidate"], task, review)
            decision, reason = result["decision"], result["reason"]
        literal[cid] = baseline["decision"]
        evidence[cid] = decision
        stripped_cases.append({k: case[k] for k in ("case_id", "group_id", "task", "expected")})
        summaries.append({"case_id": cid, "task_sha256": task_hash(task),
                          "candidate_sha256": candidate_sha256(case["candidate"]),
                          "expected": case.get("expected"),
                          "literal_decision": baseline["decision"],
                          "evidence_review_decision": decision,
                          "review_reason_code": reason,
                          "review_supplied": review is not None})
    heldout = evaluate_holdout({
        "cases": stripped_cases,
        "training_group_ids": dataset["training_group_ids"],
        "training_task_sha256": dataset["training_task_sha256"],
        "label_source": dataset["label_source"],
    }, {"B": literal, "C": evidence})
    return {
        "schema_version": "1.0", "record_type": "semantic-holdout-report",
        "dataset_sha256": digest(json_bytes(dataset)),
        "label_source": dataset["label_source"],
        "total_cases": len(cases),
        "literal_baseline": heldout["results"]["B"],
        "evidence_review_gate": heldout["results"]["C"],
        "leakage_checks": heldout["leakage_checks"],
        "cases": summaries,
        "real_llm_quality_verified": False,
        "copilot_improvement_verified": False,
        "caveat": "評価器に与えたレビュー判定の検証であり、実モデルの意味理解やCopilot品質の実測ではありません",
    }
