"""测试用例配置（case.json）的模型、校验与解析。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from geometry.regions import Region, RegionError, build_region
from models.schemas import RecMode, RegionMode, TravelMode, UserPoint, format_location, parse_location

__all__ = ["CaseConfig", "PlaceSpec", "RegionSpec", "DataSpec", "CaseError", "load_case"]


class CaseError(ValueError):
    def __init__(self, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class PlaceSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    lng: float | None = None
    lat: float | None = None
    address: str = ""
    adcode: str | None = None
    city: str | None = None
    source: str = "case"

    @field_validator("lng")
    @classmethod
    def _lng(cls, v: float | None) -> float | None:
        if v is not None and not (-180 <= v <= 180):
            raise ValueError("经度必须在 -180~180 之间")
        return v

    @field_validator("lat")
    @classmethod
    def _lat(cls, v: float | None) -> float | None:
        if v is not None and not (-90 <= v <= 90):
            raise ValueError("纬度必须在 -90~90 之间")
        return v

    @property
    def has_coords(self) -> bool:
        return self.lng is not None and self.lat is not None


class RegionSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["none", "circle", "polygon"] = "none"
    center: list[float] | None = None
    radius_m: float | None = None
    points: list[list[float]] | None = None

    def build(self) -> Region | None:
        try:
            if self.mode == "none":
                return build_region("none")
            if self.mode == "circle":
                if not self.center or len(self.center) != 2:
                    raise CaseError("INVALID_REGION", "圆形区域需要 center=[经度, 纬度]")
                if self.radius_m is None:
                    raise CaseError("INVALID_REGION", "圆形区域需要 radius_m")
                return build_region(
                    "circle", center=(self.center[0], self.center[1]), radius_m=self.radius_m
                )
            if not self.points:
                raise CaseError("INVALID_REGION", "多边形区域需要 points=[[lng,lat], ...]")
            return build_region("polygon", points=self.points)
        except RegionError as exc:
            raise CaseError("INVALID_REGION", str(exc)) from exc


class DataSpec(BaseModel):
    """数据来源：live=真实 API，mock=内置合成数据，replay=录制回放。"""

    model_config = ConfigDict(extra="ignore")

    mode: Literal["live", "mock", "replay"] = "mock"
    replay: str | None = None      # 录制文件或目录
    ai: Literal["recorded", "live", "mock"] | None = None
    record: str | None = None      # 录制输出目录


class BudgetSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_api_calls: int = Field(default=200, ge=1)
    max_tokens: int = Field(default=120_000, ge=0)
    timeout_s: float = Field(default=180.0, gt=0)
    max_cases: int = Field(default=20, ge=1)


class CaseConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = "case"
    a: PlaceSpec
    b: PlaceSpec
    travel_mode: TravelMode = TravelMode.TRANSIT
    travel_mode_b: TravelMode | None = None
    category: str = "任意类型"
    keyword: str = ""
    prefs: str = ""
    region: RegionSpec = Field(default_factory=RegionSpec)
    top_n: int = Field(default=5, ge=1, le=10)
    rec_mode: RecMode = RecMode.AI
    city_a: str | None = None
    city_b: str | None = None
    data: DataSpec = Field(default_factory=DataSpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    notes: str = ""

    @field_validator("a", "b", mode="before")
    @classmethod
    def _place_from_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            text = v.strip()
            try:
                lng, lat = parse_location(text)
                return {"name": text, "lng": lng, "lat": lat}
            except ValueError:
                return {"name": text}
        return v

    @field_validator("region", mode="before")
    @classmethod
    def _region_from_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"mode": v}
        return v

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "a": {"name": self.a.name, "lng": self.a.lng, "lat": self.a.lat},
            "b": {"name": self.b.name, "lng": self.b.lng, "lat": self.b.lat},
            "travel_mode": self.travel_mode.value,
            "category": self.category,
            "rec_mode": self.rec_mode.value,
            "top_n": self.top_n,
            "region": self.region.model_dump(),
            "data_mode": self.data.mode,
        }

    def user_points(self) -> tuple[UserPoint, UserPoint] | None:
        if not (self.a.has_coords and self.b.has_coords):
            return None
        return (
            UserPoint(
                lng=float(self.a.lng),
                lat=float(self.a.lat),
                name=self.a.name or format_location(float(self.a.lng), float(self.a.lat)),
                address=self.a.address,
                adcode=self.a.adcode,
                city=self.a.city,
                source="case",
            ),
            UserPoint(
                lng=float(self.b.lng),
                lat=float(self.b.lat),
                name=self.b.name or format_location(float(self.b.lng), float(self.b.lat)),
                address=self.b.address,
                adcode=self.b.adcode,
                city=self.b.city,
                source="case",
            ),
        )


def load_case(path: str | Path) -> CaseConfig:
    file = Path(path)
    if not file.is_file():
        raise CaseError("CASE_NOT_FOUND", f"找不到用例文件：{file}")
    try:
        raw = json.loads(file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise CaseError("CASE_INVALID_JSON", f"用例不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise CaseError("CASE_INVALID", "用例根节点必须是 JSON 对象")
    try:
        case = CaseConfig.model_validate(raw)
    except ValidationError as exc:
        messages = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]]
        raise CaseError("CASE_INVALID", "用例字段校验失败", "; ".join(messages)) from exc
    case.region.build()  # 提前暴露区域几何错误
    return case
