"""常量：接口地址、场所类型编码、状态文案与配色。"""

from __future__ import annotations

from typing import Final

AMAP_REST_BASE: Final = "https://restapi.amap.com"
AMAP_JS_VERSION: Final = "2.0"
AMAP_JS_URL: Final = "https://webapi.amap.com/maps?v=2.0&key={key}&plugin=AMap.Scale,AMap.ToolBar,AMap.PlaceSearch,AMap.Geocoder,AMap.PolygonEditor,AMap.CircleEditor"

# 高德 POI 类型编码（官方分类码，可在请求中组合使用）
CATEGORY_PRESETS: Final[dict[str, dict[str, str]]] = {
    "任意类型": {"codes": "", "keywords": ""},
    "咖啡厅": {"codes": "050500", "keywords": "咖啡"},
    "餐厅": {"codes": "050100|050200|050300", "keywords": "餐厅"},
    "甜品/冷饮": {"codes": "050700|050800|050900", "keywords": "甜品"},
    "商场": {"codes": "060100", "keywords": "商场"},
    "便利店/超市": {"codes": "060200|060400", "keywords": "超市"},
    "公园": {"codes": "110101|110100", "keywords": "公园"},
    "电影院": {"codes": "080601|080600", "keywords": "电影院"},
    "图书馆/书店": {"codes": "140400|061000", "keywords": "图书馆"},
    "博物馆/展览": {"codes": "140100|140200|140300", "keywords": "博物馆"},
    "茶艺馆": {"codes": "050600", "keywords": "茶馆"},
    "运动场馆": {"codes": "080100", "keywords": "运动馆"},
}

DEFAULT_TOP_N: Final = 5
DEFAULT_MAX_CANDIDATES: Final = 20

TRAVEL_MODE_LABELS: Final[dict[str, str]] = {
    "transit": "公共交通",
    "driving": "驾车",
    "walking": "步行",
    "bicycling": "骑行",
}

REC_MODE_LABELS: Final[dict[str, str]] = {
    "ai": "AI 综合推荐",
    "total_time": "最短总通勤时间",
    "fewest_transfers": "最少换乘",
    "category_priority": "指定场所类型优先",
    "custom": "自然语言自定义需求",
}

REGION_MODE_LABELS: Final[dict[str, str]] = {
    "none": "不限制区域",
    "circle": "圆形区域",
    "polygon": "多边形区域",
}

STATE_LABELS: Final[dict[str, str]] = {
    "ok": "正常",
    "slow": "延迟较高",
    "connect_error": "连接失败",
    "timeout": "请求超时",
    "auth_error": "鉴权失败",
    "quota_limited": "配额受限",
    "service_error": "服务异常",
    "not_configured": "未配置",
    "unknown": "状态未知",
}

STATE_COLORS: Final[dict[str, str]] = {
    "ok": "#1a7f37",
    "slow": "#b8860b",
    "connect_error": "#c62828",
    "timeout": "#c62828",
    "auth_error": "#c62828",
    "quota_limited": "#b8860b",
    "service_error": "#c62828",
    "not_configured": "#757575",
    "unknown": "#757575",
}

SERVICE_LABELS: Final[dict[str, str]] = {
    "amap_web": "高德 Web 服务 API",
    "amap_js": "高德 JS API",
    "deepseek": "DeepSeek API",
}

SERVICE_ORDER: Final[tuple[str, ...]] = ("amap_web", "amap_js", "deepseek")

# 高德业务错误码：分类为 (状态, 说明)
AMAP_INFOCODES: Final[dict[str, tuple[str, str]]] = {
    "10001": ("auth_error", "INVALID_USER_KEY：Key 无效或未激活"),
    "10002": ("auth_error", "SERVICE_NOT_AVAILABLE：无此服务权限"),
    "10003": ("quota_limited", "DAILY_QUERY_OVER_LIMIT：日调用量超限"),
    "10004": ("quota_limited", "ACCESS_TOO_FREQUENT：访问过于频繁"),
    "10005": ("auth_error", "INVALID_USER_SCODE：安全密钥无效"),
    "10006": ("auth_error", "USERKEY_PLAT_NOMATCH：Key 与平台不匹配"),
    "10007": ("service_error", "NEED_HTTPS：需要 HTTPS"),
    "10008": ("service_error", "USER_KEY_QUERY_ERROR：Key 查询异常"),
    "10009": ("auth_error", "USERKEY_PLAT_NOMATCH：Key 与绑定平台不符"),
    "10010": ("service_error", "IP_QUERY_ERROR：IP 查询失败"),
    "10011": ("service_error", "INVALID_SERVER_IP：服务端 IP 无效"),
    "10012": ("auth_error", "INSUFFICIENT_PRIVILEGES：权限不足"),
    "10013": ("service_error", "SERVICE_NOT_AVAILABLE：服务不支持 HTTPS"),
    "10019": ("quota_limited", "USER_ABROAD：海外调用受限/配额"),
    "10020": ("service_error", "INVALID_PARAMS：参数错误"),
    "10021": ("auth_error", "INSUFFICIENT_PRIVILEGES：服务权限范围不足"),
    "10026": ("auth_error", "INVALID_USER_KEY：账号被禁用"),
    "10029": ("quota_limited", "ABROAD_DAILY_QUERY_OVER_LIMIT：海外日调用超限"),
    "10041": ("quota_limited", "USER_KEY_TRAFFIC_OVERLIMIT：QPS 超限"),
    "10044": ("quota_limited", "USER_DAILY_QUERY_OVER_LIMIT：账号日调用超限"),
    "20000": ("service_error", "SERVICE_NOT_AVAILABLE：服务不可用"),
    "20001": ("service_error", "MISSING_REQUIRED_PARAMS：缺少必要参数"),
    "20002": ("service_error", "INVALID_PARAMS：参数非法"),
    "20003": ("service_error", "UNKNOWN_ERROR：未知错误"),
    "20800": ("service_error", "INVALID_PARAMS：参数格式错误"),
    "20801": ("service_error", "INVALID_PARAMS：参数超出范围"),
    "20802": ("service_error", "INVALID_PARAMS：参数长度/格式错误"),
}

# 高德 POI 搜索接口可返回的最大 offset
AMAP_POI_MAX_OFFSET: Final = 25
# 高德周边搜索允许的最大半径（米）
AMAP_MAX_AROUND_RADIUS: Final = 50000
