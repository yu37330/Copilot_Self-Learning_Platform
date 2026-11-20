"""Issue #46/#41: Codexタスクの出典・JUnit XML・GitHub PR参照を安全に照合する。

Codexのrolloutは数値とイベント型のみ。テストXMLは文書中の構造的な
検査件数を数えるだけで、テストが真正に実行されたとは認定しない。
PRはlocatorだけ受理して実GitHub状態の確認を別工程に残す。
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .codex_schema_probe import inspect_structure
from .core import InvalidData, SHA_PATTERN, assert_external_output, digest, json_bytes
from .privacy_scan import load_policy
from .skill_preview import ROOT, _path

VERSION = "codex-task-evidence-1"
MAX_TEST_BYTES = 2 * 1024 * 1024
MAX_BIND_BYTES = 64 * 1024
MAX_CASES = 5000
TASK_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
GH_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _read_private(path: Path, limit: int) -> bytes:
    """指定されたGit管理外の通常ファイルだけを安定した状態で読む。"""
    src = _path(path)
    assert_external_output(src, ROOT)
    if (src.is_symlink() or not src.is_file() or not 0 < src.stat().st_size <= limit):
        raise InvalidData("私的入力ファイルの形式・場所または容量が不正です")
    before = src.stat()
    with src.open("rb") as f:
        raw = f.read(limit + 1)
    after = src.stat()
    if (len(raw) != before.st_size or len(raw) > limit
            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns):
        raise InvalidData("ローカル入力が読み取り中に変更された可能性があります")
    return raw


def _test_xml(raw: bytes) -> dict[str, Any]:
    """JUnit XMLの数だけ抽出。任意のテスト名・ログ・失敗文は扱わない。"""
    # XXE / DTD / entities は不可。実行時にXMLを経由したネットワークアクセスはない。
    if b"<!" in raw:
        raise InvalidData("DTD/Entity/コメントを含むXMLを安全側で拒否します")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InvalidData("テスト報告XMLが不正です") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise InvalidData("JUnit XMLの最上位が未対応")
    cases = root.findall(".//testcase")
    if root.tag == "testsuite":
        cases += root.findall("./testcase")
        # findall('.//testcase') already includes children; deduplicate by identity.
        cases = list(dict.fromkeys(cases))
    if not 1 <= len(cases) <= MAX_CASES:
        raise InvalidData("JUnit testcase数が上限外")
    failed = errors = skipped = 0
    for case in cases:
        statuses = [
            key for key in ("failure", "error", "skipped") if case.find(key) is not None
        ]
        if len(statuses) > 1:
            raise InvalidData("JUnit testcaseが複数の相反する状態を持っています")
        failed += statuses == ["failure"]
        errors += statuses == ["error"]
        skipped += statuses == ["skipped"]
    # If the declared suite totals are present, reject contradictory metadata.
    for key, observed in (("tests", len(cases)), ("failures", failed),
                          ("errors", errors), ("skipped", skipped)):
        stated = root.attrib.get(key)
        if stated is not None and (not stated.isdecimal() or int(stated) != observed):
            raise InvalidData("JUnitの宣言値と実際のテストケース数が一致しません")
    return {
        "format": "junit-xml-structural-only",
        "testcases_declared_and_parsed": len(cases),
        "failed_case_count": failed,
        "error_case_count": errors,
        "skipped_case_count": skipped,
        "cases_without_fail_error_skip": len(cases) - failed - errors - skipped,
        "report_indicates_zero_failures": not (failed or errors),
        "test_process_execution_verified": False,
        "test_executor_identity_verified": False,
        "source_text_exported": False,
    }


def inspect_sources(rollout: Path, test_xml: Path | None, policy: Path,
                    *, minimum_age_minutes: int = 1440) -> dict[str, Any]:
    """原本1件だけのHash固定。GitHub、外部LLM、ネットワークは使用しない。"""
    _, policy_sha = load_policy(_path(policy))
    summary = inspect_structure(
        rollout, minimum_age_minutes=minimum_age_minutes,
        include_telemetry=True, include_source_sha256=True)
    test_raw = _read_private(test_xml, MAX_TEST_BYTES) if test_xml is not None else None
    test_info = _test_xml(test_raw) if test_raw is not None else None
    return {
        "schema_version": "1.0",
        "record_type": "offline-codex-task-evidence-source-index",
        "rollout_sha256": summary.pop("source_rollout_sha256"),
        "test_report_sha256": digest(test_raw) if test_raw is not None else None,
        "policy_sha256": policy_sha,
        "rollout_metadata": summary,
        "test_report": test_info,
        "source_text_exported": False,
        "network_calls": 0,
    }


def _binding(data: Any, index: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str]:
    if not isinstance(data, dict) or set(data) != {
        "schema_version", "record_type", "task_id", "rollout_sha256",
        "test_report_sha256", "github", "completion_claim"
    } or data["schema_version"] != "1.0" or data["record_type"] != "offline-codex-task-evidence-binding":
        raise InvalidData("タスク紐付けの必須項目が不正です")
    tid = data["task_id"]
    if not isinstance(tid, str) or not TASK_ID.fullmatch(tid):
        raise InvalidData("Task IDの形式が不正です")
    if data["rollout_sha256"] != index["rollout_sha256"] or data["test_report_sha256"] != index["test_report_sha256"]:
        raise InvalidData("指定したタスクの証拠原本SHAが一致しません")
    claim = data["completion_claim"]
    if claim not in {"not_claimed", "reported_complete"}:
        raise InvalidData("完了状態は申告のみでなければなりません")
    git = data["github"]
    if git is not None:
        if (not isinstance(git, dict) or set(git) != {"repository_full_name", "pr_number"}
                or not isinstance(git["repository_full_name"], str)
                or not GH_REPO.fullmatch(git["repository_full_name"])
                or type(git["pr_number"]) is not int or not 1 <= git["pr_number"] < 10**9):
            raise InvalidData("GitHub参照の形式が不正です")
    return tid, git, claim


def evaluate_task(index: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    """入力の出典を検査するが、未確認のPRやXMLを業務完了へ昇格させない。"""
    tid, git, claim = _binding(binding, index)
    test = index["test_report"]
    if test is None:
        recommended = "test_report_missing"
    elif test["failed_case_count"] or test["error_case_count"]:
        recommended = "test_report_contains_failures"
    else:
        recommended = "test_report_clean_but_executor_not_verified"
    telemetry = index["rollout_metadata"]["event_telemetry"]
    suggestions = telemetry["review_candidates"]
    return {
        "schema_version": "1.0",
        "record_type": "offline-codex-task-evidence-review",
        "reviewer_version": VERSION,
        "task_id_sha256": digest(("codex-task-id-v1\x00" + tid).encode("utf-8")),
        "source_binding_sha256": digest(json_bytes(binding)),
        "rollout_sha256": index["rollout_sha256"],
        "test_report_sha256": index["test_report_sha256"],
        "policy_sha256": index["policy_sha256"],
        "rollout": {
            "event_counts": index["rollout_metadata"]["event_categories"],
            "message_role_counts": index["rollout_metadata"]["message_role_categories"],
            "anomaly_candidates": suggestions,
            "cumulative_tokens": telemetry["latest_cumulative_token_usage"],
            "rollout_metadata_confirmed": True,
            "task_identity_confirmed_from_rollout": False,
        },
        "test_result": test,
        "test_status": recommended,
        "github": {
            "locator_sha256": digest(json_bytes(git)) if git is not None else None,
            "pr_supplied": git is not None,
            "provider_state": "not_checked_requires_live_provider_lookup",
            "pr_merge_verified": False,
            "link_to_codex_task_verified": False,
        },
        "completion_claimed": claim == "reported_complete",
        "business_completion_verified": False,
        "independent_acceptance_verified": False,
        "causal_effect_verified": False,
        "recommendation": "check_git_pr_tests_and_acceptance_with_independent_sources",
        "status": "manual_provider_and_business_acceptance_required",
        "source_text_exported": False,
        "approved_for_instructions": False,
        "network_calls": 0,
        "limitations": [
            "Task ID と Codex rollout の関連は本人の申告であり実行時IDの証明ではない",
            "JUnit XMLをバイト照合しても実テスト実行の真正性は保証されない",
            "GitHub PRの状態はこのオフライン処理で実際に再取得していない",
            "すべての証拠が一致しても独立検収までは業務完了扱いにしない",
        ],
    }
