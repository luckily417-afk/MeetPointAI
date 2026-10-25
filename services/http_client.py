"""HTTP 基础设施：连接池、超时、重试（指数退避 + 抖动）、调用量统计。

* 所有 URL 在日志/异常中都会被脱敏（剥离 key / token 参数）。
* 重试仅针对可重试错误（网络异常、超时、429、5xx）。
* 统计信息用于 GUI 的「API 调用量」展示。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

import httpx

from models.schemas import ServiceState
from services.errors import ServiceError, classify_exception

__all__ = ["HttpMetrics", "HttpTransport", "mask_url", "redact"]

_SECRET_PARAMS = re.compile(r"(key|token|api_key|authorization|secret)=([^&]+)", re.IGNORECASE)


def redact(text: str) -> str:
    """脱敏：把 URL 中的 key/token 以及 sk- 开头的凭据替换掉。"""
    if not text:
        return text
    text = _SECRET_PARAMS.sub(lambda m: f"{m.group(1)}=***", text)
    return re.sub(r"sk-[A-Za-z0-9]{8,}", "sk-***", text)


def mask_url(url: str) -> str:
    base, _, query = url.partition("?")
    if not query:
        return url
    pairs = []
    for part in query.split("&"):
        name = part.split("=", 1)[0]
        if name.lower() in {"key", "token", "api_key", "secret", "authorization"}:
            pairs.append(f"{name}=***")
        else:
            pairs.append(part)
    return f"{base}?{'&'.join(pairs)}"


@dataclass
class HttpMetrics:
    """按服务统计调用情况。"""

    calls: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    bytes_total: int = 0
    last_latency_ms: dict[str, int] = field(default_factory=dict)

    def record(self, service: str, latency_ms: int, ok: bool, size: int = 0) -> None:
        self.calls[service] = self.calls.get(service, 0) + 1
        self.bytes_total += size
        self.last_latency_ms[service] = latency_ms
        if not ok:
            self.failures[service] = self.failures.get(service, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "failures": dict(self.failures),
            "bytes": self.bytes_total,
            "last_latency_ms": dict(self.last_latency_ms),
        }


class HttpTransport:
    """对 httpx.AsyncClient 的薄封装，供各服务复用单个连接池。"""

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        connect_timeout: float = 5.0,
        default_headers: Mapping[str, str] | None = None,
        metrics: HttpMetrics | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._headers = dict(default_headers or {})
        self.metrics = metrics or HttpMetrics()
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=self._timeout,
                    headers=self._headers,
                    follow_redirects=True,
                    http2=False,  # 高德部分节点对 h2 支持不稳定
                    limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
                )
        return self._client

    async def aclose(self) -> None:
        async with self._lock:
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

    async def get_json(
        self,
        service: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        max_retries: int = 2,
        backoff_base: float = 0.6,
        validate: Callable[[dict[str, Any], int], None] | None = None,
    ) -> tuple[dict[str, Any], int, int]:
        """GET 并解析 JSON，返回 (payload, http_status, latency_ms)。

        `validate(payload, status)` 抛 ServiceError 时按 retryable 决定是否重试。
        """
        client = await self.client()
        attempt = 0
        last_exc: BaseException | None = None
        while attempt <= max_retries:
            started = time.perf_counter()
            try:
                response = await client.get(url, params=params, headers=headers)
                latency_ms = int((time.perf_counter() - started) * 1000)
                size = len(response.content)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                    if response.status_code < 400:
                        self.metrics.record(service, latency_ms, False, size)
                        raise ServiceError(
                            "响应不是合法 JSON",
                            service=_service_enum(service),
                            state=ServiceState.SERVICE_ERROR,
                            http_status=response.status_code,
                            detail=redact(response.text[:200]),
                        ) from None
                if validate is not None:
                    validate(payload, response.status_code)
                ok = response.status_code < 400
                self.metrics.record(service, latency_ms, ok, size)
                return payload, response.status_code, latency_ms
            except ServiceError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.metrics.record(service, latency_ms, False)
                if not exc.retryable or attempt >= max_retries:
                    raise
                last_exc = exc
            except BaseException as exc:  # noqa: BLE001 - 统一分类
                latency_ms = int((time.perf_counter() - started) * 1000)
                state, err_type, retryable = classify_exception(exc)
                self.metrics.record(service, latency_ms, False)
                if not retryable or attempt >= max_retries:
                    raise ServiceError(
                        f"{err_type}: {redact(str(exc))[:180]}",
                        service=_service_enum(service),
                        state=state,
                        retryable=retryable,
                        detail=redact(repr(exc))[:300],
                    ) from exc
                last_exc = exc
            # 指数退避 + 抖动
            delay = min(8.0, backoff_base * (2**attempt)) * (0.7 + random.random() * 0.6)
            attempt += 1
            await asyncio.sleep(delay)
        raise ServiceError(
            f"请求失败：{redact(str(last_exc))[:180]}",
            service=_service_enum(service),
            state=ServiceState.SERVICE_ERROR,
        )

    async def post_json(
        self,
        service: str,
        url: str,
        *,
        json_body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        max_retries: int = 1,
        backoff_base: float = 0.8,
        validate: Callable[[dict[str, Any], int], None] | None = None,
        timeout: float | None = None,
    ) -> tuple[dict[str, Any], int, int]:
        client = await self.client()
        attempt = 0
        last_exc: BaseException | None = None
        while attempt <= max_retries:
            started = time.perf_counter()
            try:
                response = await client.post(
                    url,
                    json=dict(json_body),
                    headers=headers,
                    timeout=timeout if timeout is not None else self._timeout,
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                size = len(response.content)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if validate is not None:
                    validate(payload, response.status_code)
                self.metrics.record(service, latency_ms, response.status_code < 400, size)
                return payload, response.status_code, latency_ms
            except ServiceError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.metrics.record(service, latency_ms, False)
                if not exc.retryable or attempt >= max_retries:
                    raise
                last_exc = exc
            except BaseException as exc:  # noqa: BLE001
                latency_ms = int((time.perf_counter() - started) * 1000)
                state, err_type, retryable = classify_exception(exc)
                self.metrics.record(service, latency_ms, False)
                if not retryable or attempt >= max_retries:
                    raise ServiceError(
                        f"{err_type}: {redact(str(exc))[:180]}",
                        service=_service_enum(service),
                        state=state,
                        retryable=retryable,
                        detail=redact(repr(exc))[:300],
                    ) from exc
                last_exc = exc
            delay = min(8.0, backoff_base * (2**attempt)) * (0.7 + random.random() * 0.6)
            attempt += 1
            await asyncio.sleep(delay)
        raise ServiceError(
            f"请求失败：{redact(str(last_exc))[:180]}",
            service=_service_enum(service),
            state=ServiceState.SERVICE_ERROR,
        )


def _service_enum(name: str):
    from models.schemas import ServiceName

    mapping = {
        "amap_web": ServiceName.AMAP_WEB,
        "amap_js": ServiceName.AMAP_JS,
        "deepseek": ServiceName.DEEPSEEK,
    }
    return mapping.get(name, ServiceName.AMAP_WEB)


async def gather_limited(
    tasks: list[Awaitable[Any]],
    limit: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Any]:
    """限并发执行；单任务异常返回异常对象，不中断整体。"""
    semaphore = asyncio.Semaphore(max(1, limit))
    results: list[Any] = [None] * len(tasks)
    done = 0
    lock = asyncio.Lock()

    async def run(index: int, coro: Awaitable[Any]) -> None:
        nonlocal done
        async with semaphore:
            try:
                results[index] = await coro
            except BaseException as exc:  # noqa: BLE001
                results[index] = exc
            finally:
                async with lock:
                    done += 1
                    if on_progress:
                        on_progress(done, len(tasks))

    await asyncio.gather(*(run(i, t) for i, t in enumerate(tasks)))
    return results
