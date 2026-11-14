"""日本語二文字gram + 英数字トークンによる軽量BM25検索。

結果は確認用の候補だけ。適用判定は語句の明示一致であり、意味的な保証はない。
"""
from __future__ import annotations

from collections import Counter
import math
import re
import unicodedata
from typing import Any

from .applicability import evaluate_applicability
from .core import InvalidData, check_sensitive
from .knowledge_store import KnowledgeStore

JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fffー]+")
TOKENS = re.compile(r"[a-z0-9_]+|[\u3040-\u30ff\u3400-\u9fffー]+")
FIELD_WEIGHTS = {"title": 3.0, "description": 1.5, "instruction": 1.2,
                 "generalization": 0.75, "applies_when": 1.2, "changed_paths": 3.0}


def tokenize(value: str) -> list[str]:
    text = unicodedata.normalize("NFKC", value).casefold()
    result: list[str] = []
    for m in TOKENS.finditer(text):
        token = m.group(0)
        if JAPANESE.fullmatch(token):
            # 日本語は語彙辞書が不要な文字bi-gram。1文字ならそのまま。
            result.extend(token[i:i+2] for i in range(max(1, len(token)-1)))
        elif len(token) >= 2:
            result.append(token)
    return result


def _features(row: dict[str, Any]) -> tuple[Counter[str], float]:
    candidate = row["candidate"]
    terms: Counter[str] = Counter()
    for key, weight in FIELD_WEIGHTS.items():
        value = row["source"].get(key, []) if key == "changed_paths" else candidate.get(key, "")
        if not isinstance(value, (str, list)):
            continue
        items = [value] if isinstance(value, str) else [x for x in value if isinstance(x, str)]
        for item in items:
            for token in tokenize(item):
                terms[token] += weight
    length = sum(terms.values())
    return terms, length


def search_active(store: KnowledgeStore, task: str, *, limit: int = 5,
                  changed_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """activeのみを対象とし、適用除外が一致する知識は返さない。"""
    if not isinstance(task, str) or not task.strip() or not isinstance(limit, int) or not 1 <= limit <= 25:
        raise InvalidData("検索タスクまたはlimitが不正です")
    if changed_paths is not None and (not isinstance(changed_paths, list) or
                                       not all(isinstance(s, str) and len(s) < 400 for s in changed_paths)):
        raise InvalidData("changed_pathsの形式が不正です")
    check_sensitive(task + "\n" + "\n".join(changed_paths or []))
    query = task + " " + " ".join(changed_paths or [])
    q = Counter(tokenize(query))
    if not q:
        return []
    candidates = store.active()
    if not candidates:
        return []
    docs = [_features(row) for row in candidates]
    n = len(candidates)
    avg_len = max(1.0, sum(length for _, length in docs) / n)
    df: Counter[str] = Counter()
    for tf, _ in docs:
        df.update({term: 1 for term in tf})
    results = []
    for row, (tf, doc_len) in zip(candidates, docs):
        applicability = evaluate_applicability(row["candidate"], query)
        if applicability["decision"] == "not_applicable":
            continue
        score = 0.0
        for word, count in q.items():
            freq = tf[word]
            if not freq:
                continue
            idf = math.log(1 + (n - df[word] + 0.5) / (df[word] + 0.5))
            score += count * idf * freq * 2.2 / (freq + 1.2 * (0.25 + 0.75 * doc_len / avg_len))
        if score <= 0:
            continue
        results.append({"knowledge_id": row["knowledge_id"], "revision": row["revision"],
                        "score": round(score, 6), "applicability": applicability["decision"],
                        "reason": applicability["reason"],
                        "approved_for_copilot": False})
    results.sort(key=lambda item: (-item["score"], item["knowledge_id"]))
    return results[:limit]
