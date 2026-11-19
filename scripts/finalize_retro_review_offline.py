#!/usr/bin/env python3
"""既存2モデル計画の最終意見をPhase Bの未適用diffに反映・再検証する。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData
from copilot_learning.retro_review_finalizer import export_final_review, verify_final_review
from scripts.dual_model_review_offline import _json_file, _sessions


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("export", "verify"))
    p.add_argument("--staging", type=Path, required=True)
    p.add_argument("--fingerprint", action="append", required=True)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--model-plan", type=Path, required=True)
    p.add_argument("--proposals", type=Path, required=True)
    p.add_argument("--reviewer-output", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--expected-manifest-sha256")
    args = p.parse_args(argv)
    try:
        if (args.command == "verify") != (args.expected_manifest_sha256 is not None):
            raise InvalidData("verify時にのみ独立保管したmanifest SHAが必要です")
        sessions = _sessions(args.staging, args.fingerprint)
        plan = _json_file(args.model_plan)
        proposals = _json_file(args.proposals)
        opinion = _json_file(args.reviewer_output)
        if args.command == "verify":
            result = verify_final_review(
                sessions, args.repo, args.policy, proposals, plan, opinion,
                args.out, args.expected_manifest_sha256)
        else:
            result = export_final_review(
                sessions, args.repo, args.policy, proposals, plan, opinion, args.out)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        # 個人の会話・ファイル名・モデル応答を標準エラーに出さない。
        print("ERROR: 独立レビュー最終検査を完了できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
