#!/usr/bin/env python3
"""Copilot/AWS無しで、合成セッションから比較レポートまで一括動作確認。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from scripts.prepare_sessions import prepare
from scripts.run_learning_poc import process


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Git管理外、空のデモ出力ディレクトリ")
    args = parser.parse_args()
    try:
        assert_external_output(args.out, Path(__file__).resolve().parents[1])
        source = args.out / "source" / "synthetic-session"
        source.mkdir(parents=True, exist_ok=False)
        content = [
            {"type": "message", "role": "user", "text": "Pythonのテストで仮想環境が一致しない", "timestamp": "2026-11-01T10:00:00Z"},
            {"type": "message", "role": "assistant", "text": "仮想環境を確認し、再実行します", "timestamp": "2026-11-01T10:01:00Z"},
            {"type": "message", "role": "tool", "text": "12 tests passed", "timestamp": "2026-11-01T10:02:00Z"},
        ]
        file = source / "events.jsonl"
        file.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in content) + "\n", encoding="utf-8")
        os.utime(file, (1700000000, 1700000000))
        archive = prepare(source, args.out / "staging", minimum_age_minutes=30)
        fingerprint = archive.stem.removeprefix("session-")
        proposals = {"candidates": [{
            "knowledge_type": "note", "title": "仮想環境確認",
            "description": "テスト前に環境を確認", "instruction": "Python実行環境を確認してからテストする",
            "generalization": "Pythonプロジェクト全般で利用可能",
            "evidence_ids": [f"{fingerprint}:1", f"{fingerprint}:2"]}]}
        candidate_path = args.out / "input_candidates.json"
        candidate_path.write_bytes(json_bytes(proposals))
        feedback_path = args.out / "input_feedback.json"
        feedback_path.write_bytes(json_bytes({
            "session_fingerprint": fingerprint, "feedback_id": "synthetic-feedback",
            "outcome": "verified_success", "reusable_lesson": "テスト前の環境確認",
            "evidence": "合成テストの12件成功を確認",
            "data_classification": "synthetic"}))
        report = process(args.out / "staging", fingerprint, args.out / "results",
                         memory_id="REPLACE_WITH_REAL_MEMORY_ID", actor_id="copilot_poc_actor",
                         feedback_file=feedback_path, candidate_file=candidate_path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("これは合成入力によるオフライン検査です。AgentCore/Bedrockの推論は実行していません。")
        return 0
    except (OSError, InvalidData, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
