"""推荐主流程编排（需求 4.3 的 9 步执行顺序）。

1. 用户绘制允许区域（由 GUI 提供 Region）
2. 程序获取区域几何数据
3. 高德搜索区域内/覆盖区域附近的真实 POI
4. Python 精确几何过滤
5. 对通过过滤的候选规划 A/B 真实路线
6. 仅把通过验证的候选交给 DeepSeek
7. DeepSeek 只返回候选 ID
8. Python 再次验证 AI 返回地点是否在允许区域内
9. 验证成功才产出推荐结果
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from algorithms.ai_validator import (
    AIValidationError,
    build_ai_messages,
    candidate_payload,
    validate_ai_payload,
)
from algorithms.candidates import CancelToken, SearchStats, discover_pois, plan_routes
from algorithms.ranking import RankContext, rank_candidates, to_recommendations
from config.constants import CATEGORY_PRESETS, REC_MODE_LABELS, REGION_MODE_LABELS, STATE_LABELS, TRAVEL_MODE_LABELS
from config.settings import Settings
from geometry.regions import Region
from models.schemas import (
    Candidate,
    Recommendation,
    RecMode,
    RegionMode,
    ServiceState,
    TravelMode,
    UserPoint,
)
from monitoring.health import HealthMonitor
from services.amap_web import AMapWebService
from services.deepseek import DeepSeekService
from services.errors import DeepSeekError, ServiceError
from storage.db import Database

__all__ = ["RecommendRequest", "RecommendResult", "RecommendationPipeline", "RecommendationError"]

ProgressCb = Callable[[int, str], None]

NO_RESULT_MESSAGE = "当前指定区域内未找到符合条件的见面地点，请扩大搜索范围或调整场所偏好。"
AI_FALLBACK_NOTICE = "AI 服务不可用，当前为基础推荐模式。"
AI_INVALID_NOTICE = "AI 返回的推荐未通过校验，已切换到基础推荐模式。"


class RecommendationError(RuntimeError):
    def __init__(self, message: str, *, state: ServiceState | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.state = state
        self.detail = detail


@dataclass
class RecommendRequest:
    point_a: UserPoint
    point_b: UserPoint
    region: Region | None = None
    region_mode: RegionMode = RegionMode.NONE
    travel_mode: TravelMode = TravelMode.TRANSIT
    category: str = "任意类型"
    keyword: str = ""
    rec_mode: RecMode = RecMode.AI
    custom_prefs: str = ""
    top_n: int = 5
    city_a: str | None = None
    city_b: str | None = None


@dataclass
class RecommendResult:
    recommendations: list[Recommendation] = field(default_factory=list)
    ai_summary: str = ""
    source: str = "rule"
    notice: str = ""
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    candidates_considered: int = 0
    elapsed_s: float = 0.0
    region_desc: str = ""
    request_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.recommendations


class RecommendationPipeline:
    def __init__(
        self,
        amap: AMapWebService,
        deepseek: DeepSeekService | None,
        settings: Settings,
        monitor: HealthMonitor | None = None,
        db: Database | None = None,
    ) -> None:
        self.amap = amap
        self.deepseek = deepseek
        self.settings = settings
        self.monitor = monitor
        self.db = db

    # ------------------------------------------------------------------ #
    async def run(
        self,
        req: RecommendRequest,
        *,
        progress: ProgressCb | None = None,
        cancel: CancelToken | None = None,
        trace: Any | None = None,
    ) -> RecommendResult:
        started = time.perf_counter()
        result = RecommendResult()
        result.request_snapshot = self._snapshot(req)
        result.region_desc = req.region.describe() if req.region else "不限制区域"

        def report(pct: int, message: str) -> None:
            if progress:
                progress(max(0, min(100, pct)), message)

        if trace is not None:
            trace.event("run.start", region=result.region_desc)

        report(2, "校验起点与区域约束…")
        self._validate_request(req)

        # ---- 步骤 3：真实 POI 检索（分块/分页/去重） ----
        report(6, "正在请求高德 POI 检索…")
        t_search = time.perf_counter()
        try:
            pois, stats = await discover_pois(
                self.amap,
                req.region,
                point_a=req.point_a,
                point_b=req.point_b,
                category=req.category,
                keyword=req.keyword,
                page_size=self.settings.poi_page_size,
                max_pages=self.settings.poi_max_pages,
                max_candidates=self.settings.max_candidates,
                target_km=self.settings.tile_target_km,
                max_tiles=self.settings.max_tiles,
                default_radius_m=self.settings.search_radius_default_m,
                progress=lambda done, total, msg: report(10 + int(35 * done / max(1, total)), msg),
                cancel=cancel,
                trace=trace,
            )
            self._note_amap(ok=True)
        except ServiceError as exc:
            self._note_amap(ok=False, exc=exc)
            if trace is not None:
                trace.error("AMAP_UNAVAILABLE", exc.message, exc.detail)
            raise RecommendationError(
                f"高德地图数据服务不可用：{exc.message}", state=exc.state, detail=exc.detail
            ) from exc

        if trace is not None:
            trace.timing("search", time.perf_counter() - t_search)
        if cancel:
            cancel.raise_if_cancelled()
        result.stats = stats.as_dict()
        if trace is not None:
            trace.count("raw", stats.raw_pois)
            trace.count("deduped", stats.unique_pois)
            trace.count("in_region", stats.in_region)
            trace.set_search(
                tiles=stats.tiles,
                api_calls=stats.api_calls,
                raw_pois=stats.raw_pois,
                unique_pois=stats.unique_pois,
                in_region=stats.in_region,
                budget_hit=stats.budget_hit,
                truncated=stats.truncated,
                center=list(stats.center),
                radius_m=stats.radius_m,
                region=result.region_desc,
            )
            if stats.budget_hit:
                trace.warn("SEARCH_BUDGET_HIT", "检索达到调用预算上限，可能有分块/分页未执行")
            if stats.truncated:
                trace.warn(
                    "CANDIDATES_TRUNCATED",
                    f"候选池被截断：区域内 {stats.in_region} 个候选只保留了 {len(pois)} 个进入路线规划",
                )
            if stats.in_region and stats.in_region < 10:
                trace.warn("LOW_COVERAGE", f"区域内只检索到 {stats.in_region} 个候选，覆盖可能不足")
            for note in stats.notes:
                trace.warn("SEARCH_TILE_NOTE", note)
        if not pois:
            if trace is not None:
                trace.error("NO_CANDIDATES", NO_RESULT_MESSAGE)
            raise RecommendationError(NO_RESULT_MESSAGE)

        # ---- 步骤 5：真实路径规划 ----
        report(48, f"对 {len(pois)} 个候选做 A/B 路线规划…")
        t_route = time.perf_counter()
        try:
            candidates = await plan_routes(
                self.amap,
                pois,
                point_a=req.point_a,
                point_b=req.point_b,
                mode=req.travel_mode,
                concurrency=self.settings.max_concurrency,
                progress=lambda done, total, msg: report(50 + int(28 * done / max(1, total)), msg),
                cancel=cancel,
                city_a=req.city_a,
                city_b=req.city_b,
                trace=trace,
            )
            self._note_amap(ok=True)
        except ServiceError as exc:
            self._note_amap(ok=False, exc=exc)
            if trace is not None:
                trace.error("ROUTE_UNAVAILABLE", exc.message)
            raise RecommendationError(
                f"路线规划失败：{exc.message}", state=exc.state
            ) from exc
        if trace is not None:
            trace.timing("routes", time.perf_counter() - t_route)

        candidates = [c for c in candidates if c.leg_a or c.leg_b]
        reachable = [c for c in candidates if c.routes_ok]
        stats.routed = len(reachable)
        stats.route_failures = len(candidates) - len(reachable)
        result.stats = stats.as_dict()
        result.candidates_considered = len(candidates)
        if trace is not None:
            trace.count("routed", len(reachable))
        if not reachable:
            if trace is not None:
                trace.error("NO_REACHABLE", "区域内候选均无法规划出双方可达路线")
            raise RecommendationError(
                "区域内候选地点均无法规划出双方可达路线，请调整出行方式或扩大范围。"
            )

        # ---- 步骤 6~9：AI 排序 + 二次校验，失败降级 ----
        report(80, "正在请求 DeepSeek 进行综合比较…")
        t_ai = time.perf_counter()
        ordered, summary, source, notice = await self._rank(
            req, reachable, cancel=cancel, trace=trace
        )
        if trace is not None:
            trace.timing("ai", time.perf_counter() - t_ai)
        report(94, "校验最终结果…")

        # 最终兜底：再验证一次区域硬约束
        if req.region is not None:
            kept = [c for c in ordered if req.region.contains(c.poi.lng, c.poi.lat)]
            if len(kept) != len(ordered):
                result.warnings.append("已剔除超出允许区域的推荐结果")
                if trace is not None:
                    kept_ids = {c.id for c in kept}
                    for cand in ordered:
                        if cand.id not in kept_ids:
                            trace.eliminate_candidate(cand, "final", "最终区域复验失败")
            ordered = kept
        if not ordered:
            if trace is not None:
                trace.error("NO_FINAL_RESULT", NO_RESULT_MESSAGE)
            raise RecommendationError(NO_RESULT_MESSAGE)

        result.recommendations = to_recommendations(ordered[: req.top_n], source=source)
        result.ai_summary = summary
        result.source = source
        result.notice = notice
        result.elapsed_s = round(time.perf_counter() - started, 2)
        if trace is not None:
            trace.count("final", len(result.recommendations))
            trace.timing("total", time.perf_counter() - started)
            if notice:
                trace.warn("DEGRADED_MODE", notice)
            for rec in result.recommendations:
                entry = trace.candidates.get(rec.poi.id)
                if entry is not None:
                    entry["selected"] = True
                    entry["rank"] = rec.rank
            trace.event(
                "run.end",
                source=source,
                recommendations=len(result.recommendations),
                elapsed_s=result.elapsed_s,
            )
        report(100, "完成")
        self._save_session(req, result)
        return result

    # ------------------------------------------------------------------ #
    def _validate_request(self, req: RecommendRequest) -> None:
        if req.point_a is None or req.point_b is None:
            raise RecommendationError("请先设置 A、B 两人的出发位置")
        if abs(req.point_a.lng - req.point_b.lng) < 1e-9 and abs(req.point_a.lat - req.point_b.lat) < 1e-9:
            raise RecommendationError("A、B 位置相同，请设置不同的出发点")
        if req.region_mode is RegionMode.CIRCLE and req.region is None:
            raise RecommendationError("已选择圆形区域但尚未设置圆心/半径")
        if req.region_mode is RegionMode.POLYGON and req.region is None:
            raise RecommendationError("已选择多边形区域但尚未完成绘制")
        if req.rec_mode is RecMode.CUSTOM and not (req.custom_prefs or "").strip():
            raise RecommendationError("自定义需求模式需要填写自然语言偏好")

    async def _rank(
        self,
        req: RecommendRequest,
        candidates: list[Candidate],
        *,
        cancel: CancelToken | None = None,
        trace: Any | None = None,
    ) -> tuple[list[Candidate], str, str, str]:
        ctx = RankContext(rec_mode=req.rec_mode, category=req.category, travel_mode=req.travel_mode)
        rule_ordered = rank_candidates(candidates, ctx, top_n=req.top_n)
        if trace is not None:
            trace.count("preselected", len(candidates))

        use_ai = req.rec_mode in (RecMode.AI, RecMode.CUSTOM)
        if trace is not None:
            trace.set_ai(enabled=use_ai, rec_mode=req.rec_mode.value)
        if not use_ai:
            return rule_ordered, "", "rule", ""

        if self.deepseek is None or not self.deepseek.configured:
            if trace is not None:
                trace.warn("AI_NOT_CONFIGURED", "未配置 DeepSeek，使用规则排序")
                trace.set_ai(used=False, reason="not_configured")
            return rule_ordered, "", "rule", AI_FALLBACK_NOTICE
        if self.monitor is not None and not self.monitor.usable("deepseek"):
            state = self.monitor.state_of("deepseek")
            if state is not ServiceState.UNKNOWN:
                snap = self.monitor.snapshot("deepseek")
                reason = STATE_LABELS.get(state.value, state.value)
                if snap.detail:
                    reason = f"{reason}：{snap.detail}"
                if trace is not None:
                    trace.warn("AI_UNHEALTHY", reason)
                    trace.set_ai(used=False, reason=reason)
                return rule_ordered, "", "rule", f"{AI_FALLBACK_NOTICE}（{reason}）"

        ai_limit = max(req.top_n, self.settings.ai_candidate_limit)
        if len(candidates) > ai_limit:
            # 上限约束单次模型调用，不再按通勤时间丢弃整个候选池。
            # 每批先产生入围地点，再让模型综合比较所有批次的入围者。
            ordered_by_id = sorted(candidates, key=lambda c: c.id)
            batch_top_n = min(req.top_n, max(1, ai_limit // 2))
            batch_req = replace(req, top_n=batch_top_n)
            finalists: list[Candidate] = []
            batch_notices: list[str] = []
            for start in range(0, len(ordered_by_id), ai_limit):
                batch = ordered_by_id[start:start + ai_limit]
                selected, _, batch_source, batch_notice = await self._rank(
                    batch_req, batch, cancel=cancel, trace=None
                )
                if batch_source != "ai":
                    return rule_ordered, "", "rule", batch_notice
                finalists.extend(selected)
                if batch_notice:
                    batch_notices.append(batch_notice)
            ordered, summary, source, notice = await self._rank(
                req, finalists, cancel=cancel, trace=trace
            )
            if trace is not None:
                trace.count("ai_input", len(candidates))
                trace.set_ai(screened_count=len(candidates), batches=(len(candidates) + ai_limit - 1) // ai_limit)
            if batch_notices and source == "ai":
                notice = "部分批次 AI 返回地点较少；" + (notice or f"最终展示 {len(ordered)} 个地点。")
            return ordered, summary, source, notice

        ai_pool = list(candidates)
        if trace is not None:
            trace.count("ai_input", len(ai_pool))
            if len(ai_pool) < len(candidates):
                trace.warn(
                    "AI_POOL_TRUNCATED",
                    f"双方可达的 {len(candidates)} 个候选中，只有 {len(ai_pool)} 个送入 AI 比较",
                )
            trace.set_ai(
                used=True,
                pool_ids=[c.id for c in ai_pool],
                model=getattr(self.deepseek, "model", None),
            )
        payload = candidate_payload(
            ai_pool,
            point_a={"name": req.point_a.display, "source": req.point_a.source},
            point_b={"name": req.point_b.display, "source": req.point_b.source},
            region_desc=req.region.describe() if req.region else "不限制区域",
            preferences={
                "travel_mode": TRAVEL_MODE_LABELS.get(req.travel_mode.value, req.travel_mode.value),
                "category": req.category,
                "category_hint": CATEGORY_PRESETS.get(req.category, {}).get("keywords", ""),
                "natural_language": (req.custom_prefs or "").strip()[:300],
                "rec_mode": REC_MODE_LABELS.get(req.rec_mode.value, req.rec_mode.value),
                "region_mode": REGION_MODE_LABELS.get(req.region_mode.value, req.region_mode.value),
            },
            top_n=req.top_n,
        )
        cache_key = "ai:" + hashlib.sha1(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        last_error = ""
        for attempt in range(3):
            if cancel:
                cancel.raise_if_cancelled()
            messages = build_ai_messages(
                payload,
                retry_hint=last_error or None,
                strict=attempt > 0,
            )
            started = time.perf_counter()
            try:
                llm = await self.deepseek.chat_json(
                    messages,
                    cache_key=cache_key if attempt == 0 else None,
                    max_tokens=8000,
                )
                self._note_deepseek(ok=True, latency_ms=llm.latency_ms)
            except DeepSeekError as exc:
                self._note_deepseek(ok=False, exc=exc)
                if trace is not None:
                    trace.error("AI_ERROR", exc.message, detail=exc.detail)
                    trace.set_ai(used=False, reason=exc.message)
                    trace.warn("AI_FALLBACK", f"DeepSeek 调用失败，改用规则排序：{exc.message}")
                return rule_ordered, "", "rule", f"{AI_FALLBACK_NOTICE}（{exc.message}）"
            except Exception as exc:  # noqa: BLE001
                self._note_deepseek(ok=False, exc=exc)
                if trace is not None:
                    trace.error("AI_ERROR", f"{type(exc).__name__}: {str(exc)[:120]}")
                    trace.set_ai(used=False, reason=type(exc).__name__)
                    trace.warn("AI_FALLBACK", "DeepSeek 调用异常，改用规则排序")
                return (
                    rule_ordered,
                    "",
                    "rule",
                    f"{AI_FALLBACK_NOTICE}（{type(exc).__name__}: {str(exc)[:120]}）",
                )

            validation = validate_ai_payload(
                llm.data, candidates, top_n=req.top_n, region=req.region
            )
            if trace is not None:
                trace.count("ai_output", len(validation.items))
                trace.set_ai(
                    used=True,
                    attempt=attempt + 1,
                    output_count=len(validation.items),
                    validation_errors=list(validation.errors),
                    dropped=list(validation.dropped),
                    latency_ms=llm.latency_ms,
                )
                for poi_id in validation.dropped:
                    trace.eliminate(poi_id, "ai_validate", "AI 输出未通过校验（不存在/区域外/非法坐标断言）")
                if validation.errors:
                    trace.warn("AI_VALIDATION", "；".join(validation.errors)[:300])
            if validation.ok:
                by_id = {c.id: c for c in candidates}
                chosen: list[Candidate] = []
                for pos, item in enumerate(validation.items, start=1):
                    cand = by_id[item.poi_id]
                    cand.ai_rank = pos
                    cand.reason = item.reason
                    cand.tags = list(item.highlights)[:5]
                    chosen.append(cand)
                if chosen:
                    expected = min(req.top_n, len(ai_pool))
                    if len(chosen) < expected:
                        last_error = (
                            f"本次要求推荐 {expected} 个，实际仅返回 {len(chosen)} 个。"
                            "请重新检查候选；若确实没有更多符合偏好的地点，可保持较少数量，"
                            "但整体说明必须准确写明实际数量。"
                        )
                        if attempt < 2:
                            continue
                        notice = f"AI 仅返回 {len(chosen)} 个地点，少于请求的 {expected} 个；已按实际数量显示。"
                        if trace is not None:
                            trace.warn("AI_SHORT_RESULT", notice)
                            trace.set_ai(valid_count=len(chosen))
                        return chosen, notice, "ai", notice
                    if trace is not None:
                        trace.set_ai(valid_count=len(chosen))
                    return chosen, validation.summary, "ai", ""
            last_error = validation.error_text or "AI 输出未通过校验"

        if self.monitor is not None:
            self._note_deepseek(ok=False, exc=DeepSeekError(last_error, state=ServiceState.SERVICE_ERROR))
        if trace is not None:
            trace.warn("AI_INVALID", f"AI 输出多次未通过校验，已降级：{last_error[:200]}")
            trace.set_ai(used=False, reason="invalid_output")
        return rule_ordered, "", "rule", AI_INVALID_NOTICE

    # ------------------------------------------------------------------ #
    def _note_amap(self, *, ok: bool, exc: BaseException | None = None) -> None:
        if self.monitor is None:
            return
        transport = getattr(self.amap, "_transport", None)
        latency = 0
        if transport is not None:
            latency = int(getattr(transport, "metrics", None).last_latency_ms.get("amap_web", 0)) if getattr(transport, "metrics", None) else 0
        if ok:
            self.monitor.note_success("amap_web", latency)
            return
        if isinstance(exc, ServiceError):
            self.monitor.note_failure(
                "amap_web",
                exc.state,
                latency_ms=latency,
                http_status=exc.http_status,
                biz_code=exc.biz_code,
                error_type=type(exc).__name__,
                detail=exc.message,
            )

    def _note_deepseek(self, *, ok: bool, latency_ms: int = 0, exc: BaseException | None = None) -> None:
        if self.monitor is None:
            return
        if ok:
            self.monitor.note_success("deepseek", latency_ms)
            return
        if isinstance(exc, ServiceError):
            self.monitor.note_failure(
                "deepseek",
                exc.state,
                latency_ms=latency_ms or None,
                http_status=exc.http_status,
                biz_code=exc.biz_code,
                error_type=type(exc).__name__,
                detail=exc.message,
            )

    # ------------------------------------------------------------------ #
    def _save_session(self, req: RecommendRequest, result: RecommendResult) -> None:
        if self.db is None:
            return
        try:
            self.db.save_session(
                title=f"{req.point_a.display} ↔ {req.point_b.display}",
                request={
                    "a": req.point_a.model_dump(mode="json"),
                    "b": req.point_b.model_dump(mode="json"),
                    "travel_mode": req.travel_mode.value,
                    "category": req.category,
                    "region_mode": req.region_mode.value,
                    "rec_mode": req.rec_mode.value,
                    "prefs": req.custom_prefs,
                },
                region=req.region.to_dict() if req.region else None,
                result={
                    "items": [
                        {
                            "rank": r.rank,
                            "name": r.poi.name,
                            "poi_id": r.poi.id,
                            "lng": r.poi.lng,
                            "lat": r.poi.lat,
                            "reason": r.reason,
                        }
                        for r in result.recommendations
                    ],
                    "summary": result.ai_summary,
                    "source": result.source,
                    "notice": result.notice,
                },
                source=result.source,
            )
        except Exception:  # noqa: BLE001 - 存储失败不影响结果展示
            pass

    @staticmethod
    def _snapshot(req: RecommendRequest) -> dict[str, Any]:
        return {
            "a": {"name": req.point_a.display, "location": req.point_a.location},
            "b": {"name": req.point_b.display, "location": req.point_b.location},
            "region_mode": req.region_mode.value,
            "travel_mode": req.travel_mode.value,
            "category": req.category,
            "rec_mode": req.rec_mode.value,
        }
