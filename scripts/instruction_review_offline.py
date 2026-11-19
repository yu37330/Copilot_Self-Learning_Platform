#!/usr/bin/env python3
"""Issue #31 Phase B: 外部LLMを使わず、根拠付き指示改善要求と差分プレビューを作成する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, normalize_events, read_archive
from copilot_learning.instruction_review import (
    export_review, export_review_request, verify_review,
)
from copilot_learning.skill_preview import _path


def _load_response(path: Path) -> dict:
    source = _path(path)
    if not source.is_file() or source.stat().st_size > 64 * 1024:
        raise InvalidData("オフライン改善案JSONが存在しないかサイズ超過です")
    with source.open("rb") as handle:
        data = handle.read(64 * 1024 + 1)
    if len(data) > 64 * 1024:
        raise InvalidData("オフライン改善案JSONがサイズ超過です")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise InvalidData("オフライン改善案はJSONオブジェクトで指定してください")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("request", "grade", "verify"))
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--fingerprint", action="append", required=True)
    parser.add_argument("--repo", type=Path, required=True, help="改善対象リポジトリのローカルpath（読み取り専用）")
    parser.add_argument("--policy", type=Path, required=True, help="機密語辞書JSON（Git管理外）")
    parser.add_argument("--out", type=Path, required=True, help="Git管理外の新規レビュー出力先")
    parser.add_argument("--proposal", type=Path, help="人手・外部レビュー担当が別途作成した提案JSON")
    parser.add_argument("--expected-manifest-sha256", help="独立保管したmanifest SHA（verify時のみ）")
    args = parser.parse_args(argv)
    try:
        if not 1 <= len(args.fingerprint) <= 32:
            raise InvalidData("セッション数の上限を超えています")
        if args.command == "request" and (args.proposal or args.expected_manifest_sha256):
            raise InvalidData("requestでは提案やmanifest SHAを受け付けません")
        if args.command == "grade" and (args.proposal is None or args.expected_manifest_sha256 is not None):
            raise InvalidData("gradeでは提案JSONが必要です")
        if args.command == "verify" and (args.proposal is None or args.expected_manifest_sha256 is None):
            raise InvalidData("verifyでは提案JSONとmanifest SHAが必要です")
        staging = _path(args.staging)
        sessions = []
        for fp in args.fingerprint:
            manifest, events = read_archive(staging, fp)
            sessions.append(normalize_events(manifest, events))
        if args.command == "request":
            result = export_review_request(sessions, args.repo, args.policy, args.out)
        else:
            response = _load_response(args.proposal)
            if args.command == "grade":
                result = export_review(sessions, args.repo, args.policy, response, args.out)
            else:
                result = verify_review(sessions, args.repo, args.policy, response, args.out,
                                       args.expected_manifest_sha256)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (InvalidData, ValueError, OSError, TypeError, UnicodeError, json.JSONDecodeError):
        # ファイルパスや本文を含む例外文字列は表示しない。
        print("ERROR: 指示改善のオフライン検査を完了できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
