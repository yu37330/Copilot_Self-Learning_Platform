#!/usr/bin/env python3
"""保存済みSQLite知識台帳の検索品質を、合成Hold-outデータで評価する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.knowledge_store import KnowledgeStore
from copilot_learning.retrieval_evals import evaluate_retrieval


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        if not args.db.is_file():
            raise InvalidData("既存のSQLite台帳を指定してください")
        result = evaluate_retrieval(KnowledgeStore(args.db), json.loads(args.cases.read_text("utf-8")), top_k=args.top_k)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as file:
            file.write(json_bytes(result))
        print(f"検索評価を保存しました（外部通信なし）: {args.out}")
        return 0
    except (InvalidData, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
