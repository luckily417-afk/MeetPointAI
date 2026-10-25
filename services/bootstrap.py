"""服务装配：把 HTTP 连接池、高德、DeepSeek、健康监测一次性构建出来。

单独成模块，供 `main.py`（启动）与 `ui/settings_dialog.py`（运行时改配置后重建）复用。
"""

from __future__ import annotations

from config.settings import Settings
from monitoring.health import HealthMonitor
from services.amap_web import AMapWebService
from services.deepseek import DeepSeekService
from services.http_client import HttpMetrics, HttpTransport
from storage.db import Database

__all__ = ["build_services"]


def build_services(
    settings: Settings, db: Database
) -> tuple[HttpTransport, AMapWebService, DeepSeekService, HealthMonitor]:
    metrics = HttpMetrics()
    transport = HttpTransport(
        timeout=settings.http_timeout,
        connect_timeout=settings.http_connect_timeout,
        default_headers={"User-Agent": "MeetPointAI/1.0 (+PySide6)"},
        metrics=metrics,
    )
    amap = AMapWebService(settings.amap_web_key, transport, cache=db)
    deepseek = DeepSeekService(
        settings.deepseek_api_key,
        transport,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        cache=db,
        llm_timeout=settings.llm_timeout,
        effort=settings.llm_effort,
    )
    monitor = HealthMonitor(
        db,
        latency_warn_ms=settings.latency_warn_ms,
        interval_s=settings.health_interval_s,
        backoff_base_s=settings.backoff_base_s,
        backoff_cap_s=settings.backoff_cap_s,
    )
    monitor.register("amap_web", amap.health_probe, configured=settings.amap_web_configured)
    monitor.register("amap_js", None, configured=settings.amap_js_configured)
    monitor.register("deepseek", deepseek.health_probe, configured=settings.deepseek_configured)
    monitor.set_latency_threshold("deepseek", settings.llm_latency_warn_ms)
    return transport, amap, deepseek, monitor
