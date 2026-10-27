"""内置合成数据：用于 --mock 模式与离线边界测试（不访问任何网络）。"""

from __future__ import annotations

import hashlib
import math
import random
from typing import Sequence

from geometry.regions import CircleRegion, PolygonRegion, Region
from models.schemas import Poi, RouteLeg, TravelMode

__all__ = ["generate_pois", "mock_route", "mock_transit"]

_SPEED_MPS = {
    TravelMode.WALKING: 1.2,
    TravelMode.BICYCLING: 4.0,
    TravelMode.DRIVING: 9.0,
    TravelMode.TRANSIT: 5.0,
}
_DETOUR = {
    TravelMode.WALKING: 1.25,
    TravelMode.BICYCLING: 1.3,
    TravelMode.DRIVING: 1.35,
    TravelMode.TRANSIT: 1.4,
}


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _haversine(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def generate_pois(
    region: Region | None,
    *,
    center: tuple[float, float],
    radius_m: float = 3000.0,
    category: str = "咖啡厅",
    count: int = 48,
    seed_text: str = "mock",
    type_code: str = "050500",
) -> list[Poi]:
    """在区域内部生成确定性的合成 POI（网格 + 抖动）。

    `category` 与 `type_code` 应当来自本次真实请求（关键词 + 类型编码），
    否则合成数据会与所求场所类型对不上，无法用来验证类别相关的逻辑。
    """
    rng = random.Random(_seed(seed_text + category))
    if region is not None:
        minx, miny, maxx, maxy = region.bbox()
    else:
        dlat = radius_m / 111_320.0
        dlng = radius_m / (111_320.0 * max(0.2, math.cos(math.radians(center[1]))))
        minx, miny = center[0] - dlng, center[1] - dlat
        maxx, maxy = center[0] + dlng, center[1] + dlat

    category = (category or "地点").strip() or "地点"
    type_name = f"模拟场所;{category}"
    pois: list[Poi] = []
    span_x = max(1e-6, maxx - minx)
    span_y = max(1e-6, maxy - miny)
    side = max(2, int(math.sqrt(count)) + 1)
    index = 0
    for ix in range(side):
        for iy in range(side):
            if len(pois) >= count:
                break
            lng = minx + span_x * (ix + 0.5) / side + rng.uniform(-0.0004, 0.0004)
            lat = miny + span_y * (iy + 0.5) / side + rng.uniform(-0.0004, 0.0004)
            if region is not None and not region.contains(lng, lat):
                continue
            index += 1
            pois.append(
                Poi(
                    id=f"MOCK{index:04d}",
                    name=f"{category}·模拟点位{index:02d}",
                    lng=round(lng, 6),
                    lat=round(lat, 6),
                    address=f"模拟市测试区样例路 {index} 号",
                    type_name=type_name,
                    type_code=type_code,
                    cityname="模拟市",
                    adname="测试区",
                    adcode="000000",
                    business_area="模拟商圈",
                    rating=round(rng.uniform(3.5, 4.9), 1),
                    cost=float(rng.choice([18, 25, 30, 45, 60, 88])),
                )
            )
    return pois[:count]


def mock_route(
    mode: TravelMode,
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    seed_text: str = "mock",
) -> RouteLeg:
    """确定性路线：距离 = 直线 × 绕行系数，时间 = 距离 / 速度。"""
    straight = _haversine(origin[0], origin[1], destination[0], destination[1])
    if straight < 1.0:
        straight = 1.0
    distance = straight * _DETOUR.get(mode, 1.3)
    duration = distance / _SPEED_MPS.get(mode, 6.0)
    transfers = 0
    walk = 0.0
    if mode is TravelMode.TRANSIT:
        transfers = min(3, int(distance // 4000))
        walk = round(min(1200.0, distance * 0.15), 1)
        duration += transfers * 300 + walk / 1.2
    if mode is TravelMode.WALKING:
        walk = distance
    rng = random.Random(_seed(f"{seed_text}:{origin}:{destination}:{mode.value}"))
    jitter = 0.9 + rng.random() * 0.2
    duration *= jitter
    points = [origin, destination]
    return RouteLeg(
        mode=mode,
        ok=True,
        distance_m=round(distance, 1),
        duration_s=round(duration, 1),
        transfers=transfers,
        walk_m=walk,
        summary=f"模拟{mode.value} {distance / 1000:.1f} 公里 / 约 {duration / 60:.0f} 分钟",
        steps=["模拟路线（离线数据）"],
        polyline=points,
    )


def mock_transit(
    origin: tuple[float, float], destination: tuple[float, float], *, seed_text: str = "mock"
) -> RouteLeg:
    return mock_route(TravelMode.TRANSIT, origin, destination, seed_text=seed_text)


def build_mock_ai_response(candidates: list[dict], top_n: int) -> dict:
    """按地点评分与人均消费做确定性排序，生成**符合 AI 输出契约**的离线响应。

    契约（见 `algorithms.ai_validator`）：poi_id 必须来自候选列表、rank 从 1 递增、
    理由中不得出现坐标数字，也不得使用通勤时间/路线距离作为推荐依据。
    候选载荷里只有 poi_id / name / type / address / business_area / rating /
    cost_per_person / both_sides_reachable，因此排序与理由都只用这些字段。
    """
    ranked = []
    for cand in candidates:
        poi_id = cand.get("poi_id")
        if not poi_id:
            continue
        rating = cand.get("rating")
        rating = float(rating) if isinstance(rating, (int, float)) else 0.0
        cost = cand.get("cost_per_person")
        cost = float(cost) if isinstance(cost, (int, float)) and cost else float("inf")
        reachable = bool(cand.get("both_sides_reachable"))
        ranked.append(((not reachable, -rating, cost, str(poi_id)), cand))
    ranked.sort(key=lambda item: item[0])

    items = []
    for rank, (_, cand) in enumerate(ranked[: max(0, int(top_n))], start=1):
        items.append(
            {
                "poi_id": cand.get("poi_id"),
                "rank": rank,
                "reason": _mock_reason(cand),
                "highlights": _mock_highlights(cand),
            }
        )
    return {
        "summary": "按地点评分、人均消费与双方可达性综合比较后的离线模拟结果（未调用真实 AI）。",
        "items": items,
    }


def _mock_reason(cand: dict) -> str:
    """只用候选载荷里真实提供的字段组织理由，避开通勤/坐标等被禁止的表述。"""
    parts = ["双方均可到达"]
    area = cand.get("business_area")
    if area:
        parts.append(f"位于{area}")
    rating = cand.get("rating")
    if isinstance(rating, (int, float)) and rating:
        parts.append(f"高德评分 {float(rating):.1f}")
    cost = cand.get("cost_per_person")
    if isinstance(cost, (int, float)) and cost:
        parts.append(f"人均 {float(cost):.0f} 元")
    return "，".join(parts)[:120]


def _mock_highlights(cand: dict) -> list[str]:
    tags = ["离线模拟"]
    type_name = cand.get("type")
    if type_name:
        tags.append(str(type_name).split(";")[-1])
    if cand.get("both_sides_reachable"):
        tags.append("双方可达")
    return tags[:5]
