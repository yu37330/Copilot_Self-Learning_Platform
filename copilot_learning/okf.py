"""OKF v0.2のWiki出力と構造検査。形式適合は真実性・承認・実行権限を意味しない。"""
from __future__ import annotations

import html
import json
import posixpath
import re
from datetime import date
from pathlib import PurePosixPath
from typing import Any, Mapping

from .core import InvalidData, digest, json_bytes

VERSION = "0.2"
RENDERER = "copilot-wiki-okf-1"
SPEC_COMMIT = "ad30107c31c06aec8a7d5636e0d1058118604e6f"
SPEC_URL = f"https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/{SPEC_COMMIT}/SPEC.md"
MAX_DOCUMENT_BYTES = 2_000_000
MAX_BUNDLE_BYTES = 16_000_000
MAX_FILES = 512
RESERVED = {"index.md", "log.md"}


def _frontmatter(fields: dict[str, Any]) -> str:
    # JSONのflow値はYAMLでも有効。改行・コロン・引用符による項目注入を防ぐ。
    return "---\n" + "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)}"
        for key, value in fields.items()
    ) + "\n---\n\n"


def _escape(text: str) -> str:
    # 出典やLLM由来のHTML/Markdownを実行可能なリンクや構文として解釈させない。
    text = html.escape(text)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>])", r"\\\1", text)


def _quote(text: str) -> str:
    return "\n".join("> " + _escape(line) for line in text.splitlines())


def _source_label(source_id: str) -> str:
    # source_idは既存検査済みASCII。大小文字が違うIDもWindows上で衝突させない。
    return "src-" + source_id.encode("utf-8").hex()


def render_wiki_payloads(revision: dict[str, Any]) -> dict[str, bytes]:
    """検査済みrevisionから決定論的に生成。生成日時や検証者を推測しない。"""
    sources = {source["source_id"]: source for source in revision["sources"]}
    payloads: dict[str, bytes] = {}
    excerpt_sets: dict[str, set[str]] = {}
    revision_sha = digest(json_bytes(revision))
    index = ["# Wiki drafts", ""]
    for page in revision["pages"]:
        page_id = page["page_id"]
        if page_id + ".md" in RESERVED:
            raise InvalidData("index/logはOKF予約名です。ページIDを変更してください")
        cited = sorted({ref["source_id"] for c in page["claims"] for ref in c["evidence"]})
        entries = []
        for sid in cited:
            source = sources[sid]
            label = _source_label(sid)
            entries.append({"id": label, "resource": f"references/{label}.json",
                            "title": source["title"], "x_source_id": sid,
                            "x_source_sha256": source["sha256"],
                            "x_provenance": source["provenance"]})
        metadata = {
            "type": "Reference", "title": page["title"],
            "description": "出典と適用条件を保持した未承認のWiki草案。",
            "status": "draft", "generated": {"by": "process:" + RENDERER},
            "sources": entries,
            "x_copilot_learning": {
                "mode": revision["mode"], "proposal_generator": revision["generator"],
                "revision_sha256": revision_sha, "source_snapshot_sha256": revision["source_snapshot_sha256"],
                "parent_sha256": revision["parent_sha256"],
                "approved_for_copilot": False, "semantic_truth_verified": False,
            },
        }
        chunks = [_frontmatter(metadata), "# Wiki draft\n", _quote(page["title"]),
                  "未承認・自動適用禁止。引用一致や形式適合は事実の保証ではありません。\n"]
        for claim in page["claims"]:
            labels = list(dict.fromkeys(_source_label(r["source_id"]) for r in claim["evidence"]))
            citations = "".join(f"[^{label}]" for label in labels)
            chunks += ["## " + claim["key"], _quote(claim["statement"]) + citations,
                       "値: " + _quote(claim["value"]),
                       "条件: " + _quote(" / ".join(claim["applies_when"]) or "未指定")]
            for ref in claim["evidence"]:
                excerpt_sets.setdefault(ref["source_id"], set()).add(ref["quote"])
                chunks += ["引用: " + _quote(ref["quote"])]
        chunks += ["## 関連ページ"] + [f"[{pid}]({pid}.md)" for pid in page["links"]]
        chunks += ["## 出典"]
        for entry in entries:
            chunks.append(f"[^{entry['id']}]: " + _escape(" ".join(entry["title"].split()))
                          + " — " + entry["x_provenance"]["kind"] + "（未検証）")
        payloads[page_id + ".md"] = ("\n\n".join(chunks) + "\n").encode("utf-8")
        index.append(f"* [{page_id}]({page_id}.md) - 出典と適用条件を保持した未承認のWiki草案。")
    # resourceは架空のS3 URLではなく、同梱した引用抜粋JSONの実在パスを指す。
    # 元の全文は再配布しない。原本SHAと抜粋のハッシュを混同しない。
    for sid, quotes in sorted(excerpt_sets.items()):
        payloads[f"references/{_source_label(sid)}.json"] = json_bytes({
            "schema_version": "1.0", "record_type": "wiki-evidence-excerpts",
            "source": sources[sid], "quotes": sorted(quotes),
            "contains_full_source": False, "semantic_truth_verified": False,
        })
    payloads["index.md"] = (_frontmatter({"okf_version": VERSION}) + "\n".join(index) + "\n").encode("utf-8")
    payloads["wiki.json"] = json_bytes(revision)
    report = validate_okf_files(payloads)
    if not report["conformant"]:
        raise InvalidData("生成WikiがOKFの構造検査に適合しません")
    return payloads


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """SafeLoaderを使う。形式の検査であり、任意のWikiを実行・採用しない。"""
    try:
        import yaml
    except ImportError as exc:
        raise InvalidData("OKF検査にはrequirements-okf.txtの依存関係が必要です") from exc
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise InvalidData("OKF文書が上限サイズを超えています")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip("\r\n") != "---":
        raise InvalidData("先頭のYAML frontmatterが必要です")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip("\r\n") == "---"), None)
    if end is None:
        raise InvalidData("YAML frontmatterが閉じていません")
    head = "".join(lines[1:end])
    try:
        # alias循環や過剰な深さを、モデル入力として利用する前に拒否する。
        # これは安全なローカルプロファイルでありOKFの必須条件とは別の制限。
        depth = 0
        for number, event in enumerate(yaml.parse(head)):
            if number > 20000 or isinstance(event, yaml.events.AliasEvent):
                raise InvalidData("YAMLの別名参照または過剰な構造は受け付けません")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise InvalidData("YAMLの階層が深すぎます")
            if isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        class UniqueLoader(yaml.SafeLoader):
            pass
        def unique_mapping(loader, node):
            pairs = loader.construct_pairs(node, deep=True)
            result = {}
            for key, value in pairs:
                if key in result:
                    raise InvalidData("YAMLに重複キーがあります")
                result[key] = value
            return result
        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
        fields = yaml.load(head, Loader=UniqueLoader)
    except (yaml.YAMLError, TypeError, ValueError, RecursionError) as exc:
        raise InvalidData("安全に解釈できるYAML mappingが必要です") from exc
    if not isinstance(fields, dict):
        raise InvalidData("YAML frontmatterはmappingが必要です")
    # 明示的な一人の検証者は仕様に従いリストへ正規化する。信頼は別の判定。
    if isinstance(fields.get("verified"), dict):
        fields["verified"] = [fields["verified"]]
    return fields, "".join(lines[end + 1:])


def validate_okf_files(files: Mapping[str, bytes]) -> dict[str, Any]:
    """§11の構造検査。未知type/任意キー/欠落索引/壊れたリンクでは拒否しない。"""
    if (not isinstance(files, Mapping) or len(files) > MAX_FILES
            or any(not isinstance(n, str) or not isinstance(b, bytes) for n, b in files.items())
            or sum(len(b) for b in files.values()) > MAX_BUNDLE_BYTES):
        raise InvalidData("OKFバンドルの形式または容量が不正です")
    errors, warnings, concepts = [], [], 0
    def issue(target, filename, code):
        target.append({"file": filename, "code": code})
    for name, raw in sorted(files.items()):
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name
                or not path.parts or any(p in {"", "."} for p in path.parts)):
            raise InvalidData("相対的なバンドル内パスが必要です")
        if not name.endswith(".md"):
            continue
        if len(raw) > MAX_DOCUMENT_BYTES:
            issue(errors, name, "document_too_large")
            continue
        try:
            text = raw.decode("utf-8")
            if path.name == "index.md":
                body = text
                if text.startswith("---\n") or text.startswith("---\r\n"):
                    fields, body = parse_frontmatter(text)
                    if name != "index.md" or set(fields) != {"okf_version"}:
                        issue(errors, name, "index_frontmatter_not_allowed")
                    elif fields["okf_version"] != VERSION:
                        issue(warnings, name, "different_okf_version")
                if not re.search(r"^#{1,6} \S", body, re.M):
                    issue(errors, name, "index_section_missing")
            elif path.name == "log.md":
                body = text
                if text.startswith("---\n") or text.startswith("---\r\n"):
                    issue(errors, name, "log_not_flat_date_grouped")
                headings = re.findall(r"^## (.+)$", text, re.M)
                if not headings:
                    issue(errors, name, "log_date_groups_missing")
                for value in headings:
                    try:
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                            raise ValueError()
                        date.fromisoformat(value)
                    except ValueError:
                        issue(errors, name, "log_date_invalid")
                if headings != sorted(headings, reverse=True):
                    issue(errors, name, "log_not_newest_first")
            else:
                fields, body = parse_frontmatter(text)
                if not isinstance(fields.get("type"), str) or not fields["type"].strip():
                    issue(errors, name, "type_missing_or_empty")
                concepts += 1
                # この形式検査はstatus/verifiedを根拠に自動採用しない。
            for link in re.findall(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)\)", body):
                if ":" in link or link.startswith("#"):
                    continue
                link = link.split("#", 1)[0]
                resolved = posixpath.normpath(link.lstrip("/") if link.startswith("/")
                                              else posixpath.join(str(path.parent), link))
                if resolved not in files and not any(n.startswith(resolved.rstrip("/") + "/") for n in files):
                    issue(warnings, name, "broken_cross_link")
        except (InvalidData, UnicodeError):
            issue(errors, name, "invalid_or_unsafe_frontmatter")
    return {"okf_version": VERSION, "spec_commit": SPEC_COMMIT,
            "conformant": not errors, "concept_count": concepts,
            "errors": errors, "warnings": warnings,
            "check_scope": "structural_only_safe_yaml_profile", "semantic_truth_verified": False,
            "external_transfer_authorized": False, "approved_for_copilot": False}
