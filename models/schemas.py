"""核心数据结构。"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class ServiceName(str, Enum):
    AMAP_WEB = "amap_web"
    AMAP_JS = "amap_js"
    DEEPSEEK = "deepseek"


class ServiceState(str, Enum):
    OK = "ok"
    SLOW = "slow"
    CONNECT_ERROR = "connect_error"
    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    QUOTA_LIMITED = "quota_limited"
    SERVICE_ERROR = "service_error"
    NOT_CONFIGURED = "not_configured"
    UNKNOWN = "unknown"

    @property
    def is_failure(self) -> bool:
        return self in {
            ServiceState.CONNECT_ERROR,
            ServiceState.TIMEOUT,
            ServiceState.AUTH_ERROR,
            ServiceState.QUOTA_LIMITED,
            ServiceState.SERVICE_ERROR,
        }

    @property
    def is_usable(self) -> bool:
        return self in {ServiceState.OK, ServiceState.SLOW}


class RegionMode(str, Enum):
    NONE = "none"
    CIRCLE = "circle"
    POLYGON = "polygon"


class TravelMode(str, Enum):
    TRANSIT = "transit"
    DRIVING = "driving"
    WALKING = "walking"
    BICYCLING = "bicycling"


class RecMode(str, Enum):
    AI = "ai"
    TOTAL_TIME = "total_time"
    FEWEST_TRANSFERS = "fewest_transfers"
    CATEGORY_PRIORITY = "category_priority"
    CUSTOM = "custom"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def parse_location(raw: str) -> tuple[float, float]:
    """解析 "lng,lat" 为浮点元组，非法时抛 ValueError。"""
    if not isinstance(raw, str):
        raise ValueError("坐标必须是字符串 '经度,纬度'")
    parts = [p.strip() for p in raw.replace("，", ",").split(",")]
    if len(parts) != 2:
        raise ValueError("坐标格式应为 '经度,纬度'")
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError as exc:  # noqa: TRY003
        raise ValueError("坐标必须为数字") from exc
    if not (math.isfinite(lng) and math.isfinite(lat)):
        raise ValueError("坐标必须为有限数值")
    if not (-180.0 <= lng <= 180.0):
        raise ValueError("经度必须在 -180 ~ 180 之间")
    if not (-90.0 <= lat <= 90.0):
        raise ValueError("纬度必须在 -90 ~ 90 之间")
    return lng, lat


def format_location(lng: float, lat: float, digits: int = 6) -> str:
    return f"{lng:.{digits}f},{lat:.{digits}f}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 地点 / 路线
# --------------------------------------------------------------------------- #
class UserPoint(BaseModel):
    """A/B 出发位置。坐标统一为 GCJ-02（高德坐标系）。"""

    model_config = ConfigDict(extra="ignore")

    lng: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)
    name: str = ""
    address: str = ""
    poi_id: str | None = None
    adcode: str | None = None
    city: str | None = None
    source: str = "manual"  # manual | search | map_click | coord

    @property
    def location(self) -> str:
        return format_location(self.lng, self.lat)

    @property
    def display(self) -> str:
        return self.name or self.address or self.location

    def to_map_dict(self) -> dict[str, Any]:
        return {
            "lng": self.lng,
            "lat": self.lat,
            "name": self.display,
            "address": self.address,
        }


class Poi(BaseModel):
    """来自高德的真实 POI。禁止人工构造用于正式推荐。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    lng: float
    lat: float
    address: str = ""
    type_name: str = ""
    type_code: str = ""
    tel: str = ""
    pname: str = ""
    cityname: str = ""
    adname: str = ""
    business_area: str = ""
    adcode: str = ""
    distance_m: float | None = None
    rating: float | None = None
    cost: float | None = None
    source: str = "amap"

    @property
    def location(self) -> str:
        return format_location(self.lng, self.lat)

    @model_validator(mode="after")
    def _check(self) -> "Poi":
        if not self.id:
            raise ValueError("POI 缺少 id")
        if not self.name:
            raise ValueError("POI 缺少名称")
        return self


class RouteLeg(BaseModel):
    """单侧通勤路线（高德实时规划结果）。"""

    model_config = ConfigDict(extra="ignore")

    mode: TravelMode = TravelMode.TRANSIT
    ok: bool = False
    distance_m: float = 0.0
    duration_s: float = 0.0
    transfers: int = 0
    walk_m: float = 0.0
    cost: float = 0.0
    summary: str = ""
    steps: list[str] = Field(default_factory=list)
    polyline: list[tuple[float, float]] = Field(default_factory=list)
    error: str = ""
    cached: bool = False
    cached_at: datetime | None = None

    @property
    def duration_min(self) -> float:
        return round(self.duration_s / 60.0, 1)

    @property
    def distance_km(self) -> float:
        return round(self.distance_m / 1000.0, 2)

    @property
    def text(self) -> str:
        if not self.ok:
            return "路线不可达"
        return f"{self.duration_min:.0f} 分钟 / {self.distance_km:.1f} 公里"


class Candidate(BaseModel):
    """通过几何约束与路线校验的候选地点。"""

    model_config = ConfigDict(extra="ignore")

    poi: Poi
    leg_a: RouteLeg | None = None
    leg_b: RouteLeg | None = None
    score: float = 0.0
    tags: list[str] = Field(default_factory=list)
    reason: str = ""
    ai_rank: int | None = None
    distance_to_mid_m: float = 0.0

    @property
    def id(self) -> str:
        return self.poi.id

    @property
    def total_duration_s(self) -> float:
        total = 0.0
        for leg in (self.leg_a, self.leg_b):
            if leg and leg.ok:
                total += leg.duration_s
        return total

    @property
    def total_transfers(self) -> int:
        return sum(leg.transfers for leg in (self.leg_a, self.leg_b) if leg and leg.ok)

    @property
    def routes_ok(self) -> bool:
        return bool(self.leg_a and self.leg_a.ok and self.leg_b and self.leg_b.ok)


class Recommendation(BaseModel):
    """最终对外展示的推荐结果。"""

    model_config = ConfigDict(extra="ignore")

    rank: int
    poi: Poi
    leg_a: RouteLeg | None = None
    leg_b: RouteLeg | None = None
    reason: str = ""
    source: str = "ai"  # ai | rule | cache
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0

    @property
    def name(self) -> str:
        return self.poi.name


# --------------------------------------------------------------------------- #
# AI 输出
# --------------------------------------------------------------------------- #
class AIItem(BaseModel):
    """DeepSeek 单条推荐。只允许引用候选集合内已存在的 poi_id。"""

    model_config = ConfigDict(extra="ignore")

    poi_id: str
    rank: int = 1
    reason: str = ""
    highlights: list[str] = Field(default_factory=list)

    @field_validator("poi_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("poi_id 不能为空")
        return value

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, value: str) -> str:
        return (value or "").strip()[:400]


class AIResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    items: list[AIItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dedupe(self) -> "AIResponse":
        seen: set[str] = set()
        unique: list[AIItem] = []
        for item in self.items:
            if item.poi_id in seen:
                continue
            seen.add(item.poi_id)
            unique.append(item)
        self.items = unique
        return self


# --------------------------------------------------------------------------- #
# 健康状态
# --------------------------------------------------------------------------- #
class HealthSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: ServiceName
    state: ServiceState = ServiceState.UNKNOWN
    last_check_at: datetime | None = None
    last_success_at: datetime | None = None
    latency_ms: int | None = None
    http_status: int | None = None
    biz_code: str | None = None
    error_type: str = ""
    detail: str = ""
    retries: int = 0
    consecutive_failures: int = 0

    @property
    def usable(self) -> bool:
        return self.state.is_usable
