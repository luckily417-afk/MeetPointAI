"""本轮修复的回归测试（每条对应一个已修复的真实缺陷）。

覆盖：
1. tile_circle / tile_polygon 的 max_tiles 硬上限
2. 录制 → 回放：geocode 负载结构、AI 响应按请求匹配、mock 运行也录制 AI
3. UTF-8 BOM 的用例/录制/套件/结果文件
4. CLI：录制错误与用法错误都要给 JSON 信封与正确退出码
5. candidates / routes 的 --timeout 生效
6. --out 写失败不影响退出码
7. benchmark 预算与汇总计数
8. 传输层重试计入 API 预算
9. UI 里与健康状态相关的纯逻辑
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from algorithms.candidates import CancelToken
from cli.main import main as cli_main
from config.settings import Settings
from diagnostics.cases import load_case
from diagnostics.recording import Recording
from diagnostics.runner import RunOptions, run_candidates, run_routes, run_search
from diagnostics.sources import (
    Budget,
    LiveAMap,
    MockAMap,
    MockDeepSeek,
    RecordingAMap,
    RecordingDeepSeek,
    ReplayAMap,
    ReplayDeepSeek,
)
from geometry.crs import destination_point, haversine
from geometry.regions import CircleRegion, PolygonRegion, tile_circle, tile_polygon
from models.schemas import ServiceState, TravelMode
from services.errors import DeepSeekError
from ui.main_window import MainWindow, _strip_exception_prefix

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "cases" / "basic.json"


# --------------------------------------------------------------------------- #
# 1. 分块上限是硬约束（以前固定 2×2 起跳，max_tiles=1 时仍然发 4 次请求）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("max_tiles", [1, 2, 4, 9, 36])
@pytest.mark.parametrize("radius", [25.0, 300.0, 3000.0, 12000.0])
def test_tile_circle_respects_max_tiles(radius: float, max_tiles: int) -> None:
    region = CircleRegion(117.2, 39.13, radius)
    tiles = tile_circle(region, target_km=2.0, max_tiles=max_tiles)
    assert 1 <= len(tiles) <= max_tiles
    # 覆盖性：圆内任意点都应落在某个分块半径内
    for bearing in range(0, 360, 45):
        for ratio in (0.3, 0.7, 0.99):
            lng, lat = destination_point(117.2, 39.13, bearing, radius * ratio)
            assert any(haversine(t.lng, t.lat, lng, lat) <= t.radius_m for t in tiles), (bearing, ratio)


@pytest.mark.parametrize("max_tiles", [1, 4, 16])
def test_tile_polygon_respects_max_tiles(max_tiles: int) -> None:
    polygon = PolygonRegion(
        [[117.19, 39.12], [117.25, 39.12], [117.25, 39.16], [117.19, 39.16]]
    )
    tiles = tile_polygon(polygon, target_km=1.0, max_tiles=max_tiles)
    assert 1 <= len(tiles) <= max_tiles


# --------------------------------------------------------------------------- #
# 2. 录制 / 回放
# --------------------------------------------------------------------------- #
def test_geocode_record_replay_roundtrip() -> None:
    """geocode 录制写的是 {"value": [...]}，回放必须能读回来（以前抛 AttributeError）。"""
    budget = Budget(max_api_calls=1000, max_tokens=10**6)
    recording = Recording(run_id="geo")
    mock = RecordingAMap(MockAMap(budget=budget), recording)

    hits = asyncio.run(mock.geocode("天津站"))
    assert hits

    replay = ReplayAMap(recording, budget=budget)
    restored = asyncio.run(replay.geocode("天津站"))
    assert len(restored) == len(hits)
    assert restored[0].lng == pytest.approx(hits[0].lng)
    assert restored[0].formatted_address == hits[0].formatted_address


def test_geocode_replay_accepts_legacy_list_payload() -> None:
    """旧录制文件里 payload 是裸列表，回放也不能崩。"""
    budget = Budget(max_api_calls=1000, max_tokens=10**6)
    recording = Recording(run_id="legacy")
    recording.add_amap(
        "geocode:x",
        "geocode:x",
        [{"formatted_address": "旧格式", "lng": 117.2, "lat": 39.13, "adcode": "120100"}],
        {"address": "天津站", "city": ""},
    )
    # 用同一参数生成 key，模拟回放命中
    from diagnostics.recording import request_key

    key = request_key("geocode", address="天津站", city="")
    recording.amap[key] = recording.amap.pop("geocode:x")
    replay = ReplayAMap(recording, budget=budget)
    hits = asyncio.run(replay.geocode("天津站"))
    assert [h.formatted_address for h in hits] == ["旧格式"]


def test_replay_deepseek_matches_prompt() -> None:
    """一次运行会多次调用 AI（候选分批），回放必须按问题匹配而不是永远拿最后一条。"""
    recording = Recording(run_id="ds")
    recording.add_deepseek(
        [{"role": "user", "content": "A"}], {"content": "{}", "data": {"summary": "a", "items": []}}, {}
    )
    recording.add_deepseek(
        [{"role": "user", "content": "B"}], {"content": "{}", "data": {"summary": "b", "items": []}}, {}
    )
    replay = ReplayDeepSeek(recording)
    first = asyncio.run(replay.chat_json([{"role": "user", "content": "A"}]))
    second = asyncio.run(replay.chat_json([{"role": "user", "content": "B"}]))
    assert first.data["summary"] == "a"
    assert second.data["summary"] == "b"
    # 未录制的请求不能套用另一批候选的响应。
    with pytest.raises(DeepSeekError):
        asyncio.run(replay.chat_json([{"role": "user", "content": "C"}]))


def test_mock_run_records_ai_and_replays_identically(tmp_path: Path) -> None:
    """--record（mock）必须把 AI 响应也录进去，否则 --replay --ai recorded 无法复现。"""
    record_dir = tmp_path / "rec"
    options = RunOptions(mock=True, record=str(record_dir))
    case = load_case(CASE)
    outcome = asyncio.run(run_search(case, options, CancelToken()))
    assert outcome.status in ("ok", "no_result")
    files = list(record_dir.glob("recording-*.json"))
    assert files, "未写出录制文件"
    recording = Recording.load(files[0])
    if outcome.result is not None and outcome.result.source == "ai":
        assert recording.deepseek, "AI 调用没有被录制"

        replay_options = RunOptions(replay=str(record_dir), offline=True, ai_mode="recorded")
        replayed = asyncio.run(run_search(case, replay_options, CancelToken()))
        assert replayed.result.source == "ai"
        assert [r.poi.id for r in replayed.result.recommendations] == [
            r.poi.id for r in outcome.result.recommendations
        ]


# --------------------------------------------------------------------------- #
# 3. UTF-8 BOM（Windows 记事本另存）不能被判成非法 JSON
# --------------------------------------------------------------------------- #
def test_case_file_with_bom_is_accepted(tmp_path: Path) -> None:
    payload = json.loads(CASE.read_text(encoding="utf-8"))
    target = tmp_path / "case_bom.json"
    target.write_text("\ufeff" + json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    case = load_case(target)
    assert case.name == payload["name"]


def test_recording_with_bom_is_accepted(tmp_path: Path) -> None:
    recording = Recording(run_id="bom")
    path = tmp_path / "recording-bom.json"
    path.write_text("\ufeff" + json.dumps(recording.to_dict(), ensure_ascii=False), encoding="utf-8")
    loaded = Recording.load(path)
    assert loaded.run_id == "bom"


# --------------------------------------------------------------------------- #
# 4. CLI：错误也要给 JSON 信封与正确退出码
# --------------------------------------------------------------------------- #
def _run_cli(capsys, *argv: str) -> tuple[int, str]:
    code = cli_main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out


def test_cli_replay_version_mismatch_returns_json(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "recording-bad.json"
    bad.write_text(
        json.dumps({"schema_version": "1.0", "data_version": 99, "amap": {}, "deepseek": []}),
        encoding="utf-8",
    )
    code, out = _run_cli(
        capsys, "search", "--config", str(CASE), "--replay", str(bad), "--json",
        "--runs-dir", str(tmp_path / "runs"),
    )
    assert code == 2
    payload = json.loads(out)  # stdout 必须是纯 JSON
    assert payload["status"] == "failed"
    assert payload["errors"][0]["code"] == "RECORDING_VERSION_MISMATCH"


def test_cli_usage_error_still_emits_json(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(["search", "--config", str(CASE), "--json", "--top-n", "99"])
    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"][0]["code"] == "USAGE_ERROR"


def test_cli_invalid_region_is_usage_error(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "case_bad.json"
    bad.write_text(
        json.dumps(
            {
                "name": "bad",
                "a": {"name": "A", "lng": 117.2, "lat": 39.13},
                "b": {"name": "B", "lng": 117.3, "lat": 39.14},
                "region": {"mode": "circle", "center": [117.2, 39.13], "radius_m": 1},
            }
        ),
        encoding="utf-8",
    )
    code, out = _run_cli(
        capsys, "search", "--config", str(bad), "--json", "--runs-dir", str(tmp_path / "runs")
    )
    assert code == 2
    assert json.loads(out)["errors"][0]["code"] == "INVALID_REGION"


def test_cli_out_write_failure_keeps_exit_code(tmp_path: Path, capsys) -> None:
    blocker = tmp_path / "outdir"
    blocker.mkdir()
    code, out = _run_cli(
        capsys, "search", "--config", str(CASE), "--mock", "--json",
        "--out", str(blocker), "--runs-dir", str(tmp_path / "runs"),
    )
    assert code == 0
    assert json.loads(out)["status"] in ("ok", "no_result")


# --------------------------------------------------------------------------- #
# 5. candidates / routes 的 --timeout 必须生效
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("runner", [run_candidates, run_routes])
def test_candidates_and_routes_honour_timeout(runner, tmp_path: Path) -> None:
    case = load_case(CASE)
    options = RunOptions(mock=True, timeout_s=0.001)
    outcome = asyncio.run(runner(case, options, CancelToken()))
    assert outcome.status == "failed"
    assert any(err["code"] == "TIMEOUT" for err in outcome.errors)


# --------------------------------------------------------------------------- #
# 6. benchmark 预算与汇总
# --------------------------------------------------------------------------- #
def test_benchmark_budget_and_counts(tmp_path: Path, capsys) -> None:
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps(
            {
                "name": "probe",
                "budget": {"max_api_calls": 5},
                "cases": [{"config": str(CASE)}, {"config": str(CASE)}],
            }
        ),
        encoding="utf-8",
    )
    code = cli_main(
        ["benchmark", "--suite", str(suite), "--json", "--runs-dir", str(tmp_path / "runs")]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["cases_total"] == 2
    indices = [row["index"] for row in payload["data"]["cases"]]
    assert indices == sorted(set(indices)), "aborted 行不能重复占用 index"
    assert payload["data"]["summary"]["executed"] == len(payload["data"]["cases"])
    assert code in (0, 1)


# --------------------------------------------------------------------------- #
# 7. 传输层重试要计入 API 预算
# --------------------------------------------------------------------------- #
def test_transport_retries_count_toward_budget() -> None:
    metrics = SimpleNamespace(calls={"amap_web": 0})

    class FakeService:
        @property
        def transport(self):
            return SimpleNamespace(metrics=metrics)

        async def place_around(self, *args, **kwargs):
            metrics.calls["amap_web"] += 3  # 1 次请求 + 2 次重试
            raise RuntimeError("boom")

    budget = Budget(max_api_calls=100, max_tokens=10**6)
    live = LiveAMap(FakeService(), budget=budget)
    with pytest.raises(RuntimeError):
        asyncio.run(live.place_around((117.2, 39.13), 3000))
    assert budget.api_calls == 3


# --------------------------------------------------------------------------- #
# 8. UI 纯逻辑
# --------------------------------------------------------------------------- #
def test_ui_treats_unknown_health_as_usable() -> None:
    class FakeMonitor:
        def __init__(self, state: ServiceState) -> None:
            self._state = state

        def state_of(self, name: str) -> ServiceState:
            return self._state

    fake = SimpleNamespace(monitor=FakeMonitor(ServiceState.UNKNOWN))
    assert MainWindow._amap_available(fake) is True
    fake = SimpleNamespace(monitor=FakeMonitor(ServiceState.OK))
    assert MainWindow._amap_available(fake) is True
    fake = SimpleNamespace(monitor=FakeMonitor(ServiceState.CONNECT_ERROR))
    assert MainWindow._amap_available(fake) is False


def test_ui_strips_exception_prefix() -> None:
    assert (
        _strip_exception_prefix("algorithms.pipeline.RecommendationError: 区域内没有候选")
        == "区域内没有候选"
    )
    assert _strip_exception_prefix("区域内没有候选") == "区域内没有候选"
    # 冒号是中文全角时不能误伤
    assert _strip_exception_prefix("推荐失败：原因") == "推荐失败：原因"
