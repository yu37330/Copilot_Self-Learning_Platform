"""共通の生成LLM/独立評価LLMのオフライン設定契約（Issue #31）。

モデル名は提案者が指定する識別子。プロバイダ所属・実際のモデル利用可否を
このコードで認証したことにはならない。API接続・ログ送信を一切行わない。
"""
from __future__ import annotations

import re
from typing import Any

from .core import InvalidData, digest, json_bytes

TRACKS = ("knowledge", "retrospective")
MODEL_ROLES = ("generator", "reviewer")
BACKENDS = frozenset({"bedrock", "openai_api", "other_offline"})
# 提供元の自己申告ラベル。AWS/各社のモデルカタログ照合は未実装。
FAMILIES = frozenset({"anthropic", "amazon", "openai", "google", "meta", "mistral", "other"})
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}")
MAX_INPUT_TOKENS = 100_000
VERSION = "dual-track-llm-review-plan-1"


def _model(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "backend", "provider_family", "model_id", "max_input_tokens",
    }:
        raise InvalidData("生成/評価モデルの設定フィールドが不正です")
    if (not isinstance(value["backend"], str) or value["backend"] not in BACKENDS
            or not isinstance(value["provider_family"], str) or value["provider_family"] not in FAMILIES
            or not isinstance(value["model_id"], str) or not MODEL_ID.fullmatch(value["model_id"])
            or type(value["max_input_tokens"]) is not int
            or not 128 <= value["max_input_tokens"] <= MAX_INPUT_TOKENS):
        raise InvalidData("モデルID、区分、入力予算が不正です")
    return dict(value)


def validate_model_plan(plan: Any) -> dict[str, Any]:
    """2系統それぞれ別モデル。別ベンダー指定時は異なる申告ラベルが必須。"""
    if not isinstance(plan, dict) or set(plan) != {
        "schema_version", "record_type", "tracks", "require_cross_vendor",
        "external_transfer_authorized",
    }:
        raise InvalidData("2系統のモデル選択計画が不正です")
    if (plan["schema_version"] != "1.0"
            or plan["record_type"] != "offline-dual-track-model-plan"
            or type(plan["require_cross_vendor"]) is not bool
            or plan["external_transfer_authorized"] is not False):
        raise InvalidData("モデル計画の安全フラグが不正です")
    tracks = plan["tracks"]
    if not isinstance(tracks, dict) or set(tracks) != set(TRACKS):
        raise InvalidData("knowledge/retrospectiveの双方を指定してください")
    for name in TRACKS:
        row = tracks[name]
        if not isinstance(row, dict) or set(row) != set(MODEL_ROLES):
            raise InvalidData("生成役・独立評価役の設定が不足しています")
        generator = _model(row["generator"])
        reviewer = _model(row["reviewer"])
        if generator["model_id"] == reviewer["model_id"]:
            raise InvalidData("同一モデルを生成と独立評価に兼用できません")
        if plan["require_cross_vendor"] and generator["provider_family"] == reviewer["provider_family"]:
            raise InvalidData("別ベンダー評価指定では別の提供元ラベルが必要です")
    return {
        "schema_version": "1.0",
        "record_type": "offline-dual-track-model-plan-validated",
        "plan_sha256": digest(json_bytes(plan)),
        "models": {
            track: {
                role: {
                    "backend": tracks[track][role]["backend"],
                    "provider_family_claim": tracks[track][role]["provider_family"],
                    "model_id": tracks[track][role]["model_id"],
                    "max_input_tokens": tracks[track][role]["max_input_tokens"],
                } for role in MODEL_ROLES
            } for track in TRACKS
        },
        "same_model_blocked": True,
        "cross_vendor_label_required": plan["require_cross_vendor"],
        "provider_identity_verified": False,
        "provider_availability_verified": False,
        "remote_execution_implemented": False,
        "external_transfer_authorized": False,
        "network_calls": 0,
    }


def enforce_distinct_model_ids(generator_model_id: Any, reviewer_model_id: Any) -> None:
    """既存Runtimeと既存学習CLIの共通チェック。空・同一IDを拒否する。"""
    if (not isinstance(generator_model_id, str) or not MODEL_ID.fullmatch(generator_model_id)
            or not isinstance(reviewer_model_id, str) or not MODEL_ID.fullmatch(reviewer_model_id)
            or generator_model_id == reviewer_model_id):
        raise InvalidData("生成モデルと独立評価モデルには異なるmodel_idが必要です")
