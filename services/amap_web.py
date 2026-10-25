"""高德地图 Web 服务 API 客户端（真实数据唯一来源）。

覆盖：关键字搜索、周边搜索、多边形搜索、地理编码、逆地理编码、
驾车/步行/骑行/公共交通路径规划，以及轻量健康探测。
所有坐标均为 GCJ-02。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

from config.constants import AMAP_REST_BASE
from geometry.crs import haversine
from models.schemas import (
    Poi,
    RouteLeg,
    ServiceName,
    ServiceState,
    TravelMode,
    format_location,
    parse_location,
)
from services.errors import AmapError, NotConfiguredError, classify_amap_payload
from services.http_client import HttpTransport, redact

__all__ = ["AMapWebService", "PoiPage", "GeocodeHit", "CacheProtocol", "HealthProbeResult"]


class CacheProtocol(Protocol):
    def get_json(self, key: str, ttl_s: float | None = None) -> Any | None: ...
    def put_json(self, key: str, value: Any) -> None: ...


@dataclass
class PoiPage:
    total: int = 0
    pois: list[Poi] = field(default_factory=list)
    page: int = 1
    offset: int = 25

    @property
    def has_more(self) -> bool:
        return self.page * self.offset < self.total


@dataclass
class GeocodeHit:
    formatted_address: str
    lng: float
    lat: float
    adcode: str = ""
    city: str = ""
    district: str = ""
    level: str = ""

    @property
    def location(self) -> str:
        return format_location(self.lng, self.lat)


@dataclass
class HealthProbeResult:
    state: ServiceState
    latency_ms: int
    detail: str = ""
    http_status: int | None = None
    biz_code: str | None = None
    error_type: str = ""


class AMapWebService:
    """高德 Web 服务封装。任何非 status=1 的返回都会抛 AmapError。"""

    def __init__(
        self,
        key: str,
        transport: HttpTransport,
        base_url: str = AMAP_REST_BASE,
        cache: CacheProtocol | None = None,
    ) -> None:
        self._key = key
        self._transport = transport
        self._base = base_url.rstrip("/")
        self._cache = cache

    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _require_key(self) -> None:
        if not self._key:
            raise NotConfiguredError(ServiceName.AMAP_WEB, "高德 Web 服务 API Key 未配置")

    def _params(self, **kwargs: Any) -> dict[str, Any]:
        params = {"key": self._key, "output": "JSON"}
        for k, v in kwargs.items():
            if v is None or v == "":
                continue
            params[k] = v
        return params

    def _validate_amap(self, payload: dict[str, Any], status: int) -> None:
        if status >= 400:
            state, _ = classify_amap_payload({})
            raise AmapError(
                f"高德接口 HTTP {status}",
                state=ServiceState.SERVICE_ERROR,
                http_status=status,
                retryable=status >= 500 or status == 429,
            )
        state, code, detail = classify_amap_payload(payload)
        if state is not ServiceState.OK:
            raise AmapError(
                detail or "高德接口返回失败",
                state=state,
                http_status=status,
                biz_code=code,
                retryable=state in {ServiceState.QUOTA_LIMITED, ServiceState.SERVICE_ERROR, ServiceState.TIMEOUT},
                detail=redact(str(payload)[:300]),
            )

    async def _get(self, path: str, *, retries: int = 2, **kwargs: Any) -> tuple[dict[str, Any], int, int]:
        self._require_key()
        url = f"{self._base}{path}"
        return await self._transport.get_json(
            "amap_web", url, self._params(**kwargs), max_retries=retries, validate=self._validate_amap
        )

    # ------------------------------------------------------------------ #
    # POI 解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_float(value: Any) -> float | None:
        try:
            if value in (None, "", []):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_poi(cls, raw: dict[str, Any]) -> Poi | None:
        try:
            lng, lat = parse_location(str(raw.get("location", "")))
        except ValueError:
            return None
        biz_ext = raw.get("biz_ext") or {}
        if not isinstance(biz_ext, dict):
            biz_ext = {}
        poi = Poi(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")).strip(),
            lng=lng,
            lat=lat,
            address=str(raw.get("address", "") or "").strip(),
            type_name=str(raw.get("type", "") or ""),
            type_code=str(raw.get("typecode", "") or ""),
            tel=str(raw.get("tel", "") or ""),
            pname=str(raw.get("pname", "") or ""),
            cityname=str(raw.get("cityname", "") or ""),
            adname=str(raw.get("adname", "") or ""),
            business_area=str(raw.get("business_area", "") or ""),
            adcode=str(raw.get("adcode", "") or ""),
            distance_m=cls._parse_float(raw.get("distance")),
            rating=cls._parse_float(biz_ext.get("rating")),
            cost=cls._parse_float(biz_ext.get("cost")),
        )
        return poi

    def _parse_page(self, payload: dict[str, Any], page: int, offset: int) -> PoiPage:
        raw_pois = payload.get("pois") or []
        pois: list[Poi] = []
        if isinstance(raw_pois, list):
            for item in raw_pois:
                if isinstance(item, dict):
                    poi = self._parse_poi(item)
                    if poi is not None:
                        pois.append(poi)
        total = 0
        try:
            total = int(payload.get("count", 0) or 0)
        except (TypeError, ValueError):
            total = len(pois)
        return PoiPage(total=total, pois=pois, page=page, offset=offset)

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    async def place_text(
        self,
        keywords: str,
        *,
        city: str | None = None,
        types: str | None = None,
        page: int = 1,
        offset: int = 25,
        citylimit: bool = False,
    ) -> PoiPage:
        payload, _, _ = await self._get(
            "/v3/place/text",
            keywords=keywords,
            city=city,
            types=types,
            page=page,
            offset=min(25, max(1, offset)),
            citylimit="true" if citylimit else None,
            extensions="all",
        )
        return self._parse_page(payload, page, offset)

    async def place_around(
        self,
        center: tuple[float, float],
        radius_m: float,
        *,
        types: str | None = None,
        keywords: str | None = None,
        page: int = 1,
        offset: int = 25,
        sortrule: str = "distance",
    ) -> PoiPage:
        payload, _, _ = await self._get(
            "/v3/place/around",
            location=format_location(*center),
            radius=int(min(50000, max(1, radius_m))),
            types=types,
            keywords=keywords,
            page=page,
            offset=min(25, max(1, offset)),
            sortrule=sortrule,
            extensions="all",
        )
        return self._parse_page(payload, page, offset)

    async def place_polygon(
        self,
        points: Sequence[Sequence[float]],
        *,
        types: str | None = None,
        keywords: str | None = None,
        page: int = 1,
        offset: int = 25,
    ) -> PoiPage:
        polygon = "|".join(f"{float(x):.6f},{float(y):.6f}" for x, y in points)
        payload, _, _ = await self._get(
            "/v3/place/polygon",
            polygon=polygon,
            types=types,
            keywords=keywords,
            page=page,
            offset=min(25, max(1, offset)),
            extensions="all",
        )
        return self._parse_page(payload, page, offset)

    # ------------------------------------------------------------------ #
    # 地理编码
    # ------------------------------------------------------------------ #
    async def geocode(self, address: str, *, city: str | None = None) -> list[GeocodeHit]:
        if not address.strip():
            return []
        payload, _, _ = await self._get("/v3/geocode/geo", address=address.strip(), city=city)
        hits: list[GeocodeHit] = []
        for item in payload.get("geocodes") or []:
            if not isinstance(item, dict):
                continue
            try:
                lng, lat = parse_location(str(item.get("location", "")))
            except ValueError:
                continue
            hits.append(
                GeocodeHit(
                    formatted_address=str(item.get("formatted_address", "") or address),
                    lng=lng,
                    lat=lat,
                    adcode=str(item.get("adcode", "") or ""),
                    city=str(item.get("city", "") or ""),
                    district=str(item.get("district", "") or ""),
                    level=str(item.get("level", "") or ""),
                )
            )
        return hits

    async def regeocode(self, lng: float, lat: float, radius: int = 200) -> dict[str, Any]:
        payload, _, _ = await self._get(
            "/v3/geocode/regeo",
            location=format_location(lng, lat),
            radius=radius,
            extensions="base",
        )
        regeo = payload.get("regeocode") or {}
        comp = regeo.get("addressComponent") or {}
        return {
            "formatted_address": str(regeo.get("formatted_address", "") or ""),
            "adcode": str(comp.get("adcode", "") or ""),
            "city": str(comp.get("city", "") or comp.get("province", "") or ""),
            "district": str(comp.get("district", "") or ""),
            "township": str(comp.get("township", "") or ""),
            "citycode": str(comp.get("citycode", "") or ""),
        }

    async def poi_detail(self, poi_id: str) -> Poi | None:
        payload, _, _ = await self._get("/v3/place/detail", id=poi_id)
        pois = payload.get("pois") or []
        if isinstance(pois, list) and pois:
            return self._parse_poi(pois[0])
        return None

    # ------------------------------------------------------------------ #
    # 路径规划
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_polyline(raw: str) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for chunk in str(raw or "").split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                lng, lat = parse_location(chunk)
            except ValueError:
                continue
            points.append((lng, lat))
        return points

    async def route(
        self,
        mode: TravelMode,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        city: str | None = None,
        dest_city: str | None = None,
        use_cache: bool = True,
        cache_ttl_s: float = 600.0,
        raise_on_error: bool = False,
    ) -> RouteLeg:
        key = (
            f"route:{mode.value}:{format_location(*origin)}:{format_location(*destination)}:"
            f"{city or ''}:{dest_city or ''}"
        )
        if use_cache and self._cache is not None:
            cached = self._cache.get_json(key, cache_ttl_s)
            if cached:
                try:
                    leg = RouteLeg.model_validate(cached["leg"])
                    leg.cached = True
                    from datetime import datetime, timezone

                    leg.cached_at = datetime.fromtimestamp(cached["ts"], tz=timezone.utc)
                    return leg
                except Exception:  # noqa: BLE001 - 缓存损坏则忽略
                    pass

        try:
            if mode is TravelMode.DRIVING:
                leg = await self._route_driving(origin, destination)
            elif mode is TravelMode.WALKING:
                leg = await self._route_walking(origin, destination)
            elif mode is TravelMode.BICYCLING:
                leg = await self._route_bicycling(origin, destination)
            else:
                leg = await self._route_transit(origin, destination, city, dest_city)
        except AmapError as exc:
            if raise_on_error:
                raise
            leg = RouteLeg(
                mode=mode,
                ok=False,
                error=str(exc),
                summary=f"路线规划失败：{exc.message}",
            )

        if use_cache and self._cache is not None and leg.ok:
            import time as _time

            self._cache.put_json(key, {"leg": leg.model_dump(mode="json"), "ts": _time.time()})
        return leg

    async def _route_driving(
        self, origin: tuple[float, float], destination: tuple[float, float]
    ) -> RouteLeg:
        payload, _, _ = await self._get(
            "/v3/direction/driving",
            origin=format_location(*origin),
            destination=format_location(*destination),
            extensions="base",
            strategy=0,
        )
        return self._parse_paths(payload, TravelMode.DRIVING)

    async def _route_walking(
        self, origin: tuple[float, float], destination: tuple[float, float]
    ) -> RouteLeg:
        payload, _, _ = await self._get(
            "/v3/direction/walking",
            origin=format_location(*origin),
            destination=format_location(*destination),
        )
        return self._parse_paths(payload, TravelMode.WALKING)

    async def _route_bicycling(
        self, origin: tuple[float, float], destination: tuple[float, float]
    ) -> RouteLeg:
        # v4 骑行接口使用 errcode/errmsg 结构
        payload, status, _ = await self._transport.get_json(
            "amap_web",
            f"{self._base}/v4/direction/bicycling",
            self._params(origin=format_location(*origin), destination=format_location(*destination)),
            max_retries=1,
        )
        if status >= 400 or int(payload.get("errcode", 0) or 0) != 0:
            raise AmapError(
                f"骑行路线失败：{payload.get('errmsg', '') or status}",
                state=classify_amap_payload({"status": "0", "info": str(payload.get("errmsg", "")), "infocode": str(payload.get("errcode", ""))})[0],
                http_status=status,
                biz_code=str(payload.get("errcode", "")),
            )
        data = payload.get("data") or {}
        paths = data.get("paths") or []
        if not paths:
            return RouteLeg(mode=TravelMode.BICYCLING, ok=False, error="未找到骑行路线", summary="未找到骑行路线")
        path = paths[0]
        points: list[tuple[float, float]] = []
        steps: list[str] = []
        for step in path.get("steps") or []:
            points.extend(self._parse_polyline(step.get("polyline", "")))
            if step.get("instruction"):
                steps.append(str(step["instruction"]))
        distance = float(path.get("distance", 0) or 0)
        duration = float(path.get("duration", 0) or 0)
        return RouteLeg(
            mode=TravelMode.BICYCLING,
            ok=True,
            distance_m=distance,
            duration_s=duration,
            transfers=0,
            walk_m=distance,
            summary=f"骑行 {distance / 1000:.1f} 公里 / 约 {duration / 60:.0f} 分钟",
            steps=steps[:8],
            polyline=points,
        )

    async def _route_transit(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        city: str | None,
        dest_city: str | None,
    ) -> RouteLeg:
        city_name = self._normalize_city(city)
        cityd_name = self._normalize_city(dest_city) or city_name
        if not city_name:
            city_name = await self._city_of(*origin)
        if not cityd_name:
            cityd_name = await self._city_of(*destination) or city_name
        if not city_name:
            return RouteLeg(
                mode=TravelMode.TRANSIT,
                ok=False,
                error="无法确定出发城市",
                summary="无法确定出发城市，公共交通不可用（可切换驾车/步行）",
            )
        payload, _, _ = await self._get(
            "/v3/direction/transit/integrated",
            origin=format_location(*origin),
            destination=format_location(*destination),
            city=city_name,
            cityd=cityd_name,
            strategy=0,
            extensions="base",
        )
        route = payload.get("route") or {}
        transits = route.get("transits") or []
        if not transits:
            straight = haversine(origin[0], origin[1], destination[0], destination[1])
            if straight <= 2500:
                walk_leg = await self._route_walking(origin, destination)
                if walk_leg.ok:
                    walk_leg.summary = (
                        f"公共交通过近无方案，改为步行：{walk_leg.distance_km:.2f} 公里 / "
                        f"约 {walk_leg.duration_min:.0f} 分钟"
                    )
                    return walk_leg
            distance = float(route.get("distance", 0) or 0)
            return RouteLeg(
                mode=TravelMode.TRANSIT,
                ok=False,
                distance_m=distance,
                error="未找到公共交通方案",
                summary="未找到公共交通方案（可尝试其他出行方式）",
            )
        best = transits[0]
        points: list[tuple[float, float]] = []
        steps: list[str] = []
        segments = best.get("segments") or []
        bus_count = 0
        walk_m = float(best.get("walking_distance", 0) or 0)
        for seg in segments:
            walking = seg.get("walking") or {}
            for step in walking.get("steps") or []:
                points.extend(self._parse_polyline(step.get("polyline", "")))
                if step.get("instruction"):
                    steps.append(f"步行：{step['instruction']}")
            for busline in ((seg.get("bus") or {}).get("buslines") or []):
                bus_count += 1
                points.extend(self._parse_polyline(busline.get("polyline", "")))
                name = str(busline.get("name", "") or "公交线路")
                dep = str((busline.get("departure_stop") or {}).get("name", ""))
                arr = str((busline.get("arrival_stop") or {}).get("name", ""))
                steps.append(f"{name}：{dep} → {arr}")
            railway = seg.get("railway") or {}
            if railway:
                points.extend(self._parse_polyline(railway.get("polyline", "")))
                steps.append(f"铁路：{railway.get('name', '')}")
        duration = float(best.get("duration", 0) or 0)
        distance = float(best.get("distance", 0) or 0)
        cost = float(best.get("cost", 0) or 0)
        transfers = max(0, bus_count - 1)
        return RouteLeg(
            mode=TravelMode.TRANSIT,
            ok=True,
            distance_m=distance,
            duration_s=duration,
            transfers=transfers,
            walk_m=walk_m,
            cost=cost,
            summary=f"公交/地铁 {duration / 60:.0f} 分钟，换乘 {transfers} 次，步行 {walk_m / 1000:.1f} 公里",
            steps=steps[:12],
            polyline=points,
        )

    def _parse_paths(self, payload: dict[str, Any], mode: TravelMode) -> RouteLeg:
        route = payload.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            return RouteLeg(mode=mode, ok=False, error="未找到可用路线", summary="未找到可用路线")
        path = paths[0]
        points: list[tuple[float, float]] = []
        steps: list[str] = []
        for step in path.get("steps") or []:
            points.extend(self._parse_polyline(step.get("polyline", "")))
            if step.get("instruction"):
                steps.append(str(step["instruction"]))
        distance = float(path.get("distance", 0) or 0)
        duration = float(path.get("duration", 0) or 0)
        return RouteLeg(
            mode=mode,
            ok=True,
            distance_m=distance,
            duration_s=duration,
            walk_m=distance if mode is TravelMode.WALKING else 0.0,
            summary=f"{'步行' if mode is TravelMode.WALKING else '驾车'} {distance / 1000:.1f} 公里 / 约 {duration / 60:.0f} 分钟",
            steps=steps[:10],
            polyline=points,
        )

    async def _city_of(self, lng: float, lat: float) -> str | None:
        try:
            info = await self.regeocode(lng, lat, radius=100)
            return self._normalize_city(info.get("citycode")) or self._normalize_city(info.get("adcode"))
        except AmapError:
            return None

    @staticmethod
    def _normalize_city(raw: str | None) -> str | None:
        """把 adcode / citycode 规范成高德公共交通接口可接受的 city 参数。

        * 6 位区县 adcode（120105）→ 市级 adcode（120100）；
        * citycode（022）原样使用；
        * 中文城市名容易触发 10021，返回 None 交给逆地理编码解析。
        """
        if not raw:
            return None
        text = str(raw).strip()
        if not text or not text.isdigit():
            return None
        if len(text) == 6:
            return text if text.endswith("00") else text[:4] + "00"
        return text

    # ------------------------------------------------------------------ #
    # 健康探测（单次轻量地理编码请求）
    # ------------------------------------------------------------------ #
    async def health_probe(self) -> HealthProbeResult:
        if not self.configured:
            return HealthProbeResult(ServiceState.NOT_CONFIGURED, 0, "未配置高德 Web 服务 Key")
        started = time.perf_counter()
        try:
            payload, status, latency = await self._transport.get_json(
                "amap_web",
                f"{self._base}/v3/geocode/geo",
                self._params(address="天安门", city="北京"),
                max_retries=0,
            )
            state, code, detail = classify_amap_payload(payload)
            return HealthProbeResult(
                state=state,
                latency_ms=latency,
                detail=detail,
                http_status=status,
                biz_code=code,
                error_type="" if state is ServiceState.OK else "BusinessError",
            )
        except Exception as exc:  # noqa: BLE001
            latency = int((time.perf_counter() - started) * 1000)
            from services.errors import classify_exception

            state, err_type, _ = classify_exception(exc)
            if isinstance(exc, AmapError):
                state = exc.state
                return HealthProbeResult(
                    state,
                    latency,
                    exc.message,
                    exc.http_status,
                    exc.biz_code,
                    type(exc).__name__,
                )
            return HealthProbeResult(state, latency, redact(str(exc))[:160], None, None, err_type)
