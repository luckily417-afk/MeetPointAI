"""确定性排序算法：地点类型和评分决定顺序，路线只校验双方可达。"""

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

@dataclass
class RankContext:
    rec_mode: RecMode = RecMode.TOTAL_TIME
    category: str = "任意类型"
    travel_mode: TravelMode = TravelMode.TRANSIT


def score_candidates(candidates: Iterable[Candidate], ctx: RankContext) -> list[Candidate]:
    """就地写入 score 并返回按分数升序的列表（分数越小越优）。"""
    scored: list[Candidate] = []
    for cand in candidates:
        rating = cand.poi.rating or 0.0
        score = -rating
        if ctx.rec_mode is RecMode.FEWEST_TRANSFERS:
            score += cand.total_transfers * 10.0
        if ctx.category != "任意类型" and not matches_category(cand, ctx.category):
            score += 10.0
        if not cand.routes_ok:
            score += 1000.0
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
        parts.append("双方均可到达")
    if cand.poi.type_name:
        parts.append(cand.poi.type_name.split(";")[-1])
    if cand.poi.business_area:
        parts.append(f"位于{cand.poi.business_area}")
    if cand.poi.rating:
        parts.append(f"高德评分 {cand.poi.rating}")
    return "，".join(parts) or "位于指定区域内，双方路线均可达"
