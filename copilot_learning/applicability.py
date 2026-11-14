"""知識の適用範囲を安全側に判定するオフライン部品。

語句一致は意味的な正しさを保証しない。applicableでも自動Skill投入しない。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .core import InvalidData, check_sensitive

SCOPE_FIELDS = ("applies_when", "not_when", "counterexamples")
MAX_SCOPE_ITEMS = 12
MAX_SCOPE_CHARS = 240


def normalize_phrase(value: str) -> str:
    """全半角・大小文字・空白を統一。日本語の部分一致に利用する。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def validate_scope(item: dict[str, Any]) -> dict[str, list[str]]:
    """任意フィールドが存在する場合は厳密に検証する。"""
    cleaned: dict[str, list[str]] = {}
    for key in SCOPE_FIELDS:
        if key not in item:
            continue
        raw = item[key]
        if not isinstance(raw, list) or len(raw) > MAX_SCOPE_ITEMS:
            raise InvalidData(f"{key} は最大{MAX_SCOPE_ITEMS}件の配列が必要です")
        values: list[str] = []
        seen: set[str] = set()
        for value in raw:
            if not isinstance(value, str) or not (2 <= len(value.strip()) <= MAX_SCOPE_CHARS):
                raise InvalidData(f"{key} の各要素は2〜{MAX_SCOPE_CHARS}文字の文字列です")
            check_sensitive(value)
            norm = normalize_phrase(value)
            if norm in seen:
                raise InvalidData(f"{key} に重複があります")
            seen.add(norm)
            values.append(value.strip())
        cleaned[key] = values
    return cleaned


def evaluate_applicability(item: dict[str, Any], task: str) -> dict[str, Any]:
    """条件の明示的一致だけを評価。欠落や曖昧さはneeds_review。"""
    if not isinstance(task, str) or not task.strip() or len(task) > 20_000:
        raise InvalidData("タスクは1〜20000文字の文字列が必要です")
    check_sensitive(task)
    scope = validate_scope(item)
    normalized = normalize_phrase(task)
    positives = scope.get("applies_when", [])
    exclusions = scope.get("not_when", []) + scope.get("counterexamples", [])
    matched_negative = [v for v in exclusions if normalize_phrase(v) in normalized]
    if matched_negative:
        return {"decision": "not_applicable", "reason": "explicit_exclusion_match", "matched": matched_negative}
    if not positives or not exclusions:
        return {"decision": "needs_review", "reason": "insufficient_scope_boundaries", "matched": []}
    matched_positive = [v for v in positives if normalize_phrase(v) in normalized]
    if len(matched_positive) != len(positives):
        return {"decision": "needs_review", "reason": "positive_conditions_not_proven", "matched": matched_positive}
    return {"decision": "applicable", "reason": "literal_scope_match_not_semantic_proof", "matched": matched_positive}
