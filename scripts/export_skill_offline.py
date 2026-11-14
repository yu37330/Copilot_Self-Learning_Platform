#!/usr/bin/env python3
"""activeな知識のSkillプレビュー作成・再検証。実インストールも通信も行わない。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData
from copilot_learning.skill_preview import _object, _read, export_skill_preview, verify_skill_preview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["export", "verify"])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--knowledge-id")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--review", type=Path)
    parser.add_argument("--max-bytes", type=int, default=16384)
    args = parser.parse_args(argv)
    try:
        task = _read(args.task).decode("utf-8")
        review = _object(_read(args.review)) if args.review else None
        if args.operation == "export":
            if args.expected_manifest_sha256:
                raise InvalidData("exportには既存manifestを指定できません")
            result = export_skill_preview(args.db, args.knowledge_id, args.expected_revision,
                                          task, args.policy, args.out, review=review, max_bytes=args.max_bytes)
        else:
            if args.knowledge_id or args.expected_revision is not None:
                raise InvalidData("verifyでは知識IDと版をmanifestから照合します")
            result = verify_skill_preview(args.db, args.out, task, args.policy, review=review,
                                          expected_manifest_sha256=args.expected_manifest_sha256)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, KeyError, RuntimeError):
        # 例外本文にファイルパス、タスク、秘密情報などが混入しないよう固定文だけを出す。
        print("ERROR: プレビューを作成・検証できません。状態・版・適用条件・機密検査・出力先を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
