#!/usr/bin/env python3
"""オフライン中心のPoC。S3実データもAWSも、オプションなしでは使用しない。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import (InvalidData, assert_external_output, build_memory_requests,
                                   json_bytes, load_feedback, normalize_events, read_archive)
from copilot_learning.learning import (BedrockConverseProvider, generate_candidates, grade_candidates,
                                       independent_llm_grade, make_report,
                                       memory_records_as_candidates, validate_candidates)
from copilot_learning.aws_bridge import assert_account, validate_approval

from copilot_learning.review_safety import require_online_release
from copilot_learning.model_review_contract import enforce_distinct_model_ids

REPO_ROOT = Path(__file__).resolve().parents[1]


def _safe_write(out_dir: Path, name: str, value: object) -> None:
    """同じ内容なら再実行可、異なる内容への上書きは禁止。"""
    target = out_dir / name
    data = json_bytes(value)
    if target.exists():
        if target.read_bytes() == data:
            return
        raise InvalidData(f"既存結果への上書きを禁止します: {name}（別フォルダを指定）")
    with target.open("xb") as file:
        file.write(data)


def process(staging: Path, fingerprint: str, out_dir: Path, memory_id: str,
            actor_id: str, feedback_file: Path | None = None,
            candidate_file: Path | None = None, memory_records_file: Path | None = None,
            provider: object | None = None,
            grade_provider: object | None = None) -> dict:
    assert_external_output(out_dir, REPO_ROOT)
    manifest, raw = read_archive(staging, fingerprint)
    session = normalize_events(manifest, raw)
    feedback = load_feedback(feedback_file, fingerprint)
    plan = build_memory_requests(session, memory_id, actor_id)
    b_graded = c_graded = None
    if provider is not None and candidate_file is not None:
        raise InvalidData("モデル生成と候補ファイル指定は同時に実行できません")
    if provider is not None:
        if grade_provider is None or provider is grade_provider:
            raise InvalidData("候補生成と独立Gradeには別のモデルアダプターが必要です")
        enforce_distinct_model_ids(getattr(provider, "model_id", None),
                                   getattr(grade_provider, "model_id", None))
        if not session["messages"]:
            raise InvalidData("認識できるイベントがなく、モデルに入力できません")
        b_graded = grade_candidates(generate_candidates(provider, session, feedback), session, feedback)
        b_graded = independent_llm_grade(grade_provider, b_graded, session, feedback)
    elif grade_provider is not None:
        raise InvalidData("独立Gradeだけを実行するには専用の評価経路を指定してください")
    elif candidate_file is not None:
        proposals = json.loads(candidate_file.read_text("utf-8"))
        b_graded = grade_candidates(validate_candidates(proposals), session, feedback)
    memory_record_count = None
    if memory_records_file is not None:
        records = json.loads(memory_records_file.read_text("utf-8"))
        if not isinstance(records, dict) or not isinstance(records.get("memoryRecordSummaries"), list):
            raise InvalidData("AgentCoreのlist-memory-records戻り値形式が必要です")
        memory_record_count = len(records["memoryRecordSummaries"])
        # 元セッションの根拠がレコードにないため勝手に結びつけない。
        c_graded = grade_candidates(memory_records_as_candidates(records["memoryRecordSummaries"], session),
                                    session, feedback, allow_unlinked_memory=True)
    report = make_report(session, plan, b_graded, c_graded, memory_record_count)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 出力先はGit外で、ローカルのみ。機微な文章を含むのでGitへ追加しない。
    _safe_write(out_dir, "normalized_session.json", session)
    _safe_write(out_dir, "memory_create_event_plan.json", plan)
    if b_graded is not None:
        _safe_write(out_dir, "agentrun_candidate_grades.json", b_graded)
    if c_graded is not None:
        _safe_write(out_dir, "hybrid_candidate_grades.json", c_graded)
    _safe_write(out_dir, "comparison_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, required=True, help="ZIPとmanifestがあるローカルのフォルダ")
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--out", type=Path, required=True, help="Gitリポジトリ外の出力先")
    parser.add_argument("--memory-id", default="REPLACE_WITH_REAL_MEMORY_ID")
    parser.add_argument("--actor-id", default="copilot_poc_actor")
    parser.add_argument("--feedback", type=Path)
    parser.add_argument("--candidates", type=Path, help="オフライン用の候補JSON。これはモデルの出力ではない")
    parser.add_argument("--memory-records", type=Path, help="実際に取得したAgentCore Memory出力JSON")
    parser.add_argument("--use-bedrock", action="store_true")
    parser.add_argument("--model-id", help="知識候補の生成に使用するモデルID")
    parser.add_argument("--grade-model-id", help="独立Gradeに使用する別のモデルID")
    parser.add_argument("--region", default="ap-northeast-1")
    parser.add_argument("--profile", default="")
    parser.add_argument("--expected-account-id", default="")
    parser.add_argument("--approval-file", type=Path)
    parser.add_argument("--confirm-approved-data", action="store_true")
    args = parser.parse_args(argv)
    try:
        provider = None
        grade_provider = None
        if args.use_bedrock:
            require_online_release()
            if not (args.confirm_approved_data and args.approval_file and args.expected_account_id
                    and args.model_id and args.grade_model_id):
                raise InvalidData("外部モデル送信には承認・AWSアカウント・2種類のモデルIDが必須です")
            enforce_distinct_model_ids(args.model_id, args.grade_model_id)
            manifest, _ = read_archive(args.staging, args.fingerprint)
            from copilot_learning.core import digest
            validate_approval(args.approval_file, args.fingerprint, manifest["archive_sha256"],
                              args.expected_account_id,
                              manifest_sha256=digest((args.staging / f"session-{args.fingerprint}.manifest.json").read_bytes()))
            assert_account(args.expected_account_id, region=args.region, profile=args.profile)
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("Bedrock利用にはpip install boto3が必要です") from exc
            sdk_session = boto3.Session(profile_name=args.profile or None, region_name=args.region)
            sdk_client = sdk_session.client("bedrock-runtime")
            provider = BedrockConverseProvider(args.model_id, args.region, client=sdk_client)
            grade_provider = BedrockConverseProvider(args.grade_model_id, args.region, client=sdk_client)
        report = process(args.staging, args.fingerprint, args.out, args.memory_id, args.actor_id,
                         feedback_file=args.feedback, candidate_file=args.candidates,
                         memory_records_file=args.memory_records, provider=provider,
                         grade_provider=grade_provider)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (InvalidData, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
