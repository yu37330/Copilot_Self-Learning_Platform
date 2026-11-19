#!/usr/bin/env python3
"""知識学習・Retrospectiveの生成/独立評価モデルをオフラインで計画・検査する。

モデルを呼び出すツールではない。レビュー要求はGit管理外へ出力し、
独立Reviewerの判断は未信頼JSONとして再計算し検査する。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, digest, json_bytes, read_archive, normalize_events
from copilot_learning.model_review_contract import validate_model_plan
from copilot_learning.privacy_scan import load_policy
from copilot_learning.retro_llm_review import prepare_independent_review, grade_independent_review
from copilot_learning.skill_preview import ROOT, RESERVED, _path

MAX_JSON_BYTES = 256 * 1024
REQUEST_FILE = "review-request.preview.json"
GRADE_FILE = "review-grade.preview.json"
PLAN_FILE = "model-routing.preview.json"
MANIFEST_FILE = "manifest.json"


def _json_file(path: Path) -> dict[str, Any]:
    checked = _path(path)
    if not checked.is_file() or checked.stat().st_size > MAX_JSON_BYTES:
        raise InvalidData("入力JSONが存在しないかサイズ上限を超えています")
    raw = checked.read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise InvalidData("入力JSONのサイズが不正です")
    def no_duplicates(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise InvalidData("JSONに重複キーがあります")
            obj[key] = value
        return obj
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates,
                       parse_constant=lambda _: (_ for _ in ()).throw(InvalidData("非有限JSON値は禁止です")))
    if not isinstance(value, dict):
        raise InvalidData("JSONトップレベルはオブジェクトが必要です")
    return value


def _directory(path: Path, repo: Path | None) -> Path:
    checked = _path(path)
    assert_external_output(checked, ROOT)
    if any(p.rstrip(" .").casefold() in RESERVED for p in checked.parts):
        raise InvalidData("自動読込先へモデル依頼を置くことはできません")
    if repo is not None:
        root = _path(repo)
        if checked == root or root in checked.parents:
            raise InvalidData("確認対象リポジトリへの出力は禁止です")
    if checked.exists():
        raise InvalidData("既存フォルダは上書きできません")
    return checked


def _write(path: Path, payloads: dict[str, bytes], repo: Path | None) -> dict[str, Any]:
    out = _directory(path, repo)
    manifest = {
        "schema_version": "1.0", "record_type": "offline-dual-track-review-manifest",
        "files": [{"name": name, "sha256": digest(data), "size_bytes": len(data)}
                  for name, data in sorted(payloads.items())],
        "status": "offline_not_submitted_not_approved",
        "external_transfer_authorized": False, "network_calls": 0,
    }
    if sum(map(len, payloads.values())) > MAX_JSON_BYTES:
        raise InvalidData("プレビュー容量が上限を超えています")
    out.parent.mkdir(parents=True, exist_ok=True)
    _path(out.parent)
    out.mkdir(mode=0o700)
    created: list[Path] = []
    try:
        for name, content in (*sorted(payloads.items()), (MANIFEST_FILE, json_bytes(manifest))):
            file = out / name
            with file.open("xb") as handle:
                created.append(file)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            file.chmod(0o400)
    except BaseException:
        for file in reversed(created):
            file.chmod(0o600)
            file.unlink()
        out.rmdir()
        raise
    return {"status": "offline_preview_written",
            "manifest_sha256": digest(json_bytes(manifest)),
            "network_calls": 0, "approved_for_copilot": False}


def _sessions(staging: Path, fingerprints: list[str]) -> list[dict[str, Any]]:
    if not 1 <= len(fingerprints) <= 32 or len(set(fingerprints)) != len(fingerprints):
        raise InvalidData("セッション数または重複が不正です")
    checked = _path(staging)
    result = []
    for ident in fingerprints:
        manifest, events = read_archive(checked, ident)
        result.append(normalize_events(manifest, events))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "retro-request", "retro-grade"))
    parser.add_argument("--model-plan", type=Path, required=True, help="生成/評価のモデル指定JSON")
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--fingerprint", action="append")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--proposals", type=Path, help="Phase Bが受け付ける生成モデルの提案JSON")
    parser.add_argument("--reviewer-output", type=Path, help="別モデルの回答JSON")
    parser.add_argument("--expected-request-sha256", help="独立保管のレビュー要求SHA")
    parser.add_argument("--out", type=Path, required=True, help="Git管理外の新規フォルダ")
    args = parser.parse_args(argv)
    try:
        plan = _json_file(args.model_plan)
        routing = validate_model_plan(plan)
        if args.command == "plan":
            if any((args.staging, args.fingerprint, args.repo, args.policy, args.proposals,
                    args.reviewer_output, args.expected_request_sha256)):
                raise InvalidData("planはモデル計画だけを対象にします")
            return_code = _write(args.out, {PLAN_FILE: json_bytes(routing)}, None)
        else:
            if any(x is None for x in (args.staging, args.repo, args.policy, args.proposals)):
                raise InvalidData("レビューにはセッション・対象repo・辞書・提案が必要です")
            sessions = _sessions(args.staging, args.fingerprint or [])
            proposals = _json_file(args.proposals)
            request = prepare_independent_review(sessions, args.repo, args.policy, proposals, plan)
            request_sha = digest(json_bytes(request))
            if args.command == "retro-request":
                if args.reviewer_output or args.expected_request_sha256:
                    raise InvalidData("retro-requestで評価回答は受け付けません")
                return_code = _write(args.out, {REQUEST_FILE: json_bytes(request)}, args.repo)
                return_code["request_sha256"] = request_sha
                return_code["candidate_count"] = len(request["candidates"])
            else:
                if not args.reviewer_output or not args.expected_request_sha256:
                    raise InvalidData("retro-gradeには独立レビューのJSONと要求SHAが必要です")
                if args.expected_request_sha256 != request_sha:
                    raise InvalidData("レビュー要求のSHAが変わっています")
                reviewer_output = _json_file(args.reviewer_output)
                terms, _ = load_policy(_path(args.policy))
                result = grade_independent_review(
                    request, reviewer_output, plan,
                    sensitive_literals=terms["sensitive_literals"])
                return_code = _write(args.out, {
                    GRADE_FILE: json_bytes(result),
                    REQUEST_FILE: json_bytes(request),
                }, args.repo)
                return_code["decision_count"] = result["decision_count"]
                return_code["request_sha256"] = request_sha
        print(json.dumps(return_code, sort_keys=True, ensure_ascii=False))
        return 0
    except (InvalidData, OSError, TypeError, ValueError, UnicodeError,
            json.JSONDecodeError, RecursionError):
        # モデル出力やファイルパス、元の実セッションの文字列を出さない。
        print("ERROR: 2系統モデルレビューのオフライン検証に失敗しました", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
