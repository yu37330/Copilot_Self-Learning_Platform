#!/usr/bin/env python3
"""検証済みCopilotセッションをオフラインで振り返り、指示改善プレビューを作る。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, normalize_events, read_archive
from copilot_learning.session_retro import export_preview, verify_preview
from copilot_learning.skill_preview import _path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, required=True, help="prepare_sessions.pyが作ったローカル保存先")
    parser.add_argument("--fingerprint", action="append", required=True, help="対象セッションのSHA-256。最大32件、複数指定可")
    parser.add_argument("--out", type=Path, required=True, help="Git管理外の新規プレビュー出力先")
    parser.add_argument("--verify", action="store_true", help="出力済みプレビューを再生成して検証")
    parser.add_argument("--expected-manifest-sha256", help="verify用の独立保管済みmanifest SHA-256")
    args = parser.parse_args(argv)
    try:
        if not (1 <= len(args.fingerprint) <= 32):
            raise InvalidData("対象セッションは1〜32件に制限しています")
        if args.verify != bool(args.expected_manifest_sha256):
            raise InvalidData("verify時だけ独立保管したmanifest SHA-256を指定してください")
        staging = _path(args.staging)
        sessions = []
        for fingerprint in args.fingerprint:
            manifest, events = read_archive(staging, fingerprint)
            sessions.append(normalize_events(manifest, events))
        if args.verify:
            receipt = verify_preview(sessions, args.out, args.expected_manifest_sha256)
        else:
            receipt = export_preview(sessions, args.out)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, FileExistsError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        # セッション本文・ファイル内容は表示しない。
        print(f"ERROR: {type(exc).__name__}: プレビュー生成・検証を完了できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
