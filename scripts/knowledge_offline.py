#!/usr/bin/env python3
"""知識台帳のローカル操作。社内SSO連携・Copilot自動投入は行わない。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData
from copilot_learning.knowledge_store import KnowledgeStore
from copilot_learning.retrieval import search_active


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True, help="Gitリポジトリ外のSQLite台帳")
    commands = p.add_subparsers(dest="command", required=True)
    add = commands.add_parser("register", help="候補をproposedで登録（自動適用なし）")
    add.add_argument("--candidate", type=Path, required=True, help="grade_candidatesが出した単一候補JSON")
    add.add_argument("--source", type=Path, required=True, help="原本SHA付き出典JSON")
    change = commands.add_parser("transition", help="状態を手動変更")
    change.add_argument("--id", required=True)
    change.add_argument("--to", required=True, choices=["pending", "active", "retired", "superseded"])
    change.add_argument("--expected-revision", required=True, type=int)
    change.add_argument("--reviewer", required=True)
    change.add_argument("--reason", required=True)
    change.add_argument("--replacement-id")
    change.add_argument("--confirm-manual-review", action="store_true")
    query = commands.add_parser("search", help="activeな知識のみ検索（結果は提案で自動適用しない）")
    query.add_argument("--task", required=True)
    query.add_argument("--path", action="append", default=[])
    query.add_argument("--limit", type=int, default=5)
    hist = commands.add_parser("history", help="版管理の履歴")
    hist.add_argument("--id", required=True)
    args = p.parse_args(argv)
    try:
        store = KnowledgeStore(args.db)
        if args.command == "register":
            data = json.loads(args.candidate.read_text("utf-8"))
            source = json.loads(args.source.read_text("utf-8"))
            result = store.register(data, source)
        elif args.command == "transition":
            if not args.confirm_manual_review:
                raise InvalidData("状態変更には --confirm-manual-review が必要です（社内承認の代替ではありません）")
            result = store.transition(args.id, args.to, expected_revision=args.expected_revision,
                                      reviewer=args.reviewer, reason=args.reason,
                                      replacement_id=args.replacement_id)
        elif args.command == "search":
            result = search_active(store, args.task, limit=args.limit, changed_paths=args.path)
        else:
            result = store.history(args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (InvalidData, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
