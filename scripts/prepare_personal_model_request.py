#!/usr/bin/env python3
"""既存Phase Bのオフラインレビュー資料を個人用LLM入力の下書きへ変換する。

重要：生成した下書きは未検査・未許可で、実送信できない。
個人/合成であることを本人が目視確認してから2項目を編集し、
その後にpreflightと個別の送信スコープ承認を行う。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.local_llm_router import read_private_json
from copilot_learning.skill_preview import ROOT, _path


def draft_from_artifact(source: dict, role: str) -> dict:
    if not isinstance(source, dict) or not isinstance(source.get("system"), str):
        raise InvalidData("レビュー原票にシステム指示がありません")
    if source.get("external_transfer_authorized") is not False:
        raise InvalidData("オフラインレビュー原票の安全指定が不一致")
    if role == "generator":
        if (source.get("record_type") != "offline-retro-review-request"
                or not isinstance(source.get("data"), dict)):
            raise InvalidData("生成用のPhase B原票ではありません")
        body = {"source_record_type": source["record_type"], "data": source["data"]}
    elif role == "reviewer":
        if (source.get("record_type") != "offline-retro-independent-review-request"
                or not isinstance(source.get("candidates"), list)):
            raise InvalidData("独立Reviewer用の原票ではありません")
        from copilot_learning.core import digest
        body = {"source_record_type": source["record_type"],
                "request_sha256": digest(json_bytes(source)),
                "candidates": source["candidates"]}
    else:
        raise InvalidData("生成/独立評価のロールが不正")
    return {
        "schema_version": "1.0", "record_type": "personal-local-model-request",
        "track": "retrospective", "role": role,
        "system": source["system"],
        "prompt": json.dumps(body, ensure_ascii=False, sort_keys=True),
        # Fail closed until the owner has reviewed every field and origin.
        "source_classification": "UNREVIEWED_REQUIRES_HUMAN_CLASSIFICATION",
        "manual_content_reviewed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--role", choices=("generator", "reviewer"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--acknowledge-local-copy", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.acknowledge_local_copy:
            raise InvalidData("ローカル下書き作成への同意が必要")
        source, _ = read_private_json(args.artifact)
        draft = draft_from_artifact(source, args.role)
        out = _path(args.out)
        assert_external_output(out, ROOT)
        if out.suffix.lower() != ".json" or out.exists():
            raise InvalidData("Git管理外に存在しないJSONファイルを指定してください")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("xb") as f:
            f.write(json_bytes(draft))
            f.flush()
            os.fsync(f.fileno())
        out.chmod(0o400)
        print(json.dumps({"status": "draft_only_not_sendable_requires_manual_review",
                          "role": args.role, "model_calls": 0, "network_calls": 0},
                         ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, OSError, UnicodeError, ValueError, TypeError):
        print("ERROR: 個人用モデル入力の下書きを作成できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
