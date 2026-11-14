#!/usr/bin/env python3
"""Copilotの終了済みセッションを、送信前にローカルで検査・梱包する。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0"
ALLOWED_FILES = ("events.jsonl", "workspace.yaml", "plan.md")
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_SESSION_BYTES = 60 * 1024 * 1024
SECRET_PATTERNS = {
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "authorization_header": re.compile(r"(?i)\bAuthorization\s*[:=]\s*[\"']?(?:Bearer|Basic)\s+\S+"),
    "aws_secret_key": re.compile(r"(?i)\baws_secret_access_key\b\s*[=:]\s*[\"']?[A-Za-z0-9/+]{24,}"),
}


class UnsafeSession(Exception):
    """送信・梱包に適さないセッション。"""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def reject_repo_output(directory: Path) -> None:
    for ancestor in (directory, *directory.parents):
        if (ancestor / ".git").exists():
            raise ValueError("出力先をGit管理ディレクトリの外にしてください（秘密情報の誤コミット防止）")


def available_sessions(source: Path) -> list[Path]:
    if not source.is_dir():
        raise ValueError(f"セッション保存先が存在しません: {source}")
    return sorted(
        (entry for entry in source.iterdir() if entry.is_dir() and not entry.is_symlink()),
        key=lambda entry: entry.name,
    )


def session_files(session: Path, minimum_age_minutes: int) -> list[tuple[str, bytes]]:
    if session.is_symlink() or not session.is_dir():
        raise UnsafeSession("セッションディレクトリが不正です")
    event_log = session / "events.jsonl"
    if not event_log.is_file() or event_log.is_symlink():
        raise UnsafeSession("events.jsonl が存在しないかシンボリックリンクです")
    if datetime.fromtimestamp(event_log.stat().st_mtime, timezone.utc) > (
        datetime.now(timezone.utc) - timedelta(minutes=minimum_age_minutes)
    ):
        raise UnsafeSession("実行中の可能性があるため保留（最終更新からの経過時間不足）")

    collected = []
    total = 0
    for name in ALLOWED_FILES:
        path = session / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or path.resolve().parent != session.resolve():
            raise UnsafeSession(f"許可されていないファイル形式: {name}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_SESSION_BYTES:
            raise UnsafeSession("ファイルまたはセッションの容量上限を超過")
        data = path.read_bytes()
        total += len(data)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsafeSession(f"UTF-8以外のデータ: {name}") from exc
        if name == "events.jsonl":
            try:
                for line in content.splitlines():
                    if line.strip():
                        json.loads(line)
            except json.JSONDecodeError as exc:
                raise UnsafeSession("events.jsonl が有効なJSON Linesではありません") from exc
        detections = [label for label, pattern in SECRET_PATTERNS.items() if pattern.search(content)]
        if detections:
            raise UnsafeSession(f"秘密情報の疑い: {','.join(detections)}（本文は非表示）")
        collected.append((name, data))
    return collected


def build_archive(files: list[tuple[str, bytes]]) -> bytes:
    """タイムスタンプを固定し、同じ入力から同じZIPを作成する。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files:
            entry = zipfile.ZipInfo(filename=filename, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o600 << 16
            archive.writestr(entry, content)
    return buffer.getvalue()


def prepare(session: Path, output_dir: Path, minimum_age_minutes: int) -> Path:
    files = session_files(session, minimum_age_minutes)
    components = [{"name": name, "sha256": sha256(data), "size_bytes": len(data)} for name, data in files]
    fingerprint = sha256(json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    archive_content = build_archive(files)
    archive_name = f"session-{fingerprint}.zip"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "copilot-session-archive",
        "session_fingerprint": fingerprint,
        "source_session_id_sha256": sha256(session.name.encode("utf-8")),
        "archive_file": archive_name,
        "archive_sha256": sha256(archive_content),
        "contents": components,
        "review_status": "requires_manual_approval",
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / archive_name
    manifest_path = output_dir / f"session-{fingerprint}.manifest.json"
    archive_path.write_bytes(archive_content)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.home() / ".copilot" / "session-state")
    parser.add_argument("--list", action="store_true", help="ローカルセッションの一覧表示のみ")
    parser.add_argument("--session", action="append", default=[], help="収集するセッションID（繰り返し指定可）")
    parser.add_argument("--all", action="store_true", help="終了済みの全セッションを対象にする")
    parser.add_argument("--out", type=Path, help="Git管理外のローカル出力ディレクトリ")
    parser.add_argument("--minimum-age-minutes", type=int, default=30, help="更新後の保留時間。既定30分")
    args = parser.parse_args(argv)
    if args.minimum_age_minutes < 1:
        parser.error("--minimum-age-minutes は1以上")
    if args.all and args.session:
        parser.error("--all と --session は併用できません")
    try:
        source = args.source.expanduser().resolve()
        sessions = available_sessions(source)
        if args.list:
            for session in sessions:
                event = session / "events.jsonl"
                state = "events.jsonlあり" if event.is_file() else "イベントなし"
                print(f"{session.name}\t{state}")
            print(f"件数: {len(sessions)}")
            return 0
        if not args.all and not args.session:
            parser.error("--list、--all、--session のいずれかを指定してください")
        if args.out is None:
            parser.error("梱包時は --out が必要です")
        output_dir = args.out.expanduser().resolve()
        reject_repo_output(output_dir)
        if within(output_dir, source):
            raise ValueError("出力先をCopilotセッションフォルダの外にしてください")
        selection = sessions if args.all else [source / session_id for session_id in args.session]
        success = 0
        failures = 0
        for session in selection:
            try:
                if not within(session.resolve(), source) or session.parent != source or session.is_symlink():
                    raise UnsafeSession("セッションIDは直下のディレクトリ名で指定してください")
                archive = prepare(session, output_dir, args.minimum_age_minutes)
                print(f"PREPARED {session.name}: {archive.name}（未承認・未送信）")
                success += 1
            except (UnsafeSession, OSError) as exc:
                print(f"SKIPPED {session.name}: {exc}", file=sys.stderr)
                failures += 1
        print(f"ローカル梱包: {success}件、スキップ: {failures}件、S3送信: 0件")
        return 1 if failures or not success else 0
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
