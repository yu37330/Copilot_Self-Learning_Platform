#!/usr/bin/env python3
"""同じ合成原本を使うWM/WOのWikiを生成。LLM・AWSの実行ではない。"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, digest, json_bytes, assert_external_output
from copilot_learning.wiki import (ROOT, RESERVED, _path, source_snapshot, wiki_prompt, compile_wiki,
                                    export_wiki, verify_wiki, query_wiki, lint_wiki)


def demo(destination: Path) -> dict:
    out = _path(destination)
    assert_external_output(out, ROOT)
    if out.exists() or any(p.casefold() in RESERVED for p in out.parts):
        raise InvalidData("新規のGit外フォルダを指定してください")
    terms = ["合成社外秘案件"]
    text = "Pythonのテストは仮想環境を揃えると成功した。API仕様の変更にはこの手順を適用しない。"
    raw = {"documents": [{"source_id": "event01", "title": "合成開発経験",
                           "text": text, "sha256": digest(text.encode())}]}
    training_sha = digest(json_bytes(raw))
    memory = {"memory_id": "DemoMemory-1234567890", "namespace": "/actor/demo/",
              "strategy_ids": ["Episodic-123"], "response": {"memoryRecordSummaries": [
                  {"memoryRecordId": "memory01", "memoryStrategyId": "Episodic-123",
                   "namespaces": ["/actor/demo/reflections/"], "content": {"text": text}}]}}
    result = {"source_kind": "synthetic", "network_calls": 0, "real_quality_verified": False, "variants": {}}
    out.mkdir(parents=True, mode=0o700)
    # 原本と模擬Memoryの内容が同じなのはデモのため。性能比較には使用しない。
    for mode, data in (("original", raw), ("agentcore", memory)):
        snap = source_snapshot(mode, data, expected_input_sha256=digest(json_bytes(data)),
                               project="demo", training_sha256=training_sha, sensitive_literals=terms)
        proposal = {"source_snapshot_sha256": digest(json_bytes(snap)), "pages": [{
            "page_id": "python-env", "title": "Python仮想環境の確認", "links": [],
            "claims": [{"key": "test-resolution", "value": "environment-check",
                        "statement": "仮想環境の不一致がある場合に環境を確認する。",
                        "applies_when": ["Python", "仮想環境の不一致"],
                        "evidence": [{"source_id": snap["sources"][0]["source_id"], "quote": text}]}]}]}
        wiki = compile_wiki(snap, proposal, sensitive_literals=terms)
        receipt = export_wiki(wiki, out / mode)
        verified = verify_wiki(out / mode, snap, expected_manifest_sha256=receipt["manifest_sha256"],
                                sensitive_literals=terms)
        matches = query_wiki(wiki, snap, "Python 仮想環境", sensitive_literals=terms)
        (out / (mode + "-prompt.json")).write_bytes(json_bytes(wiki_prompt(snap)))
        result["variants"][mode] = {"receipt": receipt, "verified": verified["status"],
                                     "matches": matches, "lint": lint_wiki(wiki, snap)}
    (out / "result.json").write_bytes(json_bytes(result))
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        print(json.dumps(demo(args.out), ensure_ascii=False, indent=2))
        return 0
    except (InvalidData, ValueError, OSError, TypeError):
        print("ERROR: 合成Wikiデモを作成できません。出力先を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
