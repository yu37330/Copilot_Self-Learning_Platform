#!/usr/bin/env python3
"""指定したCodex rollout 1ファイルのJSON構造をローカルだけで点検する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.codex_schema_probe import inspect_structure
from copilot_learning.core import InvalidData


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollout", type=Path, required=True)
    parser.add_argument("--minimum-age-minutes", type=int, default=1440)
    parser.add_argument("--acknowledge-local-inspection", action="store_true")
    parser.add_argument("--include-telemetry", action="store_true",
                        help="本文を出さず既知のイベント/累積トークン/異常件数を集計")
    args = parser.parse_args(argv)
    try:
        # No read until affirmative opt-in.
        if not args.acknowledge_local_inspection:
            raise InvalidData("ローカル構造の診断には明示同意が必要です")
        result = inspect_structure(
            args.rollout, minimum_age_minutes=args.minimum_age_minutes,
            include_telemetry=args.include_telemetry)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (InvalidData, OSError, UnicodeError, ValueError, TypeError):
        # No raw path, user text, command, or exception strings.
        print("ERROR: Codexの構造診断を完了できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
