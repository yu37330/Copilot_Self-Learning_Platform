"""Issue #31: compose the canonical Phase B and independent reviewer contracts.

No new LLM interface is introduced. Uses the already-merged model_review_contract
and retro_llm_review as the single source of truth. Reviewer 'support' is NOT approval.
All files remain uninstalled and unapproved.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .core import InvalidData, digest, json_bytes, assert_external_output
from .instruction_review import grade_response, instruction_inventory, _scan_text, _surface_from_sessions
from .privacy_scan import load_policy
from .retro_llm_review import prepare_independent_review, grade_independent_review
from .skill_preview import ROOT, RESERVED, _path

VERSION = "retro-review-finalizer-1"
FILE = "final-review.preview.json"
PATCH = "changes.preview.diff"
MANIFEST = "manifest.json"
MAX_BYTES = 256 * 1024


def prepare_final_review(sessions: list[dict[str, Any]], repo: Path, policy: Path,
                         proposals: dict[str, Any], model_plan: dict[str, Any],
                         reviewer_output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Recompute original evidence and model-plan binding BEFORE constructing a diff."""
    terms, policy_sha = load_policy(_path(policy))
    inventory = instruction_inventory(
        repo, terms["sensitive_literals"],
        surface=_surface_from_sessions(sessions))
    request = prepare_independent_review(sessions, repo, policy, proposals, model_plan)
    opinion = grade_independent_review(
        request, reviewer_output, model_plan, sensitive_literals=terms["sensitive_literals"])
    review, diff = grade_response(
        sessions, inventory, proposals, repo, terms["sensitive_literals"])
    decisions = {row["proposal_id"]: row for row in opinion["decisions"]}
    if len(request["candidates"]) != len(review["proposals"]):
        raise InvalidData("改善案の件数・元資料の対応が不正です")
    all_supported = bool(review["proposals"])
    for candidate, row in zip(request["candidates"], review["proposals"]):
        vote = decisions.get(candidate["proposal_id"])
        if vote is None:
            raise InvalidData("現在のレビュー対象に対する独立判断が欠けています")
        row["independent_opinion"] = vote["reviewer_verdict"]
        row["independent_reason"] = vote["reviewer_reason"]
        row["approved_for_copilot"] = False
        if vote["reviewer_verdict"] == "support_for_manual_review":
            row["grade"] = "pending_manual_review"
        else:
            row["grade"] = "independent_model_review_hold"
            all_supported = False
    # If any review is not a positive *advisory*, suppress all diff output.
    if not all_supported:
        diff = ""
    result = {
        "schema_version": "1.0", "record_type": "offline-retro-finalized-review",
        "version": VERSION,
        "status": "manual_review_required" if all_supported else "independent_review_blocked_diff",
        "independent_opinion": opinion,
        "review": review,
        "model_plan_sha256": opinion["model_plan_sha256"],
        "review_request_sha256": opinion["review_request_sha256"],
        "review_response_sha256": opinion["review_response_sha256"],
        "policy_sha256": policy_sha,
        "source_session_text_included": False,
        "applied": False,
        "approved_for_copilot": False,
        "actual_llm_execution_verified": False,
        "causal_improvement_verified": False, "network_calls": 0,
    }
    files = {FILE: json_bytes(result), PATCH: diff.encode("utf-8")}
    if sum(len(body) for body in files.values()) > MAX_BYTES:
        raise InvalidData("独立レビュー成果物が容量上限を超えています")
    manifest = {
        "schema_version": "1.0", "record_type": "offline-retro-final-review-manifest",
        "version": VERSION, "model_plan_sha256": result["model_plan_sha256"],
        "review_request_sha256": result["review_request_sha256"],
        "review_response_sha256": result["review_response_sha256"],
        "policy_sha256": policy_sha,
        "files": [{"name": name, "sha256": digest(body), "size_bytes": len(body)}
                  for name, body in sorted(files.items())],
        "status": "offline_unapproved_review_only",
        "approved_for_copilot": False, "network_calls": 0,
    }
    return manifest, files


def _outside(path: Path, repo: Path) -> Path:
    target = _path(path)
    root = _path(repo)
    assert_external_output(target, ROOT)
    if target == root or root in target.parents or any(
        part.rstrip(" .").casefold() in RESERVED for part in target.parts
    ):
        raise InvalidData("Git管理外の安全な出力先を指定してください")
    return target


def export_final_review(sessions: list[dict[str, Any]], repo: Path, policy: Path,
                        proposals: dict[str, Any], model_plan: dict[str, Any],
                        reviewer_output: dict[str, Any], destination: Path
                        ) -> dict[str, Any]:
    manifest, files = prepare_final_review(
        sessions, repo, policy, proposals, model_plan, reviewer_output)
    out = _outside(destination, repo)
    if out.exists():
        raise InvalidData("既存の出力先は上書きできません")
    out.parent.mkdir(parents=True, exist_ok=True)
    _path(out.parent)
    out.mkdir(mode=0o700)
    created: list[Path] = []
    try:
        for name, data in (*sorted(files.items()), (MANIFEST, json_bytes(manifest))):
            target = out / name
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
        out.rmdir()
        raise
    return {"status": "unapproved_preview_written", "manifest_sha256": digest(json_bytes(manifest)),
            "network_calls": 0, "approved_for_copilot": False}


def verify_final_review(sessions: list[dict[str, Any]], repo: Path, policy: Path,
                        proposals: dict[str, Any], model_plan: dict[str, Any],
                        reviewer_output: dict[str, Any], directory: Path,
                        expected_manifest_sha256: str) -> dict[str, Any]:
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_manifest_sha256
    ):
        raise InvalidData("独立保管したmanifest SHAが必要です")
    out = _outside(directory, repo)
    manifest, files = prepare_final_review(
        sessions, repo, policy, proposals, model_plan, reviewer_output)
    expected = {**files, MANIFEST: json_bytes(manifest)}
    if digest(expected[MANIFEST]) != expected_manifest_sha256:
        raise InvalidData("独立manifest SHAが一致しません")
    if not out.is_dir() or {p.name for p in out.iterdir()} != set(expected):
        raise InvalidData("レビューのファイル構成が変更されています")
    for name, body in expected.items():
        path = _path(out / name)
        if not path.is_file() or path.stat().st_size != len(body) or path.read_bytes() != body:
            raise InvalidData("レビューの出典・内容・指示版が一致しません")
    return {"status": "verified_unapproved_review_only",
            "approved_for_copilot": False, "files_modified": False, "network_calls": 0}
