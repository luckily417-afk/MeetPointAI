"""运行追踪：记录每个阶段的候选数量、淘汰原因、AI 输入输出与耗时。

设计原则：
* 纯数据收集，不做任何推荐决策，不改变 GUI 行为；
* 只保存可序列化的摘要，体积可控；
* 所有字段都可安全写入 JSON（不含 Key）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from models.schemas import Candidate, Recommendation, RouteLeg

__all__ = ["TraceRecorder"]

STAGE_ORDER = (
    "raw",
    "deduped",
    "in_region",
    "routed",
    "preselected",
    "ai_input",
    "ai_output",
    "final",
)


@dataclass
class TraceRecorder:
    """记录一次运行的全链路诊断信息。"""

    events: list[dict[str, Any]] = field(default_factory=list)
    eliminated: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    timings_ms: dict[str, int] = field(default_factory=dict)
    ai: dict[str, Any] = field(default_factory=dict)
    search: dict[str, Any] = field(default_factory=dict)
    cache: dict[str, int] = field(default_factory=lambda: {"route_hits": 0, "route_misses": 0})
    extras: dict[str, Any] = field(default_factory=dict)
    _started: float = field(default_factory=time.perf_counter)
    _marks: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # 基础记录
    # ------------------------------------------------------------------ #
    def event(self, kind: str, **fields: Any) -> None:
        item = {"kind": kind, "ts_ms": self._elapsed_ms()}
        item.update(fields)
        self.events.append(item)

    def warn(self, code: str, message: str) -> None:
        """记录一条警告；同一 (code, message) 只保留一条，重复出现只累加次数。

        一次运行里 AI 输出校验会重试多次，若每次都追加同一条警告，最终报告里会出现
        多条一模一样的内容，掩盖真正的不同原因。
        """
        for existing in self.warnings:
            if existing["code"] == code and existing["message"] == message:
                existing["repeat"] = int(existing.get("repeat", 1)) + 1
                return
        self.warnings.append({"code": code, "message": message})

    def error(self, code: str, message: str, detail: str = "") -> None:
        payload = {"code": code, "message": message}
        if detail:
            payload["detail"] = detail[:500]
        self.errors.append(payload)

    def count(self, stage: str, value: int) -> None:
        if stage not in STAGE_ORDER:
            self.extras.setdefault("custom_counts", {})[stage] = value
            return
        self.counts[stage] = int(value)

    def bump(self, stage: str, delta: int = 1) -> None:
        self.counts[stage] = self.counts.get(stage, 0) + delta

    def stage_done(self, name: str) -> None:
        self._marks[name] = time.perf_counter()

    def elapsed_ms(self) -> int:
        return self._elapsed_ms()

    def _elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)

    # ------------------------------------------------------------------ #
    # 淘汰记录
    # ------------------------------------------------------------------ #
    def eliminate(
        self,
        poi_id: str,
        stage: str,
        reason: str,
        *,
        name: str = "",
        lng: float | None = None,
        lat: float | None = None,
        detail: str = "",
    ) -> None:
        entry: dict[str, Any] = {"poi_id": poi_id, "stage": stage, "reason": reason}
        if name:
            entry["name"] = name
        if lng is not None and lat is not None:
            entry["location"] = [round(lng, 6), round(lat, 6)]
        if detail:
            entry["detail"] = detail[:300]
        self.eliminated.append(entry)

    def eliminate_candidate(self, cand: Candidate, stage: str, reason: str, detail: str = "") -> None:
        self.eliminate(
            cand.id,
            stage,
            reason,
            name=cand.poi.name,
            lng=cand.poi.lng,
            lat=cand.poi.lat,
            detail=detail,
        )

    # ------------------------------------------------------------------ #
    # 候选摘要
    # ------------------------------------------------------------------ #
    def candidate(self, cand: Candidate, *, selected: bool = False, rank: int | None = None) -> None:
        self.candidates[cand.id] = {
            "poi_id": cand.id,
            "name": cand.poi.name,
            "type": cand.poi.type_name,
            "lng": round(cand.poi.lng, 6),
            "lat": round(cand.poi.lat, 6),
            "leg_a": self._leg(cand.leg_a),
            "leg_b": self._leg(cand.leg_b),
            "routes_ok": cand.routes_ok,
            "selected": selected,
            "rank": rank,
        }

    @staticmethod
    def _leg(leg: RouteLeg | None) -> dict[str, Any] | None:
        if leg is None:
            return None
        return {
            "mode": leg.mode.value,
            "ok": leg.ok,
            "duration_min": leg.duration_min if leg.ok else None,
            "distance_km": leg.distance_km if leg.ok else None,
            "transfers": leg.transfers if leg.ok else None,
            "walk_m": round(leg.walk_m) if leg.ok else None,
            "cached": leg.cached,
            "error": leg.error or None,
        }

    # ------------------------------------------------------------------ #
    # 搜索 / AI / 缓存 / 耗时
    # ------------------------------------------------------------------ #
    def set_search(self, **fields: Any) -> None:
        self.search.update({k: v for k, v in fields.items() if v is not None})

    def set_ai(self, **fields: Any) -> None:
        self.ai.update({k: v for k, v in fields.items() if v is not None})

    def note_cache(self, hit: bool) -> None:
        key = "route_hits" if hit else "route_misses"
        self.cache[key] = self.cache.get(key, 0) + 1

    def timing(self, name: str, seconds: float) -> None:
        self.timings_ms[name] = int(round(seconds * 1000))

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        return {
            "counts": {stage: self.counts.get(stage, 0) for stage in STAGE_ORDER},
            "extra_counts": self.extras.get("custom_counts", {}),
            "eliminated": self.eliminated,
            "warnings": self.warnings,
            "errors": self.errors,
            "events": self.events,
            "candidates": list(self.candidates.values()),
            "timings_ms": self.timings_ms,
            "ai": self.ai,
            "search": self.search,
            "cache": self.cache,
            "extras": {k: v for k, v in self.extras.items() if k != "custom_counts"},
        }

    # ------------------------------------------------------------------ #
    # 覆盖率 / 多样性（明确标注为启发式指标，不是全区域召回率）
    # ------------------------------------------------------------------ #
    @staticmethod
    def coverage_metrics(
        candidates: list[dict[str, Any]],
        *,
        region_area_km2: float | None,
        radius_km: float | None,
        tiles: int,
    ) -> dict[str, Any]:
        points = [
            (c["lng"], c["lat"]) for c in candidates if "lng" in c and "lat" in c
        ]
        metric: dict[str, Any] = {
            "note": "启发式指标，仅描述本次检索到的候选分布，不代表全区域真实 POI 召回率",
            "candidate_points": len(points),
            "tiles": tiles,
        }
        if radius_km:
            metric["search_radius_km"] = round(radius_km, 3)
        if region_area_km2:
            metric["region_area_km2"] = round(region_area_km2, 3)
            # 估算：把候选点按 500m 网格去重，作为覆盖面积的下限估计
            cells = {(round(x * 222), round(y * 222)) for x, y in points}
            metric["occupied_cells_500m"] = len(cells)
            metric["approx_cell_coverage_km2"] = round(len(cells) * 0.25, 2)
            if region_area_km2 > 0:
                metric["coverage_ratio_estimate"] = round(
                    min(1.0, (len(cells) * 0.25) / region_area_km2), 4
                )
        if points:
            lngs = [p[0] for p in points]
            lats = [p[1] for p in points]
            metric["bbox"] = [round(min(lngs), 6), round(min(lats), 6), round(max(lngs), 6), round(max(lats), 6)]
            metric["spread_km"] = round(
                _haversine(min(lngs), min(lats), max(lngs), max(lats)) / 1000, 2
            )
        # 类型多样性
        types = {c.get("type", "").split(";")[-1] for c in candidates if c.get("type")}
        types.discard("")
        metric["distinct_types"] = len(types)
        return metric


def _haversine(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    import math

    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))
