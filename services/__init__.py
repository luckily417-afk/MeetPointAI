"""服务层：高德 Web API、DeepSeek API、HTTP 基础设施。"""

from .amap_web import AMapWebService, GeocodeHit, HealthProbeResult, PoiPage
from .deepseek import DeepSeekService, LLMResult, LLMUsage
from .errors import (
    AmapError,
    DeepSeekError,
    NotConfiguredError,
    ServiceError,
    classify_amap_payload,
    classify_exception,
    classify_http_status,
)
from .http_client import HttpMetrics, HttpTransport, gather_limited, mask_url, redact

__all__ = [
    "AMapWebService",
    "DeepSeekService",
    "GeocodeHit",
    "HealthProbeResult",
    "HttpMetrics",
    "HttpTransport",
    "LLMResult",
    "LLMUsage",
    "PoiPage",
    "AmapError",
    "DeepSeekError",
    "NotConfiguredError",
    "ServiceError",
    "classify_amap_payload",
    "classify_exception",
    "classify_http_status",
    "gather_limited",
    "mask_url",
    "redact",
]
