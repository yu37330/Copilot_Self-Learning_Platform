#!/usr/bin/env python3
"""Codexセッション保存先の存在/件数/合計サイズを内容・名前の非表示で確認する。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData
from copilot_learning.skill_preview import _path

MAX_FILES = 25000


def probe(root: Path) -> dict:
    source = _path(root)
    if not source.exists():
        return {"status": "absent", "rollout_count": None,
                "rollout_total_bytes": None, "content_read": False}
    if not source.is_dir():
        raise InvalidData("対象はCodexセッションディレクトリである必要があります")
    count = 0
    size = 0
    for base, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs
                   if not (Path(base) / name).is_symlink()]
        for name in names:
            if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                continue
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            count += 1
            size += path.stat().st_size
            if count > MAX_FILES:
                raise InvalidData("rollout数が診断上限を超えています")
    return {"status": "metadata_only", "rollout_count": count,
            "rollout_total_bytes": size, "content_read": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / ".codex" / "sessions")
    args = parser.parse_args(argv)
    try:
        result = probe(args.root)
        print(json.dumps({"schema_version": "1.0",
                          "record_type": "codex-sessions-metadata-probe",
                          **result, "network_calls": 0}, sort_keys=True))
        return 0
    except (InvalidData, OSError, UnicodeError, ValueError):
        print("ERROR: Codex保存先のメタデータ診断を完了できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
