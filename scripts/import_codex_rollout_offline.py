#!/usr/bin/env python3
"""Codexのrollout JSONLをオフラインだけで取込/再検証する（Issue #46）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.codex_rollout import import_rollout, verify_rollout
from copilot_learning.core import InvalidData


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("import", "verify"))
    parser.add_argument("--rollout", type=Path, required=True,
                        help="利用者が選択した1つのrollout-*.jsonlファイル")
    parser.add_argument("--policy", type=Path, required=True,
                        help="ローカルの機密語辞書（offline-sensitive-literals）")
    parser.add_argument("--out", type=Path, required=True,
                        help="Git管理外の新規保存フォルダ（ZIPに選別した会話本文を含む）")
    parser.add_argument("--minimum-age-minutes", type=int, default=5,
                        help="最終更新から最低限経過している必要のある分数（1以上）")
    parser.add_argument("--acknowledge-private-local-import", action="store_true",
                        help="個人/組織規程に従い、選択したrolloutをローカルで読むことを認める")
    parser.add_argument("--expected-receipt-sha256", help="verify時の独立保管したレシートSHA")
    args = parser.parse_args(argv)
    try:
        # 明示的に同意されるまでは本文も辞書も開かない。
        if not args.acknowledge_private_local_import:
            raise InvalidData("プライベート履歴の分析には明示的な同意が必要です")
        if ((args.action == "verify") != bool(args.expected_receipt_sha256)):
            raise InvalidData("verify時のみ独立SHAを指定してください")
        if args.action == "import":
            result = import_rollout(args.rollout, args.policy, args.out,
                                    min_age_minutes=args.minimum_age_minutes)
        else:
            result = verify_rollout(
                args.rollout, args.policy, args.out,
                expected_receipt_sha256=args.expected_receipt_sha256,
                min_age_minutes=args.minimum_age_minutes)
        # 本文・コマンド・ファイル名・任意モデル/セッションIDは表示しない。
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        print("ERROR: Codexのrolloutのローカル取込・検証に失敗しました", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
