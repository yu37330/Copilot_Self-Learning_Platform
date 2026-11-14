#!/usr/bin/env python3
"""合成知識の登録→手動active化→Skillプレビュー→再検証。AWS/Copilot不要。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.knowledge_store import KnowledgeStore
from copilot_learning.skill_preview import (RESERVED, _path, export_skill_preview, verify_skill_preview)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = _path(args.out)
        assert_external_output(root, Path(__file__).resolve().parents[1])
        if root.exists() or any(p.rstrip(" .").casefold() in RESERVED for p in root.parts):
            raise InvalidData("新規のGit管理外フォルダを指定してください")
        root.mkdir(parents=True, mode=0o700)
        store = KnowledgeStore(root / "synthetic-ledger.sqlite")
        item = {
            "knowledge_type": "skill", "title": "Python環境不一致の確認", "description": "Pythonの仮想環境不一致を調査する参考手順",
            "instruction": "テスト記録のPythonインタープリターと仮想環境名を比較し、差異を報告する。",
            "generalization": "Python実行環境が異なるタスクへの適用を検証するための合成候補",
            "evidence_ids": ["a" * 64 + ":0"], "applies_when": ["Python", "仮想環境不一致"],
            "not_when": ["API仕様不一致"], "counterexamples": ["環境は正常"],
            "decision": "keep", "approved_for_copilot": False,
        }
        row = store.register(item, {"session_fingerprint": "a" * 64, "archive_sha256": "b" * 64})
        for state in ("pending", "active"):
            row = store.transition(row["knowledge_id"], state, expected_revision=row["revision"],
                                    reviewer="synthetic-demo-reviewer", reason="合成データの状態遷移デモ")
        policy = root / "synthetic-policy.json"
        policy.write_bytes(json_bytes({"schema_version": "1.0", "record_type": "offline-sensitive-literals",
                                       "sensitive_literals": ["合成機密語"]}))
        task = "Pythonの仮想環境不一致を調査する"
        (root / "synthetic-task.txt").write_text(task, encoding="utf-8")
        receipt = export_skill_preview(root / "synthetic-ledger.sqlite", row["knowledge_id"], row["revision"],
                                       task, policy, root / "preview")
        verified = verify_skill_preview(root / "synthetic-ledger.sqlite", root / "preview", task, policy,
                                        expected_manifest_sha256=receipt["manifest_sha256"])
        result = {"data_source": "synthetic_fixture_not_learned", "export": receipt, "verification": verified,
                  "copilot_invocations": 0, "aws_invocations": 0, "real_improvement_verified": False}
        (root / "demo-result.json").write_bytes(json_bytes(result))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, KeyError, RuntimeError):
        print("ERROR: 合成デモを完了できません。新規のGit管理外フォルダを指定してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
