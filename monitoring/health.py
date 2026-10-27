"""API 健康监测：三路服务独立状态机 + 指数退避 + 抖动 + 故障恢复验证。

设计要点（需求七）：
* 三个服务（高德 Web / 高德 JS / DeepSeek）完全独立，互不覆盖状态；
* HTTP 200 不等于业务成功 —— 由各自探针返回业务分类后的状态；
* 业务真实请求成功/失败也会回写状态（复用真实调用，减少探测配额）；
* 连续失败后进入退避，恢复时用一次真实探测重新验证；
* 日志不含任何 Key。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from config.constants import SERVICE_ORDER
from models.schemas import HealthSnapshot, ServiceName, ServiceState
from storage.db import Database

__all__ = ["HealthMonitor", "HealthEvent"]

ProbeFn = Callable[[], Awaitable[dict[str, Any]]]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class HealthEvent:
    service: str
    state: ServiceState
    latency_ms: int | None
    http_status: int | None
    biz_code: str | None
    error_type: str
    detail: str
    retries: int
    recovered: bool = False
    ts: float = field(default_factory=time.time)


@dataclass
class _ServiceSlot:
    name: str
    probe: ProbeFn | None = None
    configured: bool = True
    snapshot: HealthSnapshot = field(
        default_factory=lambda: HealthSnapshot(service=ServiceName.AMAP_WEB)
    )
    next_check_at: float = 0.0
    failures: int = 0
    last_failure_at: float | None = None


class HealthMonitor:
    """纯 Python（不依赖 Qt），便于单元测试。"""

    def __init__(
        self,
        db: Database | None = None,
        *,
        latency_warn_ms: int = 2500,
        interval_s: int = 60,
        backoff_base_s: float = 5.0,
        backoff_cap_s: float = 300.0,
    ) -> None:
        self._slots: dict[str, _ServiceSlot] = {}
        self._db = db
        self.latency_warn_ms = latency_warn_ms
        self.interval_s = interval_s
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self._listeners: list[Callable[[str, HealthSnapshot], None]] = []
        self._event_listeners: list[Callable[[HealthEvent], None]] = []
        self._lock = asyncio.Lock()
        # 各服务的「高延迟」阈值（DeepSeek 推理耗时天然更长）
        self._thresholds: dict[str, int] = {"deepseek": 60000}

    def set_latency_threshold(self, name: str, ms: int) -> None:
        self._thresholds[name] = max(100, int(ms))

    def latency_threshold(self, name: str) -> int:
        return self._thresholds.get(name, self.latency_warn_ms)

    # ------------------------------------------------------------------ #
    def register(self, name: str, probe: ProbeFn | None, *, configured: bool = True) -> None:
        slot = self._slots.get(name) or _ServiceSlot(name=name)
        slot.probe = probe
        slot.configured = configured
        slot.snapshot = HealthSnapshot(service=ServiceName(name))
        if not configured:
            slot.snapshot.state = ServiceState.NOT_CONFIGURED
            slot.snapshot.detail = "未配置 API Key"
        self._slots[name] = slot

    def set_configured(self, name: str, configured: bool) -> None:
        slot = self._slots.get(name)
        if not slot:
            return
        slot.configured = configured
        if not configured:
            slot.snapshot.state = ServiceState.NOT_CONFIGURED
            slot.snapshot.detail = "未配置 API Key"
            self._emit(name)

    def add_listener(self, callback: Callable[[str, HealthSnapshot], None]) -> None:
        self._listeners.append(callback)

    def add_event_listener(self, callback: Callable[[HealthEvent], None]) -> None:
        self._event_listeners.append(callback)

    # ------------------------------------------------------------------ #
    def snapshot(self, name: str) -> HealthSnapshot:
        slot = self._slots.get(name)
        if slot is None:
            return HealthSnapshot(service=ServiceName(name))
        return slot.snapshot.model_copy(deep=True)

    def all_snapshots(self) -> dict[str, HealthSnapshot]:
        return {name: self.snapshot(name) for name in self._slots}

    def state_of(self, name: str) -> ServiceState:
        return self.snapshot(name).state

    def usable(self, name: str) -> bool:
        return self.state_of(name).is_usable

    def any_outage(self) -> bool:
        return any(s.state.is_failure for s in self.all_snapshots().values())

    # ------------------------------------------------------------------ #
    # 结果写入
    # ------------------------------------------------------------------ #
    def _emit(self, name: str) -> None:
        snap = self.snapshot(name)
        for cb in list(self._listeners):
            try:
                cb(name, snap)
            except Exception:  # noqa: BLE001 - 监听器异常不影响主流程
                pass

    def _apply(
        self,
        name: str,
        state: ServiceState,
        *,
        latency_ms: int | None,
        http_status: int | None = None,
        biz_code: str | None = None,
        error_type: str = "",
        detail: str = "",
        retries: int = 0,
    ) -> HealthSnapshot:
        slot = self._slots.get(name)
        if slot is None:
            slot = self._slots[name] = _ServiceSlot(name=name)
        snap = slot.snapshot
        now = _now_utc()
        snap.last_check_at = now
        snap.latency_ms = latency_ms
        snap.http_status = http_status
        snap.biz_code = biz_code
        snap.error_type = error_type
        snap.detail = detail[:300]
        snap.retries = retries

        if state is ServiceState.OK and latency_ms is not None and latency_ms > self.latency_threshold(name):
            state = ServiceState.SLOW

        previously_ok = snap.state.is_usable
        recovered = False
        if state.is_usable:
            snap.last_success_at = now
            if slot.failures > 0:
                recovered = True
            slot.failures = 0
            slot.next_check_at = time.time() + max(0, self.interval_s)
        elif state is ServiceState.NOT_CONFIGURED:
            slot.next_check_at = float("inf")
        else:
            slot.failures += 1
            slot.last_failure_at = time.time()
            delay = min(self.backoff_cap_s, self.backoff_base_s * (2 ** (slot.failures - 1)))
            delay *= 0.8 + random.random() * 0.5  # 抖动
            slot.next_check_at = time.time() + delay

        snap.state = state
        snap.consecutive_failures = slot.failures
        self._emit(name)

        event = HealthEvent(
            service=name,
            state=state,
            latency_ms=latency_ms,
            http_status=http_status,
            biz_code=biz_code,
            error_type=error_type,
            detail=detail,
            retries=retries,
            recovered=recovered and previously_ok is False,
        )
        if self._db is not None:
            try:
                self._db.log_health(
                    name,
                    state.value,
                    latency_ms=latency_ms,
                    http_status=http_status,
                    biz_code=biz_code,
                    error_type=error_type,
                    detail=detail,
                    retries=retries,
                    recovered_at=time.time() if event.recovered else None,
                )
            except Exception:  # noqa: BLE001 - 日志失败不影响业务
                pass
        for cb in list(self._event_listeners):
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                pass
        return snap

    # ------------------------------------------------------------------ #
    # 探针
    # ------------------------------------------------------------------ #
    async def check(self, name: str) -> HealthSnapshot:
        slot = self._slots.get(name)
        if slot is None:
            return HealthSnapshot(service=ServiceName(name))
        if not slot.configured:
            return self._apply(
                name,
                ServiceState.NOT_CONFIGURED,
                latency_ms=None,
                detail="未配置 API Key",
            )
        if slot.probe is None:
            # 没有探针的服务（如 JS API 只能在界面里验证）不能一直"到期"，
            # 否则 check_due() 每一轮都会把它当成待检查项。
            slot.next_check_at = time.time() + max(0, self.interval_s)
            return self.snapshot(name)
        started = time.perf_counter()
        try:
            result = await slot.probe()
        except Exception as exc:  # noqa: BLE001
            from services.errors import classify_exception

            state, err_type, _ = classify_exception(exc)
            latency = int((time.perf_counter() - started) * 1000)
            return self._apply(
                name, state, latency_ms=latency, error_type=err_type, detail=str(exc)[:200]
            )
        latency = int((time.perf_counter() - started) * 1000)
        payload = self._normalize(result, latency)
        state = payload.get("state") or ServiceState.UNKNOWN
        if isinstance(state, str):
            state = ServiceState(state)
        return self._apply(
            name,
            state,
            latency_ms=payload.get("latency_ms"),
            http_status=payload.get("http_status"),
            biz_code=payload.get("biz_code"),
            error_type=str(payload.get("error_type", "")),
            detail=str(payload.get("detail", "")),
        )

    @staticmethod
    def _normalize(result: Any, elapsed_ms: int) -> dict[str, Any]:
        """探针可返回 dataclass（如 HealthProbeResult）或 dict，这里统一成 dict。"""
        if result is None:
            return {"state": ServiceState.UNKNOWN, "latency_ms": elapsed_ms}
        if isinstance(result, dict):
            payload = dict(result)
        elif hasattr(result, "__dataclass_fields__"):
            import dataclasses

            payload = dataclasses.asdict(result)
        else:
            payload = {"state": ServiceState.UNKNOWN, "detail": str(result)[:160]}
        if not payload.get("latency_ms"):
            payload["latency_ms"] = elapsed_ms
        return payload

    async def check_all(self, names: list[str] | None = None) -> dict[str, HealthSnapshot]:
        targets = names or [n for n in self._slots if n in SERVICE_ORDER]
        if not targets:
            return {}
        results = await asyncio.gather(*(self.check(n) for n in targets), return_exceptions=True)
        out: dict[str, HealthSnapshot] = {}
        for name, res in zip(targets, results):
            out[name] = res if isinstance(res, HealthSnapshot) else self.snapshot(name)
        return out

    async def check_due(self) -> list[str]:
        """周期性调度：只检查到期的服务，避免故障期重复打点。"""
        now = time.time()
        due = [
            name
            for name, slot in self._slots.items()
            if name in SERVICE_ORDER and slot.configured and now >= slot.next_check_at
        ]
        if due:
            await self.check_all(due)
        return due

    # ------------------------------------------------------------------ #
    # 业务请求回写状态（复用真实调用）
    # ------------------------------------------------------------------ #
    def note_success(self, name: str, latency_ms: int, *, detail: str = "") -> None:
        if name not in self._slots:
            return
        if not self._slots[name].configured:
            return
        self._apply(name, ServiceState.OK, latency_ms=latency_ms, detail=detail)

    def note_failure(
        self,
        name: str,
        state: ServiceState,
        *,
        latency_ms: int | None = None,
        http_status: int | None = None,
        biz_code: str | None = None,
        error_type: str = "",
        detail: str = "",
    ) -> None:
        if name not in self._slots:
            return
        self._apply(
            name,
            state,
            latency_ms=latency_ms,
            http_status=http_status,
            biz_code=biz_code,
            error_type=error_type,
            detail=detail,
        )

    def note_js_status(self, ok: bool, detail: str = "", latency_ms: int | None = None) -> None:
        """由 QWebEngineView 内的 JS 回报结果驱动。"""
        if ok:
            self._apply(
                "amap_js",
                ServiceState.OK,
                latency_ms=latency_ms if latency_ms else None,
                detail=detail or "地图已初始化",
            )
        else:
            self._apply(
                "amap_js",
                ServiceState.SERVICE_ERROR,
                latency_ms=latency_ms,
                error_type="JavaScriptError",
                detail=detail or "地图脚本初始化失败",
            )

    # ------------------------------------------------------------------ #
    def reset(self, name: str | None = None) -> None:
        names = [name] if name else list(self._slots)
        for item in names:
            slot = self._slots.get(item)
            if not slot:
                continue
            slot.failures = 0
            slot.next_check_at = 0.0
            slot.snapshot = HealthSnapshot(service=ServiceName(item))
            if not slot.configured:
                slot.snapshot.state = ServiceState.NOT_CONFIGURED
                slot.snapshot.detail = "未配置 API Key"
            self._emit(item)
