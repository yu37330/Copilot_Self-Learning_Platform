"""Two-track LLM port: model identities are separated, real network remains blocked.

Both the knowledge track and Session Retrospective can use this JSON interface.
The only executable implementation in this iteration consumes a reply that was
already provided locally and binds it to the exact system+prompt hash.
No API credentials, SDK, outbound transport, or auto-approval are involved.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Protocol, runtime_checkable

from .core import InvalidData, check_sensitive, digest, json_bytes

MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
FAMILY_ID = re.compile(r"[a-z][a-z0-9-]{0,50}")
MAX_PROMPT_BYTES = 128 * 1024
MAX_REPLY_BYTES = 64 * 1024


@runtime_checkable
class JSONModelPort(Protocol):
    """Backend-independent signature also used by the existing learning.py."""

    def generate(self, prompt: str, *, system: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class ModelRoute:
    """An identifier supplied by configuration, NOT an authenticated model ID."""

    provider_family: str
    model_id: str
    purpose: str

    def __post_init__(self) -> None:
        if (not isinstance(self.provider_family, str)
                or not FAMILY_ID.fullmatch(self.provider_family)
                or not isinstance(self.model_id, str)
                or not MODEL_ID.fullmatch(self.model_id)
                or self.purpose not in {"generator", "reviewer"}):
            raise InvalidData("モデルの役割・識別子が不正です")

    def metadata(self) -> dict[str, Any]:
        return {"provider_family": self.provider_family,
                "model_id": self.model_id, "purpose": self.purpose,
                "identity_authenticated": False}


def request_sha256(system: str, prompt: str) -> str:
    """Every offline reply is tied to the *exact* request, not an approximate topic."""
    if not isinstance(system, str) or not isinstance(prompt, str):
        raise InvalidData("モデル要求の形式が不正です")
    payload = json_bytes({"system": system, "prompt": prompt})
    if (not system.strip() or len(system) > 10000 or not prompt.strip()
            or len(payload) > MAX_PROMPT_BYTES):
        raise InvalidData("モデル要求が空または大きすぎます")
    check_sensitive(system)
    check_sensitive(prompt)
    return digest(payload)


class OfflineBoundJSONModel:
    """A bounded local response. It **does not** run an LLM.

    Concrete type checking is intentional: duck-typed network providers cannot
    silently bypass this offline-only gate by pretending to be an offline port.
    """

    def __init__(self, route: ModelRoute, *, expected_request_sha256: str,
                 response: dict[str, Any]):
        if not isinstance(route, ModelRoute):
            raise InvalidData("モデル経路の型が不正です")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_request_sha256):
            raise InvalidData("完全一致する要求SHAが必要です")
        if not isinstance(response, dict):
            raise InvalidData("モデル応答はJSONオブジェクトが必要です")
        raw = json_bytes(response)
        if len(raw) > MAX_REPLY_BYTES:
            raise InvalidData("モデル応答が大きすぎます")
        check_sensitive(raw.decode("utf-8"))
        self.route = route
        self.expected_request_sha256 = expected_request_sha256
        self._response = deepcopy(response)
        self.calls = 0

    def generate(self, prompt: str, *, system: str) -> dict[str, Any]:
        if request_sha256(system, prompt) != self.expected_request_sha256:
            raise InvalidData("出典が違うモデル応答を再利用できません")
        if self.calls:
            raise InvalidData("同じモデル応答を複数回消費できません")
        self.calls += 1
        return deepcopy(self._response)


def require_offline_model_pair(generator: OfflineBoundJSONModel,
                               reviewer: OfflineBoundJSONModel) -> dict[str, Any]:
    """Enforce separate roles and model identities for BOTH pipelines."""
    if (type(generator) is not OfflineBoundJSONModel
            or type(reviewer) is not OfflineBoundJSONModel
            or generator is reviewer
            or generator.route.purpose != "generator"
            or reviewer.route.purpose != "reviewer"
            or generator.route.model_id == reviewer.route.model_id):
        raise InvalidData("生成・独立評価は異なるモデル設定のオフライン応答が必要です")
    return {
        "generator": generator.route.metadata(),
        "reviewer": reviewer.route.metadata(),
        "transport": "preprovided_local_json_no_model_called",
        "model_execution_verified": False, "network_calls": 0,
        "api_cost_usd": None, "input_tokens": None, "output_tokens": None,
    }
