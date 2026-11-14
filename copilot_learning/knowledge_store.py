"""承認前の知識候補を管理するローカルSQLite台帳。

S3原本とは独立した知識の正本PoC。本人性は社内SSOで検証していないため、
状態activeは人間の明示操作を示すのみで、Copilotへの自動適用を意味しない。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import InvalidData, SHA_PATTERN, assert_external_output, check_sensitive, json_bytes
from .learning import candidate_sha256

STATES = {"proposed", "pending", "active", "retired", "superseded"}
ALLOWED_TRANSITIONS = {
    "proposed": {"pending", "retired"},
    "pending": {"active", "retired"},
    "active": {"retired", "superseded"},
    "retired": set(),
    "superseded": set(),
}


def _encoded(value: dict[str, Any]) -> str:
    data = json_bytes(value).decode("utf-8")
    check_sensitive(data)
    return data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeStore:
    """標準ライブラリのみ。BEGIN IMMEDIATEとversion付きCASで競合更新を拒否。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        assert_external_output(self.path, Path(__file__).resolve().parents[1])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('proposed','pending','active','retired','superseded')),
                    candidate_hash TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    replaced_by TEXT
                );
                CREATE TABLE IF NOT EXISTS knowledge_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    reviewer TEXT,
                    reason TEXT NOT NULL,
                    replaced_by TEXT,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(id, revision)
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """SQLite接続を必ず閉じ、エラー時はロールバックする。"""
        con = sqlite3.connect(str(self.path), timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _current(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "knowledge_id": row["id"], "revision": row["revision"], "status": row["status"],
            "candidate_sha256": row["candidate_hash"],
            "candidate": json.loads(row["candidate_json"]),
            "source": json.loads(row["source_json"]),
            "replaced_by": row["replaced_by"],
        }

    def get(self, knowledge_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            return self._current(db.execute("SELECT * FROM knowledge WHERE id=?", (knowledge_id,)).fetchone())

    def register(self, candidate: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        """評価済み候補をproposedで登録。keepは自動activeにはならない。"""
        if candidate.get("decision") not in {"keep", "pending"}:
            raise InvalidData("採用不能な候補は台帳へ登録できません")
        if candidate.get("approved_for_copilot") is not False:
            raise InvalidData("自動Copilot適用フラグは必ずFalseである必要があります")
        if not isinstance(source, dict) or not all(
            isinstance(source.get(key), str) and SHA_PATTERN.fullmatch(source[key])
            for key in ("session_fingerprint", "archive_sha256")
        ):
            raise InvalidData("原本のsession_fingerprint/archive_sha256が必要です")
        try:
            claim_hash = candidate_sha256(candidate)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidData("候補の内容が不正です") from exc
        knowledge_id = claim_hash[:24]
        if candidate.get("knowledge_id") not in (None, knowledge_id):
            raise InvalidData("knowledge_idと候補内容のハッシュが不一致です")
        if candidate.get("candidate_sha256") not in (None, claim_hash):
            raise InvalidData("候補のSHA-256が不一致です")
        candidate_json, source_json = _encoded(candidate), _encoded(source)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM knowledge WHERE id=?", (knowledge_id,)).fetchone()
            if row:
                if (row["candidate_hash"] != claim_hash or row["source_json"] != source_json
                        or row["candidate_json"] != candidate_json):
                    raise InvalidData("同じ知識IDで原本/候補内容が異なります（上書き禁止）")
                return self._current(row)  # type: ignore[return-value]
            db.execute("INSERT INTO knowledge VALUES (?,?,?,?,?,?,NULL)",
                       (knowledge_id, 1, "proposed", claim_hash, candidate_json, source_json))
            db.execute("""INSERT INTO knowledge_events
                       (id,revision,status,candidate_json,source_json,reviewer,reason,replaced_by,recorded_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                       (knowledge_id, 1, "proposed", candidate_json, source_json, None,
                        "candidate_registered", None, _now()))
        result = self.get(knowledge_id)
        assert result is not None
        return result

    def transition(self, knowledge_id: str, target: str, *, expected_revision: int,
                   reviewer: str, reason: str, replacement_id: str | None = None) -> dict[str, Any]:
        """手動レビューの結果をCAS更新。署名/社内SSO認証はまだ実装していない。"""
        if target not in STATES or not isinstance(expected_revision, int) or expected_revision < 1:
            raise InvalidData("状態または期待revisionが不正です")
        if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(reason, str) or not reason.strip():
            raise InvalidData("レビュー主体・理由が必要です")
        check_sensitive(reviewer + reason)
        if target == "superseded" and (not replacement_id or replacement_id == knowledge_id):
            raise InvalidData("supersededには別のactive知識IDが必要です")
        if target != "superseded" and replacement_id is not None:
            raise InvalidData("置換先はsuperseded以外で指定できません")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM knowledge WHERE id=?", (knowledge_id,)).fetchone()
            if row is None:
                raise InvalidData("知識IDが存在しません")
            if row["revision"] != expected_revision:
                raise InvalidData("知識の版が更新されています（CAS競合）")
            if target not in ALLOWED_TRANSITIONS[row["status"]]:
                raise InvalidData(f"不正な知識状態遷移: {row['status']} -> {target}")
            if target == "superseded":
                other = db.execute("SELECT status FROM knowledge WHERE id=?", (replacement_id,)).fetchone()
                if other is None or other["status"] != "active":
                    raise InvalidData("置換先はactiveな知識である必要があります")
            revision = expected_revision + 1
            done = db.execute("""UPDATE knowledge SET status=?, revision=?, replaced_by=?
                                  WHERE id=? AND revision=?""",
                              (target, revision, replacement_id, knowledge_id, expected_revision))
            if done.rowcount != 1:
                raise InvalidData("競合により知識の更新に失敗しました")
            db.execute("""INSERT INTO knowledge_events
                       (id,revision,status,candidate_json,source_json,reviewer,reason,replaced_by,recorded_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                       (knowledge_id, revision, target, row["candidate_json"], row["source_json"],
                        reviewer.strip(), reason.strip(), replacement_id, _now()))
        result = self.get(knowledge_id)
        assert result is not None
        return result

    def active(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM knowledge WHERE status='active' ORDER BY id ASC").fetchall()
            return [self._current(x) for x in rows]  # type: ignore[misc]

    def history(self, knowledge_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM knowledge_events WHERE id=? ORDER BY revision ASC", (knowledge_id,)).fetchall()
            return [{"knowledge_id": r["id"], "revision": r["revision"], "status": r["status"],
                     "candidate": json.loads(r["candidate_json"]), "source": json.loads(r["source_json"]),
                     "reviewer": r["reviewer"], "reason": r["reason"], "replaced_by": r["replaced_by"],
                     "recorded_at": r["recorded_at"]} for r in rows]
