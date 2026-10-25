"""AI 输出拦截与几何二次校验测试（需求 4.3 第 7-8 步、六）。"""

from __future__ import annotations

import pytest

from algorithms.ai_validator import build_ai_messages, candidate_payload, validate_ai_payload
from geometry.regions import CircleRegion
from models.schemas import Candidate, Poi, RouteLeg, TravelMode


def make_candidate(poi_id: str, lng: float, lat: float, name: str = "测试地点") -> Candidate:
    poi = Poi(id=poi_id, name=name, lng=lng, lat=lat, address="天津市和平区", type_code="050500")
    leg = RouteLeg(mode=TravelMode.TRANSIT, ok=True, distance_m=1200, duration_s=900, transfers=0)
    return Candidate(poi=poi, leg_a=leg, leg_b=leg.model_copy())


@pytest.fixture()
def candidates():
    return [
        make_candidate("B001", 117.2100, 39.1350),
        make_candidate("B002", 117.2150, 39.1350),
        make_candidate("B003", 117.2200, 39.1350),
    ]


def test_valid_response(candidates):
    raw = {
        "summary": "综合通勤与氛围推荐",
        "items": [
            {"poi_id": "B002", "rank": 1, "reason": "双方均在 20 分钟内到达"},
            {"poi_id": "B001", "rank": 2, "reason": "A 步行更短"},
        ],
    }
    result = validate_ai_payload(raw, candidates, top_n=5)
    assert result.ok
    assert [i.poi_id for i in result.items] == ["B002", "B001"]
    assert result.summary


def test_rejects_unknown_id(candidates):
    raw = {"summary": "", "items": [{"poi_id": "FAKE-1", "rank": 1, "reason": "虚构地点"}]}
    result = validate_ai_payload(raw, candidates, top_n=5)
    assert not result.ok
    assert "FAKE-1" in result.error_text


def test_rejects_duplicates_and_keeps_first(candidates):
    raw = {
        "items": [
            {"poi_id": "B001", "rank": 1, "reason": "ok"},
            {"poi_id": "B001", "rank": 2, "reason": "dup"},
        ]
    }
    result = validate_ai_payload(raw, candidates, top_n=5)
    assert result.ok
    assert len(result.items) == 1


def test_rejects_out_of_region(candidates):
    region = CircleRegion(117.2100, 39.1350, 500)  # 只覆盖 B001 附近
    raw = {"items": [{"poi_id": "B003", "rank": 1, "reason": "在区域外"}]}
    result = validate_ai_payload(raw, candidates, top_n=5, region=region)
    assert not result.ok
    assert "区域外" in result.error_text


def test_rejects_coordinates_in_reason(candidates):
    raw = {"items": [{"poi_id": "B001", "rank": 1, "reason": "位于 117.2100,39.1350 附近"}]}
    result = validate_ai_payload(raw, candidates, top_n=5)
    assert not result.ok
    assert "坐标" in result.error_text


def test_rejects_empty_payload(candidates):
    assert not validate_ai_payload(None, candidates).ok
    assert not validate_ai_payload({}, candidates).ok


def test_top_n_truncation(candidates):
    raw = {
        "items": [
            {"poi_id": "B001", "rank": 1, "reason": "a"},
            {"poi_id": "B002", "rank": 2, "reason": "b"},
            {"poi_id": "B003", "rank": 3, "reason": "c"},
        ]
    }
    result = validate_ai_payload(raw, candidates, top_n=2)
    assert result.ok
    assert len(result.items) == 2


def test_reason_length_capped(candidates):
    raw = {"items": [{"poi_id": "B001", "rank": 1, "reason": "很" * 800}]}
    result = validate_ai_payload(raw, candidates, top_n=5)
    assert result.ok
    assert len(result.items[0].reason) <= 400


def test_messages_contain_hard_constraints(candidates):
    payload = candidate_payload(
        candidates,
        point_a={"name": "天津站"},
        point_b={"name": "天津大学"},
        region_desc="圆形区域：半径 3 千米",
        preferences={"natural_language": "安静"},
        top_n=5,
    )
    messages = build_ai_messages(payload, strict=True)
    system = messages[0]["content"]
    assert "严禁编造" in system
    assert "poi_id" in system
    assert "硬性规则" in system
    assert "上一次" in system
    assert "candidates" in messages[1]["content"]
