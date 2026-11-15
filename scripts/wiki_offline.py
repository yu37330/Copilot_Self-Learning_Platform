#!/usr/bin/env python3
"""2方式のWiki準備/構築/検証/検索と比較計画。外部APIは呼ばない。"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, json_bytes
from copilot_learning.privacy_scan import load_policy
from copilot_learning.wiki import (ROOT, _path, read_json, source_snapshot, wiki_prompt,
                                    compile_wiki, export_wiki, verify_wiki, query_wiki, migrate_wiki_to_okf)
from copilot_learning.wiki_matrix import build_wiki_matrix


def write_new(path: Path, data: dict) -> None:
    from copilot_learning.wiki import RESERVED
    path = _path(path)
    assert_external_output(path, ROOT)
    if any(p.rstrip(" .").casefold() in RESERVED for p in path.parts):
        raise InvalidData("自動読込先は禁止です")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(json_bytes(data))
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=["prompt", "build", "verify", "query", "matrix", "migrate-okf"])
    for name in ("input", "policy", "proposal", "previous", "out", "directory", "base-suite"):
        p.add_argument("--" + name, type=Path)
    p.add_argument("--mode", choices=["agentcore", "original"])
    p.add_argument("--input-sha256")
    p.add_argument("--training-sha256")
    p.add_argument("--project")
    p.add_argument("--manifest-sha256")
    p.add_argument("--query")
    p.add_argument("--generator", choices=["synthetic", "imported_model_output"], default="synthetic")
    args = p.parse_args(argv)
    try:
        if args.operation == "matrix":
            write_new(args.out, build_wiki_matrix(read_json(args.base_suite)))
            print("OFFLINE: Wiki比較計画のみ。実行結果は未測定です。")
            return 0
        terms, _ = load_policy(args.policy)
        snapshot = source_snapshot(args.mode, read_json(args.input), expected_input_sha256=args.input_sha256,
                                   project=args.project, training_sha256=args.training_sha256,
                                   sensitive_literals=terms["sensitive_literals"])
        if args.operation == "prompt":
            previous = read_json(args.previous) if args.previous else None
            write_new(args.out, wiki_prompt(snapshot, previous))
            result = {"status": "prompt_prepared_not_executed"}
        elif args.operation == "build":
            previous = read_json(args.previous) if args.previous else None
            revision = compile_wiki(snapshot, read_json(args.proposal), sensitive_literals=terms["sensitive_literals"],
                                    generator=args.generator, previous=previous)
            result = export_wiki(revision, args.out)
        elif args.operation == "migrate-okf":
            result = migrate_wiki_to_okf(args.directory, snapshot, args.out,
                                         expected_manifest_sha256=args.manifest_sha256,
                                         sensitive_literals=terms["sensitive_literals"])
        else:
            result = verify_wiki(args.directory, snapshot, expected_manifest_sha256=args.manifest_sha256,
                                 sensitive_literals=terms["sensitive_literals"])
            if args.operation == "query":
                result["matches"] = query_wiki(read_json(args.directory / "wiki.json"), snapshot, args.query,
                                               sensitive_literals=terms["sensitive_literals"])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, KeyError, AttributeError):
        print("ERROR: Wiki入力・出典・ハッシュ・機密検査・出力先を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
