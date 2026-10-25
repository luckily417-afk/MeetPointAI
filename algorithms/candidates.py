"""候选地点发现与路线规划（全部走真实高德接口）。

* 大区域必须分块检索：圆形→多圆覆盖，多边形→矩形网格（polygon 检索）；
* 分页 + 去重 + 调用配额上限 + 轻微节流；
* 几何过滤为硬约束：任何候选都必须通过 `region.contains()`；
* 每个候选都要做 A→POI、B→POI 的真实路径规划；
* 瞬时限流类错误（10021/10004/10041 等）做一次短退避重试，其余鉴权错误直接上报。
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from config.constants import AMAP_POI_MAX_OFFSET, CATEGORY_PRESETS
from geometry.crs import haversine
from geometry.regions import CircleRegion, PolygonRegion, Region, tile_circle, tile_polygon
from models.schemas import Candidate, Poi, RouteLeg, TravelMode, UserPoint
from services.amap_web import AMapWebService
from services.errors import AmapError, ServiceError
from services.http_client import gather_limited

__all__ = ["CancelToken", "SearchStats", "discover_pois", "plan_routes", "ProgressCb"]

ProgressCb = Callable[[int, int, str], None]

# 高德偶发的瞬时限流/权限抖动码：这些错误在单次请求层面可以安全重试
_TRANSIENT_BIZ_CODES = {"10021", "10004", "10041", "10029"}


class CancelToken:
    """轻量取消令牌：用户改动输入后旧任务可被标记取消。"""

    __slots__ = ("_cancelled", "reason")

    def __init__(self) -> None:
        self._cancelled = False
        self.reason = ""

    def cancel(self, reason: str = "") -> None:
        self._cancelled = True
        self.reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise asyncio.CancelledError(self.reason or "任务已取消")


@dataclass
class SearchStats:
    tiles: int = 0
    api_calls: int = 0
    raw_pois: int = 0
    unique_pois: int = 0
    in_region: int = 0
    routed: int = 0
    route_failures: int = 0
    budget_hit: bool = False
    truncated: bool = False
    center: tuple[float, float] = (0.0, 0.0)
    radius_m: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tiles": self.tiles,
            "api_calls": self.api_calls,
            "raw_pois": self.raw_pois,
            "unique_pois": self.unique_pois,
            "in_region": self.in_region,
            "routed": self.routed,
            "route_failures": self.route_failures,
            "budget_hit": self.budget_hit,
            "truncated": self.truncated,
            "center": list(self.center),
            "radius_m": self.radius_m,
            "notes": list(self.notes),
        }


def _midpoint(a: UserPoint, b: UserPoint) -> tuple[float, float]:
    return (a.lng + b.lng) / 2.0, (a.lat + b.lat) / 2.0


def _dedupe_key(poi: Poi) -> tuple[str, int, int]:
    # 约 50 米网格，避免同名同店不同采集点的重复
    return (poi.name.strip(), round(poi.lng * 20000), round(poi.lat * 20000))


def _dedupe(pois: Iterable[Poi]) -> list[Poi]:
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, int, int]] = set()
    out: list[Poi] = []
    for poi in pois:
        if poi.id in seen_ids:
            continue
        key = _dedupe_key(poi)
        if key in seen_keys and poi.name:
            continue
        seen_ids.add(poi.id)
        seen_keys.add(key)
        out.append(poi)
    return out


def _resolve_query(category: str, custom_keyword: str) -> tuple[str, str]:
    preset = CATEGORY_PRESETS.get(category) or {"codes": "", "keywords": ""}
    types = preset["codes"]
    keywords = (custom_keyword or "").strip() or preset["keywords"]
    return types, keywords


async def _call_with_retry(factory: Callable[[], Any], attempts: int = 3) -> Any:
    """执行一次高德请求；对瞬时限流类错误做短退避重试（带抖动）。"""
    last: BaseException | None = None
    for index in range(attempts):
        try:
            return await factory()
        except AmapError as exc:
            last = exc
            transient = exc.biz_code in _TRANSIENT_BIZ_CODES or exc.state.value == "service_error"
            if index + 1 >= attempts or not transient:
                raise
            await asyncio.sleep(0.5 * (2**index) + random.random() * 0.2)
    if last is not None:
        raise last
    raise RuntimeError("高德请求失败")


async def discover_pois(
    amap: AMapWebService,
    region: Region | None,
    *,
    point_a: UserPoint,
    point_b: UserPoint,
    category: str = "任意类型",
    keyword: str = "",
    page_size: int = 25,
    max_pages: int = 4,
    max_candidates: int = 20,
    target_km: float = 2.0,
    max_tiles: int = 36,
    default_radius_m: int = 5000,
    progress: ProgressCb | None = None,
    cancel: CancelToken | None = None,
) -> tuple[list[Poi], SearchStats]:
    """按区域检索真实 POI，并做几何硬过滤。"""
    types, keywords = _resolve_query(category, keyword)
    stats = SearchStats()
    offset = max(1, min(AMAP_POI_MAX_OFFSET, page_size))

    calls_budget = max(8, min(64, max_tiles * 2))
    collected: list[Poi] = []
    errors: list[AmapError] = []

    async def fetch_around(center: tuple[float, float], radius: float) -> list[Poi]:
        got: list[Poi] = []
        for page in range(1, max_pages + 1):
            if cancel:
                cancel.raise_if_cancelled()
            if stats.api_calls >= calls_budget:
                stats.budget_hit = True
                break
            page_result = await _call_with_retry(
                lambda: amap.place_around(
                    center,
                    radius,
                    types=types or None,
                    keywords=keywords or None,
                    page=page,
                    offset=offset,
                )
            )
            stats.api_calls += 1
            got.extend(page_result.pois)
            if not page_result.has_more or not page_result.pois:
                break
            await asyncio.sleep(0.06)  # 分页之间轻微节流，规避 QPS 抖动
        return got

    async def fetch_polygon(points: Sequence[Sequence[float]]) -> list[Poi]:
        got: list[Poi] = []
        for page in range(1, max_pages + 1):
            if cancel:
                cancel.raise_if_cancelled()
            if stats.api_calls >= calls_budget:
                stats.budget_hit = True
                break
            page_result = await _call_with_retry(
                lambda: amap.place_polygon(
                    points,
                    types=types or None,
                    keywords=keywords or None,
                    page=page,
                    offset=offset,
                )
            )
            stats.api_calls += 1
            got.extend(page_result.pois)
            if not page_result.has_more or not page_result.pois:
                break
            await asyncio.sleep(0.06)
        return got

    if region is None:
        # 不限制区域：以 A、B 中点为圆心、默认半径检索（仍为真实数据，不做虚构）
        center = _midpoint(point_a, point_b)
        radius = float(default_radius_m)
        ab_distance = haversine(point_a.lng, point_a.lat, point_b.lng, point_b.lat)
        if ab_distance > radius * 1.6:
            radius = min(50000.0, ab_distance / 1.6)
        stats.center = center
        stats.radius_m = radius
        stats.tiles = 1
        if progress:
            progress(0, 1, f"检索 A、B 中点 {radius / 1000:.1f} 公里范围内的真实地点…")
        collected = await fetch_around(center, radius)
        stats.raw_pois = len(collected)
    elif isinstance(region, CircleRegion):
        stats.center = (region.center_lng, region.center_lat)
        stats.radius_m = region.radius_m
        tiles = tile_circle(region, target_km=target_km, max_tiles=max_tiles)
        stats.tiles = len(tiles)
        for index, tile in enumerate(tiles, start=1):
            if cancel:
                cancel.raise_if_cancelled()
            if progress:
                progress(
                    index - 1,
                    len(tiles),
                    f"分块检索第 {index}/{len(tiles)} 块（半径 {tile.radius_m:.0f} 米）…",
                )
            try:
                collected.extend(await fetch_around((tile.lng, tile.lat), tile.radius_m))
            except AmapError as exc:
                if exc.state.value in {"auth_error", "quota_limited"}:
                    raise
                errors.append(exc)
                stats.notes.append(f"分块 {index} 失败：{exc.message}")
            stats.raw_pois = len(collected)
            await asyncio.sleep(0.1)  # 分块之间轻微节流
    else:
        assert isinstance(region, PolygonRegion)
        stats.center = region.center()
        stats.radius_m = math.sqrt(region.area_m2() / math.pi)
        tiles = tile_polygon(region, target_km=target_km, max_tiles=max_tiles)
        stats.tiles = len(tiles)
        for index, tile in enumerate(tiles, start=1):
            if cancel:
                cancel.raise_if_cancelled()
            if progress:
                progress(index - 1, len(tiles), f"多边形分块检索第 {index}/{len(tiles)} 块…")
            try:
                collected.extend(await fetch_polygon(tile.points))
            except AmapError as exc:
                if exc.state.value in {"auth_error", "quota_limited"}:
                    raise
                errors.append(exc)
                stats.notes.append(f"分块 {index} 失败：{exc.message}")
            stats.raw_pois = len(collected)
            await asyncio.sleep(0.1)

    # 全部区块都失败且没有任何结果时，上报真实服务错误，而不是误报"区域内无结果"
    if not collected and errors:
        raise errors[-1]

    if progress:
        progress(stats.tiles, stats.tiles, f"共获取 {len(collected)} 条原始记录，开始去重与几何校验…")

    unique = _dedupe(collected)
    stats.unique_pois = len(unique)

    if region is not None:
        inside = [p for p in unique if region.contains(p.lng, p.lat)]
    else:
        inside = unique
    stats.in_region = len(inside)

    # 以 A、B 中点为参照排序；候选过多时做等距抽样，避免大区域下结果全部挤在圆心附近
    mid = _midpoint(point_a, point_b)
    inside.sort(key=lambda p: haversine(mid[0], mid[1], p.lng, p.lat))
    if len(inside) > max_candidates:
        stats.truncated = True
        total = len(inside)
        step = max(1, total // max_candidates)
        picked = [inside[i] for i in range(0, total, step)][:max_candidates]
        if len(picked) < max_candidates:
            chosen = {p.id for p in picked}
            for poi in inside:
                if poi.id not in chosen:
                    picked.append(poi)
                    if len(picked) >= max_candidates:
                        break
        inside = picked
    return inside, stats


async def plan_routes(
    amap: AMapWebService,
    pois: Sequence[Poi],
    *,
    point_a: UserPoint,
    point_b: UserPoint,
    mode: TravelMode = TravelMode.TRANSIT,
    concurrency: int = 4,
    progress: ProgressCb | None = None,
    cancel: CancelToken | None = None,
    city_a: str | None = None,
    city_b: str | None = None,
) -> list[Candidate]:
    """为每个候选规划 A/B 两侧路线（限并发）。"""
    if not pois:
        return []

    tasks: list[Any] = []
    for poi in pois:
        tasks.append(
            _call_with_retry(
                lambda poi=poi: amap.route(
                    mode,
                    (point_a.lng, point_a.lat),
                    (poi.lng, poi.lat),
                    city=city_a,
                    dest_city=poi.adcode or None,
                    raise_on_error=True,
                )
            )
        )
        tasks.append(
            _call_with_retry(
                lambda poi=poi: amap.route(
                    mode,
                    (point_b.lng, point_b.lat),
                    (poi.lng, poi.lat),
                    city=city_b,
                    dest_city=poi.adcode or None,
                    raise_on_error=True,
                )
            )
        )

    total = len(tasks)

    def on_progress(done: int, _total: int) -> None:
        if progress:
            progress(done, total, f"路线规划 {done}/{total}…")

    if cancel:
        cancel.raise_if_cancelled()

    results = await gather_limited(tasks, limit=max(1, concurrency), on_progress=on_progress)

    candidates: list[Candidate] = []
    for index, poi in enumerate(pois):
        leg_a = results[index * 2]
        leg_b = results[index * 2 + 1]
        if isinstance(leg_a, BaseException):
            leg_a = RouteLeg(mode=mode, ok=False, error=_err_text(leg_a))
        if isinstance(leg_b, BaseException):
            leg_b = RouteLeg(mode=mode, ok=False, error=_err_text(leg_b))
        cand = Candidate(poi=poi, leg_a=leg_a, leg_b=leg_b)
        cand.distance_to_mid_m = haversine(
            (point_a.lng + point_b.lng) / 2, (point_a.lat + point_b.lat) / 2, poi.lng, poi.lat
        )
        candidates.append(cand)
    return candidates


def _err_text(exc: BaseException) -> str:
    if isinstance(exc, ServiceError):
        return exc.message
    return str(exc)[:160]


async def note_amap_health(monitor: Any, amap: AMapWebService, state_hint: str = "ok") -> None:
    """把真实业务调用结果回写到健康状态（减少额外探测）。"""
    if monitor is None:
        return
    latency = int(amap._transport.metrics.last_latency_ms.get("amap_web", 0))  # noqa: SLF001
    if state_hint == "ok":
        monitor.note_success("amap_web", latency)
    else:
        from models.schemas import ServiceState

        monitor.note_failure("amap_web", ServiceState(state_hint), latency_ms=latency)
