"""API 异常与故障注入（需求八·API 异常）。全部离线，不访问网络。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from algorithms.pipeline import (
    AI_FALLBACK_NOTICE,
    AI_INVALID_NOTICE,
    RecommendRequest,
    RecommendationError,
    RecommendationPipeline,
)
from algorithms.candidates import plan_routes
from config.settings import Settings
from diagnostics.cases import CaseConfig
from diagnostics.runner import RunOptions, run_search
from geometry.regions import CircleRegion
from models.schemas import (
    Candidate,
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
from services.errors import AmapError, DeepSeekError
from storage.db import Database

CENTER = (117.2115, 39.1360)


def _pois(n: int = 5) -> list[Poi]:
    result = []
    for i in range(n):
        lng = CENTER[0] + 0.001 * (i + 1)
        lat = CENTER[1] + 0.0005 * (i + 1)
        result.append(
            Poi(id=f"F{i+1}", name=f"测试点{i+1}", lng=lng, lat=lat, address="测试地址", type_code="050500")
        )
    return result


class FaultAMap:
    configured = True

    def __init__(self, *, search_error=None, route_error=None, routes_ok=True, pois=None) -> None:
        self.search_error = search_error
        self.route_error = route_error
        self.routes_ok = routes_ok
        self.pois = pois if pois is not None else _pois()
        self.calls = 0

    async def place_around(self, center, radius, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if self.search_error:
            raise self.search_error
        return PoiPage(total=len(self.pois), pois=list(self.pois))

    async def place_polygon(self, points, **kwargs):  # noqa: ANN001, ANN003
        return await self.place_around(CENTER, 500, **kwargs)

    async def route(self, mode, origin, destination, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if self.route_error:
            raise self.route_error
        if not self.routes_ok:
            return RouteLeg(mode=mode, ok=False, error="模拟不可达")
        return RouteLeg(
            mode=mode, ok=True, duration_s=600, distance_m=1500, transfers=1, walk_m=300,
            summary="模拟路线",
        )


class FaultDeepSeek:
    configured = True
    model = "fake-model"

    def __init__(self, *, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def chat_json(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def _settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        top_n=3,
        max_candidates=6,
        ai_candidate_limit=4,
        poi_page_size=25,
        poi_max_pages=1,
        max_tiles=2,
        tile_target_km=2.0,
    )


def _request() -> RecommendRequest:
    return RecommendRequest(
        point_a=UserPoint(lng=117.20, lat=39.13, name="A"),
        point_b=UserPoint(lng=117.22, lat=39.14, name="B"),
        region=CircleRegion(CENTER[0], CENTER[1], 600),
        region_mode=RegionMode.CIRCLE,
        travel_mode=TravelMode.TRANSIT,
        category="咖啡厅",
        rec_mode=RecMode.AI,
        top_n=3,
    )


def _run(pipeline: RecommendationPipeline):
    return asyncio.run(pipeline.run(_request()))


def _llm(data, content="{}") -> LLMResult:
    return LLMResult(content=content, data=data, latency_ms=5, model="fake-model", usage={})


# --------------------------------------------------------------------------- #
# 高德故障
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "state",
    [
        ServiceState.TIMEOUT,
        ServiceState.AUTH_ERROR,
        ServiceState.QUOTA_LIMITED,
        ServiceState.CONNECT_ERROR,
        ServiceState.SERVICE_ERROR,
    ],
)
def test_amap_search_failure_is_reported_as_service_error(tmp_path, state):
    error = AmapError("模拟高德故障", state=state)
    pipeline = RecommendationPipeline(FaultAMap(search_error=error), None, _settings(tmp_path))
    with pytest.raises(RecommendationError) as exc:
        _run(pipeline)
    assert "高德地图数据服务不可用" in str(exc.value)
    assert exc.value.state is state


def test_route_failure_is_not_misjudged_as_api_outage(tmp_path):
    pipeline = RecommendationPipeline(FaultAMap(routes_ok=False), None, _settings(tmp_path))
    with pytest.raises(RecommendationError) as exc:
        _run(pipeline)
    message = str(exc.value)
    assert "均无法规划出双方可达路线" in message
    assert "服务不可用" not in message


def test_route_exception_is_contained_per_candidate(tmp_path):
    # 单条路线抛异常时，该候选被标记不可达，而不是让整个任务变成 API 故障
    pipeline = RecommendationPipeline(
        FaultAMap(route_error=AmapError("单点路线失败", state=ServiceState.SERVICE_ERROR)), None, _settings(tmp_path)
    )
    with pytest.raises(RecommendationError) as exc:
        _run(pipeline)
    assert "均无法规划出双方可达路线" in str(exc.value)


def test_transient_route_failure_is_recovered_after_batch():
    class FlakyAMap(FaultAMap):
        def __init__(self):
            super().__init__()
            self.b_calls = 0

        async def route(self, mode, origin, destination, **kwargs):
            if origin == (117.22, 39.14):
                self.b_calls += 1
                if self.b_calls == 1:
                    raise AmapError("瞬时连接失败", state=ServiceState.CONNECT_ERROR)
            return await super().route(mode, origin, destination, **kwargs)

    amap = FlakyAMap()
    candidates = asyncio.run(plan_routes(
        amap, _pois(1),
        point_a=UserPoint(lng=117.20, lat=39.13, name="A"),
        point_b=UserPoint(lng=117.22, lat=39.14, name="B"),
        mode=TravelMode.DRIVING,
    ))
    assert amap.b_calls == 2
    assert candidates[0].routes_ok


# --------------------------------------------------------------------------- #
# DeepSeek 故障
# --------------------------------------------------------------------------- #
def test_deepseek_timeout_falls_back_to_rule(tmp_path):
    amap = FaultAMap()
    ds = FaultDeepSeek(error=DeepSeekError("请求超时", state=ServiceState.TIMEOUT))
    pipeline = RecommendationPipeline(amap, ds, _settings(tmp_path))
    result = _run(pipeline)
    assert result.source == "rule"
    assert result.notice.startswith(AI_FALLBACK_NOTICE)
    assert result.recommendations


def test_deepseek_quota_falls_back(tmp_path):
    ds = FaultDeepSeek(error=DeepSeekError("余额不足", state=ServiceState.QUOTA_LIMITED))
    pipeline = RecommendationPipeline(FaultAMap(), ds, _settings(tmp_path))
    result = _run(pipeline)
    assert result.source == "rule"
    assert "配额" in result.notice or "余额" in result.notice or AI_FALLBACK_NOTICE in result.notice


def test_deepseek_invalid_json_retries_then_falls_back(tmp_path):
    ds = FaultDeepSeek(result=_llm(None, content="not json"))
    pipeline = RecommendationPipeline(FaultAMap(), ds, _settings(tmp_path))
    result = _run(pipeline)
    assert result.source == "rule"
    assert result.notice == AI_INVALID_NOTICE
    assert ds.calls == 3


def test_deepseek_unknown_poi_id_falls_back(tmp_path):
    ds = FaultDeepSeek(result=_llm({"items": [{"poi_id": "NOT-EXIST", "rank": 1, "reason": "虚构"}]}))
    pipeline = RecommendationPipeline(FaultAMap(), ds, _settings(tmp_path))
    result = _run(pipeline)
    assert result.source == "rule"
    assert result.notice == AI_INVALID_NOTICE


def test_deepseek_out_of_region_recommendation_rejected(tmp_path):
    ds = FaultDeepSeek(
        result=_llm({"items": [{"poi_id": "OUTSIDE", "rank": 1, "reason": "区域外"}]})
    )
    pois = _pois(3)
    far = Poi(id="OUTSIDE", name="区域外", lng=118.0, lat=40.0)
    amap = FaultAMap(pois=pois + [far])
    pipeline = RecommendationPipeline(amap, ds, _settings(tmp_path))
    result = _run(pipeline)
    # 区域外的 POI 在检索阶段就被过滤，AI 无法选中它
    assert result.source in {"rule", "ai"}
    assert all(rec.poi.id != "OUTSIDE" for rec in result.recommendations)


def test_ai_valid_result_is_used(tmp_path):
    ds = FaultDeepSeek(
        result=_llm(
            {
                "summary": "综合较好",
                "items": [
                    {"poi_id": "F2", "rank": 1, "reason": "环境舒适"},
                    {"poi_id": "F1", "rank": 2, "reason": "品牌丰富"},
                ],
            }
        )
    )
    pipeline = RecommendationPipeline(FaultAMap(), ds, replace(_settings(tmp_path), ai_candidate_limit=6))
    result = _run(pipeline)
    assert result.source == "ai"
    assert [r.poi.id for r in result.recommendations] == ["F2", "F1"]
    assert result.recommendations[0].reason == "环境舒适"


def test_ai_short_result_is_retried_and_reported(tmp_path):
    ds = FaultDeepSeek(result=_llm({
        "summary": "精选三个地点",
        "items": [{"poi_id": "F1", "rank": 1, "reason": "场所环境合适"}],
    }))
    result = _run(RecommendationPipeline(FaultAMap(), ds, replace(_settings(tmp_path), ai_candidate_limit=6)))
    assert ds.calls == 3
    assert result.source == "ai"
    assert len(result.recommendations) == 1
    assert "仅返回 1 个" in result.notice
    assert "仅返回 1 个" in result.ai_summary


def test_ai_compares_every_reachable_candidate_in_batches(tmp_path):
    class BatchDeepSeek:
        configured = True
        model = "fake-model"

        def __init__(self):
            self.seen = []

        async def chat_json(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            ids = [item["poi_id"] for item in payload["candidates"]]
            self.seen.append(ids)
            return _llm({"items": [
                {"poi_id": pid, "rank": i + 1, "reason": "地点信息符合要求"}
                for i, pid in enumerate(ids[:payload["top_n"]])
            ]})

    ds = BatchDeepSeek()
    settings = replace(_settings(tmp_path), ai_candidate_limit=40)
    pipeline = RecommendationPipeline(FaultAMap(), ds, settings)
    leg = RouteLeg(mode=TravelMode.DRIVING, ok=True, duration_s=600)
    pool = [Candidate(poi=Poi(id=f"P{i:02d}", name=f"地点{i}", lng=117.2, lat=39.13),
                      leg_a=leg, leg_b=leg.model_copy()) for i in range(60)]
    request = _request()
    request.region = None
    request.top_n = 3
    ordered, _, source, _ = asyncio.run(pipeline._rank(request, pool))
    assert source == "ai"
    assert len(ordered) == 3
    assert len(ds.seen) == 3
    assert set(ds.seen[0] + ds.seen[1]) == {c.id for c in pool}


# --------------------------------------------------------------------------- #
# 缓存损坏 / 网络隔离
# --------------------------------------------------------------------------- #
def test_corrupt_cache_entry_is_ignored(tmp_path):
    db = Database(tmp_path / "cache.sqlite3")
    with db._cursor() as cur:  # noqa: SLF001
        cur.execute(
            "INSERT INTO kv_cache(key, payload, created_at) VALUES(?,?,?)",
            ("route:broken", "{not-json", 1.0),
        )
    assert db.get_json("route:broken") is None
    db.close()


def test_mock_search_makes_no_network_calls(tmp_path, monkeypatch):
    import httpx

    async def boom(*args, **kwargs):  # noqa: ANN001, ANN003
        raise AssertionError("mock/replay 模式不应发起网络请求")

    monkeypatch.setattr(httpx.AsyncClient, "request", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    case = CaseConfig.model_validate(
        {
            "name": "offline-mock",
            "a": {"name": "A", "lng": 117.20, "lat": 39.13},
            "b": {"name": "B", "lng": 117.22, "lat": 39.14},
            "region": {"mode": "circle", "center": list(CENTER), "radius_m": 800},
            "top_n": 3,
        }
    )
    outcome = asyncio.run(run_search(case, RunOptions(mock=True, offline=True)))
    assert outcome.status in {"ok", "no_result"}
    assert outcome.stats.get("api_calls", 0) > 0
