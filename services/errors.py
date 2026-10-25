"""服务层：异常类型 + 统一错误分类。

分类原则（对应需求 7.1）：
    正常 / 延迟较高 / 连接失败 / 请求超时 / 鉴权失败 /
    配额受限 / 服务异常 / 未配置 / 状态未知
必须区分 DNS/TCP/TLS 异常、超时、HTTP 状态码、业务错误码，并把 HTTP 200 但业务失败的
情况单独识别出来。
"""

from __future__ import annotations

import socket
import ssl
from typing import Any

import httpx

from config.constants import AMAP_INFOCODES
from models.schemas import ServiceName, ServiceState

__all__ = [
    "ServiceError",
    "NotConfiguredError",
    "AmapError",
    "DeepSeekError",
    "classify_http_status",
    "classify_exception",
    "classify_amap_payload",
    "classify_deepseek_payload",
    "is_auth_error",
]


class ServiceError(Exception):
    """业务异常基类，携带健康状态分类信息。"""

    def __init__(
        self,
        message: str,
        *,
        service: ServiceName,
        state: ServiceState,
        http_status: int | None = None,
        biz_code: str | None = None,
        retryable: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.service = service
        self.state = state
        self.http_status = http_status
        self.biz_code = biz_code
        self.retryable = retryable
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 展示用
        extra = []
        if self.http_status:
            extra.append(f"HTTP {self.http_status}")
        if self.biz_code:
            extra.append(f"code {self.biz_code}")
        suffix = f"（{' / '.join(extra)}）" if extra else ""
        return f"{self.message}{suffix}"


class NotConfiguredError(ServiceError):
    def __init__(self, service: ServiceName, message: str = "未配置 API Key") -> None:
        super().__init__(
            message, service=service, state=ServiceState.NOT_CONFIGURED, retryable=False
        )


class AmapError(ServiceError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("service", ServiceName.AMAP_WEB)
        super().__init__(message, **kwargs)


class DeepSeekError(ServiceError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("service", ServiceName.DEEPSEEK)
        super().__init__(message, **kwargs)


def classify_http_status(status: int) -> tuple[ServiceState, bool]:
    """返回 (状态, 是否可重试)。"""
    if 200 <= status < 300:
        return ServiceState.OK, False
    if status in (401, 403):
        return ServiceState.AUTH_ERROR, False
    if status == 402:
        return ServiceState.QUOTA_LIMITED, False
    if status == 404:
        return ServiceState.SERVICE_ERROR, False
    if status == 408:
        return ServiceState.TIMEOUT, True
    if status == 429:
        return ServiceState.QUOTA_LIMITED, True
    if 400 <= status < 500:
        return ServiceState.SERVICE_ERROR, False
    if status >= 500:
        return ServiceState.SERVICE_ERROR, True
    return ServiceState.SERVICE_ERROR, False


def classify_exception(exc: BaseException) -> tuple[ServiceState, str, bool]:
    """把底层异常映射为 (状态, 错误类型, 是否可重试)。"""
    if isinstance(exc, ServiceError):
        return exc.state, type(exc).__name__, exc.retryable
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return ServiceState.TIMEOUT, type(exc).__name__, True
    if isinstance(exc, httpx.TimeoutException):
        return ServiceState.TIMEOUT, type(exc).__name__, True
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.ProxyError)):
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, socket.gaierror):
            return ServiceState.CONNECT_ERROR, "DNS 解析失败", True
        if isinstance(cause, ssl.SSLError) or isinstance(exc, httpx.ProxyError):
            return ServiceState.CONNECT_ERROR, "TLS/代理握手失败", True
        return ServiceState.CONNECT_ERROR, "网络连接失败", True
    if isinstance(exc, (httpx.TooManyRedirects, httpx.DecodingError)):
        return ServiceState.SERVICE_ERROR, type(exc).__name__, False
    if isinstance(exc, ssl.SSLError):
        return ServiceState.CONNECT_ERROR, "TLS 握手失败", True
    if isinstance(exc, socket.gaierror):
        return ServiceState.CONNECT_ERROR, "DNS 解析失败", True
    if isinstance(exc, (ConnectionError, OSError)):
        return ServiceState.CONNECT_ERROR, type(exc).__name__, True
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return ServiceState.SERVICE_ERROR, "响应结构无效", False
    return ServiceState.SERVICE_ERROR, type(exc).__name__, False


def classify_amap_payload(payload: dict[str, Any]) -> tuple[ServiceState, str, str]:
    """解析高德业务返回，返回 (状态, 业务码, 说明)。

    高德以 status=1 表示成功；HTTP 200 但 status=0 属于业务失败。
    """
    status = str(payload.get("status", "")).strip()
    info = str(payload.get("info", "")).strip()
    infocode = str(payload.get("infocode", "")).strip()
    if status == "1":
        return ServiceState.OK, infocode, info or "OK"
    if status == "0":
        mapped = AMAP_INFOCODES.get(infocode)
        if mapped:
            state_str, desc = mapped
            return ServiceState(state_str), infocode, desc
        lowered = info.lower()
        if "key" in lowered or "user" in lowered or "privilege" in lowered:
            return ServiceState.AUTH_ERROR, infocode, info
        if "limit" in lowered or "frequent" in lowered or "abroad" in lowered:
            return ServiceState.QUOTA_LIMITED, infocode, info
        return ServiceState.SERVICE_ERROR, infocode, info or "高德返回未知错误"
    # 完全不符合预期的结构
    return ServiceState.SERVICE_ERROR, infocode, "响应结构不符合高德接口规范"


def classify_deepseek_payload(payload: dict[str, Any], http_status: int) -> tuple[ServiceState, str, str]:
    """DeepSeek 错误体：{"error": {"message": ..., "type": ..., "code": ...}}"""
    if 200 <= http_status < 300:
        return ServiceState.OK, "", "OK"
    state, _retryable = classify_http_status(http_status)
    err = payload.get("error") if isinstance(payload, dict) else None
    message, code = "", ""
    if isinstance(err, dict):
        message = str(err.get("message", ""))[:200]
        code = str(err.get("code") or err.get("type") or "")
    return state, code, message or f"HTTP {http_status}"


def is_auth_error(state: ServiceState) -> bool:
    return state in {ServiceState.AUTH_ERROR, ServiceState.NOT_CONFIGURED}
