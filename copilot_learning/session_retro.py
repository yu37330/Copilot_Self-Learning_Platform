"""Issue #31: セッションから指示改善の候補を作る、完全オフラインの振り返り。

証拠の「検出」と改善策の「採用」を区別する。原文や秘密情報は出力しない。
既存のCopilot設定/Skills/Agentsを読み書きせず、推定原因を事実として扱わない。
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .core import InvalidData, SHA_PATTERN, assert_external_output, check_sensitive, digest, json_bytes
from .skill_preview import ROOT, RESERVED, _path

REPORT_FILE = "retrospective.preview.json"
MARKDOWN_FILE = "RETROSPECTIVE.preview.md"
MANIFEST_FILE = "manifest.json"
VERSION = "offline-session-retro-1"
MAX_SESSIONS = 32
MAX_MESSAGES = 15000
MAX_EVIDENCE_PER_RULE = 12
MAX_OUTPUT_BYTES = 256 * 1024

# 原因/改善内容の意味理解ではなく、レビューが必要な兆候を示す限定的な検出規則。
# ユーザー発言は明示的な訂正・要求に限り、ツールのERROR等はツール発言に限る。
RULES = (
    {
        "id": "instruction_conflict",
        "role": "USER",
        "pattern": re.compile(
            r"指示と違う|指示を守っていない|指示を無視|"
            r"(?:ignored?|disregarded?) (?:my|the) instructions", re.I),
        "target": ".github/copilot-instructions.md",
        "title": "共通指示と実際の応答の不一致",
        "recommendation": "元の指示と適用範囲を見直し、曖昧な完了条件を小さな検証可能なルールへ整理する案を検討する。",
        "validation": "既存の共通/局所指示と矛盾しないか照合し、独立した再現タスクで比較する。",
    },
    {
        "id": "role_boundary",
        "role": "USER",
        "pattern": re.compile(
            r"担当(?:が|は)違う|役割分担(?:が|を)違|別(?:の)?エージェント(?:の仕事|の担当)|"
            r"(?:wrong agent|agent responsibility|wrong role)", re.I),
        "target": ".github/agents/NAME.agent.md",
        "title": "エージェントの役割境界の確認",
        "recommendation": "担当範囲・禁止事項・引き渡し条件をカスタムエージェント定義に分離する必要があるか検討する。",
        "validation": "役割別の同一課題で受け渡し回数と越権操作が減るか検査する。",
    },
    {
        "id": "path_specific",
        "role": "USER",
        "pattern": re.compile(
            r"この(?:フォルダ|ディレクトリ)(?:だけ|のみ)|特定の(?:ファイル|ディレクトリ)(?:だけ|のみ)|"
            r"only (?:in|for) (?:this|these) (?:folder|directory|files?)", re.I),
        "target": ".github/instructions/NAME.instructions.md",
        "title": "指示の適用範囲を狭める余地",
        "recommendation": "共通ルールにせず対象パスとファイル種別が確認できる局所指示を検討する。",
        "validation": "対象外ファイルへ指示が漏れず、対象ファイルで期待どおり動作するか検査する。",
    },
    {
        "id": "missing_verification",
        "role": "USER",
        "pattern": re.compile(
            r"テストしていない|検証していない|未検証なのに|検証せず|未確認なのに|"
            r"(?:did not|didn't) (?:run|perform) (?:the )?tests?|(?:not|never) tested", re.I),
        "target": "AGENTS.md",
        "title": "検証・完了条件の明確化",
        "recommendation": "完了報告前に必須のテスト・実施不可なら未検証と明示する基準を整理する。",
        "validation": "テスト未実行の状態を成功扱いしない回帰ケースを追加する。",
    },
    {
        "id": "repeatable_workflow",
        "role": "USER",
        "pattern": re.compile(
            r"毎回同じ|何度も同じ|繰り返し作業|毎度(?:この)?手順|"
            r"(?:same steps|same task) (?:every time|again)|repeatedly perform", re.I),
        "target": ".github/skills/NAME/SKILL.md",
        "title": "繰り返し手順のSkill化候補",
        "recommendation": "何度も実施する手順の入力・出力・失敗条件を整理し、再利用Skillにする価値を検討する。",
        "validation": "Skillを使わない対照群と同じタスクで、成功率・再試行数・トークンを比較する。",
    },
    {
        "id": "explicit_correction",
        "role": "USER",
        "pattern": re.compile(
            r"意図と違う|まだ直っていない|やり直して|何度も修正|"
            r"(?:not what I asked|that's (?:incorrect|wrong)|please redo|still not fixed)", re.I),
        "target": "prompt",
        "title": "依頼文の完了条件・制約の確認",
        "recommendation": "次回の依頼文へ目的・変更範囲・禁止事項・確認方法を明示するテンプレートを検討する。",
        "validation": "同種の未学習タスクで訂正回数と成果物の適合率を比較する。",
    },
    {
        "id": "tool_failure",
        "role": "TOOL",
        "pattern": re.compile(
            r"(?:^|\n)(?:Traceback \(most recent call last\)|ERROR:|Error:|fatal:)|"
            r"\b(?:exit code|exit status)\s+[1-9]\d*\b", re.I),
        "target": "test_or_skill",
        "title": "ツール失敗時の手順・テストの見直し",
        "recommendation": "エラーが環境・操作手順・製品仕様のどれに由来するか確認してから、テストまたはSkill化を検討する。",
        "validation": "同じ失敗ケースで診断手順と再現テストが正しく動作するか調べる。",
    },
)


def _validated_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """正規化済みイベントの出典と証拠IDを検証し、重複セッションを拒否する。"""
    if not isinstance(sessions, list) or not (1 <= len(sessions) <= MAX_SESSIONS):
        raise InvalidData("セッション数は1〜32件に制限しています")
    result: list[dict[str, Any]] = []
    seen_fp: set[str] = set()
    message_total = 0
    for session in sessions:
        if not isinstance(session, dict) or session.get("record_type") != "normalized-session":
            raise InvalidData("検証済みの正規化セッションが必要です")
        fp = session.get("session_fingerprint")
        source = session.get("source_provenance")
        if (not isinstance(fp, str) or not SHA_PATTERN.fullmatch(fp) or fp in seen_fp
                or not isinstance(source, dict) or not isinstance(source.get("archive_sha256"), str)
                or not SHA_PATTERN.fullmatch(source["archive_sha256"])):
            raise InvalidData("原本の識別情報が不足・重複しています")
        seen_fp.add(fp)
        skipped = session.get("skipped_event_count")
        messages = session.get("messages")
        if (type(skipped) is not int or skipped < 0 or not isinstance(messages, list)
                or session.get("recognized_event_count") != len(messages)):
            raise InvalidData("イベント件数または欠落情報が不正です")
        message_total += len(messages)
        if message_total > MAX_MESSAGES:
            raise InvalidData("イベント件数の上限を超えています")
        refs: set[str] = set()
        for event in messages:
            if not isinstance(event, dict):
                raise InvalidData("イベント形式が不正です")
            index = event.get("event_index")
            text = event.get("text")
            role = event.get("role")
            evidence_id = event.get("evidence_id")
            if (type(index) is not int or index < 0 or role not in {"USER", "ASSISTANT", "TOOL"}
                    or not isinstance(text, str) or not text.strip() or len(text) > 10000
                    or evidence_id != f"{fp}:{index}" or evidence_id in refs):
                raise InvalidData("出典とイベントIDの整合性が不正です")
            check_sensitive(text)
            refs.add(evidence_id)
        result.append(session)
    return sorted(result, key=lambda item: item["session_fingerprint"])


def analyze_sessions(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """限定的な規則で候補を抽出。根本原因や改善効果を実測したとは扱わない。"""
    valid = _validated_sessions(sessions)
    matches: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for session in valid:
        fp = session["session_fingerprint"]
        for event in session["messages"]:
            for rule in RULES:
                if event["role"] == rule["role"] and rule["pattern"].search(event["text"]):
                    matches[rule["id"]].append((fp, event["evidence_id"]))
    findings = []
    for rule in RULES:
        rows = sorted(matches.get(rule["id"], []))
        if not rows:
            continue
        findings.append({
            "rule_id": rule["id"],
            "target": rule["target"],
            "title": rule["title"],
            "recommendation": rule["recommendation"],
            "validation": rule["validation"],
            "signal_count": len(rows),
            "session_count": len({fp for fp, _ in rows}),
            "evidence_ids": [evidence_id for _, evidence_id in rows[:MAX_EVIDENCE_PER_RULE]],
            "evidence_sample_truncated": len(rows) > MAX_EVIDENCE_PER_RULE,
            "assessment": "heuristic_only_review_required",
            "existing_instructions_reviewed": False,
        })
    return {
        "schema_version": "1.0",
        "record_type": "offline-session-retrospective",
        "analyzer_version": VERSION,
        "source_sessions": [
            {"session_fingerprint": session["session_fingerprint"],
             "archive_sha256": session["source_provenance"]["archive_sha256"],
             "recognized_event_count": len(session["messages"]),
             "skipped_event_count": session["skipped_event_count"]}
            for session in valid
        ],
        "coverage": "partial_unrecognized_events" if any(s["skipped_event_count"] for s in valid) else "known_events_only",
        "findings": findings,
        "finding_count": len(findings),
        "status": "manual_review_required",
        "causal_conclusion": None,
        "measured_improvement": None,
        "approved_for_copilot": False,
        "files_modified": False,
        "network_calls": 0,
    }


def _render_markdown(report: dict[str, Any]) -> bytes:
    # 固定文言と検証済みのハッシュ/整数しか出力しない。会話本文をコピーしない。
    lines = [
        "# セッション振り返り：未採用の改善提案",
        "",
        "これはキーワードによる検出結果であり、原因・推奨修正・効果を実証していません。",
        "原文は含みません。イベントIDと承認済み原本を人手で照合してください。",
        "既存の AGENTS.md / Skills / agent.md / Copilot設定は変更していません。",
        "",
        f"対象セッション: {len(report['source_sessions'])} / 検出種類: {report['finding_count']}",
        f"入力カバレッジ: {report['coverage']}",
        "",
    ]
    for item in report["findings"]:
        lines.extend([
            f"## {item['title']}",
            "",
            f"- 検討先: {item['target']}（自動適用禁止）",
            f"- 兆候: {item['signal_count']}件 / {item['session_count']}セッション",
            f"- 改善案（要確認）: {item['recommendation']}",
            f"- 検証案: {item['validation']}",
            "- 証拠ID（参照用）: " + ", ".join(item["evidence_ids"]),
            "- 根拠表示は最大12件。元のセッション内で周辺文脈と反証を確認すること。",
            "",
        ])
    if not report["findings"]:
        lines.extend(["検出規則に一致する明示的な兆候はありませんでした。問題がなかった証拠ではありません。", ""])
    lines.extend([
        "## 採用前の確認",
        "",
        "既存指示の重複・競合、原本の秘匿情報、ルールの適用外条件、独立テストを確認する。",
        "今後LLMやChronicleで質的分析する場合も、検出結果から自動反映してはいけません。",
        "",
    ])
    return "\n".join(lines).encode("utf-8")


def _destination(path: Path) -> Path:
    path = _path(path)
    assert_external_output(path, ROOT)
    if any(part.rstrip(" .").casefold() in RESERVED for part in path.parts):
        raise InvalidData("Copilot自動読込ディレクトリを出力先にできません")
    return path


def prepare_preview(sessions: list[dict[str, Any]]) -> tuple[dict[str, Any], bytes, bytes]:
    """生成結果をメモリ内で組み立て、サイズを確認する。"""
    report = analyze_sessions(sessions)
    report_bytes = json_bytes(report)
    markdown = _render_markdown(report)
    if len(report_bytes) + len(markdown) > MAX_OUTPUT_BYTES:
        raise InvalidData("振り返り結果がサイズ制限を超過しました")
    manifest = {
        "schema_version": "1.0", "record_type": "offline-retro-preview-manifest",
        "analyzer_version": VERSION,
        "inputs_sha256": digest(json_bytes(report["source_sessions"])),
        "files": [
            {"name": REPORT_FILE, "sha256": digest(report_bytes), "size_bytes": len(report_bytes)},
            {"name": MARKDOWN_FILE, "sha256": digest(markdown), "size_bytes": len(markdown)},
        ],
        "status": "not_installed_manual_review_required",
        "approved_for_copilot": False,
        "network_calls": 0,
    }
    return manifest, report_bytes, markdown


def export_preview(sessions: list[dict[str, Any]], destination: Path) -> dict[str, Any]:
    """Git管理外の新規ディレクトリにのみ出力し、途中状態は削除する。"""
    manifest, report, markdown = prepare_preview(sessions)
    out = _destination(destination)
    if out.exists():
        raise InvalidData("既存の出力先は上書きしません")
    out.parent.mkdir(parents=True, exist_ok=True)
    _path(out.parent)
    out.mkdir(mode=0o700)
    created = []
    try:
        for name, data in ((REPORT_FILE, report), (MARKDOWN_FILE, markdown), (MANIFEST_FILE, json_bytes(manifest))):
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
    return {"status": "preview_written_not_installed",
            "manifest_sha256": digest(json_bytes(manifest)), "finding_count": json.loads(report)["finding_count"],
            "network_calls": 0}


def verify_preview(sessions: list[dict[str, Any]], directory: Path, expected_manifest_sha256: str) -> dict[str, Any]:
    """独立保管したハッシュと元セッションに対して、現在のプレビューを再生成・照合する。"""
    if not isinstance(expected_manifest_sha256, str) or not SHA_PATTERN.fullmatch(expected_manifest_sha256):
        raise InvalidData("独立保管したmanifest SHA-256が必要です")
    out = _destination(directory)
    if not out.is_dir() or {p.name for p in out.iterdir()} != {REPORT_FILE, MARKDOWN_FILE, MANIFEST_FILE}:
        raise InvalidData("プレビューのファイル構成に変更があります")
    manifest, report, markdown = prepare_preview(sessions)
    expected = ((MANIFEST_FILE, json_bytes(manifest)), (REPORT_FILE, report), (MARKDOWN_FILE, markdown))
    for name, content in expected:
        file = _path(out / name)
        if not file.is_file() or file.stat().st_size != len(content) or file.read_bytes() != content:
            raise InvalidData("プレビューの内容または入力が一致しません")
    if digest(json_bytes(manifest)) != expected_manifest_sha256:
        raise InvalidData("独立保管したmanifest SHA-256と一致しません")
    return {"status": "verified_preview_only", "approved_for_copilot": False,
            "installation_status": "not_installed", "network_calls": 0}
