#!/usr/bin/env python3
"""個人/合成データ専用ローカルLLMルータ。preflight -> run -> verify。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, SHA_PATTERN, assert_external_output, digest, json_bytes
from copilot_learning.local_llm_router import (
    make_preflight, read_private_json, run_with_approved_scope,
)
from copilot_learning.privacy_scan import load_policy
from copilot_learning.skill_preview import ROOT, RESERVED, _path

PREFLIGHT = "scope.preflight.json"
RESPONSE = "model-response.preview.json"
MANIFEST = "manifest.json"
MAX_RESPONSE_BYTES = 256 * 1024


def _outside(path: Path) -> Path:
    out = _path(path)
    assert_external_output(out, ROOT)
    if any(part.rstrip(" .").casefold() in (RESERVED | {".codex", ".copilot"}) for part in out.parts):
        raise InvalidData("エージェント自動読込先を保存先にはできません")
    return out


def _read_preflight(folder: Path) -> dict[str, Any]:
    source = _outside(folder) / PREFLIGHT
    data, _ = read_private_json(source)
    return data


def _write(out: Path, files: dict[str, bytes]) -> None:
    folder = _outside(out)
    if folder.exists():
        raise InvalidData("既存のモデル結果フォルダには上書きしません")
    folder.parent.mkdir(parents=True, exist_ok=True)
    _path(folder.parent)
    folder.mkdir(mode=0o700)
    created = []
    try:
        for name, data in sorted(files.items()):
            target = folder / name
            with target.open("xb") as handle:
                created.append(target)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o400)
    except BaseException:
        for target in reversed(created):
            target.chmod(0o600)
            target.unlink()
        folder.rmdir()
        raise


def _current(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan, _ = read_private_json(args.plan)
    request, _ = read_private_json(args.request)
    policy, policy_sha = load_policy(_path(args.policy))
    scope = make_preflight(plan, request, policy_sha256=policy_sha,
                           literals=policy["sensitive_literals"])
    return plan, request, scope


def _read_result(path: Path) -> dict[str, Any]:
    checked = _outside(path)
    if checked.is_symlink() or not checked.is_file() or not 0 < checked.stat().st_size <= MAX_RESPONSE_BYTES:
        raise InvalidData("実行レポートが不在または不正")
    try:
        value = json.loads(checked.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise InvalidData("実行レポートがJSONではありません") from exc
    if not isinstance(value, dict):
        raise InvalidData("実行レポート形式が不正")
    return value


def _manifest(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "record_type": "personal-model-route-output-manifest",
        "scope_sha256": report["scope_sha256"],
        "report_sha256": digest(json_bytes(report)),
        "status": "model_output_unapproved_requires_qa",
        "approved_for_instructions": False,
        "model_identity_independently_verified": False,
    }


def _verify(args: argparse.Namespace, scope: dict[str, Any]) -> dict[str, Any]:
    if (not isinstance(args.expected_manifest_sha256, str)
            or not SHA_PATTERN.fullmatch(args.expected_manifest_sha256)):
        raise InvalidData("独立保管済みのmanifest SHAが必要")
    if _read_preflight(args.preflight) != scope:
        raise InvalidData("モデル送信スコープが変更されています")
    out = _outside(args.out)
    if not out.is_dir() or {p.name for p in out.iterdir()} != {RESPONSE, MANIFEST}:
        raise InvalidData("モデル結果の構成が一致しません")
    response = _read_result(out / RESPONSE)
    receipt = _read_result(out / MANIFEST)
    if (response.get("record_type") != "untrusted-personal-model-response"
            or response.get("scope_sha256") != scope["scope_sha256"]
            or response.get("request_sha256") != scope["scope"]["request_sha256"]
            or response.get("model_plan_sha256") != scope["scope"]["model_plan_sha256"]
            or response.get("policy_sha256") != scope["scope"]["policy_sha256"]
            or response.get("external_model_invoked") is not True
            or response.get("approved_for_copilot") is not False
            or not isinstance(response.get("response"), dict)
            or response.get("output_sha256") != digest(json_bytes(response["response"]))):
        raise InvalidData("保存モデル結果の出典契約が一致しません")
    expected = _manifest(response)
    if (receipt != expected or digest(json_bytes(receipt)) != args.expected_manifest_sha256
            or (out / RESPONSE).read_bytes() != json_bytes(response)
            or (out / MANIFEST).read_bytes() != json_bytes(receipt)):
        raise InvalidData("レポート・manifestのSHAまたは出典が不一致です")
    return {
        "status": "local_model_output_bytes_verified_not_approved",
        "model_identity_independently_verified": False,
        "knowledge_quality_verified": False, "network_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "run", "verify", "export-json"))
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--approved-scope-sha256")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--output-json", type=Path,
                        help="verified model responseを既存Phase Bへ渡すための新規非承認JSON")
    parser.add_argument("--acknowledge-external-send", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        if args.action == "preflight":
            if (args.acknowledge_external_send or args.preflight is not None
                    or args.approved_scope_sha256 or args.expected_manifest_sha256
                    or args.output_json is not None):
                raise InvalidData("preflightでは実行・承認引数を受け付けません")
        elif args.action == "run":
            # Fail before opening the private request for accidental runs.
            if not (args.acknowledge_external_send and args.preflight
                    and args.approved_scope_sha256 and not args.expected_manifest_sha256
                    and args.output_json is None):
                raise InvalidData("外部送信には事前SHA・明示同意が必要")
        elif (not args.preflight or not args.expected_manifest_sha256
              or args.approved_scope_sha256 or args.acknowledge_external_send
              or (args.action == "export-json") != bool(args.output_json)):
            raise InvalidData("verify/export-jsonは独立manifest SHAとpreflightが必要")
        plan, request, scope = _current(args)
        if args.action == "preflight":
            _write(args.out, {PREFLIGHT: json_bytes(scope)})
            result = {
                "status": "preflight_only_no_model_call",
                "backend": scope["scope"]["target"]["backend"],
                "model_id": scope["scope"]["target"]["model_id"],
                "track": request["track"], "role": request["role"],
                "scope_sha256": scope["scope_sha256"], "prompt_bytes": scope["scope"]["prompt_bytes"],
                "network_calls": 0,
            }
        elif args.action == "run":
            pinned = _read_preflight(args.preflight)
            report, _ = run_with_approved_scope(
                plan, request, args.policy, preflight=pinned,
                approved_scope_sha256=args.approved_scope_sha256,
                acknowledged=args.acknowledge_external_send, timeout=args.timeout)
            manifest = _manifest(report)
            _write(args.out, {RESPONSE: json_bytes(report), MANIFEST: json_bytes(manifest)})
            result = {
                "status": "model_called_output_unapproved",
                "backend": report["backend"], "role": report["role"],
                "scope_sha256": report["scope_sha256"],
                "manifest_sha256": digest(json_bytes(manifest)),
                "approved_for_instructions": False,
            }
        else:
            result = _verify(args, scope)
            if args.action == "export-json":
                # No provider call: extract previously verified, untrusted JSON only.
                extracted = _read_result(_outside(args.out) / RESPONSE)["response"]
                target = _outside(args.output_json)
                completed = _outside(args.out)
                if (target == completed or completed in target.parents
                        or target.suffix.lower() != ".json" or target.exists()):
                    raise InvalidData("保存済み検証結果フォルダにはJSONを書き込めません")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(json_bytes(extracted))
                    handle.flush()
                    os.fsync(handle.fileno())
                target.chmod(0o400)
                result = {
                    "status": "untrusted_json_exported_for_existing_phase_b_checks",
                    "response_sha256": digest(json_bytes(extracted)),
                    "approved_for_instructions": False, "network_calls": 0,
                }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (InvalidData, OSError, ValueError, TypeError, UnicodeError):
        # Do not print the original exception, prompt, secrets or tool stderr.
        print("ERROR: ローカルモデル経路を安全に検査/実行できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
