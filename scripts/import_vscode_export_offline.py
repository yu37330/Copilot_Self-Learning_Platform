#!/usr/bin/env python3
"""VS Code Chat: Export Chat JSONを承認済みローカルファイルから安全に取込/照合する。

本文・原本パスは標準出力しない。外部送信/LLM/AWS/自動Skills修正なし。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData
from copilot_learning.vscode_export_adapter import export_import, verify_import


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("import", "verify"))
    parser.add_argument("--export", type=Path, required=True,
                        help="利用者が手動でエクスポートした1つのJSONファイル")
    parser.add_argument("--policy", type=Path, required=True,
                        help="Git管理外に置いた既存の機密語辞書JSON")
    parser.add_argument("--out", type=Path, required=True, help="Git管理外の新規フォルダ")
    parser.add_argument("--acknowledge-local-import", action="store_true",
                        help="選択したJSON本文をローカル変換することに明示同意")
    parser.add_argument("--expected-receipt-sha256",
                        help="verify時に独立保管した64桁receipt SHAを指定")
    args = parser.parse_args(argv)
    if not args.acknowledge_local_import:
        print("ERROR: エクスポート本文の処理には明示的なローカル同意が必要です",
              file=sys.stderr)
        return 2
    if ((args.command == "verify") != bool(args.expected_receipt_sha256)):
        print("ERROR: verify時だけ独立保管したreceipt SHAが必要です", file=sys.stderr)
        return 2
    try:
        if args.command == "import":
            receipt = export_import(args.export, args.policy, args.out)
        else:
            receipt = verify_import(args.export, args.policy, args.out,
                                    args.expected_receipt_sha256)
        # raw会話・ファイルパス・個人名を表示せず、受理状態とハッシュだけを返す。
        print(json.dumps(receipt, sort_keys=True, ensure_ascii=False))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, UnicodeError, OverflowError):
        print("ERROR: VS Codeエクスポートのローカル取込・検証に失敗しました",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
