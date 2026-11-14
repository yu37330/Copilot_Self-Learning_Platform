#!/usr/bin/env python3
"""Memory全戦略・独自実装・Hybrid・対照群を一括準備する。実行・送信は行わない。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData
from copilot_learning.experiment_suite import build_experiment_suite, export_experiment_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--training-snapshot-sha256", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--budget-tokens", type=int, default=8000)
    parser.add_argument("--context-budget-tokens", type=int, default=2000)
    parser.add_argument("--top-k-total", type=int, default=6)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        path = args.dataset.expanduser().absolute()
        if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents)):
            raise InvalidData("リンクを入力にできません")
        with path.open("rb") as handle:
            raw = handle.read(4_000_001)
        if len(raw) > 4_000_000:
            raise InvalidData("評価データが上限を超えています")
        data = json.loads(raw.decode("utf-8"))
        plan = build_experiment_suite(data, project=args.project,
                                     training_snapshot_sha256=args.training_snapshot_sha256,
                                     model_id=args.model_id, budget_tokens=args.budget_tokens,
                                     context_budget_tokens=args.context_budget_tokens,
                                     top_k_total=args.top_k_total, repetitions=args.repetitions)
        print(json.dumps(export_experiment_suite(plan, args.out), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        print("ERROR: 準備を中止しました。入力・分割・条件・出力先を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
