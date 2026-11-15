"""CodeZipのHTTP契約。Python標準ライブラリで/pingと非同期/invocationsを提供。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from copilot_learning.core import InvalidData, json_bytes
from copilot_learning.review_safety import ExternalExecutionBlocked
from .contracts import Settings, Rejected, command, decode, fail
from .pipeline import WaitingMemory


class JobService:
    def __init__(self, settings: Settings, pipeline: Any, ledger: Any, *, workers: int = 4):
        self.settings, self.pipeline, self.ledger = settings, pipeline, ledger
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="learning")
        self.slots = threading.BoundedSemaphore(workers)
        self.lock = threading.Lock()
        self.running = 0

    def health(self) -> str:
        with self.lock:
            return "HealthyBusy" if self.running else "Healthy"

    def summary(self, row: dict | None, job_id: str) -> dict:
        if row is None:
            return {"job_id": job_id, "status": "NOT_FOUND"}
        status = row["status"]
        if status == "RUNNING" and row["deadline"] < int(time.time()):
            status = "RECOVERY_REQUIRED"
        return {"job_id": job_id, "status": status, "revision": int(row["revision"]),
                "attempt": int(row["attempt"]), "error_code": row.get("error_code"),
                "result_key": self.settings.output_prefix + job_id + "/complete.json" if status == "SUCCEEDED" else None}

    def invoke(self, payload: dict) -> tuple[int, dict]:
        value = command(payload, self.settings)
        jid = value["job_id"]
        if value["action"] == "status":
            return 200, self.summary(self.ledger.get(jid), jid)
        if not self.slots.acquire(blocking=False):
            return 429, {"error": "LOCAL_CAPACITY_EXCEEDED"}
        try:
            row, claimed = self.ledger.claim(jid, value.get("expected_revision"))
            if not claimed:
                self.slots.release()
                return 200, self.summary(row, jid)
            # 本番台帳は不変な再開要求を保存。旧オフライン台帳は従来どおり利用可能。
            binder = getattr(self.ledger, "bind_request", None)
            if binder is not None:
                try:
                    row = binder(row, value)
                except Exception:
                    self.ledger.finish(row, "FAILED", "RESUME_BINDING_REJECTED")
                    raise
            with self.lock:
                self.running += 1
            try:
                self.pool.submit(self._work, value, row)
            except Exception:
                with self.lock:
                    self.running -= 1
                self.ledger.finish(row, "FAILED", "WORKER_NOT_STARTED")
                raise
            return 202, {"job_id": jid, "status": "ACCEPTED", "revision": int(row["revision"]),
                         "attempt": int(row["attempt"]), "result_ready": False}
        except Exception:
            self.slots.release()
            raise

    def _work(self, payload: dict, row: dict):
        try:
            try:
                self.pipeline.execute(payload, row)
                self.ledger.finish(row, "SUCCEEDED")
            except WaitingMemory:
                self.ledger.finish(row, "WAITING_MEMORY", "ASYNC_EXTRACTION_NOT_CONFIRMED")
            except InvalidData as exc:
                code = str(exc) if isinstance(exc, Rejected) else "VALIDATION_FAILED"
                self.ledger.finish(row, "FAILED", code)
            except Exception:
                # AWS応答に本文や識別情報が入る可能性があるため、例外のreprもログへ出さない。
                self.ledger.finish(row, "FAILED", "PROCESSING_FAILED")
        except Exception:
            # 台帳更新自体の失敗はRUNNINGを残す。期限後RECOVERY_REQUIREDとなり勝手に再実行しない。
            pass
        finally:
            with self.lock:
                self.running -= 1
            self.slots.release()

    def close(self):
        self.pool.shutdown(wait=True)


class RuntimeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], factory: Callable[[], JobService]):
        self.factory, self.service, self.service_lock = factory, None, threading.Lock()
        super().__init__(address, RuntimeHandler)

    def get_service(self):
        with self.service_lock:
            if self.service is None:
                self.service = self.factory()
            return self.service

    def server_close(self):
        super().server_close()
        if self.service is not None:
            self.service.close()


class RuntimeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass  # URL/ヘッダー/入力本文を標準アクセスログへ残さない。

    def reply(self, status: int, value: dict):
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if self.path != "/ping":
            self.reply(404, {"error": "NOT_FOUND"})
            return
        service = self.server.service
        self.reply(200, {"status": service.health() if service is not None else "Healthy"})

    def do_POST(self):
        if self.path != "/invocations":
            self.reply(404, {"error": "NOT_FOUND"})
            return
        try:
            if (self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json"
                    or self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1):
                fail("INVALID_HTTP_HEADERS")
            size = int(self.headers["Content-Length"])
            if not 1 <= size <= 32768:
                self.reply(413, {"error": "REQUEST_TOO_LARGE"})
                return
            data = self.rfile.read(size)
            if len(data) != size:
                fail("INCOMPLETE_HTTP_BODY")
            value = decode(data)
            status, result = self.server.get_service().invoke(value)
            self.reply(status, result)
        except ExternalExecutionBlocked:
            self.reply(503, {"error": "ONLINE_RELEASE_BLOCKED"})
        except (InvalidData, ValueError, KeyError, TypeError):
            self.reply(400, {"error": "INVALID_REQUEST_OR_CONFIGURATION"})
        except Exception:
            self.reply(503, {"error": "TEMPORARILY_UNAVAILABLE"})
