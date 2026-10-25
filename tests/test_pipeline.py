"""主流程集成测试：几何约束、AI 拦截、降级（使用假的高德/DeepSeek 客户端）。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from algorithms.pipeline import (
    AI_FALLBACK_NOTICE,
    AI_INVALID_NOTICE,
    NO_RESULT_MESSAGE,
    RecommendRequest,
    RecommendationError,
    RecommendationPipeline,
)
from config.settings import Settings
from geometry.regions import CircleRegion, PolygonRegion
from models.schemas import (
    Poi,
    RecMode,
    RegionMode,
    RouteLeg,
    ServiceState,
    TravelMode,
    UserPoint,
)
from services.amap_web import PoiPage
from services.deepseek import LLMResult
from services.errors import AmapError

POINTS = {
    "B001": (117.2100, 39.1350),
    "B002": (117.2110, 39.1352),
    "B003": (117.2120, 39.1354),
    "OUTSIDE": (117.4000, 39.5000),
}


class FakeAMap:
    def __init__(self, fail: bool = False, empty: bool = False) -> None:
        self.fail = fail
        self.empty = empty
        self.calls = 0

    def _poi(self, pid: str) -> Poi:
        lng, lat = POINTS[pid]
        return Poi(id=pid, name=f"地点{pid}", lng=lng, lat=lat, address="天津", type_code="050500")

    async def place_around(self, center, radius, **kwargs) -> PoiPage:
        self.calls += 1
        if self.fail:
            raise AmapError("高德服务异常", state=ServiceState.SERVICE_ERROR)
        if self.empty:
            return PoiPage(total=0, pois=[])
        pois = [self._poi(pid) for pid in ("B001", "B002", "B003", "OUTSIDE")]
        return PoiPage(total=len(pois), pois=pois)

    async def place_polygon(self, points, **kwargs) -> PoiPage:
        return await self.place_around((0, 0), 0)

    async def route(self, mode, origin, destination, **kwargs) -> RouteLeg:
        self.calls += 1
        dist = 1000 + abs(origin[0] - destination[0]) * 100000
        return RouteLeg(
            mode=mode,
            ok=True,
            distance_m=dist,
            duration_s=600 + dist / 10,
            transfers=1,
            walk_m=200,
            summary="测试路线",
        )


class FakeDeepSeek:
    def __init__(self, payload: dict[str, Any] | None = None, configured: bool = True) -> None:
        self.payload = payload
        self.configured = configured
        self.calls = 0

    async def chat_json(self, messages, **kwargs) -> LLMResult:
        self.calls += 1
        return LLMResult(content="{}", data=self.payload, latency_ms=120, model="fake", usage={})


def make_settings(tmp_path) -> Settings:
    return Settings(
        amap_web_key="k", amap_js_key="k", deepseek_api_key="k", top_n=3, max_candidates=10,
        data_dir=tmp_path, poi_page_size=25, poi_max_pages=1, max_tiles=4,
        tile_target_km=3.0, search_radius_default_m=3000, max_concurrency=2,
    )


def make_request(region_mode: RegionMode = RegionMode.CIRCLE, **kwargs) -> RecommendRequest:
    region = None
    if region_mode is RegionMode.CIRCLE:
        region = CircleRegion(117.2110, 39.1352, 2000)
    elif region_mode is RegionMode.POLYGON:
        region = PolygonRegion(
            [(117.2000, 39.1300), (117.2200, 39.1300), (117.2200, 39.1400), (117.2000, 39.1400)]
        )
    defaults = dict(
        point_a=UserPoint(lng=117.20, lat=39.13, name="A 家"),
        point_b=UserPoint(lng=117.22, lat=39.14, name="B 公司"),
        region=region,
        region_mode=region_mode,
        travel_mode=TravelMode.TRANSIT,
        category="咖啡厅",
        rec_mode=RecMode.AI,
        top_n=3,
    )
    defaults.update(kwargs)
    return RecommendRequest(**defaults)


def test_pipeline_ai_success(tmp_path):
    amap = FakeAMap()
    deepseek = FakeDeepSeek(
        {
            "summary": "综合推荐",
            "items": [
                {"poi_id": "B002", "rank": 1, "reason": "双方通勤最短"},
                {"poi_id": "B001", "rank": 2, "reason": "环境安静"},
            ],
        }
    )
    pipeline = RecommendationPipeline(amap, deepseek, make_settings(tmp_path), monitor=None, db=None)
    result = asyncio.run(pipeline.run(make_request()))

    assert result.source == "ai"
    assert [r.poi.id for r in result.recommendations] == ["B002", "B001"]
    assert result.recommendations[0].reason == "双方通勤最短"
    # 区域外的 OUTSIDE 必须被几何硬过滤
    assert all(r.poi.id != "OUTSIDE" for r in result.recommendations)
    assert result.stats["in_region"] == 3


def test_pipeline_filters_outside_polygon(tmp_path):
    amap = FakeAMap()
    deepseek = FakeDeepSeek({"items": [{"poi_id": "B001", "rank": 1, "reason": "ok"}]})
    pipeline = RecommendationPipeline(amap, deepseek, make_settings(tmp_path))
    result = asyncio.run(pipeline.run(make_request(RegionMode.POLYGON)))
    assert all(r.poi.id != "OUTSIDE" for r in result.recommendations)
    assert result.region_desc.startswith("多边形区域")


def test_pipeline_ai_invalid_falls_back(tmp_path):
    amap = FakeAMap()
    deepseek = FakeDeepSeek({"items": [{"poi_id": "NOT-EXIST", "rank": 1, "reason": "虚构"}]})
    pipeline = RecommendationPipeline(amap, deepseek, make_settings(tmp_path))
    result = asyncio.run(pipeline.run(make_request()))

    assert result.source == "rule"
    assert result.notice == AI_INVALID_NOTICE
    assert result.recommendations
    assert all(r.poi.id in {"B001", "B002", "B003"} for r in result.recommendations)
    assert deepseek.calls >= 2  # 有限次重试


def test_pipeline_ai_unavailable_uses_rule(tmp_path):
    amap = FakeAMap()
    pipeline = RecommendationPipeline(amap, None, make_settings(tmp_path))
    result = asyncio.run(pipeline.run(make_request()))
    assert result.source == "rule"
    assert result.notice == AI_FALLBACK_NOTICE
    assert len(result.recommendations) <= 3


def test_pipeline_no_poi_message(tmp_path):
    amap = FakeAMap(empty=True)
    pipeline = RecommendationPipeline(amap, None, make_settings(tmp_path))
    with pytest.raises(RecommendationError) as exc:
        asyncio.run(pipeline.run(make_request()))
    assert str(exc.value) == NO_RESULT_MESSAGE


def test_pipeline_amap_failure_message(tmp_path):
    amap = FakeAMap(fail=True)
    pipeline = RecommendationPipeline(amap, None, make_settings(tmp_path))
    with pytest.raises(RecommendationError) as exc:
        asyncio.run(pipeline.run(make_request()))
    assert "高德地图数据服务不可用" in str(exc.value)


def test_pipeline_requires_region_when_mode_circle(tmp_path):
    pipeline = RecommendationPipeline(FakeAMap(), None, make_settings(tmp_path))
    with pytest.raises(RecommendationError):
        asyncio.run(pipeline.run(make_request(RegionMode.CIRCLE, region=None)))


def test_pipeline_requires_distinct_points(tmp_path):
    point = UserPoint(lng=117.2, lat=39.1, name="同一点")
    pipeline = RecommendationPipeline(FakeAMap(), None, make_settings(tmp_path))
    with pytest.raises(RecommendationError):
        asyncio.run(pipeline.run(make_request(point_a=point, point_b=point)))


def test_pipeline_cancel(tmp_path):
    from algorithms.candidates import CancelToken

    amap = FakeAMap()
    pipeline = RecommendationPipeline(amap, None, make_settings(tmp_path))
    token = CancelToken()
    token.cancel("用户取消")

    async def run():
        return await pipeline.run(make_request(), cancel=token)

    with pytest.raises((asyncio.CancelledError, RecommendationError)):
        asyncio.run(run())
