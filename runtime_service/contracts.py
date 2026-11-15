"""S3入力・実行条件の契約。呼出元から送信先や任意パスを選ばせない。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from copilot_learning.core import InvalidData, SHA_PATTERN, digest, json_bytes

VERSION = "single-runtime-1"
MODES = {"custom", "original_wiki", "custom_and_original_wiki", "memory_ingest", "memory_wiki", "hybrid"}
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


class Rejected(InvalidData):
    """本文・パス・資格情報を含めない固定エラーコード。"""


def fail(code: str = "INVALID_INPUT") -> None:
    raise Rejected(code)


def obj(value: Any, keys: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        fail()


def integer(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def decode(data: bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: fail("NONFINITE_JSON"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected("INVALID_JSON") from exc
    if not isinstance(value, dict):
        fail()
    return value


def valid_key(value: Any, prefix: str) -> bool:
    return (isinstance(value, str) and len(value.encode("utf-8")) <= 1024
            and value.startswith(prefix) and len(value) > len(prefix)
            and not any(ord(c) < 32 for c in value)
            and "\\" not in value and not set(value.split("/")) & {"", ".", ".."})


@dataclass(frozen=True)
class Settings:
    bucket: str
    owner: str
    region: str
    kms_key: str
    ready_prefix: str = "inputs/ready/"
    object_prefix: str = "inputs/objects/"
    output_prefix: str = "results/"
    code_sha256: str = "0" * 64
    model_ids: tuple[str, ...] = ()
    memory_profiles: dict[str, Any] = field(default_factory=dict)
    sensitive_literals: tuple[str, ...] = ()
    max_model_calls: int = 3
    max_output_tokens: int = 4096
    max_job_seconds: int = 900
    memory_polls: int = 3
    poll_seconds: int = 5

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            fail("INVALID_BUCKET")
        if not re.fullmatch(r"\d{12}", self.owner) or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", self.region):
            fail("INVALID_ACCOUNT_OR_REGION")
        if not re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:kms:" + re.escape(self.region) + ":" + self.owner + r":key/[A-Za-z0-9-]+", self.kms_key):
            fail("INVALID_KMS_KEY")
        if not isinstance(self.code_sha256, str) or not SHA_PATTERN.fullmatch(self.code_sha256):
            fail("INVALID_CODE_DIGEST")
        prefixes = (self.ready_prefix, self.object_prefix, self.output_prefix)
        if any(not re.fullmatch(r"(?:[a-zA-Z0-9_-]+/)+", p) for p in prefixes):
            fail("INVALID_PREFIX")
        if any(a.startswith(b) or b.startswith(a) for i, a in enumerate(prefixes) for b in prefixes[i+1:]):
            fail("OVERLAPPING_PREFIX")
        if (not integer(self.max_model_calls, 1, 6) or not integer(self.max_output_tokens, 256, 16000)
                or not integer(self.max_job_seconds, 30, 3600) or not integer(self.memory_polls, 1, 20)
                or not integer(self.poll_seconds, 1, 60)):
            fail("INVALID_LIMITS")
        if any(not isinstance(m, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", m) for m in self.model_ids):
            fail("INVALID_MODEL_ALLOWLIST")
        if not isinstance(self.memory_profiles, dict):
            fail("INVALID_MEMORY_PROFILES")
        if any(not isinstance(x, str) or len(x.strip()) < 3 for x in self.sensitive_literals):
            fail("INVALID_PRIVACY_POLICY")

    @classmethod
    def from_env(cls):
        return cls(bucket=os.environ["DATA_BUCKET"], owner=os.environ["EXPECTED_BUCKET_OWNER"],
                   region=os.environ.get("RUNTIME_REGION", os.environ.get("AWS_REGION", "")), kms_key=os.environ["DATA_KMS_KEY_ARN"],
                   code_sha256=os.environ["CODE_SHA256"],
                   model_ids=tuple(x for x in os.environ.get("ALLOWED_MODEL_IDS", "").split(",") if x),
                   memory_profiles=json.loads(os.environ.get("MEMORY_PROFILES_JSON", "{}")),
                   sensitive_literals=tuple(json.loads(os.environ.get("SENSITIVE_LITERALS_JSON", "[]"))))

    def model(self, value: Any) -> str:
        if not isinstance(value, str) or value not in self.model_ids:
            fail("MODEL_NOT_ALLOWED")
        return value

    def memory(self, name: Any, project: str) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self.memory_profiles:
            fail("MEMORY_PROFILE_NOT_ALLOWED")
        value = self.memory_profiles[name]
        obj(value, {"memory_id", "actor_id", "namespace", "strategy_ids", "project"})
        if (value["project"] != project or not isinstance(value["memory_id"], str)
                or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,99}-[a-zA-Z0-9]{10}", value["memory_id"])
                or not isinstance(value["actor_id"], str) or not TOKEN.fullmatch(value["actor_id"])
                or not isinstance(value["namespace"], str)
                or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+", value["namespace"])
                or not isinstance(value["strategy_ids"], list) or not 1 <= len(value["strategy_ids"]) <= 8
                or len(set(value["strategy_ids"])) != len(value["strategy_ids"])
                or any(not isinstance(x, str) or not TOKEN.fullmatch(x) for x in value["strategy_ids"])):
            fail("INVALID_MEMORY_BINDING")
        return json.loads(json_bytes(value))


def reference(value: Any, settings: Settings, *, ready: bool = False) -> dict:
    obj(value, {"bucket", "key", "version_id", "sha256"})
    if (value["bucket"] != settings.bucket
            or not valid_key(value["key"], settings.ready_prefix if ready else settings.object_prefix)
            or not isinstance(value["version_id"], str) or not 1 <= len(value["version_id"]) <= 1024
            or value["version_id"] == "null" or any(ord(c) < 32 for c in value["version_id"])
            or not isinstance(value["sha256"], str) or not SHA_PATTERN.fullmatch(value["sha256"])):
        fail("INVALID_OBJECT_REFERENCE")
    if ready and not value["key"].endswith(".job.json"):
        fail("NOT_A_READY_JOB")
    return value


def command(value: Any, settings: Settings) -> dict:
    if not isinstance(value, dict) or value.get("action") not in {"run", "status", "retry"}:
        fail()
    keys = {"schema_version", "action", "job_id", "job_ref"}
    if value["action"] == "retry":
        keys.add("expected_revision")
        if not integer(value.get("expected_revision"), 1, 10000):
            fail("EXPECTED_REVISION_REQUIRED")
    obj(value, keys)
    ref = reference(value["job_ref"], settings, ready=True)
    if value["schema_version"] != VERSION or value["job_id"] != digest(json_bytes(ref)):
        fail("JOB_ID_MISMATCH")
    return value


def job(value: Any, settings: Settings) -> dict:
    obj(value, {"schema_version", "project", "mode", "session_fingerprint", "artifacts",
                "extract_model_id", "grade_model_id", "wiki_model_id", "memory_profile"})
    if (value["schema_version"] != VERSION or value["mode"] not in MODES
            or not isinstance(value["project"], str) or not TOKEN.fullmatch(value["project"])
            or not isinstance(value["session_fingerprint"], str) or not SHA_PATTERN.fullmatch(value["session_fingerprint"])):
        fail()
    refs = value["artifacts"]
    if not isinstance(refs, dict) or not {"archive", "manifest"} <= set(refs) or set(refs) - {"archive", "manifest", "feedback"}:
        fail("INVALID_ARTIFACT_SET")
    for ref in refs.values():
        reference(ref, settings)
    mode = value["mode"]
    custom = mode in {"custom", "custom_and_original_wiki"}
    wiki = mode in {"original_wiki", "custom_and_original_wiki", "memory_wiki"}
    for key, required in (("extract_model_id", custom), ("grade_model_id", custom or mode == "hybrid"),
                          ("wiki_model_id", wiki)):
        if required:
            settings.model(value[key])
        elif value[key] is not None:
            fail("UNUSED_MODEL_SETTING")
    if mode in {"memory_ingest", "memory_wiki", "hybrid"}:
        settings.memory(value["memory_profile"], value["project"])
    elif value["memory_profile"] is not None:
        fail("UNUSED_MEMORY_SETTING")
    return value
