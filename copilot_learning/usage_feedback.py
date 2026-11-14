"""Skill試行と手動申告結果を、知識台帳とは別の追記専用台帳に記録する。

観測の真実性・本人性・因果効果は証明しない。知識状態の更新、Copilot実行、通信は行わない。
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .core import InvalidData, SHA_PATTERN, assert_external_output, digest, json_bytes
from .privacy_scan import load_policy
from .skill_preview import ROOT, RESERVED, MANIFEST_FILE, _path, _read, _check_text, verify_skill_preview

KINDS = {"synthetic", "manual_report"}
OUTCOMES = {"reported_success", "reported_failure", "inconclusive", "not_used"}


def _safe_path(path: Path) -> Path:
    path = _path(path)
    assert_external_output(path, ROOT)
    if any(part.rstrip(" .").casefold() in RESERVED for part in path.parts):
        raise InvalidData("自動読込用の保存先は禁止です")
    return path


def _sha(value: str) -> None:
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise InvalidData("独立保管したSHA-256が必要です")


@contextmanager
def _db(path: Path, *, create: bool = False, write: bool = False) -> Iterator[sqlite3.Connection]:
    path = _safe_path(path)
    if not path.exists() and not create:
        raise InvalidData("試行台帳が存在しません")
    if path.exists() and not path.is_file():
        raise InvalidData("試行台帳が通常ファイルではありません")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    mode = "rwc" if create else "rw" if write else "ro"
    conn = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if fresh:
            path.chmod(0o600)  # Windowsでは別途ACL・ディスク暗号化が必要。
        if create:
            # 別種のDBを誤って変更しない。既存の知識台帳には書き込まない。
            tables = {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {"usage_trials", "usage_results"}:
                raise InvalidData("別用途のDBを試行台帳として利用できません")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS usage_trials (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS usage_results (
                    trial_id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
                    sha256 TEXT NOT NULL, PRIMARY KEY(trial_id, revision));
            """)
        if write:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validated(row: sqlite3.Row) -> dict[str, Any]:
    body = row["document"].encode("utf-8")
    if digest(body) != row["sha256"]:
        raise InvalidData("台帳の整合性が失われています")
    data = json.loads(body)
    if not isinstance(data, dict) or json_bytes(data) != body:
        raise InvalidData("台帳の形式が不正です")
    return data


def _trial(db: sqlite3.Connection, trial_id: str, expected_sha256: str | None = None) -> dict[str, Any]:
    if not isinstance(trial_id, str) or not re.fullmatch(r"[a-f0-9]{24}", trial_id):
        raise InvalidData("試行IDが不正です")
    row = db.execute("SELECT * FROM usage_trials WHERE id=?", (trial_id,)).fetchone()
    if row is None:
        raise InvalidData("試行がありません")
    data = _validated(row)
    if data.get("trial_id") != trial_id or (expected_sha256 is not None and row["sha256"] != expected_sha256):
        raise InvalidData("試行の識別情報が一致しません")
    identity = {k: v for k, v in data.items() if k not in {"trial_id", "registered_at"}}
    if digest(json_bytes(identity))[:24] != trial_id:
        raise InvalidData("試行の内容が識別子と一致しません")
    return data


def _history(db: sqlite3.Connection, trial_id: str) -> list[dict[str, Any]]:
    previous = None
    rows = db.execute("SELECT * FROM usage_results WHERE trial_id=? ORDER BY revision", (trial_id,)).fetchall()
    history = []
    for number, row in enumerate(rows, 1):
        data = _validated(row)
        if (row["revision"] != number or data.get("revision") != number or data.get("trial_id") != trial_id
                or data.get("previous_sha256") != previous
                or data.get("payload_sha256") != digest(json_bytes(data.get("payload")))):
            raise InvalidData("結果履歴の順序・内容が一致しません")
        if data["payload"].get("outcome") not in OUTCOMES:
            raise InvalidData("結果の判定値が不正です")
        history.append({**data, "result_sha256": row["sha256"]})
        previous = row["sha256"]
    return history


def begin_trial(journal: Path, database: Path, preview: Path, task: str, policy: Path, *,
                expected_manifest_sha256: str, run_id: str, source_kind: str,
                review: dict[str, Any] | None = None) -> dict[str, Any]:
    """activeなプレビューを確認して試行を登録する。まだ使用・成功したとは数えない。"""
    if source_kind not in KINDS or not isinstance(run_id, str) or not (1 <= len(run_id) <= 200):
        raise InvalidData("試行の種別・実行IDが不正です")
    journal, database = _safe_path(journal), _safe_path(database)
    if journal == database or journal.is_relative_to(_path(preview)):
        raise InvalidData("試行台帳は知識台帳・プレビューと分離してください")
    _sha(expected_manifest_sha256)
    verify_skill_preview(database, preview, task, policy,
                         expected_manifest_sha256=expected_manifest_sha256, review=review)
    raw = _read(Path(preview) / MANIFEST_FILE)
    if digest(raw) != expected_manifest_sha256:
        raise InvalidData("検証中にプレビューが変更されました")
    manifest = json.loads(raw)
    terms, policy_sha = load_policy(_path(policy))
    if policy_sha != manifest["policy_sha256"]:
        raise InvalidData("検証中に機密辞書が変更されました")
    _check_text(run_id, terms["sensitive_literals"])
    identity = {
        "schema_version": "1.0", "record_type": "offline-usage-trial", "source_kind": source_kind,
        "run_id_sha256": digest(run_id.encode("utf-8")), "manifest_sha256": expected_manifest_sha256,
        **{k: manifest[k] for k in ("knowledge_id", "knowledge_revision", "candidate_sha256",
                                   "record_sha256", "task_sha256", "policy_sha256", "source")},
        "copilot_execution_verified": False, "external_transfer_authorized": False,
    }
    trial_id = digest(json_bytes(identity))[:24]
    with _db(journal, create=True, write=True) as db:
        row = db.execute("SELECT * FROM usage_trials WHERE id=?", (trial_id,)).fetchone()
        if row is not None:
            _trial(db, trial_id)
            return {"trial_id": trial_id, "trial_sha256": row["sha256"], "status": "already_registered"}
        data = {**identity, "trial_id": trial_id, "registered_at": datetime.now(timezone.utc).isoformat()}
        body = json_bytes(data)
        sha = digest(body)
        db.execute("INSERT INTO usage_trials VALUES (?,?,?)", (trial_id, body.decode("utf-8"), sha))
    return {"trial_id": trial_id, "trial_sha256": sha, "status": "registered_not_yet_observed"}


def record_result(journal: Path, trial_id: str, *, expected_trial_sha256: str,
                  expected_revision: int, outcome: str, knowledge_used: bool,
                  policy: Path, reporter: str, evidence: Path | None = None,
                  previous_result_sha256: str | None = None) -> dict[str, Any]:
    """結果は追記し、訂正も旧記録を消さない。active/keepの状態は一切変更しない。"""
    _sha(expected_trial_sha256)
    if type(expected_revision) is not int or expected_revision < 0 or type(knowledge_used) is not bool:
        raise InvalidData("期待版・使用申告が不正です")
    if outcome not in OUTCOMES or (outcome == "not_used" and knowledge_used):
        raise InvalidData("判定と使用申告が一致しません")
    if outcome in {"reported_success", "reported_failure"} and (not knowledge_used or evidence is None):
        raise InvalidData("成功・失敗の申告には使用申告と証拠ファイルが必要です")
    if expected_revision == 0 and previous_result_sha256 is not None:
        raise InvalidData("初回記録に前版は指定できません")
    if expected_revision > 0:
        _sha(previous_result_sha256)
    terms, policy_sha = load_policy(_path(policy))
    if not isinstance(reporter, str) or not (1 <= len(reporter.strip()) <= 200):
        raise InvalidData("記録者の識別子が必要です")
    _check_text(reporter, terms["sensitive_literals"])
    proof = None
    if evidence is not None:
        raw = _read(_safe_path(evidence))
        text = raw.decode("utf-8")
        if not text.strip():
            raise InvalidData("証拠ファイルが空です")
        _check_text(text, terms["sensitive_literals"])
        proof = {"sha256": digest(raw), "size_bytes": len(raw), "validation": "bytes_only_not_truth"}
    payload = {"outcome": outcome, "knowledge_used_reported": knowledge_used, "evidence": proof,
               "reporter_sha256": digest(reporter.encode("utf-8")), "policy_sha256": policy_sha}
    payload_sha = digest(json_bytes(payload))
    with _db(journal, write=True) as db:
        trial = _trial(db, trial_id, expected_trial_sha256)
        if trial["policy_sha256"] != policy_sha:
            raise InvalidData("試行登録時と機密辞書が異なります")
        history = _history(db, trial_id)
        # 同一内容の再送は同じ記録を返す。別内容の競合は上書きせず拒否する。
        if (history and len(history) == expected_revision + 1
                and history[-1]["payload_sha256"] == payload_sha
                and history[-1]["previous_sha256"] == previous_result_sha256):
            return {k: history[-1][k] for k in ("trial_id", "revision", "result_sha256")}
        if len(history) != expected_revision or (history and history[-1]["result_sha256"] != previous_result_sha256):
            raise InvalidData("結果の版が更新されています（CAS競合）")
        data = {"trial_id": trial_id, "revision": expected_revision + 1,
                "previous_sha256": previous_result_sha256, "payload": payload,
                "payload_sha256": payload_sha, "recorded_at": datetime.now(timezone.utc).isoformat()}
        body = json_bytes(data)
        sha = digest(body)
        db.execute("INSERT INTO usage_results VALUES (?,?,?,?)",
                   (trial_id, data["revision"], body.decode("utf-8"), sha))
    return {"trial_id": trial_id, "revision": data["revision"], "result_sha256": sha}


def usage_report(journal: Path) -> dict[str, Any]:
    """合成結果と手動申告を別集計。失敗や訂正を再レビュー候補として返すだけ。"""
    summaries: dict[tuple[str, str, int], dict[str, Any]] = {}
    with _db(journal) as db:
        for row in db.execute("SELECT id FROM usage_trials ORDER BY id"):
            trial = _trial(db, row["id"])
            history = _history(db, row["id"])
            key = (trial["source_kind"], trial["knowledge_id"], trial["knowledge_revision"])
            group = summaries.setdefault(key, {"source_kind": key[0], "knowledge_id": key[1], "revision": key[2],
                "trials": 0, "unreported": 0, **{k: 0 for k in sorted(OUTCOMES)},
                "corrections": 0, "requires_review": False})
            group["trials"] += 1
            group[history[-1]["payload"]["outcome"] if history else "unreported"] += 1
            group["corrections"] += max(0, len(history) - 1)
            if len(history) > 1 or any(x["payload"]["outcome"] in {"reported_failure", "inconclusive"} for x in history):
                group["requires_review"] = True
    return {"schema_version": "1.0", "record_type": "offline-usage-report", "groups": list(summaries.values()),
            "automatic_knowledge_changes": 0, "copilot_execution_verified": False,
            "causal_improvement_verified": False, "network_calls": 0}
