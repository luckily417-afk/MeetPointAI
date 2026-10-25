"""确定性排序算法（Python 侧，不依赖 AI）。

取消原有"公平性评分"：不再以通勤时间差为目标，只保留可量化的排序维度：
* 最短总通勤时间（A+B 之和）
* 最少换乘
* 指定场所类型优先
所有排序均基于高德真实路线数据；无路线的候选项永远排在可用项之后。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from config.constants import CATEGORY_PRESETS
from models.schemas import Candidate, Recommendation, RecMode, TravelMode

__all__ = [
    "score_candidates",
    "rank_candidates",
    "to_recommendations",
    "matches_category",
]

# 各维度惩罚权重（仅影响排序，不是"公平性"评分）
PENALTY_NO_ROUTE = 10_000.0
PENALTY_TRANSFER = 180.0  # 每次换乘折算 3 分钟
PENALTY_LONG_WALK = 1.2  # 每米步行折算


@dataclass
class RankContext:
    rec_mode: RecMode = RecMode.TOTAL_TIME
    category: str = "任意类型"
    travel_mode: TravelMode = TravelMode.TRANSIT


def _route_penalty(leg) -> float:
    if leg is None or not leg.ok:
        return PENALTY_NO_ROUTE
    penalty = leg.transfers * PENALTY_TRANSFER
    if leg.mode is TravelMode.TRANSIT and leg.walk_m > 800:
        penalty += (leg.walk_m - 800) * PENALTY_LONG_WALK
    return penalty


def score_candidates(candidates: Iterable[Candidate], ctx: RankContext) -> list[Candidate]:
    """就地写入 score 并返回按分数升序的列表（分数越小越优）。"""
    scored: list[Candidate] = []
    for cand in candidates:
        total = cand.total_duration_s
        base = total / 60.0 if total > 0 else PENALTY_NO_ROUTE
        leg_a_penalty = _route_penalty(cand.leg_a)
        leg_b_penalty = _route_penalty(cand.leg_b)

        if ctx.rec_mode is RecMode.FEWEST_TRANSFERS:
            score = cand.total_transfers * 120.0 + base * 0.3
        elif ctx.rec_mode is RecMode.CATEGORY_PRIORITY:
            score = base + (0.0 if matches_category(cand, ctx.category) else 90.0)
        else:  # TOTAL_TIME / CUSTOM / fallback for AI
            score = base
        score += (leg_a_penalty + leg_b_penalty) / 60.0 * 0.5
        if not cand.routes_ok:
            score += PENALTY_NO_ROUTE
        cand.score = round(score, 2)
        scored.append(cand)
    scored.sort(key=lambda c: (c.score, c.poi.name))
    return scored


def matches_category(candidate: Candidate, category: str) -> bool:
    preset = CATEGORY_PRESETS.get(category)
    if not preset or not preset["codes"]:
        return True
    codes = preset["codes"].split("|")
    type_code = candidate.poi.type_code or ""
    return any(type_code.startswith(code) for code in codes)


def rank_candidates(
    candidates: Sequence[Candidate],
    ctx: RankContext,
    *,
    top_n: int = 5,
    require_routes: bool = True,
) -> list[Candidate]:
    pool = [c for c in candidates if c.routes_ok] if require_routes else list(candidates)
    if not pool:
        pool = [c for c in candidates if (c.leg_a and c.leg_a.ok) or (c.leg_b and c.leg_b.ok)]
    scored = score_candidates(pool, ctx)
    return scored[:top_n]


def to_recommendations(
    ordered: Sequence[Candidate],
    *,
    source: str = "rule",
    reasons: dict[str, str] | None = None,
    tags: dict[str, list[str]] | None = None,
) -> list[Recommendation]:
    """把排序后的候选转成对外结果（rank 从 1 开始）。"""
    out: list[Recommendation] = []
    for index, cand in enumerate(ordered, start=1):
        out.append(
            Recommendation(
                rank=index,
                poi=cand.poi,
                leg_a=cand.leg_a,
                leg_b=cand.leg_b,
                reason=(reasons or {}).get(cand.id, cand.reason)
                or _default_reason(cand),
                source=source,
                tags=(tags or {}).get(cand.id, cand.tags),
                score=cand.score,
            )
        )
    return out


def _default_reason(cand: Candidate) -> str:
    parts: list[str] = []
    if cand.leg_a and cand.leg_a.ok and cand.leg_b and cand.leg_b.ok:
        parts.append(
            f"A 约 {cand.leg_a.duration_min:.0f} 分钟、B 约 {cand.leg_b.duration_min:.0f} 分钟可达"
        )
    if cand.total_transfers == 0:
        parts.append("双方均无需换乘或换乘较少")
    if cand.poi.business_area:
        parts.append(f"位于{cand.poi.business_area}")
    if cand.poi.rating:
        parts.append(f"高德评分 {cand.poi.rating}")
    return "，".join(parts) or "位于指定区域内，双方路线均可达"
