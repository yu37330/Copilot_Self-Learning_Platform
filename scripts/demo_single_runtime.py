#!/usr/bin/env python3
"""S3→Lambda→単一Runtime→既存処理→OKFを、全てメモリ内の合成応答で確認する。"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from copilot_learning.core import assert_external_output, json_bytes
from runtime_service.contracts import MODES, decode
from runtime_service.pipeline import Pipeline
from runtime_service.server import JobService
from runtime_service.trigger import dispatch
from runtime_support import fixture


def demo(out: Path):
    assert_external_output(out, ROOT)
    out.mkdir(parents=True, exist_ok=False)
    summary = {"source_kind": "synthetic_in_memory_mocks", "aws_calls": 0, "real_llm_calls": 0,
               "real_quality_verified": False, "modes": {}}
    for mode in sorted(MODES):
        settings, store, ledger, models, memory, spec, request = fixture(mode)
        service = JobService(settings, Pipeline(settings, store, models, memory, sleep=lambda _:None), ledger)
        class Runtime:
            def invoke_agent_runtime(self, **kwargs):
                code, response = service.invoke(decode(kwargs["payload"]))
                return {"statusCode": code, "response": io.BytesIO(json_bytes(response))}
        event = {"Records": [{"eventSource": "aws:s3", "awsRegion": settings.region, "eventName": "ObjectCreated:Put",
                              "s3": {"bucket": {"name":settings.bucket}, "object": {"key":request["job_ref"]["key"], "versionId":"v-001"}}}]}
        try:
            acceptance = dispatch(event, settings, store, Runtime(), runtime_arn="synthetic-not-an-arn")
        finally:
            service.close()
        row = ledger.get(request["job_id"])
        if row["status"] != "SUCCEEDED":
            raise RuntimeError("合成パイプラインが完了しませんでした")
        folder = out / mode
        folder.mkdir()
        (folder / "ready.job.json").write_bytes(json_bytes(spec))
        (folder / "invocation.json").write_bytes(json_bytes(request))
        for key, data in store.outputs.items():
            relative = key.split("/", 2)[2]
            path = folder / "results" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        summary["modes"][mode] = {"status":row["status"], "acceptance":acceptance,
                                 "mock_model_calls":len(models.calls), "mock_memory_events":len(memory.sent),
                                 "artifact_count":len(store.outputs)}
    (out / "demo-result.json").write_bytes(json_bytes(summary))
    return summary


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",required=True,type=Path)
    args=p.parse_args()
    print(json.dumps(demo(args.out),ensure_ascii=False,indent=2))
