"""数据源：真实 API（含录制）、录制回放、离线合成。

三种实现都满足 `algorithms` 层使用的最小接口，可直接注入 `RecommendationPipeline`：
    place_around / place_polygon / place_text / geocode / regeocode / route / health_probe
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from algorithms.candidates import CancelToken
from diagnostics.mockdata import build_mock_ai_response, generate_pois, mock_route
from diagnostics.recording import Recording, RecordingError, request_key
from geometry.crs import haversine
from geometry.regions import CircleRegion, PolygonRegion
from models.schemas import Poi, RouteLeg, ServiceState, TravelMode, format_location
from services.amap_web import AMapWebService, GeocodeHit, HealthProbeResult, PoiPage
from services.deepseek import LLMResult
from services.errors import AmapError, DeepSeekError

__all__ = [
    "Budget",
    "BudgetExceeded",
    "ReplayMiss",
    "LiveAMap",
    "ReplayAMap",
    "MockAMap",
    "LiveDeepSeek",
    "ReplayDeepSeek",
    "MockDeepSeek",
    "RecordingAMap",
    "RecordingDeepSeek",
]


class BudgetExceeded(RuntimeError):
    def __init__(self, kind: str, limit: int, used: int) -> None:
        super().__init__(f"{kind} 预算超限：已用 {used}，上限 {limit}")
        self.code = "BUDGET_EXCEEDED"
        self.kind = kind
        self.limit = limit
        self.used = used


class ReplayMiss(RecordingError):
    def __init__(self, key: str, params: dict[str, Any] | None = None) -> None:
        detail = json.dumps(params, ensure_ascii=False)[:200] if params else ""
        super().__init__("REPLAY_MISS", f"录制数据缺少该请求：{key} {detail}")
        self.key = key


@dataclass
class Budget:
    max_api_calls: int | None = 200
    max_tokens: int | None = 120_000
    api_calls: int = 0
    api_failures: int = 0
    tokens: int = 0
    ai_calls: int = 0
    started: float = field(default_factory=time.perf_counter)

    def spend_api(self, n: int = 1) -> None:
        self.api_calls += n
        if self.max_api_calls is not None and self.api_calls > self.max_api_calls:
            raise BudgetExceeded("api_calls", self.max_api_calls, self.api_calls)

    def fail_api(self) -> None:
        self.api_failures += 1

    def spend_tokens(self, n: int) -> None:
        self.tokens += max(0, int(n))
        if self.max_tokens is not None and self.tokens > self.max_tokens:
            raise BudgetExceeded("tokens", self.max_tokens, self.tokens)

    def elapsed_s(self) -> float:
        return time.perf_counter() - self.started

    def usage_ratio(self) -> float:
        if not self.max_api_calls:
            return 0.0
        return self.api_calls / float(self.max_api_calls)

    def snapshot(self) -> dict[str, Any]:
        return {
            "api_calls": self.api_calls,
            "api_failures": self.api_failures,
            "tokens": self.tokens,
            "ai_calls": self.ai_calls,
            "elapsed_s": round(self.elapsed_s(), 2),
            "limits": {"api_calls": self.max_api_calls, "tokens": self.max_tokens},
        }


# --------------------------------------------------------------------------- #
# 请求参数与序列化
# --------------------------------------------------------------------------- #
def _around_params(center: tuple[float, float], radius: float, types: str | None,
                   keywords: str | None, page: int, offset: int) -> dict[str, Any]:
    return {
        "center": [round(center[0], 6), round(center[1], 6)],
        "radius": int(radius),
        "types": types or "",
        "keywords": keywords or "",
        "page": page,
        "offset": offset,
    }


def _polygon_params(points: Sequence[Sequence[float]], types: str | None,
                    keywords: str | None, page: int, offset: int) -> dict[str, Any]:
    return {
        "points": [[round(float(x), 6), round(float(y), 6)] for x, y in points],
        "types": types or "",
        "keywords": keywords or "",
        "page": page,
        "offset": offset,
    }


def _route_params(mode: TravelMode, origin: tuple[float, float], destination: tuple[float, float],
                  city: str | None, dest_city: str | None) -> dict[str, Any]:
    return {
        "mode": mode.value,
        "origin": [round(origin[0], 6), round(origin[1], 6)],
        "destination": [round(destination[0], 6), round(destination[1], 6)],
        "city": city or "",
        "dest_city": dest_city or "",
    }


def _page_dict(page: PoiPage) -> dict[str, Any]:
    return {
        "total": page.total,
        "page": page.page,
        "offset": page.offset,
        "pois": [poi.model_dump(mode="json") for poi in page.pois],
    }


def _page_from(data: dict[str, Any]) -> PoiPage:
    pois = [Poi.model_validate(item) for item in data.get("pois", [])]
    return PoiPage(
        total=int(data.get("total", len(pois))),
        page=int(data.get("page", 1)),
        offset=int(data.get("offset", 25)),
        pois=pois,
    )


# --------------------------------------------------------------------------- #
# 高德
# --------------------------------------------------------------------------- #
def _record_payload(result: Any) -> Any:
    if isinstance(result, PoiPage):
        return _page_dict(result)
    if isinstance(result, list):
        # 列表型返回值（geocode 的 GeocodeHit 列表）统一包一层，
        # 保证「录制写入」与「回放读取」用同一个结构，回放才不会抛属性错误。
        return {"value": [_item_dict(item) for item in result]}
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    return {"value": result}


def _item_dict(item: Any) -> Any:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    if hasattr(item, "__dict__"):
        return {k: v for k, v in item.__dict__.items() if not k.startswith("_")}
    return item


class RecordingAMap:
    """通用录制包装：把任意 AMapLike 数据源（mock/replay/live）的响应写入 Recording。"""

    configured = True

    def __init__(self, inner: Any, recording: Recording) -> None:
        self._inner = inner
        self._recording = recording
        self.model = getattr(inner, "model", None)

    async def _call(self, kind: str, key: str, params: dict[str, Any], func) -> Any:  # noqa: ANN001
        try:
            result = await func()
        except Exception as exc:  # noqa: BLE001
            self._recording.add_amap_error(kind, key, params, exc)
            raise
        self._recording.add_amap(kind, key, _record_payload(result), params)
        return result

    async def place_around(self, center, radius_m, *, types=None, keywords=None, page=1, offset=25,
                           sortrule="distance") -> PoiPage:  # noqa: ANN001
        params = _around_params(center, radius_m, types, keywords, page, offset)
        return await self._call(
            "around", request_key("around", **params), params,
            lambda: self._inner.place_around(
                center, radius_m, types=types, keywords=keywords, page=page, offset=offset, sortrule=sortrule
            ),
        )

    async def place_polygon(self, points, *, types=None, keywords=None, page=1, offset=25) -> PoiPage:  # noqa: ANN001
        params = _polygon_params(points, types, keywords, page, offset)
        return await self._call(
            "polygon", request_key("polygon", **params), params,
            lambda: self._inner.place_polygon(points, types=types, keywords=keywords, page=page, offset=offset),
        )

    async def place_text(self, keywords: str, *, city=None, types=None, page=1, offset=25,
                         citylimit=False) -> PoiPage:  # noqa: ANN001
        params = {"keywords": keywords, "city": city or "", "types": types or "", "page": page, "offset": offset}
        return await self._call(
            "text", request_key("text", **params), params,
            lambda: self._inner.place_text(keywords, city=city, types=types, page=page, offset=offset),
        )

    async def geocode(self, address: str, *, city=None) -> list[GeocodeHit]:  # noqa: ANN001
        params = {"address": address, "city": city or ""}
        return await self._call(
            "geocode", request_key("geocode", **params), params,
            lambda: self._inner.geocode(address, city=city),
        )

    async def regeocode(self, lng: float, lat: float, radius: int = 200) -> dict[str, Any]:
        params = {"lng": round(lng, 6), "lat": round(lat, 6), "radius": radius}
        return await self._call(
            "regeocode", request_key("regeocode", **params), params,
            lambda: self._inner.regeocode(lng, lat, radius),
        )

    async def route(self, mode: TravelMode, origin, destination, *, city=None, dest_city=None,
                    use_cache=True, cache_ttl_s=600.0, raise_on_error=False) -> RouteLeg:  # noqa: ANN001
        params = _route_params(mode, origin, destination, city, dest_city)
        return await self._call(
            "route", request_key("route", **params), params,
            lambda: self._inner.route(
                mode, origin, destination, city=city, dest_city=dest_city,
                use_cache=use_cache, cache_ttl_s=cache_ttl_s, raise_on_error=raise_on_error,
            ),
        )

    async def health_probe(self) -> HealthProbeResult:
        return await self._inner.health_probe()


class RecordingDeepSeek:
    """通用 AI 录制包装：把任意 AI 数据源（mock/live）的响应写入 Recording。

    mock 模式此前只录制高德响应，AI 响应为空，于是 `--replay ... --ai recorded`
    无法复现原结果（会静默降级成规则排序）。这里补齐 AI 侧录制。
    """

    configured = True

    def __init__(self, inner: Any, recording: Recording) -> None:
        self._inner = inner
        self._recording = recording
        self.model = getattr(inner, "model", None)

    async def chat_json(self, messages, **kwargs):  # noqa: ANN001, ANN003
        result: LLMResult = await self._inner.chat_json(messages, **kwargs)
        usage = result.usage or {}
        self._recording.add_deepseek(
            messages,
            {"content": result.content, "data": result.data},
            usage if isinstance(usage, dict) else {},
        )
        return result


class LiveAMap:
    """真实高德服务 + 可选录制 + 调用预算。"""

    configured = True

    def __init__(
        self,
        service: AMapWebService,
        *,
        budget: Budget,
        recording: Recording | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self._service = service
        self._budget = budget
        self._recording = recording
        self._cancel = cancel

    async def _call(self, kind: str, key: str, params: dict[str, Any], func) -> Any:  # noqa: ANN001
        if self._cancel:
            self._cancel.raise_if_cancelled()
        self._budget.spend_api()
        before = self._http_attempts()
        try:
            result = await func()
        except (AmapError, asyncio.CancelledError) as exc:
            self._budget.fail_api()
            if self._recording is not None:
                self._recording.add_amap_error(kind, key, params, exc)
            raise
        finally:
            self._charge_retries(before)
        if self._recording is not None:
            self._recording.add_amap(kind, key, _record_payload(result), params)
        return result

    # 传输层会自行重试（网络错误/5xx/限流），每次重发都是真实的配额消耗。
    # 预算按「真实 HTTP 次数」计，否则 --max-api-calls 最多会超支到重试倍率。
    def _http_attempts(self) -> int:
        transport = getattr(self._service, "transport", None)
        metrics = getattr(transport, "metrics", None)
        if metrics is None:
            return 0
        return int(sum(metrics.calls.values()))

    def _charge_retries(self, before: int) -> None:
        after = self._http_attempts()
        extra = max(0, after - before - 1)
        for _ in range(extra):
            self._budget.spend_api()

    async def place_around(self, center, radius_m, *, types=None, keywords=None, page=1, offset=25,
                           sortrule="distance") -> PoiPage:  # noqa: ANN001
        params = _around_params(center, radius_m, types, keywords, page, offset)
        key = request_key("around", **params)
        return await self._call(
            "around", key, params,
            lambda: self._service.place_around(
                center, radius_m, types=types, keywords=keywords, page=page, offset=offset, sortrule=sortrule
            ),
        )

    async def place_polygon(self, points, *, types=None, keywords=None, page=1, offset=25) -> PoiPage:  # noqa: ANN001
        params = _polygon_params(points, types, keywords, page, offset)
        key = request_key("polygon", **params)
        return await self._call(
            "polygon", key, params,
            lambda: self._service.place_polygon(
                points, types=types, keywords=keywords, page=page, offset=offset
            ),
        )

    async def place_text(self, keywords: str, *, city=None, types=None, page=1, offset=25,
                         citylimit=False) -> PoiPage:  # noqa: ANN001
        params = {"keywords": keywords, "city": city or "", "types": types or "", "page": page, "offset": offset}
        key = request_key("text", **params)
        return await self._call(
            "text", key, params,
            lambda: self._service.place_text(
                keywords, city=city, types=types, page=page, offset=offset, citylimit=citylimit
            ),
        )

    async def geocode(self, address: str, *, city=None) -> list[GeocodeHit]:  # noqa: ANN001
        params = {"address": address, "city": city or ""}
        key = request_key("geocode", **params)
        return await self._call("geocode", key, params, lambda: self._service.geocode(address, city=city))

    async def regeocode(self, lng: float, lat: float, radius: int = 200) -> dict[str, Any]:
        params = {"lng": round(lng, 6), "lat": round(lat, 6), "radius": radius}
        key = request_key("regeocode", **params)
        return await self._call("regeocode", key, params, lambda: self._service.regeocode(lng, lat, radius))

    async def route(self, mode: TravelMode, origin, destination, *, city=None, dest_city=None,
                    use_cache=True, cache_ttl_s=600.0, raise_on_error=False) -> RouteLeg:  # noqa: ANN001
        params = _route_params(mode, origin, destination, city, dest_city)
        key = request_key("route", **params)
        leg: RouteLeg = await self._call(
            "route", key, params,
            lambda: self._service.route(
                mode, origin, destination, city=city, dest_city=dest_city,
                use_cache=use_cache, cache_ttl_s=cache_ttl_s, raise_on_error=raise_on_error,
            ),
        )
        return leg

    async def health_probe(self) -> HealthProbeResult:
        return await self._service.health_probe()


class ReplayAMap:
    """只读录制数据，绝不发起网络请求。"""

    configured = True

    def __init__(self, recording: Recording, *, budget: Budget, cancel: CancelToken | None = None) -> None:
        self._recording = recording
        self._budget = budget
        self._cancel = cancel

    def _get(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._cancel:
            self._cancel.raise_if_cancelled()
        entry = self._recording.amap.get(key)
        if entry is None:
            raise ReplayMiss(key, params)
        self._budget.spend_api()
        if "error" in entry:
            self._budget.fail_api()
            raise AmapError(
                f"录制中记录的失败：{entry['error'].get('message', '')}",
                state=ServiceState.SERVICE_ERROR,
            )
        return entry.get("payload") or {}

    async def place_around(self, center, radius_m, *, types=None, keywords=None, page=1, offset=25,
                           sortrule="distance") -> PoiPage:  # noqa: ANN001
        params = _around_params(center, radius_m, types, keywords, page, offset)
        return _page_from(self._get(request_key("around", **params), params))

    async def place_polygon(self, points, *, types=None, keywords=None, page=1, offset=25) -> PoiPage:  # noqa: ANN001
        params = _polygon_params(points, types, keywords, page, offset)
        return _page_from(self._get(request_key("polygon", **params), params))

    async def place_text(self, keywords: str, *, city=None, types=None, page=1, offset=25,
                         citylimit=False) -> PoiPage:  # noqa: ANN001
        params = {"keywords": keywords, "city": city or "", "types": types or "", "page": page, "offset": offset}
        return _page_from(self._get(request_key("text", **params), params))

    async def geocode(self, address: str, *, city=None) -> list[GeocodeHit]:  # noqa: ANN001
        params = {"address": address, "city": city or ""}
        data = self._get(request_key("geocode", **params), params)
        # 兼容两种录制格式：{"value": [...]}（新）与裸列表（旧录制文件）
        raw_items = data.get("value", []) if isinstance(data, dict) else data
        if not isinstance(raw_items, list):
            return []
        hits: list[GeocodeHit] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                hits.append(
                    GeocodeHit(
                        formatted_address=str(item.get("formatted_address", "") or ""),
                        lng=float(item["lng"]),
                        lat=float(item["lat"]),
                        adcode=str(item.get("adcode", "") or ""),
                        city=str(item.get("city", "") or ""),
                        district=str(item.get("district", "") or ""),
                        level=str(item.get("level", "") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return hits

    async def regeocode(self, lng: float, lat: float, radius: int = 200) -> dict[str, Any]:
        params = {"lng": round(lng, 6), "lat": round(lat, 6), "radius": radius}
        return self._get(request_key("regeocode", **params), params)

    async def route(self, mode: TravelMode, origin, destination, *, city=None, dest_city=None,
                    use_cache=True, cache_ttl_s=600.0, raise_on_error=False) -> RouteLeg:  # noqa: ANN001
        params = _route_params(mode, origin, destination, city, dest_city)
        data = self._get(request_key("route", **params), params)
        return RouteLeg.model_validate(data)

    async def health_probe(self) -> HealthProbeResult:
        return HealthProbeResult(
            state=ServiceState.UNKNOWN,
            latency_ms=0,
            detail="replay 模式：未发起网络请求，状态 not_tested",
        )


class MockAMap:
    """离线合成数据；不访问网络，不依赖录制文件。"""

    configured = True

    FIXTURES = {
        "天津站": (117.210089, 39.135951, "120105"),
        "天津大学": (117.311568, 38.992672, "120112"),
        "天津之眼": (117.180294, 39.153818, "120104"),
        "天安门": (116.397499, 39.908722, "110101"),
    }

    def __init__(self, *, budget: Budget, cancel: CancelToken | None = None, poi_count: int = 48,
                 seed_text: str = "mock", fail_routes: int = 0) -> None:
        self._budget = budget
        self._cancel = cancel
        self._poi_count = poi_count
        self._seed = seed_text
        self._fail_routes = fail_routes
        self._route_failures = 0
        self._cache: dict[str, PoiPage] = {}
        # 每个坐标派生 ID 对应一个稳定的中文序号：同一物理点在不同分块被重复检索时
        # 拿到同一个名字，不同物理点不会重名（否则结果里会出现多条同名推荐）。
        self._name_index: dict[str, int] = {}

    def _region_pois(self, region, center, radius, page, offset, tag: str,
                     label: str = "咖啡厅", type_code: str = "050500"):  # noqa: ANN001
        if self._cancel:
            self._cancel.raise_if_cancelled()
        self._budget.spend_api()
        pois = generate_pois(
            region,
            center=center,
            radius_m=radius,
            category=label,
            count=self._poi_count,
            seed_text=f"{self._seed}:{tag}",
            type_code=type_code,
        )
        # 用坐标派生稳定 ID：重叠分块检索到的同一物理点会得到同一个 ID，便于去重
        for poi in pois:
            poi.id = f"MOCK-{round(poi.lng * 100000):d}-{round(poi.lat * 100000):d}"
            poi.name = f"{label}·模拟点位{self._name_of(poi.id)}"
        start = (page - 1) * offset
        return PoiPage(total=len(pois), page=page, offset=offset, pois=pois[start : start + offset])

    @staticmethod
    def _query_label(keywords: str | None, types: str | None) -> tuple[str, str]:
        """把本次请求的关键词/类型编码映射成合成数据的类别与类型码。"""
        label = (keywords or "").strip() or "地点"
        code = (types or "").split("|")[0].strip() or "050500"
        return label, code

    def _name_of(self, poi_id: str) -> str:
        """把坐标派生 ID 映射成只分配一次的序号，保证不同 POI 名称互不相同。"""
        index = self._name_index.get(poi_id)
        if index is None:
            index = len(self._name_index) + 1
            self._name_index[poi_id] = index
        return f"{index:03d}"

    async def place_around(self, center, radius_m, *, types=None, keywords=None, page=1, offset=25,
                           sortrule="distance") -> PoiPage:  # noqa: ANN001
        region = CircleRegion(center[0], center[1], max(20.0, float(radius_m)))
        label, code = self._query_label(keywords, types)
        return self._region_pois(region, center, radius_m, page, offset, "around", label, code)

    async def place_polygon(self, points, *, types=None, keywords=None, page=1, offset=25) -> PoiPage:  # noqa: ANN001
        region = PolygonRegion(points)
        center = ((region.bbox()[0] + region.bbox()[2]) / 2, (region.bbox()[1] + region.bbox()[3]) / 2)
        label, code = self._query_label(keywords, types)
        return self._region_pois(region, center, 3000, page, offset, "polygon", label, code)

    async def place_text(self, keywords: str, *, city=None, types=None, page=1, offset=25,
                         citylimit=False) -> PoiPage:  # noqa: ANN001
        self._budget.spend_api()
        hits: list[Poi] = []
        for index, (name, (lng, lat, adcode)) in enumerate(self.FIXTURES.items(), start=1):
            if keywords and (keywords in name or name in keywords):
                hits.append(
                    Poi(
                        id=f"FIX{index:03d}",
                        name=name,
                        lng=lng,
                        lat=lat,
                        address=f"模拟市{name}",
                        adcode=adcode,
                        cityname="模拟市",
                    )
                )
        return PoiPage(total=len(hits), page=1, offset=offset, pois=hits)

    async def geocode(self, address: str, *, city=None) -> list[GeocodeHit]:  # noqa: ANN001
        self._budget.spend_api()
        for name, (lng, lat, adcode) in self.FIXTURES.items():
            if name in address:
                return [
                    GeocodeHit(
                        formatted_address=f"模拟市{name}",
                        lng=lng,
                        lat=lat,
                        adcode=adcode,
                        city="模拟市",
                        district="测试区",
                        level="兴趣点",
                    )
                ]
        return []

    async def regeocode(self, lng: float, lat: float, radius: int = 200) -> dict[str, Any]:
        self._budget.spend_api()
        return {
            "formatted_address": f"模拟地址 {format_location(lng, lat)}",
            "adcode": "000000",
            "city": "模拟市",
            "district": "测试区",
            "township": "模拟街道",
            "citycode": "0000",
        }

    async def route(self, mode: TravelMode, origin, destination, *, city=None, dest_city=None,
                    use_cache=True, cache_ttl_s=600.0, raise_on_error=False) -> RouteLeg:  # noqa: ANN001
        self._budget.spend_api()
        if self._route_failures < self._fail_routes:
            self._route_failures += 1
            self._budget.fail_api()
            if raise_on_error:
                raise AmapError("模拟路线规划失败", state=ServiceState.SERVICE_ERROR)
            return RouteLeg(mode=mode, ok=False, error="模拟路线规划失败", summary="模拟路线规划失败")
        leg = mock_route(mode, origin, destination, seed_text=self._seed)
        if haversine(origin[0], origin[1], destination[0], destination[1]) < 30:
            self._budget.fail_api()
            if raise_on_error:
                raise AmapError("模拟路线返回空路径", state=ServiceState.SERVICE_ERROR)
            return RouteLeg(mode=mode, ok=False, error="模拟路线返回空路径")
        return leg

    async def health_probe(self) -> HealthProbeResult:
        return HealthProbeResult(
            state=ServiceState.UNKNOWN, latency_ms=0, detail="mock 模式：未发起网络请求，状态 not_tested"
        )


# --------------------------------------------------------------------------- #
# DeepSeek
# --------------------------------------------------------------------------- #
DP_MODEL = "deepseek-v4-pro"


class LiveDeepSeek:
    configured = True

    def __init__(self, service, *, budget: Budget, recording: Recording | None = None,
                 cancel: CancelToken | None = None) -> None:  # noqa: ANN001
        self._service = service
        self._budget = budget
        self._recording = recording
        self._cancel = cancel
        self.model = getattr(service, "model", DP_MODEL)

    def _http_attempts(self) -> int:
        transport = getattr(self._service, "transport", None)
        metrics = getattr(transport, "metrics", None)
        if metrics is None:
            return 0
        return int(sum(metrics.calls.values()))

    async def chat_json(self, messages, **kwargs):  # noqa: ANN001, ANN003
        if self._cancel:
            self._cancel.raise_if_cancelled()
        self._budget.spend_api()
        self._budget.ai_calls += 1
        before = self._http_attempts()
        try:
            result: LLMResult = await self._service.chat_json(messages, **kwargs)
        except DeepSeekError:
            self._budget.fail_api()
            raise
        finally:
            self._charge_retries(before)
        usage = result.usage or {}
        self._budget.spend_tokens(int(usage.get("total_tokens", 0) or 0))
        if self._recording is not None:
            self._recording.add_deepseek(messages, {"content": result.content, "data": result.data}, usage)
        return result

    def _charge_retries(self, before: int) -> None:
        extra = max(0, self._http_attempts() - before - 1)
        for _ in range(extra):
            self._budget.spend_api()


def _messages_key(messages: Any) -> str:
    """消息指纹：用于回放时把「哪一次提问」对应到「哪一条录制响应」。"""
    try:
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return digest


class ReplayDeepSeek:
    """只回放录制中的 AI 响应，绝不发起网络请求。

    优先按消息指纹精确匹配：一次运行可能调用多次 AI（候选分批），
    只按索引取「最后一条」会把不同批次的答案混在一起，导致回放结果与录制不一致。
    """

    configured = True

    def __init__(self, recording: Recording, *, index: int = -1) -> None:
        self._recording = recording
        self._index = index
        self.model = "recorded"
        self._by_prompt: dict[str, dict[str, Any]] = {}
        for entry in recording.deepseek:
            if not isinstance(entry, dict):
                continue
            key = _messages_key(entry.get("messages"))
            if key:
                self._by_prompt.setdefault(key, entry)

    def _pick(self, messages: Any) -> dict[str, Any]:
        entry = self._by_prompt.get(_messages_key(messages))
        if entry is not None:
            return entry
        raise DeepSeekError(
            "录制数据中没有与当前请求匹配的 DeepSeek 响应，请使用原用例回放或改用 --ai mock",
            state=ServiceState.SERVICE_ERROR,
        )

    async def chat_json(self, messages, **kwargs):  # noqa: ANN001, ANN003
        if not self._recording.deepseek:
            raise DeepSeekError(
                "录制数据中没有 DeepSeek 响应（录制时未包含 AI 调用，请重新录制或改用 --ai mock）",
                state=ServiceState.SERVICE_ERROR,
            )
        entry = self._pick(messages)
        response = entry.get("response") or {}
        return LLMResult(
            content=str(response.get("content", "")),
            data=response.get("data"),
            latency_ms=0,
            model="recorded",
            usage={"recorded": 1},
        )


class MockDeepSeek:
    configured = True

    def __init__(self, *, budget: Budget | None = None, mode: str = "mock") -> None:
        self._budget = budget
        self.model = f"{mode}-model"

    async def chat_json(self, messages, **kwargs):  # noqa: ANN001, ANN003
        if self._budget:
            self._budget.spend_api()
            self._budget.ai_calls += 1
        payload: dict[str, Any] = {}
        for message in messages:
            if message.get("role") == "user":
                try:
                    payload = json.loads(message.get("content", "{}"))
                except json.JSONDecodeError:
                    payload = {}
        candidates = payload.get("candidates") or []
        top_n = int(payload.get("top_n") or 5)
        data = build_mock_ai_response(candidates, top_n)
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "mock": 1}
        if self._budget:
            self._budget.spend_tokens(0)
        return LLMResult(
            content=json.dumps(data, ensure_ascii=False),
            data=data,
            latency_ms=0,
            model=self.model,
            usage=usage,
        )
