"""家庭用モデルテスト画面。標準ライブラリのみ・localhost専用。

安全境界:
  * 起動時は実モデル送信を禁止。--allow-live と毎回の個別承認が必要
  * 既存ルータv2の事前機密検査、SHA承認、モデル呼出をそのまま再利用
  * 生セッション、プロンプト、モデル応答をディスクやアクセスログに保存しない
  * 同一PC以外からのアクセス/クロスサイト送信/再送信は拒否する
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .core import InvalidData
from .local_llm_router import make_preflight, run_with_approved_scope
from .model_review_contract import BACKENDS, FAMILIES, MODEL_ID, MODEL_ROLES, TRACKS
from .privacy_scan import load_policy

ASSETS = Path(__file__).with_name("personal_model_assets")
BODY_LIMIT = 72 * 1024
PENDING_TTL_SECONDS = 15 * 60
SAFE_BACKENDS = frozenset({"openai_api", "codex_cli", "copilot_cli"})
SENT_CONFIRMATION = "SEND"
SAMPLE_SYSTEM = "Return one JSON object with summary and suggestions. Do not run any tools."
SAMPLE_PROMPT = (
    "Synthetic example: the assistant reported a task complete before "
    "checking tests. Suggest one concrete verification step."
)


def build_plan(backend: str, model_id: str, family: str,
               track: str, role: str) -> dict[str, Any]:
    """1件のUIテストを、既存の2系統×2ロールのモデル計画へ変換。"""
    if (backend not in SAFE_BACKENDS
            or not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id)
            or not isinstance(family, str) or family not in FAMILIES
            or track not in TRACKS or role not in MODEL_ROLES):
        raise InvalidData("モデル/経路の指定が不正です")
    tracks: dict[str, dict[str, dict[str, Any]]] = {}
    for t in TRACKS:
        tracks[t] = {}
        for r in MODEL_ROLES:
            tracks[t][r] = {
                "backend": "other_offline", "model_id": f"ui_unused_{t}_{r}",
                "provider_family": "other", "max_input_tokens": 4096,
            }
    tracks[track][role] = {
        "backend": backend, "model_id": model_id,
        "provider_family": family, "max_input_tokens": 4096,
    }
    return {
        "schema_version": "1.0",
        "record_type": "offline-dual-track-model-plan",
        "require_cross_vendor": False,
        "external_transfer_authorized": False,
        "tracks": tracks,
    }


def build_request(values: dict[str, Any]) -> dict[str, Any]:
    if (not isinstance(values, dict) or set(values) != {
        "backend", "model_id", "provider_family", "track", "role",
        "system", "prompt", "reviewed",
    } or values.get("reviewed") is not True):
        raise InvalidData("本人の内容確認が必要です")
    if not all(isinstance(values.get(name), str) for name in
               ("system", "prompt", "backend", "model_id",
                "provider_family", "track", "role")):
        raise InvalidData("入力が文字列ではありません")
    return {
        "schema_version": "1.0",
        "record_type": "personal-local-model-request",
        "track": values["track"], "role": values["role"],
        "system": values["system"], "prompt": values["prompt"],
        "source_classification": "personal_or_synthetic_only",
        "manual_content_reviewed": True,
    }


def readiness(env: dict[str, str] | None = None,
              which: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    environment = os.environ if env is None else env
    lookup = shutil.which if which is None else which
    return {
        "openai_api": {"installed": True, "key_configured": bool(environment.get("OPENAI_API_KEY"))},
        "codex_cli": {"installed": lookup("codex") is not None,
                      "key_configured": bool(environment.get("CODEX_API_KEY") or
                                             environment.get("OPENAI_API_KEY"))},
        "copilot_cli": {"installed": lookup("copilot") is not None,
                        "key_configured": any(bool(environment.get(k)) for k in
                                              ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"))},
        "authentication_verified": False,
        "model_availability_verified": False,
        "network_calls": 0,
    }


@dataclass
class Pending:
    id: str
    created: float
    plan: dict[str, Any]
    request: dict[str, Any]
    preflight: dict[str, Any]


class TestUI:
    """単一セッションの一時承認。プロンプト・応答はメモリにのみ存在。"""

    def __init__(self, policy: Path, *, allow_live: bool = False,
                 executor: Callable[..., Any] = run_with_approved_scope,
                 clock: Callable[[], float] = time.monotonic):
        self.policy = policy
        self.allow_live = allow_live
        self.executor = executor
        self.clock = clock
        self.csrf = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._pending: Pending | None = None

    def status(self) -> dict[str, Any]:
        return {
            "csrf_token": self.csrf, "allow_live": self.allow_live,
            "readiness": readiness(), "mode": "offline_by_default",
            "policy_configured": True,
            "credentials_verified": False,
        }

    def preflight(self, values: dict[str, Any]) -> dict[str, Any]:
        req = build_request(values)
        plan = build_plan(
            values["backend"], values["model_id"], values["provider_family"],
            values["track"], values["role"])
        terms, policy_sha = load_policy(self.policy)
        scope = make_preflight(
            plan, req, policy_sha256=policy_sha,
            literals=terms["sensitive_literals"])
        prepared = Pending(secrets.token_urlsafe(22), self.clock(), plan, req, scope)
        with self._lock:
            self._pending = prepared
        target = scope["scope"]["target"]
        return {
            "status": "preflight_only_no_send",
            "pending_id": prepared.id,
            "scope_sha256": scope["scope_sha256"],
            "backend": target["backend"], "model_id": target["model_id"],
            "track": target["track"], "role": target["role"],
            "prompt_bytes": scope["scope"]["prompt_bytes"],
            "expires_in_seconds": PENDING_TTL_SECONDS,
            "allow_live": self.allow_live,
            "external_send_performed": False,
        }

    def _matching(self, data: dict[str, Any], *, consume: bool) -> Pending:
        if (not isinstance(data, dict)
                or set(data) not in ({"pending_id", "scope_sha256"},
                                    {"pending_id", "scope_sha256", "confirmation"})
                or not isinstance(data.get("pending_id"), str)
                or not isinstance(data.get("scope_sha256"), str)):
            raise InvalidData("事前確認の識別子が不正です")
        with self._lock:
            p = self._pending
            if (p is None or self.clock() - p.created > PENDING_TTL_SECONDS
                    or data["pending_id"] != p.id
                    or data["scope_sha256"] != p.preflight["scope_sha256"]):
                raise InvalidData("事前検査が期限切れか一致しません")
            if consume:
                # One-shot: a timeout or ambiguous network error must not be retried silently.
                self._pending = None
            return p

    def mock(self, data: dict[str, Any]) -> dict[str, Any]:
        p = self._matching(data, consume=False)
        return {
            "status": "synthetic_mock_only",
            "backend": p.preflight["scope"]["target"]["backend"],
            "scope_sha256": p.preflight["scope_sha256"],
            "response": {
                "summary": "模擬実行です。LLMには送信していません。",
                "suggestions": [
                    {"action": "完了報告の前にテスト実行記録を確認する",
                     "evidence": "synthetic:0"}
                ],
                "quality_verified": False,
            },
            "external_model_invoked": False,
            "network_calls": 0,
        }

    def run(self, data: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_live:
            raise InvalidData("サーバーは実送信を無効にしています")
        if not isinstance(data, dict) or set(data) != {
            "pending_id", "scope_sha256", "confirmation"
        } or data.get("confirmation") != SENT_CONFIRMATION:
            raise InvalidData("この送信への追加確認が必要です")
        p = self._matching(data, consume=True)
        # Existing provider gate re-reads policy and re-checks scope before any call.
        report, _ = self.executor(
            p.plan, p.request, self.policy,
            preflight=p.preflight,
            approved_scope_sha256=p.preflight["scope_sha256"],
            acknowledged=True)
        return {
            "status": "untrusted_model_output_not_approved",
            "backend": report["backend"], "model_id": report["model_id"],
            "scope_sha256": report["scope_sha256"],
            "response": report["response"],
            "metadata": report["metadata"],
            "external_model_invoked": True,
            "business_completion_verified": False,
            "learning_effect_verified": False,
            "approved_for_instructions": False,
        }


def make_server(policy: Path, *, port: int = 8765, allow_live: bool = False,
                executor: Callable[..., Any] = run_with_approved_scope,
                clock: Callable[[], float] = time.monotonic) -> ThreadingHTTPServer:
    """127.0.0.1固定。0指定時のみOSの空きポートを使用（テスト用）。"""
    if type(port) is not int or not 0 <= port <= 65535:
        raise InvalidData("ポートが不正です")
    service = TestUI(policy, allow_live=allow_live, executor=executor, clock=clock)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            # Never log user prompts, query strings, or model responses.
            return

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "").lower()
            allowed = {f"127.0.0.1:{self.server.server_port}",
                       f"localhost:{self.server.server_port}"}
            return host in allowed

        def _headers(self, status: int, mime: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; form-action 'none'; base-uri 'none'; "
                "frame-ancestors 'none'")
            self.end_headers()

        def _send(self, status: int, content: bytes, mime: str) -> None:
            self._headers(status, mime, len(content))
            try:
                self.wfile.write(content)
            except (ConnectionResetError, BrokenPipeError):
                pass

        def _json(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "ローカルホストからのみ利用できます"})
                return
            route = urlsplit(self.path).path
            if route == "/api/status":
                self._json(200, service.status())
                return
            name = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }.get(route)
            if name is None:
                self._json(404, {"error": "対象ページがありません"})
                return
            self._send(200, (ASSETS / name[0]).read_bytes(), name[1])

        def do_POST(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "ローカルホストからのみ利用できます"})
                return
            origins = {f"http://127.0.0.1:{self.server.server_port}",
                       f"http://localhost:{self.server.server_port}"}
            if (self.headers.get("Origin") not in origins
                    or self.headers.get("X-Local-UI-Token") != service.csrf):
                self._json(403, {"error": "同じ画面からの操作確認が必要です"})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"error": "JSON形式で送信してください"})
                return
            try:
                size = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                size = -1
            if not 0 <= size <= BODY_LIMIT:
                self._json(413, {"error": "入力の容量が上限を超えています"})
                return
            try:
                data = json.loads(self.rfile.read(size).decode("utf-8"))
                route = urlsplit(self.path).path
                if route == "/api/preflight":
                    result = service.preflight(data)
                elif route == "/api/mock":
                    result = service.mock(data)
                elif route == "/api/run":
                    result = service.run(data)
                else:
                    self._json(404, {"error": "対象操作がありません"})
                    return
                self._json(200, result)
            except (InvalidData, ValueError, TypeError, UnicodeError):
                # Provider/secret specifics are never surfaced to the browser.
                self._json(400, {"error": "入力・承認・機密検査を確認してください。"
                                         "実行した場合は同じ操作を自動再試行しません。"})
            except Exception:
                self._json(500, {"error": "実行できませんでした。"
                                         "原因が不明な場合、課金されていないとは断定できません。"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.ui_service = service  # type: ignore[attr-defined]
    return server
