#!/usr/bin/env python3
"""根拠付きLLM改善提案を2役割でオフライン再生。外部APIやCopilot操作なし。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import (
    InvalidData, assert_external_output, digest, json_bytes,
    normalize_events, read_archive,
)
from copilot_learning.dual_track_review import (
    export_retrospective_offline, generator_request, reviewer_request,
)
from copilot_learning.offline_model_port import (
    ModelRoute, OfflineBoundJSONModel, request_sha256, MAX_REPLY_BYTES,
)
from copilot_learning.skill_preview import ROOT, RESERVED, _path


def _reply(path: Path) -> dict:
    file = _path(path)
    assert_external_output(file, ROOT)
    if not file.is_file() or file.stat().st_size > MAX_REPLY_BYTES:
        raise InvalidData("ローカル応答JSONが存在しないか大きすぎます")
    raw = file.read_bytes()
    if len(raw) > MAX_REPLY_BYTES:
        raise InvalidData("ローカル応答JSONのサイズ上限を超えています")
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise InvalidData("応答JSONはオブジェクトである必要があります")
    return parsed


def _export_request(payload: dict, output: Path, root: Path) -> str:
    out = _path(output)
    assert_external_output(out, ROOT)
    repo = _path(root)
    if (out == repo or repo in out.parents or out.exists()
            or any(part.rstrip(" .").casefold() in RESERVED for part in out.parts)):
        raise InvalidData("Git管理外の新規フォルダを指定してください")
    request = {
        "schema_version": "1.0",
        "record_type": "offline-bound-model-request",
        "system": payload["system"], "prompt": payload["prompt"],
        "request_sha256": request_sha256(payload["system"], payload["prompt"]),
        "external_transfer_authorized": False,
        "real_model_executed": False,
        "approved_for_copilot": False,
    }
    body = json_bytes(request)
    manifest = {
        "schema_version": "1.0", "record_type": "offline-model-request-manifest",
        "request_sha256": request["request_sha256"],
        "request_file_sha256": digest(body),
        "status": "not_executed_manual_review_required",
        "network_calls": 0, "approved_for_copilot": False,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    _path(out.parent)
    out.mkdir(mode=0o700)
    created: list[Path] = []
    try:
        for name, data in (("model-request.preview.json", body),
                           ("manifest.json", json_bytes(manifest))):
            p = out / name
            with p.open("xb") as handle:
                created.append(p)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            p.chmod(0o400)
    except BaseException:
        for p in reversed(created):
            p.chmod(0o600)
            p.unlink()
        out.rmdir()
        raise
    return request["request_sha256"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generator-request", "reviewer-request", "review"))
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--fingerprint", action="append", required=True)
    parser.add_argument("--repo", type=Path, required=True, help="既存指示を読取専用で参照する対象リポジトリ")
    parser.add_argument("--policy", type=Path, required=True, help="Git管理外の機密語辞書")
    parser.add_argument("--proposal", type=Path, help="持込みの改善案JSON。reviewer-request/reviewで必要")
    parser.add_argument("--opinion", type=Path, help="別モデルによる意見JSON。reviewで必要")
    parser.add_argument("--generator-model-id", default="offline-generator-01")
    parser.add_argument("--reviewer-model-id", default="offline-reviewer-02")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not 1 <= len(args.fingerprint) <= 32:
            raise InvalidData("セッションの上限を超えています")
        if (args.command in {"reviewer-request", "review"}) != (args.proposal is not None):
            raise InvalidData("提案JSONの指定が不正です")
        if (args.command == "review") != (args.opinion is not None):
            raise InvalidData("独立レビューの指定が不正です")
        staging = _path(args.staging)
        sessions = []
        for fp in args.fingerprint:
            manifest, events = read_archive(staging, fp)
            sessions.append(normalize_events(manifest, events))
        if args.command == "generator-request":
            request = generator_request(sessions, args.repo, args.policy)
            sha = _export_request(request, args.out, args.repo)
            result = {"status": "generator_request_only_no_model_invocation",
                      "request_sha256": sha, "network_calls": 0}
        elif args.command == "reviewer-request":
            request = reviewer_request(sessions, args.repo, args.policy,
                                       _reply(args.proposal))
            sha = _export_request(request, args.out, args.repo)
            result = {"status": "reviewer_request_only_no_model_invocation",
                      "request_sha256": sha, "network_calls": 0}
        else:
            proposal = _reply(args.proposal)
            opinion = _reply(args.opinion)
            gr = generator_request(sessions, args.repo, args.policy)
            rv = reviewer_request(sessions, args.repo, args.policy, proposal)
            generator = OfflineBoundJSONModel(
                ModelRoute("offline-local", args.generator_model_id, "generator"),
                expected_request_sha256=request_sha256(gr["system"], gr["prompt"]),
                response=proposal)
            reviewer = OfflineBoundJSONModel(
                ModelRoute("offline-local", args.reviewer_model_id, "reviewer"),
                expected_request_sha256=request_sha256(rv["system"], rv["prompt"]),
                response=opinion)
            result = export_retrospective_offline(
                sessions, args.repo, args.policy, generator, reviewer, args.out)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        # データ内容・個人情報・ローカルパスを端末出力しない。
        print("ERROR: オフライン二重レビューの入力検査・保存に失敗しました",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
