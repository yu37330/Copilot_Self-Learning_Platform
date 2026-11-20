#!/usr/bin/env python3
"""自宅PCの個人モデル接続準備を本文・認証キー未表示で点検する。"""
from __future__ import annotations

import json
import os
import shutil


def probe(env: dict[str, str] | None = None) -> dict:
    environment = env if env is not None else os.environ
    return {
        "schema_version": "1.0", "record_type": "personal-local-model-backend-probe",
        "openai_api_key_configured": bool(environment.get("OPENAI_API_KEY")),
        "codex_cli_on_path": shutil.which("codex") is not None,
        "copilot_cli_on_path": shutil.which("copilot") is not None,
        "authentication_validated": False,
        "models_accessible_verified": False,
        "external_send_performed": False,
        "cost_usd": None,
        "network_calls": 0,
    }


def main() -> int:
    print(json.dumps(probe(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
