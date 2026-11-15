#!/usr/bin/env python3
"""単一AgentCore RuntimeのCodeZip entrypoint。Managed Harnessは使用しない。"""
from runtime_service.contracts import Settings
from runtime_service.aws_adapters import production
from runtime_service.pipeline import Pipeline
from runtime_service.server import JobService, RuntimeHTTPServer
from copilot_learning.review_safety import require_online_release


def production_service():
    # サーバー起動・pingは可能。社内安全レビュー前のAWSアクセスは呼出時に必ず停止。
    require_online_release()
    settings = Settings.from_env()
    store, ledger, models, memory = production(settings)
    return JobService(settings, Pipeline(settings, store, models, memory), ledger)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080, help="ローカル検証用。AgentCore配備では既定8080を使用")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("portは1〜65535です")
    with RuntimeHTTPServer(("0.0.0.0", args.port), production_service) as server:
        server.serve_forever()
