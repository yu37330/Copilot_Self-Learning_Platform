#!/usr/bin/env python3
"""Issue #31 Phase C: 変更前後の提出済み測定値を、実行なし・通信なしで比較する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.instruction_effect_eval import (
    blank_observations, evaluate_instruction_effect,
)
from copilot_learning.privacy_scan import load_policy
from copilot_learning.skill_preview import ROOT, RESERVED, _path

MAX_INPUT_BYTES = 2 * 1024 * 1024


def _read_json(path: Path) -> dict:
    source = _path(path)
    if not source.is_file() or source.stat().st_size > MAX_INPUT_BYTES:
        raise InvalidData("入力は通常の小さいJSONファイルが必要です")
    raw = source.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise InvalidData("入力JSONがサイズ上限を超えています")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise InvalidData("トップレベルはJSONオブジェクトが必要です")
    return document


def _destination(path: Path) -> Path:
    out = _path(path)
    assert_external_output(out, ROOT)
    if any(part.rstrip(" .").casefold() in RESERVED for part in out.parts):
        raise InvalidData("自動読込ディレクトリには保存できません")
    if out.exists():
        raise InvalidData("既存の評価ファイルを上書きしません")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("blank", "evaluate"))
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--observations", type=Path, help="evaluateで指定するオフライン観測JSON")
    parser.add_argument("--policy", type=Path, help="ローカルに保存した機密語辞書JSON")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if (args.command == "evaluate") != (args.observations is not None):
            raise InvalidData("evaluate時だけ観測ファイルが必要です")
        dataset = _read_json(args.dataset)
        # 実データの扱いはGit管理外とローカルポリシーの指定が必須。
        if dataset.get("label_source") != "synthetic":
            assert_external_output(_path(args.dataset), ROOT)
            if args.policy is None:
                raise InvalidData("実データ評価にはローカルの機密語辞書が必要です")
        literals = load_policy(_path(args.policy))[0]["sensitive_literals"] if args.policy else []
        if args.command == "blank":
            result = blank_observations(dataset, sensitive_literals=literals)
        else:
            obs = _read_json(args.observations)
            if any(isinstance(v, dict) and v.get("source_kind") != "synthetic"
                   for v in (obs.get("arms") or {}).values()):
                assert_external_output(_path(args.observations), ROOT)
            result = evaluate_instruction_effect(dataset, obs, sensitive_literals=literals)
        body = json_bytes(result)
        out = _destination(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        _path(out.parent)
        with out.open("xb") as handle:
            handle.write(body)
            handle.flush()
        print(json.dumps({
            "status": result.get("comparison_status", "blank_not_executed"),
            "record_type": result["record_type"],
            "observed_cases": result.get("n_cases"),
            "network_calls": 0,
            "real_copilot_execution_verified": False,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        # ファイル内容・個人情報をログへ出さない。
        print("ERROR: オフライン指示改善評価の入力検証または出力に失敗しました", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
