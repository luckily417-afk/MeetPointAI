"""执行器：把 case 配置 + 数据源接到现有推荐引擎上，并产出结构化诊断数据。

GUI 与 CLI 共用同一个 `RecommendationPipeline`；这里只负责：
* 选择数据源（live / mock / replay）；
* 预算、超时、取消；
* 采集 trace、汇总统计、生成录制。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from algorithms.candidates import CancelToken, discover_pois, plan_routes
from algorithms.pipeline import (
    NO_RESULT_MESSAGE,
    RecommendRequest,
    RecommendationError,
    RecommendationPipeline,
)
from config.settings import Settings, get_settings
from diagnostics.cases import CaseConfig, CaseError
from diagnostics.recording import Recording
from diagnostics.sources import (
    Budget,
    BudgetExceeded,
    LiveAMap,
    LiveDeepSeek,
    MockAMap,
    MockDeepSeek,
    RecordingAMap,
    RecordingDeepSeek,
    ReplayAMap,
    ReplayDeepSeek,
)
from diagnostics.tracing import TraceRecorder
from models.schemas import Poi, RegionMode, TravelMode, UserPoint, format_location
from services.bootstrap import build_services
from services.errors import AmapError

__all__ = ["RunOptions", "RunOutcome", "run_search", "run_candidates", "run_routes", "new_run_id"]


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


def _dedupe_errors(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一条错误可能同时写进 trace 与 outcome，输出前按 (code, message) 去重。"""
    seen: set[tuple[Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = (item.get("code"), item.get("message"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


@dataclass
class RunOptions:
    live: bool = False
    mock: bool = False
    replay: str | None = None
    offline: bool = False
    record: str | None = None
    ai_mode: str | None = None
    max_api_calls: int | None = None
    max_tokens: int | None = None
    timeout_s: float | None = None
    top_n: int | None = None

    def data_mode(self, case: CaseConfig) -> str:
        if self.live:
            return "live"
        if self.mock:
            return "mock"
        if self.replay:
            return "replay"
        return case.data.mode


@dataclass
class RunOutcome:
    run_id: str
    case: CaseConfig
    status: str                       # ok / no_result / failed / cancelled
    data_mode: str
    elapsed_ms: int = 0
    result: Any | None = None
    trace: TraceRecorder = field(default_factory=TraceRecorder)
    warnings: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    points: dict[str, Any] = field(default_factory=dict)
    recording_path: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------ #
    def diagnostics(self) -> dict[str, Any]:
        snap = self.trace.snapshot()
        return {
            "counts": snap["counts"],
            "route_api_failed": snap["extra_counts"].get("route_api_failed", 0),
            "eliminated": snap["eliminated"],
            "eliminated_total": len(snap["eliminated"]),
            "warnings": self.warnings + snap["warnings"],
            "errors": _dedupe_errors(self.errors + snap["errors"]),
            "ai": snap["ai"],
            "search": snap["search"],
            "timings_ms": snap["timings_ms"],
            "cache": snap["cache"],
            "coverage": self._coverage(snap),
            "stats": self.stats,
            "data_mode": self.data_mode,
            "points": self.points,
            "region": self.case.region.model_dump(),
        }

    def _coverage(self, snap: dict[str, Any]) -> dict[str, Any]:
        region = self.case.region
        area_km2 = None
        radius_km = None
        try:
            reg = region.build()
            if reg is not None:
                area_km2 = reg.area_m2() / 1e6
                if region.mode == "circle" and region.radius_m:
                    radius_km = region.radius_m / 1000.0
        except Exception:  # noqa: BLE001
            pass
        metrics = TraceRecorder.coverage_metrics(
            list(snap["candidates"]),
            region_area_km2=area_km2,
            radius_km=radius_km,
            tiles=int((snap["search"] or {}).get("tiles", 0) or 0),
        )
        metrics["region_mode"] = region.mode
        return metrics


# --------------------------------------------------------------------------- #
# 内部：装配数据源与请求
# --------------------------------------------------------------------------- #
@dataclass
class _Prepared:
    outcome: RunOutcome
    amap: Any
    deepseek: Any
    budget: Budget
    recording: Recording | None
    settings: Settings
    request: RecommendRequest


def _latest_recording(path: Path) -> Path:
    if path.is_file():
        return path
    files = sorted(path.glob("recording-*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise CaseError("REPLAY_REQUIRED", f"目录中没有录制文件：{path}")
    return files[-1]


def _make_sources(case: CaseConfig, options: RunOptions) -> tuple[Any, Any, Budget, Recording | None, str]:
    mode = options.data_mode(case)
    if options.offline and mode == "live":
        raise CaseError("OFFLINE_CONFLICT", "--offline 禁止真实 API；请改用 --replay 或 --mock")
    if options.live and options.mock:
        raise CaseError("USAGE_CONFLICT", "--live 与 --mock 不能同时使用")

    budget = Budget(
        max_api_calls=options.max_api_calls if options.max_api_calls is not None else case.budget.max_api_calls,
        max_tokens=options.max_tokens if options.max_tokens is not None else case.budget.max_tokens,
    )

    if mode == "live":
        settings = get_settings()
        if not settings.amap_web_configured:
            raise CaseError("AMAP_KEY_MISSING", "未配置高德 Web 服务 Key，无法使用 --live")
        _transport, amap_service, deepseek_service, _monitor = build_services(settings, None)  # type: ignore[arg-type]
        recording = Recording(case=case.describe()) if options.record else None
        amap = LiveAMap(amap_service, budget=budget, recording=recording)
        ai_mode = options.ai_mode or case.data.ai or "live"
        if ai_mode == "mock":
            deepseek: Any = MockDeepSeek(budget=budget)
            if recording is not None:
                deepseek = RecordingDeepSeek(deepseek, recording)
        elif ai_mode == "recorded":
            raise CaseError("AI_MODE_CONFLICT", "live 模式下不能使用 recorded AI")
        else:
            if not settings.deepseek_configured:
                raise CaseError("DEEPSEEK_KEY_MISSING", "未配置 DeepSeek Key，无法调用 live AI")
            deepseek = LiveDeepSeek(deepseek_service, budget=budget, recording=recording)
        return amap, deepseek, budget, recording, mode

    if mode == "mock":
        amap: Any = MockAMap(budget=budget, seed_text=case.name)
        deepseek: Any = MockDeepSeek(budget=budget)
        if options.record:
            recording = Recording(case=case.describe())
            amap = RecordingAMap(amap, recording)
            # AI 响应也要录制，否则 --replay --ai recorded 无法复现本次结果
            deepseek = RecordingDeepSeek(deepseek, recording)
            return amap, deepseek, budget, recording, mode
        return amap, deepseek, budget, None, mode

    replay_path = options.replay or case.data.replay
    if not replay_path:
        raise CaseError("REPLAY_REQUIRED", "replay 模式需要 --replay <文件或目录>，或在 case.data.replay 中指定")
    recording = Recording.load(_latest_recording(Path(replay_path)))
    amap = ReplayAMap(recording, budget=budget)
    ai_mode = options.ai_mode or case.data.ai or "recorded"
    if ai_mode == "mock":
        deepseek: Any = MockDeepSeek(budget=budget)
    elif ai_mode == "live":
        if options.offline:
            raise CaseError("OFFLINE_CONFLICT", "--offline 下不能重新调用 DeepSeek")
        settings = get_settings()
        if not settings.deepseek_configured:
            raise CaseError("DEEPSEEK_KEY_MISSING", "未配置 DeepSeek Key，无法重新调用")
        _t, _a, deepseek_service, _m = build_services(settings, None)  # type: ignore[arg-type]
        deepseek = LiveDeepSeek(deepseek_service, budget=budget)
    else:
        deepseek = ReplayDeepSeek(recording)
    return amap, deepseek, budget, None, mode


async def _resolve_point(spec, amap: Any, *, label: str) -> UserPoint:  # noqa: ANN001
    if spec.has_coords:
        return UserPoint(
            lng=float(spec.lng),
            lat=float(spec.lat),
            name=spec.name or format_location(float(spec.lng), float(spec.lat)),
            address=spec.address,
            adcode=spec.adcode,
            city=spec.city,
            source="case",
        )
    if not spec.name:
        raise CaseError("CASE_POINT_MISSING", f"{label} 既没有坐标也没有名称")
    page = await amap.place_text(spec.name, offset=5)
    if page.pois:
        poi = page.pois[0]
        return UserPoint(
            lng=poi.lng,
            lat=poi.lat,
            name=poi.name,
            address=poi.address,
            adcode=poi.adcode,
            city=poi.cityname,
            source="case",
        )
    hits = await amap.geocode(spec.name)
    if hits:
        hit = hits[0]
        return UserPoint(
            lng=hit.lng,
            lat=hit.lat,
            name=spec.name,
            address=hit.formatted_address,
            adcode=hit.adcode,
            city=hit.city,
            source="case",
        )
    raise CaseError(
        "CASE_POINT_NOT_FOUND",
        f"无法解析 {label} 的位置：{spec.name}（replay/mock 模式请直接给出 lng/lat）",
    )


def _settings_for(case: CaseConfig, options: RunOptions) -> Settings:
    settings = get_settings()
    top_n = options.top_n or case.top_n
    return settings.with_overrides(top_n=top_n)


async def _prepare(case: CaseConfig, options: RunOptions, prefix: str) -> _Prepared:
    amap, deepseek, budget, recording, mode = _make_sources(case, options)
    settings = _settings_for(case, options)
    outcome = RunOutcome(run_id=new_run_id(prefix), case=case, status="ok", data_mode=mode)
    a = await _resolve_point(case.a, amap, label="A")
    b = await _resolve_point(case.b, amap, label="B")
    city_a = case.city_a or a.adcode
    city_b = case.city_b or b.adcode
    if mode == "live" and case.travel_mode is TravelMode.TRANSIT:
        # Resolve each origin once.  Letting every concurrent route call reverse
        # geocode the same point can hit AMap's rate limit and falsely mark POIs
        # unreachable when the case supplies coordinates without an adcode.
        try:
            if not city_a or not str(city_a).isdigit():
                info = await amap.regeocode(a.lng, a.lat)
                city_a = info.get("citycode") or info.get("adcode")
            if not city_b or not str(city_b).isdigit():
                info = await amap.regeocode(b.lng, b.lat)
                city_b = info.get("citycode") or info.get("adcode")
        except AmapError as exc:
            raise CaseError("AMAP_CITY_LOOKUP_FAILED", f"查询出发城市失败：{exc.message}") from exc
        if not city_a or not city_b:
            raise CaseError("CASE_CITY_NOT_FOUND", "无法确定 A/B 出发城市，公共交通路线无法规划")
    outcome.points = {"a": a.model_dump(mode="json"), "b": b.model_dump(mode="json")}
    region = case.region.build() if case.region.mode != "none" else None
    request = RecommendRequest(
        point_a=a,
        point_b=b,
        region=region,
        region_mode=RegionMode(case.region.mode),
        travel_mode=case.travel_mode,
        category=case.category,
        keyword=case.keyword,
        rec_mode=case.rec_mode,
        custom_prefs=case.prefs,
        top_n=settings.top_n,
        city_a=city_a,
        city_b=city_b,
    )
    return _Prepared(outcome, amap, deepseek, budget, recording, settings, request)


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #

def _budget_warning(outcome: RunOutcome, budget: Budget) -> None:
    ratio = budget.usage_ratio()
    if ratio >= 0.8:
        outcome.trace.warn(
            "BUDGET_NEAR_LIMIT",
            f"API 调用已使用 {budget.api_calls}/{budget.max_api_calls}（{ratio:.0%}），后续阶段可能被中断",
        )


# --------------------------------------------------------------------------- #
# 超时与录制：所有命令共用（准备阶段也算在超时里）
# --------------------------------------------------------------------------- #
def _timeout_of(case: CaseConfig, options: RunOptions) -> float:
    return float(options.timeout_s or case.budget.timeout_s or 0.0)


def _timeout_outcome(case: CaseConfig, options: RunOptions, prefix: str, started: float, timeout_s: float) -> RunOutcome:
    outcome = RunOutcome(
        run_id=new_run_id(prefix),
        case=case,
        status="failed",
        data_mode=options.data_mode(case),
    )
    message = f"执行超过 {timeout_s:g} 秒被取消（含准备阶段）"
    outcome.errors.append({"code": "TIMEOUT", "message": message})
    outcome.trace.error("TIMEOUT", message)
    outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
    outcome.stats = {"timeout_s": timeout_s}
    return outcome


async def _run_with_timeout(
    coro,  # noqa: ANN001
    *,
    case: CaseConfig,
    options: RunOptions,
    prefix: str,
    started: float,
):
    timeout_s = _timeout_of(case, options)
    try:
        if timeout_s:
            return await asyncio.wait_for(coro, timeout_s)
        return await coro
    except TimeoutError:
        return _timeout_outcome(case, options, prefix, started, timeout_s)


def _save_recording(outcome: RunOutcome, recording: Recording | None, options: RunOptions) -> None:
    if recording is None or not options.record:
        return
    recording.run_id = outcome.run_id
    recording.timings_ms = dict(outcome.trace.timings_ms)
    outcome.recording_path = str(recording.save(options.record))


async def run_candidates(case: CaseConfig, options: RunOptions, cancel: CancelToken | None = None) -> RunOutcome:
    started = time.perf_counter()
    return await _run_with_timeout(
        _run_candidates(case, options, cancel, started),
        case=case, options=options, prefix="cand", started=started,
    )


async def _run_candidates(
    case: CaseConfig, options: RunOptions, cancel: CancelToken | None, started: float
) -> RunOutcome:
    prepared = await _prepare(case, options, "cand")
    outcome, amap, budget, settings, req = (
        prepared.outcome, prepared.amap, prepared.budget, prepared.settings, prepared.request
    )
    trace = outcome.trace
    trace.event("candidates.start", region=req.region.describe() if req.region else "不限制区域")

    try:
        pois, stats = await discover_pois(
            amap,
            req.region,
            point_a=req.point_a,
            point_b=req.point_b,
            category=req.category,
            keyword=req.keyword,
            page_size=settings.poi_page_size,
            max_pages=settings.poi_max_pages,
            max_candidates=settings.max_candidates,
            target_km=settings.tile_target_km,
            max_tiles=settings.max_tiles,
            default_radius_m=settings.search_radius_default_m,
            cancel=cancel,
            trace=trace,
        )
    except BudgetExceeded as exc:
        outcome.status = "failed"
        outcome.errors.append({"code": exc.code, "message": str(exc)})
        outcome.stats = budget.snapshot()
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    outcome.trace.set_search(
        tiles=stats.tiles,
        api_calls=stats.api_calls,
        raw_pois=stats.raw_pois,
        unique_pois=stats.unique_pois,
        in_region=stats.in_region,
        budget_hit=stats.budget_hit,
        truncated=stats.truncated,
        center=list(stats.center),
        radius_m=stats.radius_m,
    )
    if stats.budget_hit:
        trace.warn("SEARCH_BUDGET_HIT", "检索达到调用预算上限，可能有分块未执行")
    if stats.truncated:
        trace.warn("CANDIDATES_TRUNCATED", f"区域内 {stats.in_region} 个候选只保留 {len(pois)} 个进入后续流程")
    if stats.in_region and stats.in_region < 10:
        trace.warn("LOW_COVERAGE", f"区域内仅 {stats.in_region} 个候选，覆盖可能不足")
    for note in stats.notes:
        trace.warn("SEARCH_TILE_NOTE", note)

    outcome.result = {
        "search": {
            "tiles": stats.tiles,
            "api_calls": stats.api_calls,
            "raw_pois": stats.raw_pois,
            "unique_pois": stats.unique_pois,
            "in_region": stats.in_region,
            "returned": len(pois),
            "budget_hit": stats.budget_hit,
            "truncated": stats.truncated,
            "center": list(stats.center),
            "radius_m": stats.radius_m,
            "notes": stats.notes,
        },
        "pois": [
            {
                "poi_id": p.id,
                "name": p.name,
                "lng": p.lng,
                "lat": p.lat,
                "type": p.type_name,
                "address": p.address,
            }
            for p in pois
        ],
    }
    if not pois:
        outcome.status = "no_result"
        trace.error("NO_CANDIDATES", NO_RESULT_MESSAGE)
    outcome.stats = budget.snapshot()
    _budget_warning(outcome, budget)
    outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
    trace.timing("search", outcome.elapsed_ms / 1000)
    trace.timing("total", outcome.elapsed_ms / 1000)
    _save_recording(outcome, prepared.recording, options)
    return outcome


async def run_routes(case: CaseConfig, options: RunOptions, cancel: CancelToken | None = None) -> RunOutcome:
    started = time.perf_counter()
    return await _run_with_timeout(
        _run_routes(case, options, cancel, started),
        case=case, options=options, prefix="routes", started=started,
    )


async def _run_routes(
    case: CaseConfig, options: RunOptions, cancel: CancelToken | None, started: float
) -> RunOutcome:
    prepared = await _prepare(case, options, "routes")
    outcome, amap, budget, settings, req = (
        prepared.outcome, prepared.amap, prepared.budget, prepared.settings, prepared.request
    )
    trace = outcome.trace

    try:
        pois, stats = await discover_pois(
            amap,
            req.region,
            point_a=req.point_a,
            point_b=req.point_b,
            category=req.category,
            keyword=req.keyword,
            page_size=settings.poi_page_size,
            max_pages=settings.poi_max_pages,
            max_candidates=settings.max_candidates,
            target_km=settings.tile_target_km,
            max_tiles=settings.max_tiles,
            default_radius_m=settings.search_radius_default_m,
            cancel=cancel,
            trace=trace,
        )
        trace.count("raw", stats.raw_pois)
        trace.count("deduped", stats.unique_pois)
        trace.count("in_region", stats.in_region)
        trace.set_search(
            tiles=stats.tiles, api_calls=stats.api_calls, raw_pois=stats.raw_pois,
            unique_pois=stats.unique_pois, in_region=stats.in_region,
            budget_hit=stats.budget_hit, truncated=stats.truncated,
            center=list(stats.center), radius_m=stats.radius_m,
        )
        if not pois:
            outcome.status = "no_result"
            trace.error("NO_CANDIDATES", NO_RESULT_MESSAGE)
            outcome.stats = budget.snapshot()
            outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        candidates = await plan_routes(
            amap,
            pois,
            point_a=req.point_a,
            point_b=req.point_b,
            mode=req.travel_mode,
            concurrency=settings.max_concurrency,
            city_a=req.city_a,
            city_b=req.city_b,
            cancel=cancel,
            trace=trace,
        )
    except BudgetExceeded as exc:
        outcome.status = "failed"
        outcome.errors.append({"code": exc.code, "message": str(exc)})
        outcome.stats = budget.snapshot()
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    reachable = [c for c in candidates if c.routes_ok]
    trace.count("routed", len(reachable))
    outcome.result = {
        "search": {
            "tiles": stats.tiles,
            "api_calls": stats.api_calls,
            "raw_pois": stats.raw_pois,
            "unique_pois": stats.unique_pois,
            "in_region": stats.in_region,
            "returned": len(pois),
            "truncated": stats.truncated,
        },
        "summary": {
            "candidates": len(candidates),
            "reachable": len(reachable),
            "unreachable": len(candidates) - len(reachable),
            "api_failed": trace.extras.get("custom_counts", {}).get("route_api_failed", 0),
            "travel_mode": req.travel_mode.value,
        },
        "routes": [
            {
                "poi_id": c.id,
                "name": c.poi.name,
                "reachable": c.routes_ok,
                "leg_a": trace.candidates.get(c.id, {}).get("leg_a"),
                "leg_b": trace.candidates.get(c.id, {}).get("leg_b"),
            }
            for c in candidates
        ],
    }
    if not reachable:
        outcome.status = "no_result"
        trace.warn(
            "NO_REACHABLE",
            "所有候选的 A/B 路线都不可达：属于业务无结果，不应判定为 API 断供",
        )
    outcome.stats = budget.snapshot()
    _budget_warning(outcome, budget)
    outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
    trace.timing("total", outcome.elapsed_ms / 1000)
    _save_recording(outcome, prepared.recording, options)
    return outcome


async def run_search(case: CaseConfig, options: RunOptions, cancel: CancelToken | None = None) -> RunOutcome:
    started = time.perf_counter()
    return await _run_with_timeout(
        _run_search(case, options, cancel, started),
        case=case, options=options, prefix="run", started=started,
    )


async def _run_search(
    case: CaseConfig, options: RunOptions, cancel: CancelToken | None, started: float
) -> RunOutcome:
    prepared = await _prepare(case, options, "run")
    outcome, amap, deepseek, budget, recording, settings, req = (
        prepared.outcome, prepared.amap, prepared.deepseek, prepared.budget,
        prepared.recording, prepared.settings, prepared.request,
    )
    trace = outcome.trace
    pipeline = RecommendationPipeline(amap, deepseek, settings, monitor=None, db=None)

    try:
        result = await pipeline.run(req, cancel=cancel, trace=trace)
    except RecommendationError as exc:
        message = str(exc)
        is_no_result = message == NO_RESULT_MESSAGE
        outcome.status = "no_result" if is_no_result else "failed"
        code = "NO_RESULT" if is_no_result else "ENGINE_ERROR"
        outcome.errors.append({"code": code, "message": message})
        trace.error(code, message)
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        outcome.stats = budget.snapshot()
        return outcome
    except BudgetExceeded as exc:
        outcome.status = "failed"
        outcome.errors.append({"code": exc.code, "message": str(exc)})
        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        outcome.stats = budget.snapshot()
        return outcome

    outcome.result = result
    outcome.stats = budget.snapshot()
    _budget_warning(outcome, budget)
    outcome.stats["engine"] = result.stats
    outcome.stats["cache"] = trace.cache
    outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
    trace.timing("total", outcome.elapsed_ms / 1000)
    if not result.recommendations:
        outcome.status = "no_result"

    _save_recording(outcome, recording, options)
    return outcome
