"""Issue #31 Phase C: 指示変更前後の提出済み観測をオフラインでペア評価する。

本評価器はCopilot/LLMを実行しない。観測の真実性や改善の因果関係を保証せず、
未観測・未検証を0や失敗で埋めない。全入力は未信頼データとして扱う。
"""
from __future__ import annotations

import math
import re
from typing import Any

from .core import InvalidData, SHA_PATTERN, check_sensitive, digest, json_bytes
from .evals import task_fingerprint, wilson_interval
from .privacy_scan import BUILTIN_RULES

ARMS = ("baseline", "candidate")
FAMILIES = ("troubleshooting", "project_facts", "session_resumption",
            "preferences", "negative_scope")
METRICS = ("tool_calls", "retry_count", "input_tokens", "output_tokens",
           "latency_ms", "cost_usd")
MAX_CASES = 500
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
MODEL = re.compile(r"[A-Za-z0-9_.:/-]{1,128}")
OUTCOMES = ("success", "failure", "inconclusive")
SOURCES = ("synthetic", "imported_replay")


def _sha(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA_PATTERN.fullmatch(value))


def _token(value: Any) -> bool:
    return isinstance(value, str) and bool(TOKEN.fullmatch(value))


def _number(value: Any, *, cost: bool = False) -> bool:
    if value is None:
        return True
    if cost:
        return (type(value) in (int, float) and math.isfinite(value)
                and 0 <= value < 1_000_000)
    return type(value) is int and 0 <= value <= 100_000_000


def _scan(text: str, sensitive_literals: list[str]) -> None:
    check_sensitive(text)
    if any(rule.search(text) for rule in BUILTIN_RULES.values()):
        raise InvalidData("セッション由来の個人情報が疑われるため評価できません")
    if any(term.strip().casefold() in text.casefold() for term in sensitive_literals):
        raise InvalidData("組織固有の機密語が含まれる可能性があります")


def validate_dataset(dataset: dict[str, Any], *, sensitive_literals: list[str] | None = None) -> dict[str, Any]:
    """系列単位の分割・文字列fingerprintによる重複を検査する。意味的な漏えいは未保証。"""
    literals = sensitive_literals if sensitive_literals is not None else []
    if not isinstance(literals, list) or any(not isinstance(v, str) or not v.strip() for v in literals):
        raise InvalidData("機密語辞書の形式が不正です")
    expected = {"schema_version", "record_type", "label_source", "training_group_ids",
                "training_task_sha256", "proposal_review_sha256", "evaluation_protocol_sha256",
                "instruction_snapshots", "cases"}
    if (not isinstance(dataset, dict) or set(dataset) != expected
            or dataset["schema_version"] != "1.0"
            or dataset["record_type"] != "offline-instruction-effect-holdout"
            or dataset["label_source"] not in {"synthetic", "human_verified"}
            or not _sha(dataset["proposal_review_sha256"])
            or not _sha(dataset["evaluation_protocol_sha256"])):
        raise InvalidData("指示改善評価データの契約が不正です")
    revisions = dataset["instruction_snapshots"]
    if (not isinstance(revisions, dict) or set(revisions) != set(ARMS)
            or not all(_sha(x) for x in revisions.values())
            or revisions["baseline"] == revisions["candidate"]):
        raise InvalidData("比較指示の2つの異なる版ハッシュが必要です")
    groups = dataset["training_group_ids"]
    known = dataset["training_task_sha256"]
    if (not isinstance(groups, list) or not groups
            or len(groups) > 1000 or not all(_token(x) for x in groups)
            or len(set(groups)) != len(groups) or not isinstance(known, list)
            or len(known) > 10000 or not all(_sha(x) for x in known)
            or len(set(known)) != len(known)):
        raise InvalidData("訓練系列とfingerprintの一覧が不正です")
    cases = dataset["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise InvalidData("未学習ケース数は1〜500件で指定してください")
    ids: set[str] = set()
    hashes: set[str] = set()
    case_groups: set[str] = set()
    for case in cases:
        if (not isinstance(case, dict)
                or set(case) != {"case_id", "group_id", "task", "task_sha256", "family"}
                or not _token(case["case_id"]) or case["case_id"] in ids
                or not _token(case["group_id"]) or case["group_id"] in groups
                or case["family"] not in FAMILIES):
            raise InvalidData("未学習ケースの識別子・系列または領域が不正です")
        text = case["task"]
        if not isinstance(text, str):
            raise InvalidData("評価ケース本文が不正です")
        _scan(text, literals)
        fingerprint = task_fingerprint(text)
        if (case["task_sha256"] != fingerprint or fingerprint in hashes
                or fingerprint in known):
            raise InvalidData("評価タスクのハッシュ不一致または重複・漏えいがあります")
        hashes.add(fingerprint)
        ids.add(case["case_id"])
        case_groups.add(case["group_id"])
    return {
        "dataset_sha256": digest(json_bytes(dataset)),
        "case_count": len(cases),
        "family_coverage": {key: sum(case["family"] == key for case in cases) for key in FAMILIES},
        "leakage_checks": {"train_group_disjoint": True, "task_hash_disjoint": True,
                           "semantic_paraphrase_leakage_excluded": False,
                           "training_snapshot_complete_verified": False},
        "label_source": dataset["label_source"],
    }


def blank_observations(dataset: dict[str, Any], *, sensitive_literals: list[str] | None = None) -> dict[str, Any]:
    """計画だけを生成。観測がない方式の成績を0とみなさない。"""
    summary = validate_dataset(dataset, sensitive_literals=sensitive_literals)
    return {"schema_version": "1.0", "record_type": "offline-instruction-effect-observations",
            "dataset_sha256": summary["dataset_sha256"],
            "arms": {k: None for k in ARMS}}


def _profile(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict)
            or set(value) != {"model_id", "budget_tokens", "tool_policy", "sampling_seed"}
            or not isinstance(value["model_id"], str) or not MODEL.fullmatch(value["model_id"])
            or type(value["budget_tokens"]) is not int or not 1 <= value["budget_tokens"] <= 10_000_000
            or not _token(value["tool_policy"])
            or (value["sampling_seed"] is not None
                and (type(value["sampling_seed"]) is not int or not 0 <= value["sampling_seed"] <= 2**32 - 1))):
        raise InvalidData("モデル・予算・ツール権限・seedの評価条件が不正です")
    return value


def _validate_observation(item: Any, executor_sha256: str) -> None:
    fields = {"run_id", "trace_sha256", "outcome", "reviewer_sha256", "review_receipt_sha256", *METRICS}
    if not isinstance(item, dict) or set(item) != fields:
        raise InvalidData("各試行の観測項目が不足しています")
    if not _token(item["run_id"]) or not _sha(item["trace_sha256"]) or item["outcome"] not in OUTCOMES:
        raise InvalidData("試行ID・証跡SHA・判定が不正です")
    reviewer = item["reviewer_sha256"]
    receipt = item["review_receipt_sha256"]
    if item["outcome"] != "inconclusive":
        # 完了判定の申告には実行者と異なるレビュー主体と証跡のSHAを要求する。
        if not _sha(reviewer) or not _sha(receipt) or reviewer == executor_sha256:
            raise InvalidData("独立レビューの申告・証跡が不足しています")
    elif reviewer is not None or receipt is not None:
        if not _sha(reviewer) or not _sha(receipt) or reviewer == executor_sha256:
            raise InvalidData("保留レビューの証跡が不正です")
    for name in METRICS:
        if not _number(item[name], cost=name == "cost_usd"):
            raise InvalidData("計測値は有限・非負またはnullで指定してください")


def _arm(dataset: dict[str, Any], arm_name: str, value: Any, expected_ids: set[str],
         used_runs: set[str], used_traces: set[str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    fields = {"source_kind", "instruction_sha256", "profile", "executor_sha256", "cases"}
    if (not isinstance(value, dict) or set(value) != fields or value["source_kind"] not in SOURCES
            or value["instruction_sha256"] != dataset["instruction_snapshots"][arm_name]
            or not _sha(value["executor_sha256"])):
        raise InvalidData("実行方式・指示版・実行者ハッシュが不正です")
    profile = _profile(value["profile"])
    observations = value["cases"]
    if not isinstance(observations, dict) or set(observations) != expected_ids:
        raise InvalidData("同じ評価ケースすべての観測値が必要です")
    for case_id in sorted(observations):
        item = observations[case_id]
        _validate_observation(item, value["executor_sha256"])
        if item["run_id"] in used_runs or item["trace_sha256"] in used_traces:
            raise InvalidData("別の試行との実行IDまたは証跡の重複があります")
        used_runs.add(item["run_id"])
        used_traces.add(item["trace_sha256"])
    return observations, {
        "source_kind": value["source_kind"],
        "profile_sha256": digest(json_bytes(profile)),
        "instruction_sha256": value["instruction_sha256"],
        "execution_claim": "submitted_not_independently_authenticated",
    }


def _aggregate(observations: dict[str, dict[str, Any]] | None, n: int,
               metadata: dict[str, Any] | None) -> dict[str, Any]:
    if observations is None:
        return {"status": "not_executed", "n": None, "conclusive": None,
                "successes": None, "failures": None, "inconclusive": None,
                "success_rate": None, "wilson_95": None, "metrics": None,
                "execution_claim": None}
    rows = list(observations.values())
    successes = sum(v["outcome"] == "success" for v in rows)
    failures = sum(v["outcome"] == "failure" for v in rows)
    conclusive = successes + failures
    metrics = {}
    for name in METRICS:
        values = [item[name] for item in rows]
        complete = all(x is not None for x in values)
        metrics[name] = {"n_measured": sum(x is not None for x in values),
                         "total": round(sum(values), 7) if complete else None,
                         "mean": round(sum(values) / n, 7) if complete else None}
    return {
        "status": "submitted_observations_unverified",
        "n": n, "conclusive": conclusive, "successes": successes, "failures": failures,
        "inconclusive": n - conclusive, "success_rate": round(successes / conclusive, 5) if conclusive else None,
        "wilson_95": wilson_interval(successes, conclusive),
        "metrics": metrics, **metadata,
    }


def _mcnemar_exact(a_only: int, b_only: int) -> float | None:
    """discordant pairsの両側二項検定（独立性と事前計画を別途要確認）。"""
    n = a_only + b_only
    if n == 0:
        return None
    tail = sum(math.comb(n, k) for k in range(min(a_only, b_only) + 1)) * 0.5 ** n
    return round(min(1.0, 2 * tail), 7)


def _paired(baseline: dict[str, dict[str, Any]] | None,
            candidate: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    if baseline is None or candidate is None:
        return None
    both_good = both_bad = baseline_only = candidate_only = 0
    inconclusive = 0
    paired_metric: dict[str, list[float]] = {key: [] for key in METRICS}
    for cid in sorted(baseline):
        b, c = baseline[cid], candidate[cid]
        if "inconclusive" in (b["outcome"], c["outcome"]):
            inconclusive += 1
        elif b["outcome"] == c["outcome"]:
            if b["outcome"] == "success":
                both_good += 1
            else:
                both_bad += 1
        elif b["outcome"] == "success":
            baseline_only += 1
        else:
            candidate_only += 1
        for key in METRICS:
            if b[key] is not None and c[key] is not None:
                paired_metric[key].append(c[key] - b[key])
    size = both_good + both_bad + baseline_only + candidate_only
    return {
        "conclusive_paired_n": size, "inconclusive_pairs": inconclusive,
        "both_success": both_good, "both_failure": both_bad,
        "baseline_only_success": baseline_only,
        "candidate_only_success": candidate_only,
        "delta_success_rate": round((candidate_only - baseline_only) / size, 5) if size else None,
        "mcnemar_exact_two_sided_p": _mcnemar_exact(baseline_only, candidate_only),
        "delta_metrics_candidate_minus_baseline": {
            key: {"n_paired_observed": len(values),
                  "mean_delta": round(sum(values) / len(values), 7) if values else None}
            for key, values in paired_metric.items()
        },
        "small_sample_warning": size < 30,
        "complete_outcome_coverage": inconclusive == 0,
        "causal_effect_established": False,
    }


def evaluate_instruction_effect(dataset: dict[str, Any], submission: dict[str, Any],
                                *, sensitive_literals: list[str] | None = None) -> dict[str, Any]:
    """同一未学習ケースの提出済み実績のみ集計。観測の真実性は別の検収が必要。"""
    source = validate_dataset(dataset, sensitive_literals=sensitive_literals)
    if (not isinstance(submission, dict)
            or set(submission) != {"schema_version", "record_type", "dataset_sha256", "arms"}
            or submission["schema_version"] != "1.0"
            or submission["record_type"] != "offline-instruction-effect-observations"
            or submission["dataset_sha256"] != source["dataset_sha256"]
            or not isinstance(submission["arms"], dict)
            or set(submission["arms"]) != set(ARMS)):
        raise InvalidData("観測データのスキーマか評価データSHAが一致しません")
    expected_ids = {case["case_id"] for case in dataset["cases"]}
    used_runs: set[str] = set()
    used_traces: set[str] = set()
    observations = {}
    metadata = {}
    for arm in ARMS:
        observations[arm], metadata[arm] = _arm(
            dataset, arm, submission["arms"][arm], expected_ids, used_runs, used_traces)
    if all(observations.values()):
        if (metadata["baseline"]["profile_sha256"] != metadata["candidate"]["profile_sha256"]
                or metadata["baseline"]["source_kind"] != metadata["candidate"]["source_kind"]):
            raise InvalidData("モデル・ツール設定または実行種別が条件間で一致しません")
    pairs = _paired(observations["baseline"], observations["candidate"])
    return {
        "schema_version": "1.0", "record_type": "offline-instruction-effect-comparison",
        "dataset_sha256": source["dataset_sha256"],
        "observations_sha256": digest(json_bytes(submission)),
        "proposal_review_sha256": dataset["proposal_review_sha256"],
        "evaluation_protocol_sha256": dataset["evaluation_protocol_sha256"],
        "instruction_snapshots": dataset["instruction_snapshots"],
        "label_source": source["label_source"],
        "n_cases": source["case_count"], "family_coverage": source["family_coverage"],
        "leakage_checks": source["leakage_checks"],
        "arms": {key: _aggregate(observations[key], source["case_count"], metadata[key]) for key in ARMS},
        "paired_comparison": pairs,
        "comparison_status": (
            "not_executed" if all(v is None for v in observations.values()) else
            "one_arm_missing" if any(v is None for v in observations.values()) else
            "submitted_not_independently_verified"),
        "real_copilot_execution_verified": False,
        "instruction_snapshot_installation_verified": False,
        "causal_improvement_established": False,
        "external_transfer_authorized": False,
        "network_calls": 0,
        "limitations": [
            "提出されたrun/trace/reviewerハッシュは実行・独立検収の真正性を証明しない",
            "モデル/予算/ツール設定が同じでも実行順序や環境差・時刻差は統制されていない",
            "課題の意味的な言い換えや系列漏えいは自動で検出できない",
            "観測の欠落・保留・小標本がある場合は成果を断定できない",
        ],
    }
