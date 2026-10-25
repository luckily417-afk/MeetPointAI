"""几何与区域约束测试（需求 4.1 / 4.2 / 4.3）。"""

from __future__ import annotations

import math

import pytest

from geometry.crs import (
    circle_bbox,
    destination_point,
    gcj02_to_wgs84,
    haversine,
    wgs84_to_gcj02,
)
from geometry.regions import (
    CircleRegion,
    PolygonRegion,
    RegionError,
    build_region,
    tile_circle,
    tile_polygon,
)

TIANJIN_STATION = (117.210089, 39.135951)
TIANJIN_UNIV = (117.311568, 38.992672)


def test_haversine_real_distance():
    dist = haversine(*TIANJIN_STATION, *TIANJIN_UNIV)
    assert 17_000 < dist < 20_000, dist


def test_haversine_is_symmetric_and_zero():
    assert haversine(*TIANJIN_STATION, *TIANJIN_STATION) == pytest.approx(0.0, abs=1e-6)
    assert haversine(*TIANJIN_STATION, *TIANJIN_UNIV) == pytest.approx(
        haversine(*TIANJIN_UNIV, *TIANJIN_STATION), rel=1e-9
    )


def test_gcj02_roundtrip():
    wlng, wlat = gcj02_to_wgs84(*TIANJIN_STATION)
    glng, glat = wgs84_to_gcj02(wlng, wlat)
    assert glng == pytest.approx(TIANJIN_STATION[0], abs=1e-7)
    assert glat == pytest.approx(TIANJIN_STATION[1], abs=1e-7)


def test_circle_contains_and_boundary():
    region = CircleRegion(117.21, 39.13, 2000)
    assert region.contains(117.21, 39.13)
    edge = destination_point(117.21, 39.13, 45.0, 2000)
    assert region.contains(*edge)
    outside = destination_point(117.21, 39.13, 45.0, 2200)
    assert not region.contains(*outside)


def test_circle_rejects_invalid_radius():
    with pytest.raises(RegionError):
        CircleRegion(117.21, 39.13, 1)
    with pytest.raises(RegionError):
        CircleRegion(117.21, 39.13, 999_999)


def test_circle_bbox_covers_region():
    minx, miny, maxx, maxy = circle_bbox(117.21, 39.13, 3000)
    assert minx < 117.21 < maxx
    assert miny < 39.13 < maxy
    assert haversine(minx, 39.13, 117.21, 39.13) == pytest.approx(3000, rel=0.01)


def test_polygon_contains_interior_boundary_and_outside():
    points = [(117.20, 39.12), (117.24, 39.12), (117.24, 39.16), (117.20, 39.16)]
    region = PolygonRegion(points)
    assert region.contains(117.22, 39.14)
    assert region.contains(117.20, 39.14)  # 边界算在内
    assert not region.contains(117.30, 39.14)
    assert region.area_m2() > 1_000_000


def test_polygon_concave_supported():
    # 凹多边形（L 形）
    points = [
        (117.20, 39.12),
        (117.24, 39.12),
        (117.24, 39.14),
        (117.22, 39.14),
        (117.22, 39.16),
        (117.20, 39.16),
    ]
    region = PolygonRegion(points)
    assert region.contains(117.21, 39.15)     # 竖臂
    assert region.contains(117.23, 39.13)     # 横臂
    assert not region.contains(117.23, 39.155)  # 缺口处


def test_polygon_self_intersection_rejected():
    bowtie = [(117.20, 39.12), (117.24, 39.16), (117.24, 39.12), (117.20, 39.16)]
    with pytest.raises(RegionError) as exc:
        PolygonRegion(bowtie)
    assert "自相交" in str(exc.value)


def test_polygon_too_few_points_rejected():
    with pytest.raises(RegionError):
        PolygonRegion([(117.20, 39.12), (117.21, 39.13)])


def test_polygon_duplicate_points_collapsed():
    pts = [(117.20, 39.12), (117.20, 39.12), (117.24, 39.12), (117.24, 39.16), (117.20, 39.16)]
    region = PolygonRegion(pts)
    assert len(region.points) == 4


def test_build_region_factory():
    assert build_region("none") is None
    circle = build_region("circle", center=(117.2, 39.1), radius_m=1500)
    assert isinstance(circle, CircleRegion)
    polygon = build_region("polygon", points=[(117.2, 39.1), (117.3, 39.1), (117.3, 39.2)])
    assert isinstance(polygon, PolygonRegion)
    with pytest.raises(RegionError):
        build_region("circle", center=(117.2, 39.1), radius_m=None)


def test_tile_polygon_covers_all_vertices():
    points = [(117.20, 39.12), (117.30, 39.12), (117.30, 39.20), (117.20, 39.20)]
    region = PolygonRegion(points)
    tiles = tile_polygon(region, target_km=2.0, max_tiles=36)
    assert 1 <= len(tiles) <= 36
    inside = [(117.25, 39.16), (117.201, 39.1201), (117.299, 39.199)]
    for lng, lat in inside:
        covered = any(
            min(p[0] for p in tile.points) <= lng <= max(p[0] for p in tile.points)
            and min(p[1] for p in tile.points) <= lat <= max(p[1] for p in tile.points)
            for tile in tiles
        )
        assert covered, (lng, lat)


def test_tile_polygon_respects_max_tiles():
    points = [(117.0, 39.0), (118.5, 39.0), (118.5, 40.2), (117.0, 40.2)]
    region = PolygonRegion(points)
    tiles = tile_polygon(region, target_km=2.0, max_tiles=12)
    assert len(tiles) <= 12


def test_tile_circle_covers_boundary_points():
    region = CircleRegion(117.21, 39.13, 4000)
    tiles = tile_circle(region, target_km=1.5, max_tiles=24)
    assert len(tiles) >= 1
    # 圆边界上的点必须落在某个分块圆内
    for bearing in (0, 72, 144, 216, 288):
        edge = destination_point(117.21, 39.13, bearing, 4000)
        assert any(haversine(edge[0], edge[1], t.lng, t.lat) <= t.radius_m for t in tiles), bearing


def test_polygon_amap_string():
    region = PolygonRegion([(117.20, 39.12), (117.24, 39.12), (117.24, 39.16)])
    s = region.amap_polygon()
    assert s.count("|") == 2
    assert all(math.isfinite(float(v)) for pair in s.split("|") for v in pair.split(","))
