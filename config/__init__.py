"""MeetPoint AI 配置包。"""

from .settings import Settings, get_settings, reset_settings
from .constants import (
    AMAP_REST_BASE,
    AMAP_JS_VERSION,
    AMAP_JS_URL,
    CATEGORY_PRESETS,
    DEFAULT_TOP_N,
    DEFAULT_MAX_CANDIDATES,
    TRAVEL_MODE_LABELS,
    REC_MODE_LABELS,
    STATE_COLORS,
    STATE_LABELS,
)

__all__ = [
    "Settings",
    "get_settings",
    "reset_settings",
    "AMAP_REST_BASE",
    "AMAP_JS_VERSION",
    "AMAP_JS_URL",
    "CATEGORY_PRESETS",
    "DEFAULT_TOP_N",
    "DEFAULT_MAX_CANDIDATES",
    "TRAVEL_MODE_LABELS",
    "REC_MODE_LABELS",
    "STATE_COLORS",
    "STATE_LABELS",
]
