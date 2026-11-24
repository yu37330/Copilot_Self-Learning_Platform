#!/usr/bin/env python3
"""自宅用 OpenAI API / Codex CLI / Copilot CLI のブラウザテスト画面。

実行例:
  py -3.12 .\scripts\start_personal_model_ui.py
  py -3.12 .\scripts\start_personal_model_ui.py --allow-live --policy C:\private\policy.json

本人が画面で送信先と本文を確認しない限りモデルへ送信しない。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copilot_learning.core import InvalidData, json_bytes
from copilot_learning.personal_model_webui import make_server
from copilot_learning.privacy_scan import load_policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--policy", type=Path, help="Git管理外の機密語辞書JSON")
    parser.add_argument("--allow-live", action="store_true",
                        help="実API/CLI送信をUI内の個別承認後に許可する")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="local-model-ui-private-") as temp:
            if args.policy is None:
                # Empty dictionary is for synthetic tests only and does NOT
                # represent a corporate DLP check.
                policy = Path(temp) / "policy.json"
                policy.write_bytes(json_bytes({
                    "schema_version": "1.0",
                    "record_type": "offline-sensitive-literals",
                    "sensitive_literals": [],
                }))
            else:
                policy = args.policy
            load_policy(policy)
            with make_server(policy, port=args.port, allow_live=args.allow_live) as server:
                url = f"http://127.0.0.1:{server.server_port}/"
                print(f"Personal Model Test UI: {url}")
                print("送信: " + ("個別承認した場合のみ可能" if args.allow_live else "無効（モックのみ）"))
                if args.policy is None:
                    print("機密辞書: 合成テスト用の空辞書（組織DLPではありません）")
                print("終了: Ctrl+C")
                if not args.no_browser:
                    webbrowser.open(url, new=2)
                try:
                    server.serve_forever(poll_interval=0.2)
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
        return 0
    except (InvalidData, OSError, ValueError):
        print("ERROR: ローカルUIを起動できません。ポート・policy形式・保存場所を確認。",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
