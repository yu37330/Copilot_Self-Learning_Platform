#!/usr/bin/env python3
"""OKFの構造だけを読み取り検査する。採用承認・外部送信・コード実行はしない。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.okf import MAX_FILES, MAX_DOCUMENT_BYTES, MAX_BUNDLE_BYTES, validate_okf_files
from copilot_learning.wiki import ROOT, _path


def inspect_directory(directory: Path) -> dict:
    root = _path(directory)
    if not root.is_dir():
        raise InvalidData("バンドルのディレクトリが必要です")
    files: dict[str, bytes] = {}
    stack = [(root, 0)]
    total = 0
    entry_count = 0
    while stack:
        current, depth = stack.pop()
        if depth > 16:
            raise InvalidData("バンドルの階層が深すぎます")
        for path in current.iterdir():
            entry_count += 1
            if entry_count > MAX_FILES:
                raise InvalidData("バンドルの件数が上限を超えています")
            _path(path)
            if path.is_dir():
                stack.append((path, depth + 1))
            elif path.is_file():
                with path.open("rb") as handle:
                    data = handle.read(MAX_DOCUMENT_BYTES + 1)
                total += len(data)
                if len(data) > MAX_DOCUMENT_BYTES or total > MAX_BUNDLE_BYTES:
                    raise InvalidData("バンドルの容量が上限を超えています")
                files[path.relative_to(root).as_posix()] = data
            else:
                raise InvalidData("通常のファイルとフォルダだけが必要です")
    return validate_okf_files(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = inspect_directory(args.directory)
        if args.out:
            out = _path(args.out)
            assert_external_output(out, ROOT)
            directory = _path(args.directory)
            if out == directory or directory in out.parents:
                raise InvalidData("検査レポートは検査対象の外へ保存してください")
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("xb") as handle:
                handle.write(json_bytes(report))
            out.chmod(0o600)
        print(json.dumps({"conformant": report["conformant"], "concept_count": report["concept_count"],
                          "errors": len(report["errors"]), "warnings": len(report["warnings"]),
                          "check_scope": report["check_scope"], "network_calls": 0}, ensure_ascii=False))
        return 0 if report["conformant"] else 1
    except (InvalidData, OSError, ValueError, TypeError, KeyError):
        print("ERROR: OKF形式・入力・依存関係・保存先を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
