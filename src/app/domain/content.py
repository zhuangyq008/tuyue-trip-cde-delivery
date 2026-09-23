"""可信内容检索（带来源与时效标注）。

需求文档 §4.3 P0：「关键事实可追溯；资料缺失、冲突或过期时明确提示」。

检索结果的每一条都必须携带 `source`、`updated_at`、`freshness`。
没有来源的内容不进结果集——这是「信息可追溯」（§2.3）在代码层的落点。

检索实现为确定性关键词打分（非向量检索）：原型阶段的目标是验证
「有来源、能标时效、冲突可暴露」这套契约，而不是比拼召回率。
换成 OpenSearch / Bedrock Knowledge Base 时只需替换本模块的 search()。
"""

from __future__ import annotations

from typing import Any

from ..adapters.catalog import Catalog
from ..core.clock import parse_date, today_jst
from ..core.config import get_config

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"


def _freshness(updated_at: str) -> tuple[str, int | None]:
    cfg = get_config()
    updated = parse_date(str(updated_at)[:10])
    if updated is None:
        return FRESHNESS_UNKNOWN, None
    age_days = (today_jst() - updated).days
    return (FRESHNESS_STALE if age_days > cfg.content_stale_days else FRESHNESS_FRESH), age_days


def _score(doc: dict[str, Any], terms: list[str], poi_id: str | None, tags: list[str]) -> float:
    score = 0.0
    haystack = f"{doc['title']}\n{doc['body']}\n{' '.join(doc.get('keywords') or [])}".lower()
    for term in terms:
        if term and term in haystack:
            score += 10.0
    if poi_id and doc.get("poi_id") == poi_id:
        score += 25.0
    for tag in tags:
        if tag in (doc.get("tags") or []):
            score += 6.0
    # 新鲜度轻微加权：不足以让过期资料被藏起来，只影响同分排序。
    freshness, age_days = _freshness(doc.get("updated_at", ""))
    if freshness == FRESHNESS_FRESH:
        score += 2.0
    elif freshness == FRESHNESS_STALE:
        score -= 2.0
    if age_days is not None:
        score -= min(age_days / 365.0, 2.0)
    return round(score, 3)


def _tokenise(query: str) -> list[str]:
    """中文场景下按字符二元组切分，配合关键词字段已足够支撑原型检索。"""
    cleaned = (query or "").strip().lower()
    if not cleaned:
        return []
    whitespace_terms = [t for t in cleaned.split() if t]
    bigrams = [cleaned[i : i + 2] for i in range(len(cleaned) - 1)] if len(cleaned) >= 2 else [cleaned]
    return list(dict.fromkeys([*whitespace_terms, *bigrams]))


def to_result(doc: dict[str, Any], score: float | None = None) -> dict[str, Any]:
    freshness, age_days = _freshness(doc.get("updated_at", ""))
    result: dict[str, Any] = {
        "doc_id": doc["doc_id"],
        "poi_id": doc.get("poi_id"),
        "title": doc["title"],
        "body": doc["body"],
        "tags": list(doc.get("tags") or []),
        # 来源是必填项，不是可选装饰。
        "source": doc["source"],
        "updated_at": doc["updated_at"],
        "freshness": freshness,
        "age_days": age_days,
        "has_conflict": bool(doc.get("conflicts_with_poi_hours")),
    }
    if score is not None:
        result["relevance_score"] = score

    notices: list[str] = []
    if freshness == FRESHNESS_STALE:
        notices.append(
            f"该资料最后更新于 {str(doc['updated_at'])[:10]}，已超过 {get_config().content_stale_days} 天，"
            "内容可能过期，请勿作为当前事实依据。"
        )
    if freshness == FRESHNESS_UNKNOWN:
        notices.append("该资料缺少可解析的更新时间，时效未知，标记为待确认。")
    if doc.get("conflicts_with_poi_hours"):
        notices.append("该资料与目的地知识库记录存在冲突，需人工核对；本系统不自行裁决。")
    if notices:
        result["notices"] = notices
    return result


def search(
    catalog: Catalog,
    *,
    query: str = "",
    poi_id: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    terms = _tokenise(query)
    tag_filter = tags or []

    scored: list[tuple[float, dict[str, Any]]] = []
    for doc in catalog.documents:
        if poi_id and doc.get("poi_id") != poi_id:
            continue
        if tag_filter and not any(tag in (doc.get("tags") or []) for tag in tag_filter):
            continue
        score = _score(doc, terms, poi_id, tag_filter)
        # 无查询词时按相关性基线全量返回（受 limit 约束）。
        if terms and score <= 0:
            continue
        scored.append((score, doc))

    # 排序键含 doc_id，保证同分结果顺序稳定（幂等前提）。
    scored.sort(key=lambda pair: (-pair[0], pair[1]["doc_id"]))
    selected = scored[: max(1, min(limit, 50))]

    results = [to_result(doc, score) for score, doc in selected]
    stale_count = sum(1 for r in results if r["freshness"] != FRESHNESS_FRESH)
    conflict_count = sum(1 for r in results if r["has_conflict"])

    return {
        "query": query,
        "poi_id": poi_id,
        "tags": tag_filter,
        "total_matched": len(scored),
        "returned": len(results),
        "results": results,
        "provenance_summary": {
            "all_results_have_source": all(bool(r.get("source")) for r in results),
            "stale_or_unknown_count": stale_count,
            "conflict_count": conflict_count,
            "source_types": sorted({r["source"].get("type", "unknown") for r in results}),
        },
        "notice": (
            "未检索到符合条件的授权内容；缺少依据时本系统不生成替代性描述。"
            if not results
            else "以上内容均来自授权语料，已标注来源与更新时间；动态的价格与库存不来自这些资料。"
        ),
    }
