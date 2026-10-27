"""本轮 bug 修复的回归测试（每条对应一个已修复的真实缺陷）。"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from algorithms.ai_validator import validate_ai_payload
from algorithms.candidates import _call_with_retry
from cli.main import build_parser, main as cli_main
from diagnostics.cases import load_case
from diagnostics.mockdata import build_mock_ai_response
from diagnostics.runner import RunOptions, run_search
from diagnostics.sources import Budget, MockAMap
from diagnostics.tracing import TraceRecorder
from models.schemas import Candidate, Poi, RouteLeg, ServiceState, TravelMode
from services.amap_web import AMapWebService
from services.errors import AmapError, NotConfiguredError, ServiceError
from services.http_client import HttpTransport, gather_limited
from storage.db import Database

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "tests" / "data" / "small_case.json"


# --------------------------------------------------------------------------- #
# mock 数据必须符合 AI 输出契约（否则 --mock 永远降级为规则排序）
# --------------------------------------------------------------------------- #
def _candidates(n: int) -> list[Candidate]:
    leg = RouteLeg(mode=TravelMode.TRANSIT, ok=True, duration_s=600, distance_m=1000)
    return [
        Candidate(
            poi=Poi(
                id=f"P{i}",
                name=f"地点{i}",
                lng=117.2 + 0.001 * i,
                lat=39.1 + 0.001 * i,
                rating=3.0 + 0.1 * i,
                cost=20.0 + i,
            ),
            leg_a=leg,
            leg_b=leg.model_copy(),
        )
        for i in range(n)
    ]


def test_mock_ai_response_passes_validator():
    cands = _candidates(6)
    payload = [
        {
            "poi_id": c.id,
            "name": c.poi.name,
            "type": c.poi.type_name,
            "address": c.poi.address,
            "business_area": c.poi.business_area,
            "rating": c.poi.rating,
            "cost_per_person": c.poi.cost,
            "both_sides_reachable": c.routes_ok,
        }
        for c in cands
    ]
    response = build_mock_ai_response(payload, top_n=4)
    result = validate_ai_payload(response, cands, top_n=4)
    assert result.ok, result.errors
    assert len(result.items) == 4
    assert [item.rank for item in result.items] == [1, 2, 3, 4]
    # 理由里不能出现通勤/坐标之类被契约禁止的内容
    for item in result.items:
        assert "分钟" not in item.reason
        assert "通勤" not in item.reason


def test_mock_ai_prefers_rated_candidates():
    cands = _candidates(3)
    payload = [
        {"poi_id": c.id, "rating": c.poi.rating, "cost_per_person": c.poi.cost, "both_sides_reachable": True}
        for c in cands
    ]
    response = build_mock_ai_response(payload, top_n=1)
    assert response["items"][0]["poi_id"] == "P2"


# --------------------------------------------------------------------------- #
# mock POI 名称必须唯一（否则最终推荐出现多条同名地点）
# --------------------------------------------------------------------------- #
def test_mock_pois_have_unique_names_across_tiles():
    source = MockAMap(budget=Budget(), seed_text="unique")
    pages = [
        asyncio.run(source.place_around((117.20 + 0.002 * i, 39.13), 900, page=1, offset=25))
        for i in range(3)
    ]
    names = [poi.name for page in pages for poi in page.pois]
    ids = [poi.id for page in pages for poi in page.pois]
    assert len(ids) == len(set(ids)), "同一坐标应得到同一 ID"
    assert len(names) == len(set(names)), f"不同 POI 不应重名：{names[:5]}"
    assert all("模拟点位" in name for name in names)


def test_mock_uses_requested_category():
    source = MockAMap(budget=Budget(), seed_text="cat")
    page = asyncio.run(source.place_around((117.20, 39.13), 900, types="110101|110100", keywords="公园"))
    assert page.pois
    assert page.pois[0].type_code == "110101"
    assert page.pois[0].type_name.endswith("公园")
    assert page.pois[0].name.startswith("公园")


# --------------------------------------------------------------------------- #
# 警告去重
# --------------------------------------------------------------------------- #
def test_trace_warn_dedupes_identical_messages():
    trace = TraceRecorder()
    for _ in range(3):
        trace.warn("AI_VALIDATION", "同样的原因")
    trace.warn("AI_VALIDATION", "另一个原因")
    warnings = trace.snapshot()["warnings"]
    assert len(warnings) == 2
    assert warnings[0]["repeat"] == 3


# --------------------------------------------------------------------------- #
# CLI：report 的非法 run_id 必须走结构化失败，而不是抛 traceback
# --------------------------------------------------------------------------- #
def test_cli_report_invalid_run_id_is_usage_error(tmp_path, capsys):
    code = cli_main(
        ["report", "--run-id", "ab", "--json", "--runs-dir", str(tmp_path)]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["errors"][0]["code"] == "RUN_INVALID_ID"


def test_cli_rejects_out_of_range_numbers():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["search", "--config", "x.json", "--top-n", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["history", "--limit", "0"])
    args = parser.parse_args(["search", "--config", "x.json", "--top-n", "3", "--timeout", "5"])
    assert args.top_n == 3
    assert args.timeout == 5.0


# --------------------------------------------------------------------------- #
# 高德服务层：错误分类/脏数据/offset/骑行 Key
# --------------------------------------------------------------------------- #
class _StubTransport:
    def __init__(self, payload=None, status=200, error: BaseException | None = None) -> None:
        self.payload = payload if payload is not None else {"status": "1", "count": "0", "pois": []}
        self.status = status
        self.error = error
        self.calls: list[dict] = []

    async def get_json(self, service, url, params=None, *, max_retries=2, validate=None, **kw):  # noqa: ANN001
        self.calls.append({"url": url, "params": dict(params or {})})
        if self.error is not None:
            raise self.error
        if validate is not None:
            validate(self.payload, self.status)
        return self.payload, self.status, 1


def test_http_error_status_is_classified():
    svc = AMapWebService("k", _StubTransport())
    with pytest.raises(AmapError) as exc:
        svc._validate_amap({"status": "1"}, 502)
    assert exc.value.state is ServiceState.SERVICE_ERROR
    assert exc.value.retryable is True

    with pytest.raises(AmapError) as exc:
        svc._validate_amap({"status": "0", "info": "INVALID_PARAMS", "infocode": "20003"}, 200)
    assert exc.value.retryable is False, "参数类业务错误不应触发重试"


def test_dirty_poi_is_dropped_not_fatal():
    svc = AMapWebService("k", _StubTransport())
    assert svc._parse_poi({"id": "", "name": "", "location": "117.2,39.1"}) is None
    page = svc._parse_page(
        {"count": "2", "pois": [{"id": "", "name": "", "location": "117.2,39.1"},
                                {"id": "ok", "name": "正常", "location": "117.2,39.1"}]},
        1,
        25,
    )
    assert [poi.id for poi in page.pois] == ["ok"]


def test_transport_error_becomes_amap_error():
    err = ServiceError(
        "网络连接失败",
        service=__import__("models.schemas", fromlist=["ServiceName"]).ServiceName.AMAP_WEB,
        state=ServiceState.CONNECT_ERROR,
        retryable=True,
    )
    svc = AMapWebService("k", _StubTransport(error=err))
    with pytest.raises(AmapError) as exc:
        asyncio.run(svc.place_text("咖啡"))
    assert exc.value.state is ServiceState.CONNECT_ERROR

    leg = asyncio.run(svc.route(TravelMode.DRIVING, (117.2, 39.1), (117.21, 39.11)))
    assert not leg.ok and "网络连接失败" in leg.error


def test_offset_is_clamped_consistently():
    stub = _StubTransport(payload={"status": "1", "count": "40", "pois": []})
    svc = AMapWebService("k", stub)
    page = asyncio.run(svc.place_around((117.2, 39.1), 500, page=1, offset=50))
    assert stub.calls[-1]["params"]["offset"] == 25
    assert page.offset == 25
    assert page.has_more is True  # 25 * 1 < 40


def test_bicycling_requires_key():
    stub = _StubTransport()
    svc = AMapWebService("", stub)
    with pytest.raises(NotConfiguredError):
        asyncio.run(svc.route(TravelMode.BICYCLING, (117.2, 39.1), (117.21, 39.11)))
    assert stub.calls == []


def test_business_param_error_is_not_retried():
    attempts = {"n": 0}

    async def factory():
        attempts["n"] += 1
        raise AmapError("INVALID_PARAMS", state=ServiceState.SERVICE_ERROR, biz_code="20003")

    with pytest.raises(AmapError):
        asyncio.run(_call_with_retry(factory))
    assert attempts["n"] == 1


# --------------------------------------------------------------------------- #
# HTTP 层：取消不被吞、指标不翻倍
# --------------------------------------------------------------------------- #
def test_cancelled_error_propagates_through_transport():
    transport = HttpTransport()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    async def run():
        transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await transport.get_json("amap_web", "https://example.invalid/x")
        finally:
            await transport.aclose()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    async def boom():
        raise asyncio.CancelledError()

    async def gather_case():
        return await gather_limited([boom(), boom()])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gather_case())


def test_non_json_response_is_counted_once():
    transport = HttpTransport()
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, content=b"<html>not json</html>")

    async def run():
        transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await transport.get_json("amap_web", "https://example.invalid/x", max_retries=1)
        finally:
            await transport.aclose()

    with pytest.raises(ServiceError):
        asyncio.run(run())
    snap = transport.metrics.snapshot()
    assert snap["calls"]["amap_web"] == attempts["n"] == 2
    assert snap["failures"]["amap_web"] == 2


# --------------------------------------------------------------------------- #
# 存储：失败事务必须回滚
# --------------------------------------------------------------------------- #
def test_failed_transaction_is_rolled_back():
    db = Database(Path(tempfile.mkdtemp()) / "t.sqlite3")
    db.put_json("k1", {"a": 1})
    with pytest.raises(Exception):
        with db._cursor() as cur:  # noqa: SLF001
            cur.execute("DELETE FROM kv_cache")
            cur.execute("DELETE FROM table_that_does_not_exist")
    db.put_json("k2", {"b": 2})
    assert db.get_json("k1") == {"a": 1}, "失败事务被下一次 commit 顺手提交了"
    db.close()


# --------------------------------------------------------------------------- #
# 健康监测：没有探针的服务不会一直"到期"
# --------------------------------------------------------------------------- #
def test_service_without_probe_is_not_always_due():
    from monitoring.health import HealthMonitor

    monitor = HealthMonitor(interval_s=60)
    monitor.register("amap_js", None, configured=True)

    async def run():
        return await monitor.check_due(), await monitor.check_due()

    first, second = asyncio.run(run())
    assert first == ["amap_js"]
    assert second == []


# --------------------------------------------------------------------------- #
# 端到端：mock 搜索必须走 AI 通路、同名地点不重复、警告不重复
# --------------------------------------------------------------------------- #
def test_mock_search_uses_ai_and_has_no_duplicates():
    case = load_case(CASE)
    outcome = asyncio.run(run_search(case, RunOptions(mock=True, top_n=4)))
    assert outcome.status == "ok", outcome.errors
    result = outcome.result
    assert result.source == "ai", result.notice
    names = [rec.poi.name for rec in result.recommendations]
    assert len(names) == len(set(names))
    codes = [w["code"] for w in outcome.trace.snapshot()["warnings"]]
    assert codes.count("AI_VALIDATION") == 0
    assert len(codes) == len(set(codes))


def test_mock_search_top_n_override_is_honoured():
    case = load_case(CASE)
    outcome = asyncio.run(run_search(case, RunOptions(mock=True, top_n=2)))
    assert outcome.status == "ok"
    assert len(outcome.result.recommendations) == 2
