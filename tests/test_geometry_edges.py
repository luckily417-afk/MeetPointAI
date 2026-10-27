"""地理边界与坐标系异常（需求八·地理计算）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geometry.crs import destination_point, gcj02_to_wgs84, haversine, wgs84_to_gcj02
from geometry.regions import CircleRegion, PolygonRegion, RegionError, build_region
from models.schemas import UserPoint, parse_location
from diagnostics.cases import CaseConfig
from diagnostics.validate import validate_result


def test_zero_radius_rejected():
    with pytest.raises(RegionError):
        CircleRegion(117.21, 39.13, 0)
    with pytest.raises(RegionError):
        build_region("circle", center=(117.21, 39.13), radius_m=0)


def test_26km_radius_boundary_and_beyond():
    region = CircleRegion(117.2115, 39.1360, 26000)
    inside = destination_point(117.2115, 39.1360, 45.0, 25999)
    edge = destination_point(117.2115, 39.1360, 45.0, 26000)
    outside = destination_point(117.2115, 39.1360, 45.0, 26001)
    assert region.contains(*inside)
    assert region.contains(*edge)          # 边界算内
    assert not region.contains(*outside)


def test_tiny_circle():
    region = CircleRegion(117.2115, 39.1360, 20)
    assert region.contains(*destination_point(117.2115, 39.1360, 90, 19.5))
    assert not region.contains(*destination_point(117.2115, 39.1360, 90, 20.5))
    assert region.area_m2() == pytest.approx(3.141592653589793 * 400, rel=1e-6)


def test_concave_polygon_notch_excluded():
    points = [
        (117.20, 39.12), (117.24, 39.12), (117.24, 39.14),
        (117.22, 39.14), (117.22, 39.16), (117.20, 39.16),
    ]
    region = PolygonRegion(points)
    assert region.contains(117.21, 39.15)     # 竖臂
    assert region.contains(117.23, 39.13)     # 横臂
    assert not region.contains(117.23, 39.155)  # 凹口


def test_self_intersecting_polygon_rejected():
    bowtie = [(117.20, 39.12), (117.24, 39.16), (117.24, 39.12), (117.20, 39.16)]
    with pytest.raises(RegionError):
        PolygonRegion(bowtie)


def test_polygon_boundary_counts_as_inside():
    points = [(117.20, 39.12), (117.24, 39.12), (117.24, 39.16), (117.20, 39.16)]
    region = PolygonRegion(points)
    assert region.contains(117.20, 39.14)
    assert region.contains(117.22, 39.12)


@pytest.mark.parametrize("text", ["200,39", "117,95", "abc", "117.2", "nan,39"])
def test_invalid_coordinates_rejected(text):
    with pytest.raises(ValueError):
        parse_location(text)


def test_user_point_rejects_out_of_range():
    with pytest.raises(ValidationError):
        UserPoint(lng=181.0, lat=39.0)
    with pytest.raises(ValidationError):
        UserPoint(lng=117.2, lat=-91.0)


def test_wgs84_vs_gcj02_shift_is_detectable():
    """坐标系用错时，点会偏移数百米，小范围校验应能发现。"""
    gcj = (117.210089, 39.135951)
    wgs = gcj02_to_wgs84(*gcj)
    assert haversine(gcj[0], gcj[1], wgs[0], wgs[1]) > 300
    region = CircleRegion(117.210089, 39.135951, 150)
    assert region.contains(*gcj)
    assert not region.contains(*wgs)


def test_validate_reports_out_of_region_with_coords():
    case = CaseConfig.model_validate(
        {
            "name": "validate-edged",
            "a": {"name": "A", "lng": 117.20, "lat": 39.13},
            "b": {"name": "B", "lng": 117.22, "lat": 39.14},
            "region": {"mode": "circle", "center": [117.21, 39.135], "radius_m": 500},
            "top_n": 3,
        }
    )
    result = {
        "recommendations": [
            {"rank": 1, "poi_id": "IN", "name": "内", "lng": 117.2105, "lat": 39.1351},
            {"rank": 2, "poi_id": "OUT", "name": "外", "lng": 117.30, "lat": 39.14},
        ]
    }
    report = validate_result(case, result)
    assert not report["ok"]
    codes = [v["code"] for v in report["violations"]]
    assert "OUT_OF_REGION" in codes
    violation = next(v for v in report["violations"] if v["code"] == "OUT_OF_REGION")
    assert violation["poi_id"] == "OUT"
    assert violation["location"] == [117.3, 39.14]


def test_validate_detects_null_island_and_duplicates():
    case = CaseConfig.model_validate(
        {"name": "v2", "a": {"lng": 117.2, "lat": 39.13}, "b": {"lng": 117.22, "lat": 39.14}}
    )
    result = {
        "recommendations": [
            {"rank": 1, "poi_id": "X", "lng": 0, "lat": 0},
            {"rank": 2, "poi_id": "X", "lng": 117.21, "lat": 39.135},
            {"rank": 3, "poi_id": "Y", "lng": 117.21, "lat": 39.135},
            {"rank": 4, "poi_id": "Z", "lng": 117.21, "lat": 39.135},
            {"rank": 5, "poi_id": "W", "lng": 117.21, "lat": 39.135},
            {"rank": 6, "poi_id": "V", "lng": 117.21, "lat": 39.135},
        ]
    }
    codes = {v["code"] for v in validate_result(case, result)["violations"]}
    assert {"NULL_ISLAND", "DUPLICATE_POI_ID", "TOO_MANY_RESULTS"} <= codes


def test_validate_rejects_non_object_items_without_ignoring_count():
    case = CaseConfig.model_validate(
        {"a": {"lng": 117.2, "lat": 39.13}, "b": {"lng": 117.22, "lat": 39.14}, "top_n": 1}
    )
    result = {"recommendations": [
        {"poi_id": "X", "lng": 117.21, "lat": 39.135},
        "bad entry",
    ]}
    report = validate_result(case, result)
    assert report["checked"] == 2
    assert not report["ok"]
    assert {"INVALID_ITEM", "TOO_MANY_RESULTS"} <= {v["code"] for v in report["violations"]}
