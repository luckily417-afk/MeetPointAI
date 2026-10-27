"""推荐区域几何：圆形 / 多边形，含硬约束点面判断与搜索分块。

* 圆形：点面判断用 haversine（球面），边界算"在内"。
* 多边形：以质心为原点做局部等距投影后交给 Shapely，`covers` 判定（边界算"在内"）。
* 无效几何（顶点不足、自相交、面积为零）一律显式报错，绝不静默放过。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.validation import explain_validity

from models.schemas import RegionMode

from .crs import EARTH_RADIUS_M, LocalProjector, circle_bbox, haversine, local_scale

__all__ = [
    "CircleRegion",
    "PolygonRegion",
    "Region",
    "RegionError",
    "CircleTile",
    "PolygonTile",
    "build_region",
    "region_from_dict",
    "tile_circle",
    "tile_polygon",
]

MIN_POLYGON_AREA_M2 = 100.0
MIN_CIRCLE_RADIUS_M = 20.0
MAX_CIRCLE_RADIUS_M = 50000.0


class RegionError(ValueError):
    """区域参数非法（几何无效、超出范围等）。"""


def _round_coords(points: Iterable[tuple[float, float]], digits: int = 6) -> list[tuple[float, float]]:
    return [(round(x, digits), round(y, digits)) for x, y in points]


@dataclass(frozen=True)
class CircleTile:
    lng: float
    lat: float
    radius_m: float

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "circle", "lng": self.lng, "lat": self.lat, "radius_m": self.radius_m}


@dataclass(frozen=True)
class PolygonTile:
    points: tuple[tuple[float, float], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "polygon", "points": [list(p) for p in self.points]}

    @property
    def amap_polygon(self) -> str:
        """高德 polygon 参数格式：lng,lat|lng,lat|...（首尾不重复）。"""
        return "|".join(f"{x:.6f},{y:.6f}" for x, y in self.points)


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #
class Region(ABC):
    mode: RegionMode

    @abstractmethod
    def contains(self, lng: float, lat: float) -> bool:
        """点是否在区域内或边界上（硬约束）。"""

    def contains_many(self, points: Iterable[tuple[float, float]]) -> list[bool]:
        return [self.contains(x, y) for x, y in points]

    @abstractmethod
    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lng, min_lat, max_lng, max_lat)。"""

    @abstractmethod
    def area_m2(self) -> float: ...

    @abstractmethod
    def to_dict(self) -> dict[str, Any]: ...

    @abstractmethod
    def describe(self) -> str: ...

    def center(self) -> tuple[float, float]:
        minx, miny, maxx, maxy = self.bbox()
        return (minx + maxx) / 2.0, (miny + maxy) / 2.0

    def map_payload(self) -> dict[str, Any]:
        """给 JS 端绘制用的载荷。"""
        raise RegionError("该区域类型不支持地图绘制")


# --------------------------------------------------------------------------- #
# 圆形
# --------------------------------------------------------------------------- #
@dataclass
class CircleRegion(Region):
    center_lng: float
    center_lat: float
    radius_m: float
    mode: RegionMode = field(default=RegionMode.CIRCLE, init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.radius_m):
            raise RegionError("半径必须是有效数值")
        if self.radius_m < MIN_CIRCLE_RADIUS_M:
            raise RegionError(f"半径不能小于 {MIN_CIRCLE_RADIUS_M:.0f} 米")
        if self.radius_m > MAX_CIRCLE_RADIUS_M:
            raise RegionError(f"半径不能大于 {MAX_CIRCLE_RADIUS_M / 1000:.0f} 千米")
        if not (-180 <= self.center_lng <= 180 and -90 <= self.center_lat <= 90):
            raise RegionError("圆心坐标非法")

    @property
    def center_location(self) -> str:
        return f"{self.center_lng:.6f},{self.center_lat:.6f}"

    def contains(self, lng: float, lat: float) -> bool:
        return haversine(self.center_lng, self.center_lat, lng, lat) <= self.radius_m + 1e-6

    def bbox(self) -> tuple[float, float, float, float]:
        return circle_bbox(self.center_lng, self.center_lat, self.radius_m)

    def area_m2(self) -> float:
        return math.pi * self.radius_m**2

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "center_lng": self.center_lng,
            "center_lat": self.center_lat,
            "radius_m": self.radius_m,
        }

    def describe(self) -> str:
        return f"圆形区域：圆心 {self.center_location}，半径 {self.radius_m / 1000:.2f} 千米"

    def map_payload(self) -> dict[str, Any]:
        return {
            "mode": "circle",
            "center": [self.center_lng, self.center_lat],
            "radius": self.radius_m,
        }


# --------------------------------------------------------------------------- #
# 多边形
# --------------------------------------------------------------------------- #
class PolygonRegion(Region):
    mode = RegionMode.POLYGON

    def __init__(self, points: Sequence[Sequence[float]]) -> None:
        raw: list[tuple[float, float]] = []
        for item in points:
            if len(item) != 2:
                raise RegionError("多边形顶点格式应为 [经度, 纬度]")
            lng, lat = float(item[0]), float(item[1])
            if not (-180 <= lng <= 180 and -90 <= lat <= 90):
                raise RegionError(f"多边形顶点坐标越界: {lng},{lat}")
            if raw and abs(raw[-1][0] - lng) < 1e-9 and abs(raw[-1][1] - lat) < 1e-9:
                continue  # 去掉相邻重复点
            raw.append((lng, lat))
        if len(raw) >= 2 and abs(raw[0][0] - raw[-1][0]) < 1e-9 and abs(raw[0][1] - raw[-1][1]) < 1e-9:
            raw.pop()  # 去掉闭合重复点

        if len(raw) < 3:
            raise RegionError("多边形至少需要 3 个不同顶点")

        self.points: tuple[tuple[float, float], ...] = tuple(raw)
        self._lon0 = sum(p[0] for p in raw) / len(raw)
        self._lat0 = sum(p[1] for p in raw) / len(raw)
        self._proj = LocalProjector(self._lon0, self._lat0)
        self._geom: Polygon | MultiPolygon = self._build_geometry()
        self._prepared = prep(self._geom)

    # -- 构建与校验 --------------------------------------------------------
    def _build_geometry(self) -> Polygon | MultiPolygon:
        ring = [self._proj.project(x, y) for x, y in self.points]
        if len(set(ring)) < 3:
            raise RegionError("多边形顶点过于集中，无法构成有效区域")
        polygon = Polygon(ring)
        if not polygon.is_simple or not polygon.is_valid:
            reason = explain_validity(polygon)
            if "Self-intersection" in reason or "self-intersection" in reason:
                raise RegionError("多边形存在自相交，请重新绘制（相邻边不能交叉）")
            repaired = polygon.buffer(0)
            if repaired.is_empty or repaired.area <= MIN_POLYGON_AREA_M2:
                raise RegionError(f"多边形几何无效：{reason}")
            polygon = repaired if isinstance(repaired, Polygon) else unary_union(repaired)
        if polygon.area < MIN_POLYGON_AREA_M2:
            raise RegionError(f"多边形面积过小（< {MIN_POLYGON_AREA_M2:.0f} 平方米），请扩大区域")
        return polygon

    # -- Region 接口 -------------------------------------------------------
    def contains(self, lng: float, lat: float) -> bool:
        x, y = self._proj.project(lng, lat)
        return bool(self._prepared.covers(Point(x, y)))

    def bbox(self) -> tuple[float, float, float, float]:
        lngs = [p[0] for p in self.points]
        lats = [p[1] for p in self.points]
        return min(lngs), min(lats), max(lngs), max(lats)

    def area_m2(self) -> float:
        return float(self._geom.area)

    def perimeter_m(self) -> float:
        return float(self._geom.length)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "points": [list(p) for p in self.points]}

    def describe(self) -> str:
        return (
            f"多边形区域：{len(self.points)} 个顶点，"
            f"面积约 {self.area_m2() / 1e6:.3f} 平方千米"
        )

    def map_payload(self) -> dict[str, Any]:
        return {"mode": "polygon", "points": [list(p) for p in self.points]}

    def amap_polygon(self, max_points: int = 10) -> str:
        """高德 polygon 检索参数（顶点过多时自动降采样，仅用于粗筛）。"""
        pts = list(self.points)
        if len(pts) > max_points:
            step = len(pts) / max_points
            pts = [pts[int(i * step)] for i in range(max_points)]
        return "|".join(f"{x:.6f},{y:.6f}" for x, y in pts)

    def shapely_geometry(self) -> Polygon | MultiPolygon:
        return self._geom

    @property
    def projector(self) -> LocalProjector:
        return self._proj


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def build_region(
    mode: str | RegionMode,
    *,
    center: tuple[float, float] | None = None,
    radius_m: float | None = None,
    points: Sequence[Sequence[float]] | None = None,
) -> Region | None:
    try:
        mode_enum = RegionMode(mode)
    except ValueError as exc:
        raise RegionError(f"未知区域模式：{mode}") from exc
    if mode_enum is RegionMode.NONE:
        return None
    if mode_enum is RegionMode.CIRCLE:
        if center is None or radius_m is None:
            raise RegionError("圆形区域需要圆心与半径")
        return CircleRegion(float(center[0]), float(center[1]), float(radius_m))
    if not points:
        raise RegionError("多边形区域需要至少 3 个顶点")
    return PolygonRegion(points)


def region_from_dict(data: dict[str, Any] | None) -> Region | None:
    if not data:
        return None
    mode = data.get("mode", "none")
    if mode == "circle":
        return CircleRegion(
            float(data["center_lng"]), float(data["center_lat"]), float(data["radius_m"])
        )
    if mode == "polygon":
        return PolygonRegion(data["points"])
    return None


# --------------------------------------------------------------------------- #
# 分块搜索
# --------------------------------------------------------------------------- #
def tile_circle(
    region: CircleRegion,
    target_km: float = 2.0,
    max_tiles: int = 36,
) -> list[CircleTile]:
    """把圆形区域切成若干小圆，保证覆盖整个圆（含边界）。

    `max_tiles` 是硬上限：块数超限时放大块半径直到满足上限，
    绝不因为「至少 2×2 网格」之类的固定公式而突破配置（否则会多烧 API 配额）。
    """
    parent_r = region.radius_m
    limit = max(1, int(max_tiles))
    # 目标块半径：不大于 2 千米，也不大于父圆自身半径
    cell_r = max(300.0, min(2000.0, target_km * 1000.0 / 2.0))
    cell_r = min(cell_r, max(150.0, parent_r / 1.4))
    minx, miny, maxx, maxy = region.bbox()
    lat_span_m = (maxy - miny) * 111320.0
    lng_span_m = (maxx - minx) * local_scale(region.center_lat)
    # 保证块数不超限：块边长（spacing = 1.4×半径）增大，网格数单调下降，循环必然收敛
    while True:
        spacing = cell_r * 1.4
        nx = max(1, math.ceil(lng_span_m / spacing))
        ny = max(1, math.ceil(lat_span_m / spacing))
        if nx * ny <= limit:
            break
        cell_r *= 1.25

    cx, cy = region.center_lng, region.center_lat
    tiles: list[CircleTile] = []
    for i in range(nx):
        for j in range(ny):
            lng = minx + (i + 0.5) * (maxx - minx) / nx
            lat = miny + (j + 0.5) * (maxy - miny) / ny
            # 圆心落在（略微外扩的）父圆内的分块才检索
            if haversine(cx, cy, lng, lat) <= parent_r + spacing:
                tiles.append(CircleTile(lng, lat, cell_r))
    return tiles or [CircleTile(cx, cy, parent_r)]


def tile_polygon(
    region: PolygonRegion,
    target_km: float = 2.0,
    max_tiles: int = 36,
) -> list[PolygonTile]:
    """把多边形 bbox 切成矩形网格，丢弃与多边形无交集的格子。

    `max_tiles` 是硬上限：网格数超限时放大边长（最多放大到覆盖整个 bbox）。
    """
    limit = max(1, int(max_tiles))
    minx, miny, maxx, maxy = region.bbox()
    proj = region.projector
    x0, y0 = proj.project(minx, miny)
    x1, y1 = proj.project(maxx, maxy)
    width = max(1.0, x1 - x0)
    height = max(1.0, y1 - y0)
    cell = max(200.0, target_km * 1000.0)
    while True:
        nx = max(1, math.ceil(width / cell))
        ny = max(1, math.ceil(height / cell))
        if nx * ny <= limit:
            break
        cell *= 1.3

    geom = region.shapely_geometry()
    tiles: list[PolygonTile] = []
    dx = width / nx
    dy = height / ny
    for i in range(nx):
        for j in range(ny):
            bx0 = x0 + i * dx
            by0 = y0 + j * dy
            cell_geom = box(bx0, by0, bx0 + dx, by0 + dy)
            if not cell_geom.intersects(geom):
                continue
            corners = [
                proj.unproject(bx0, by0),
                proj.unproject(bx0 + dx, by0),
                proj.unproject(bx0 + dx, by0 + dy),
                proj.unproject(bx0, by0 + dy),
            ]
            tiles.append(PolygonTile(tuple(_round_coords(corners))))
    if not tiles:
        # 极端情况（多边形极细）：退化为中心圆点矩形
        tiles.append(
            PolygonTile(tuple(_round_coords([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)])))
        )
    return tiles


def approx_area_bbox_m2(min_lng: float, min_lat: float, max_lng: float, max_lat: float) -> float:
    """粗略估算经纬度矩形面积（仅用于日志/进度）。"""
    w = (max_lng - min_lng) * local_scale((min_lat + max_lat) / 2)
    h = (max_lat - min_lat) * 110574.0
    return abs(w * h)


def great_circle_area_radius(radius_m: float) -> float:
    return math.pi * (radius_m**2) / (EARTH_RADIUS_M**2)
