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
from models.schemas import Candidate, Poi, RouteLeg, ServiceState, TravelMode, UserPoint
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


def _dedupe(pois: Iterable[Poi], dropped: list[Poi] | None = None) -> list[Poi]:
    seen_ids: set[str] = set()
    by_name: dict[str, list[Poi]] = {}
    out: list[Poi] = []
    for poi in pois:
        if poi.id in seen_ids:
            if dropped is not None:
                dropped.append(poi)
            continue
        name = poi.name.strip().casefold()
        if name and any(
            haversine(poi.lng, poi.lat, other.lng, other.lat) <= 50
            for other in by_name.get(name, ())
        ):
            if dropped is not None:
                dropped.append(poi)
            continue
        seen_ids.add(poi.id)
        if name:
            by_name.setdefault(name, []).append(poi)
        out.append(poi)
    return out


def _preselect_pois(
    pois: Sequence[Poi], point_a: UserPoint, point_b: UserPoint, limit: int
) -> list[Poi]:
    """按地点评分与空间覆盖取样；起点距离不参与入围。"""
    if len(pois) <= limit:
        return list(pois)
    limit = max(1, limit)
    ordered = sorted(
        pois,
        key=lambda p: (
            -(p.rating or 0),
            p.id,
        ),
    )
    promising_count = max(1, math.ceil(limit * 0.6))
    selected = ordered[:promising_count]
    remaining = ordered[promising_count:]
    rank = {p.id: index for index, p in enumerate(ordered)}
    while remaining and len(selected) < limit:
        best = max(
            remaining,
            key=lambda p: (
                min(haversine(p.lng, p.lat, s.lng, s.lat) for s in selected),
                -rank[p.id],
            ),
        )
        selected.append(best)
        remaining.remove(best)
    return selected


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
            transient = exc.biz_code in _TRANSIENT_BIZ_CODES or (
                exc.state.value in {"service_error", "connect_error", "timeout"} and not exc.biz_code
            )
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
    trace: Any | None = None,
) -> tuple[list[Poi], SearchStats]:
    """按区域检索真实 POI，并做几何硬过滤。"""
    types, keywords = _resolve_query(category, keyword)
    stats = SearchStats()
    offset = max(1, min(AMAP_POI_MAX_OFFSET, page_size))

    calls_budget = max(8, min(64, max_tiles * 2))
    collected: list[Poi] = []
    errors: list[AmapError] = []

    tile_requests: list[Callable[[int], Any]] = []
    if region is None:
        # 不限制区域：以 A、B 中点为圆心、默认半径检索（仍为真实数据，不做虚构）
        center = _midpoint(point_a, point_b)
        radius = float(default_radius_m)
        ab_distance = haversine(point_a.lng, point_a.lat, point_b.lng, point_b.lat)
        if ab_distance > radius * 1.6:
            radius = min(50000.0, ab_distance / 1.6)
        stats.center = center
        stats.radius_m = radius
        tile_requests.append(
            lambda page: amap.place_around(
                center, radius, types=types or None, keywords=keywords or None,
                page=page, offset=offset,
            )
        )
    elif isinstance(region, CircleRegion):
        stats.center = (region.center_lng, region.center_lat)
        stats.radius_m = region.radius_m
        tiles = tile_circle(region, target_km=target_km, max_tiles=max_tiles)
        for tile in tiles:
            tile_requests.append(
                lambda page, tile=tile: amap.place_around(
                    (tile.lng, tile.lat), tile.radius_m,
                    types=types or None, keywords=keywords or None,
                    page=page, offset=offset,
                )
            )
    else:
        assert isinstance(region, PolygonRegion)
        stats.center = region.center()
        stats.radius_m = math.sqrt(region.area_m2() / math.pi)
        tiles = tile_polygon(region, target_km=target_km, max_tiles=max_tiles)
        for tile in tiles:
            tile_requests.append(
                lambda page, tile=tile: amap.place_polygon(
                    tile.points, types=types or None, keywords=keywords or None,
                    page=page, offset=offset,
                )
            )

    stats.tiles = len(tile_requests)
    calls_budget = max(calls_budget, stats.tiles)  # 每块至少检索第一页
    active = set(range(stats.tiles))
    planned_calls = min(calls_budget, stats.tiles * max_pages)
    for page in range(1, max_pages + 1):
        for index in sorted(active):
            if cancel:
                cancel.raise_if_cancelled()
            if stats.api_calls >= calls_budget:
                stats.budget_hit = True
                break
            if progress:
                progress(stats.api_calls, planned_calls,
                         f"分块检索第 {index + 1}/{stats.tiles} 块，第 {page} 页…")
            try:
                page_result = await _call_with_retry(lambda: tile_requests[index](page))
            except AmapError as exc:
                if region is None or exc.state.value in {"auth_error", "quota_limited"}:
                    raise
                errors.append(exc)
                stats.notes.append(f"分块 {index + 1} 第 {page} 页失败：{exc.message}")
                active.remove(index)
                continue
            stats.api_calls += 1
            collected.extend(page_result.pois)
            if not page_result.has_more or not page_result.pois:
                active.remove(index)
            await asyncio.sleep(0.06)
        if stats.budget_hit or not active:
            break
    stats.raw_pois = len(collected)

    # 全部区块都失败且没有任何结果时，上报真实服务错误，而不是误报"区域内无结果"
    if not collected and errors:
        raise errors[-1]

    if progress:
        progress(stats.tiles, stats.tiles, f"共获取 {len(collected)} 条原始记录，开始去重与几何校验…")

    dupes: list[Poi] = []
    unique = _dedupe(collected, dupes)
    stats.unique_pois = len(unique)
    if trace is not None:
        trace.count("raw", len(collected))
        trace.count("deduped", len(unique))
        for poi in dupes:
            trace.eliminate(poi.id, "dedupe", "重复 POI（ID 或同名且相距不超过 50 米）", name=poi.name)

    if region is not None:
        inside = []
        for poi in unique:
            if region.contains(poi.lng, poi.lat):
                inside.append(poi)
            elif trace is not None:
                trace.eliminate(
                    poi.id,
                    "region",
                    "位于允许区域之外",
                    name=poi.name,
                    lng=poi.lng,
                    lat=poi.lat,
                )
    else:
        inside = unique
    stats.in_region = len(inside)
    if trace is not None:
        trace.count("in_region", len(inside))

    # 用地点评分和空间覆盖控制上限，不按双方距离淘汰地点。
    if len(inside) > max_candidates:
        stats.truncated = True
        total = len(inside)
        picked = _preselect_pois(inside, point_a, point_b, max_candidates)
        if trace is not None:
            keep = {p.id for p in picked}
            for poi in inside:
                if poi.id not in keep:
                    trace.eliminate(
                        poi.id,
                        "preselect",
                        "候选池截断（未进入路线规划）",
                        name=poi.name,
                        lng=poi.lng,
                        lat=poi.lat,
                        detail=f"区域内共 {total} 个，保留上限 {max_candidates}",
                    )
        inside = picked
    else:
        inside.sort(key=lambda p: (-(p.rating or 0), p.id))
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
    trace: Any | None = None,
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
    for item in results:
        if isinstance(item, BaseException) and getattr(item, "code", None) == "BUDGET_EXCEEDED":
            raise item

    # 并发请求遇到瞬时限流时，等整批请求结束后逐条补查失败的路线。
    # 这能避免仅一侧路线临时失败，就把真实可达的地点永久移出推荐池。
    recovered = 0
    for index, failed in enumerate(results):
        if cancel:
            cancel.raise_if_cancelled()
        if not isinstance(failed, ServiceError):
            continue
        transient = (
            failed.biz_code in _TRANSIENT_BIZ_CODES
            or failed.state in {ServiceState.CONNECT_ERROR, ServiceState.TIMEOUT, ServiceState.SERVICE_ERROR}
        )
        if not transient:
            continue
        poi = pois[index // 2]
        origin = point_a if index % 2 == 0 else point_b
        try:
            await asyncio.sleep(0.15)
            results[index] = await amap.route(
                mode,
                (origin.lng, origin.lat),
                (poi.lng, poi.lat),
                city=city_a if index % 2 == 0 else city_b,
                dest_city=poi.adcode or None,
                raise_on_error=True,
            )
            recovered += 1
        except ServiceError:
            pass
    if trace is not None and recovered:
        trace.event("routes.recovered", count=recovered)

    candidates: list[Candidate] = []
    api_failed_pois = 0
    for index, poi in enumerate(pois):
        leg_a = results[index * 2]
        leg_b = results[index * 2 + 1]
        api_failed = isinstance(leg_a, ServiceError) or isinstance(leg_b, ServiceError)
        if api_failed:
            api_failed_pois += 1
        if isinstance(leg_a, BaseException):
            leg_a = RouteLeg(mode=mode, ok=False, error=_err_text(leg_a))
        if isinstance(leg_b, BaseException):
            leg_b = RouteLeg(mode=mode, ok=False, error=_err_text(leg_b))
        cand = Candidate(poi=poi, leg_a=leg_a, leg_b=leg_b)
        cand.distance_to_mid_m = haversine(
            (point_a.lng + point_b.lng) / 2, (point_a.lat + point_b.lat) / 2, poi.lng, poi.lat
        )
        candidates.append(cand)
        if trace is not None:
            trace.candidate(cand)
            if (leg_a is not None and leg_a.cached) or (leg_b is not None and leg_b.cached):
                trace.note_cache(True)
            if not cand.routes_ok:
                detail = (leg_a.error if leg_a and not leg_a.ok else "") or (
                    leg_b.error if leg_b and not leg_b.ok else ""
                )
                reason = "路线接口调用失败" if api_failed else "A/B 路线未同时可达"
                trace.eliminate_candidate(cand, "route", reason, detail)
    if trace is not None:
        trace.count("route_api_failed", api_failed_pois)
        if api_failed_pois:
            trace.warn("ROUTE_API_FAILURE", f"{api_failed_pois} 个候选的路线接口调用失败，推荐池可能不完整")
    return candidates


def _err_text(exc: BaseException) -> str:
    if isinstance(exc, ServiceError):
        return exc.message
    return str(exc)[:160]


async def note_amap_health(monitor: Any, amap: AMapWebService, state_hint: str = "ok") -> None:
    """把真实业务调用结果回写到健康状态（减少额外探测）。"""
    if monitor is None:
        return
    transport = getattr(amap, "_transport", None)
    metrics = getattr(transport, "metrics", None)
    latency = int(metrics.last_latency_ms.get("amap_web", 0)) if metrics is not None else 0
    if state_hint == "ok":
        monitor.note_success("amap_web", latency)
    else:
        from models.schemas import ServiceState

        monitor.note_failure("amap_web", ServiceState(state_hint), latency_ms=latency)
