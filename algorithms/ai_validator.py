"""AI 输出校验与拦截（需求 5.3 / 六 / 4.3 第 8 步）。

规则：
1. `poi_id` 必须存在于本次候选集合；
2. 不得重复、不得超过 top_n；
3. 推荐理由不得为空/超长，且不得包含明显虚构的地理断言（如自造坐标）；
4. 推荐地点必须再次通过本地几何硬约束（由调用方传入 region）；
5. 校验失败 → 有限次重试 → 仍失败进入降级（规则排序）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from pydantic import ValidationError

from models.schemas import AIItem, AIResponse, Candidate
from geometry.regions import Region

__all__ = ["ValidationResult", "validate_ai_payload", "build_ai_messages", "AIValidationError"]

_COORD_PATTERN = re.compile(r"\d{2,3}\.\d{3,}\s*[,，]\s*\d{1,2}\.\d{3,}")
_COMMUTE_REASON_PATTERN = re.compile(r"通勤|车程|路程|耗时|\d+\s*分钟|最短|最快|换乘")


class AIValidationError(ValueError):
    pass


@dataclass
class ValidationResult:
    ok: bool
    items: list[AIItem] = field(default_factory=list)
    summary: str = ""
    errors: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def error_text(self) -> str:
        return "；".join(self.errors)


def validate_ai_payload(
    raw: dict[str, Any] | None,
    candidates: Sequence[Candidate],
    *,
    top_n: int = 5,
    region: Region | None = None,
) -> ValidationResult:
    if not raw:
        return ValidationResult(False, errors=["AI 未返回可解析的 JSON"])
    try:
        parsed = AIResponse.model_validate(raw)
    except ValidationError as exc:
        messages = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:4]]
        return ValidationResult(False, errors=[f"AI 输出结构不符合要求：{'; '.join(messages)}"])

    known = {c.id: c for c in candidates}
    errors: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    valid: list[AIItem] = []

    for item in parsed.items:
        if item.poi_id not in known:
            errors.append(f"AI 返回了不存在的候选 ID：{item.poi_id}")
            dropped.append(item.poi_id)
            continue
        if item.poi_id in seen:
            errors.append(f"AI 重复推荐：{item.poi_id}")
            continue
        seen.add(item.poi_id)
        if _COORD_PATTERN.search(item.reason or ""):
            errors.append(f"AI 在理由中自行写入坐标：{item.poi_id}")
            dropped.append(item.poi_id)
            continue
        if _COMMUTE_REASON_PATTERN.search(item.reason or ""):
            errors.append(f"AI 使用通勤信息作为推荐理由：{item.poi_id}")
            dropped.append(item.poi_id)
            continue
        cand = known[item.poi_id]
        if region is not None and not region.contains(cand.poi.lng, cand.poi.lat):
            errors.append(f"AI 推荐了区域外的地点：{cand.poi.name}")
            dropped.append(item.poi_id)
            continue
        valid.append(item)

    if not valid:
        errors.append("AI 未给出任何通过校验的推荐")
        return ValidationResult(False, errors=errors, dropped=dropped)

    valid.sort(key=lambda i: (i.rank if i.rank > 0 else 999, i.poi_id))
    if len(valid) > top_n:
        valid = valid[:top_n]
    summary = (parsed.summary or "").strip()[:300]
    if _COMMUTE_REASON_PATTERN.search(summary):
        summary = "已按地点信息和用户偏好筛选，通勤数据仅用于确认双方可达。"
    return ValidationResult(
        ok=True,
        items=valid,
        summary=summary,
        errors=errors,
        dropped=dropped,
    )


def build_ai_messages(
    payload: dict[str, Any],
    *,
    retry_hint: str | None = None,
    strict: bool = False,
) -> list[dict[str, str]]:
    """构造 DeepSeek 消息。system 约束极度明确，user 只给结构化事实。"""
    system = (
        "你是「MeetPoint AI」的见面地点推荐官。你只能从用户提供的 candidates 数组里挑选地点，"
        "严禁编造任何地点、坐标、路线、通勤时间或营业信息。\n"
        "输出必须是单个 JSON 对象，结构严格如下：\n"
        '{"summary": "整体说明（<=200字）", "items": [{"poi_id": "候选ID", "rank": 1, '
        '"reason": "推荐理由（<=120字，只能引用提供的真实数据）", "highlights": ["标签"]}]}\n'
        "硬性规则：\n"
        "1) items 中每个 poi_id 必须逐字来自 candidates 的 poi_id，不得改写、不得新增；\n"
        "2) rank 从 1 开始且不重复，最多推荐 N 个（见 top_n）；\n"
        "3) reason 中不得出现坐标数字，不得声称任何 candidates 未提供的设施或营业状态；\n"
        "4) 通勤时间、路线距离及出发点远近不得用于筛选、排序或推荐理由；候选均已验证双方可达；\n"
        "5) 用户的地理范围是硬约束，你无权扩大或忽略；\n"
        "6) 若候选不足，宁可少给，不得凑数。\n"
        "只输出 JSON，不要 markdown 代码围栏，不要额外解释。"
    )
    if strict:
        system += "\n注意：上一次回答包含了不存在的 poi_id 或区域外地点，本次必须严格从给定列表中选择。"
    user_payload = dict(payload)
    if retry_hint:
        user_payload["_retry_hint"] = retry_hint
    import json

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def candidate_payload(
    candidates: Sequence[Candidate],
    *,
    point_a: dict[str, Any],
    point_b: dict[str, Any],
    region_desc: str,
    preferences: dict[str, Any],
    top_n: int,
) -> dict[str, Any]:
    """把候选压缩成 AI 可读的结构化 JSON（最小化地理位置暴露）。"""
    items: list[dict[str, Any]] = []
    for cand in candidates:
        item: dict[str, Any] = {
            "poi_id": cand.id,
            "name": cand.poi.name,
            "type": cand.poi.type_name,
            "address": cand.poi.address,
            "business_area": cand.poi.business_area,
            "rating": cand.poi.rating,
            "cost_per_person": cand.poi.cost,
            "both_sides_reachable": cand.routes_ok,
        }
        items.append(item)
    return {
        "task": "recommend_meeting_point",
        "top_n": top_n,
        "constraints": {
            "region": region_desc,
            "region_is_hard_constraint": True,
            "must_pick_from_candidates": True,
        },
        "points": {"A": point_a, "B": point_b},
        "preferences": preferences,
        "candidates": items,
        "output_contract": {
            "format": "json_object",
            "fields": ["summary", "items[].poi_id", "items[].rank", "items[].reason", "items[].highlights"],
        },
    }


