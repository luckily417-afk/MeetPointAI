"""推荐系统边界：确定性、候选截断、覆盖告警、top_n 大于候选数。"""

from __future__ import annotations

import asyncio

import pytest

from algorithms.pipeline import RecommendRequest, RecommendationPipeline
from config.settings import Settings
from diagnostics.cases import CaseConfig
from diagnostics.runner import RunOptions, run_search
from diagnostics.tracing import TraceRecorder
from geometry.regions import CircleRegion
from models.schemas import Poi, RecMode, RegionMode, RouteLeg, TravelMode, UserPoint
from services.amap_web import PoiPage

CENTER = (117.2115, 39.1360)


class StaticAMap:
    configured = True

    def __init__(self, pois: list[Poi], *, ok: bool = True) -> None:
        self.pois = pois
        self.ok = ok

    async def place_around(self, center, radius, **kwargs):  # noqa: ANN001, ANN003
        return PoiPage(total=len(self.pois), pois=list(self.pois))

    async def place_polygon(self, points, **kwargs):  # noqa: ANN001, ANN003
        return PoiPage(total=len(self.pois), pois=list(self.pois))

    async def route(self, mode, origin, destination, **kwargs):  # noqa: ANN001, ANN003
        if not self.ok:
            return RouteLeg(mode=mode, ok=False, error="不可达")
        return RouteLeg(mode=mode, ok=True, duration_s=600, distance_m=1000, transfers=0, walk_m=100)


def _pois(n: int) -> list[Poi]:
    return [
        Poi(
            id=f"P{i}",
            name=f"点{i}",
            lng=CENTER[0] + 0.0006 * i,
            lat=CENTER[1] + 0.0003 * i,
            type_code="050500",
        )
        for i in range(n)
    ]


def _settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, top_n=5, max_candidates=10, ai_candidate_limit=6,
                    poi_max_pages=1, max_tiles=2)


def _request(top_n: int = 5) -> RecommendRequest:
    return RecommendRequest(
        point_a=UserPoint(lng=117.20, lat=39.13, name="A"),
        point_b=UserPoint(lng=117.22, lat=39.14, name="B"),
        region=CircleRegion(CENTER[0], CENTER[1], 900),
        region_mode=RegionMode.CIRCLE,
        travel_mode=TravelMode.TRANSIT,
        category="咖啡厅",
        rec_mode=RecMode.TOTAL_TIME,
        top_n=top_n,
    )


def test_repeated_runs_are_deterministic(tmp_path):
    case = CaseConfig.model_validate(
        {
            "name": "deterministic",
            "a": {"name": "A", "lng": 117.210089, "lat": 39.135951},
            "b": {"name": "B", "lng": 117.216, "lat": 39.14},
            "region": {"mode": "circle", "center": list(CENTER), "radius_m": 700},
            "top_n": 3,
        }
    )
    first = asyncio.run(run_search(case, RunOptions(mock=True)))
    second = asyncio.run(run_search(case, RunOptions(mock=True)))
    ids1 = [r.poi.id for r in first.result.recommendations]
    ids2 = [r.poi.id for r in second.result.recommendations]
    assert ids1 == ids2
    assert first.trace.snapshot()["counts"] == second.trace.snapshot()["counts"]


def test_different_seed_changes_pool(tmp_path):
    base = {
        "a": {"name": "A", "lng": 117.210089, "lat": 39.135951},
        "b": {"name": "B", "lng": 117.216, "lat": 39.14},
        "region": {"mode": "circle", "center": list(CENTER), "radius_m": 700},
        "top_n": 3,
    }
    case1 = CaseConfig.model_validate({"name": "seed-a", **base})
    case2 = CaseConfig.model_validate({"name": "seed-b", **base})
    ids1 = [r.poi.id for r in asyncio.run(run_search(case1, RunOptions(mock=True))).result.recommendations]
    ids2 = [r.poi.id for r in asyncio.run(run_search(case2, RunOptions(mock=True))).result.recommendations]
    assert ids1 != ids2


def test_truncation_emits_warning(tmp_path):
    trace = TraceRecorder()
    pipeline = RecommendationPipeline(StaticAMap(_pois(30)), None, _settings(tmp_path))
    asyncio.run(pipeline.run(_request(top_n=3), trace=trace))
    codes = {w["code"] for w in trace.warnings}
    assert "CANDIDATES_TRUNCATED" in codes
    assert trace.counts["in_region"] > _settings(tmp_path).max_candidates
    assert trace.counts["preselected"] <= _settings(tmp_path).max_candidates


def test_low_coverage_warning(tmp_path):
    trace = TraceRecorder()
    pipeline = RecommendationPipeline(StaticAMap(_pois(3)), None, _settings(tmp_path))
    asyncio.run(pipeline.run(_request(top_n=3), trace=trace))
    assert "LOW_COVERAGE" in {w["code"] for w in trace.warnings}


def test_top_n_larger_than_candidates(tmp_path):
    pipeline = RecommendationPipeline(StaticAMap(_pois(2)), None, _settings(tmp_path))
    result = asyncio.run(pipeline.run(_request(top_n=5)))
    assert len(result.recommendations) == 2
    assert [r.rank for r in result.recommendations] == [1, 2]


def test_all_candidates_unreachable_is_business_no_result(tmp_path):
    pipeline = RecommendationPipeline(StaticAMap(_pois(4), ok=False), None, _settings(tmp_path))
    with pytest.raises(Exception) as exc:
        asyncio.run(pipeline.run(_request()))
    assert "均无法规划出双方可达路线" in str(exc.value)


def test_trace_records_elimination_reasons(tmp_path):
    trace = TraceRecorder()
    pipeline = RecommendationPipeline(StaticAMap(_pois(25)), None, _settings(tmp_path))
    asyncio.run(pipeline.run(_request(top_n=3), trace=trace))
    snap = trace.snapshot()
    stages = {item["stage"] for item in snap["eliminated"]}
    assert "preselect" in stages
    assert snap["counts"]["raw"] >= 25
    assert snap["counts"]["final"] == 3
