#!/usr/bin/env python3
"""CLI/VS Codeのセッション所在をメタデータだけで点検する（会話本文は読まない）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _directory(path: Path) -> dict:
    """名前・絶対パス・ファイル内容は記録しない。"""
    path = Path(path).expanduser()
    if not path.exists():
        return {"status": "absent", "directory_count": None, "event_logs": None}
    if path.is_symlink() or not path.is_dir():
        return {"status": "untrusted_path", "directory_count": None, "event_logs": None}
    directories = 0
    event_logs = 0
    for item in path.iterdir():
        if item.is_symlink():
            # リンク経由のアクセスを禁止する（収集対象からも除外）。
            continue
        if item.is_dir():
            directories += 1
            log = item / "events.jsonl"
            if log.is_file() and not log.is_symlink():
                event_logs += 1
            if directories > 100000:
                return {"status": "count_limit", "directory_count": None, "event_logs": None}
    return {"status": "directory_present_metadata_only",
            "directory_count": directories, "event_logs": event_logs}


def _export_directory(path: Path) -> dict:
    path = Path(path).expanduser()
    if not path.exists():
        return {"status": "absent", "json_files": None,
                "format_support": "unverified_schema"}
    if path.is_symlink() or not path.is_dir():
        return {"status": "untrusted_path", "json_files": None,
                "format_support": "unverified_schema"}
    found = 0
    for child in path.iterdir():
        if child.is_file() and not child.is_symlink() and child.suffix.casefold() == ".json":
            found += 1
            if found > 10000:
                return {"status": "count_limit", "json_files": None,
                        "format_support": "unverified_schema"}
    return {"status": "directory_present_metadata_only", "json_files": found,
            "format_support": "manual_export_supported_parser_pending"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-root", type=Path, default=Path.home() / ".copilot" / "session-state")
    parser.add_argument("--vscode-export-dir", type=Path,
                        help="VS Codeの Chat: Export Chat... で手動保存したJSONのフォルダ")
    args = parser.parse_args(argv)
    try:
        output = {
            "schema_version": "1.0", "record_type": "local-session-source-probe",
            "cli": _directory(args.cli_root),
            "vscode_export": _export_directory(args.vscode_export_dir)
            if args.vscode_export_dir is not None else {"status": "not_requested",
                                                          "json_files": None,
                                                          "format_support": "unverified_schema"},
            "session_content_read": False,
            "vscode_export_parsed": False,
            "external_transfer_authorized": False,
            "network_calls": 0,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except (PermissionError, OSError, ValueError):
        print("ERROR: セッションの所在を安全に点検できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
