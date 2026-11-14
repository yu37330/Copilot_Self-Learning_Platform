#!/usr/bin/env python3
"""手動の知識試行・結果・訂正をローカル記録する。通信・Skill配布・自動昇格なし。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.usage_feedback import (KINDS, OUTCOMES, begin_trial,
                                              record_result, usage_report)
from copilot_learning.skill_preview import _read, _object
from copilot_learning.core import InvalidData


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    start = sub.add_parser("begin")
    start.add_argument("--db", type=Path, required=True)
    start.add_argument("--preview", type=Path, required=True)
    start.add_argument("--task", type=Path, required=True)
    start.add_argument("--policy", type=Path, required=True)
    start.add_argument("--expected-manifest-sha256", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--source-kind", choices=sorted(KINDS), required=True)
    start.add_argument("--review", type=Path)
    finish = sub.add_parser("result")
    finish.add_argument("--trial-id", required=True)
    finish.add_argument("--expected-trial-sha256", required=True)
    finish.add_argument("--expected-revision", type=int, required=True)
    finish.add_argument("--previous-result-sha256")
    finish.add_argument("--outcome", choices=sorted(OUTCOMES), required=True)
    finish.add_argument("--used", action="store_true")
    finish.add_argument("--evidence", type=Path)
    finish.add_argument("--policy", type=Path, required=True)
    finish.add_argument("--reporter", required=True)
    sub.add_parser("report")
    for command in sub.choices.values():
        command.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "begin":
            data = begin_trial(args.journal, args.db, args.preview, _read(args.task).decode("utf-8"), args.policy,
                               expected_manifest_sha256=args.expected_manifest_sha256, run_id=args.run_id,
                               source_kind=args.source_kind,
                               review=_object(_read(args.review)) if args.review else None)
        elif args.operation == "result":
            data = record_result(args.journal, args.trial_id, expected_trial_sha256=args.expected_trial_sha256,
                                 expected_revision=args.expected_revision, outcome=args.outcome,
                                 knowledge_used=args.used, policy=args.policy, reporter=args.reporter,
                                 evidence=args.evidence, previous_result_sha256=args.previous_result_sha256)
        else:
            data = usage_report(args.journal)
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print("ERROR: 試行・結果を記録できません。対象版、証拠、機密辞書、履歴を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
