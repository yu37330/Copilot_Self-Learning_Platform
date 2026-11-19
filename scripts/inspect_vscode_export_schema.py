#!/usr/bin/env python3
"""VS Codeの手動 Chat: Export Chat JSON の形だけをローカルで診断する。

JSON本文は標準出力・ファイルへ書かない。未知の構造を会話として推測変換しない。
ユーザーが選択したファイルのみに限定し、明示的なopt-inを必要とする。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_REQUESTS = 10000


def inspect_export(path: Path) -> dict:
    """固定キーの存在・型を確認し、任意のテキストや値は表示しない。"""
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("選択したJSONファイルを読み取れません")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("JSONファイルが容量上限を超えています")
    value = json.loads(raw.decode("utf-8"))
    root = value if isinstance(value, dict) else {}
    requests = root.get("requests")
    request_count = len(requests) if isinstance(requests, list) else None
    if request_count is not None and request_count > MAX_REQUESTS:
        raise ValueError("JSONリクエスト数の上限を超えています")
    first = requests[0] if isinstance(requests, list) and requests and isinstance(requests[0], dict) else {}
    message = first.get("message")
    response = first.get("response")
    msg_obj = message if isinstance(message, dict) else {}
    response0 = response[0] if isinstance(response, list) and response else None
    return {
        "schema_version": "1.0", "record_type": "vscode-export-shape-only",
        "file_kind": "json", "top_level_object": isinstance(value, dict),
        "has_session_id": isinstance(root.get("sessionId"), str),
        "has_requests_array": isinstance(requests, list),
        "request_count": request_count,
        "first_request_object": bool(first),
        "first_message_text_string": isinstance(msg_obj.get("text"), str),
        "first_message_string": isinstance(message, str),
        "first_response_array": isinstance(response, list),
        "first_response_string": isinstance(response, str),
        "first_response_element_object": isinstance(response0, dict),
        "first_response_element_string": isinstance(response0, str),
        "session_content_exported": False,
        "normalized_events_produced": False,
        "format_status": "schema_review_required_no_conversion",
        "real_copilot_compatibility_verified": False,
        "network_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, type=Path,
                        help="利用者がChat: Export Chat...で明示的に保存したJSON")
    parser.add_argument("--acknowledge-local-inspection", action="store_true",
                        help="選択したファイルをローカルで読むことを明示承認する")
    args = parser.parse_args(argv)
    if not args.acknowledge_local_inspection:
        print("ERROR: ローカルJSONの構造検査には明示承認が必要です", file=sys.stderr)
        return 2
    try:
        print(json.dumps(inspect_export(args.export), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, UnicodeError, TypeError, json.JSONDecodeError):
        print("ERROR: JSON構造のローカル検査に失敗しました（内容は表示しません）", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
