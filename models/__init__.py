"""数据模型与枚举（Pydantic v2）。

所有外部数据（高德 / DeepSeek / 用户输入）进入业务层前必须先通过这里定义的模型校验。
"""

from .schemas import (
    AIItem,
    AIResponse,
    Candidate,
    HealthSnapshot,
    Poi,
    Recommendation,
    RegionMode,
    RecMode,
    RouteLeg,
    ServiceName,
    ServiceState,
    TravelMode,
    UserPoint,
    format_location,
    parse_location,
)

__all__ = [
    "AIItem",
    "AIResponse",
    "Candidate",
    "HealthSnapshot",
    "Poi",
    "Recommendation",
    "RegionMode",
    "RecMode",
    "RouteLeg",
    "ServiceName",
    "ServiceState",
    "TravelMode",
    "UserPoint",
    "format_location",
    "parse_location",
]
