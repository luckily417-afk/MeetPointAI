"""Qt <-> 高德 JS API 桥接。

* `MapBridge` 是注入到网页的 QObject，页面通过 QWebChannel 回传事件；
* `MapController` 封装 QWebEngineView 的加载与 runJavaScript 调用；
* 页面加载失败 / JS 异常 / 初始化超时都会被上报，用于「高德 JS API 健康状态」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from config.settings import Settings, resource_root

__all__ = ["MapBridge", "MapController"]


class MapBridge(QObject):
    ready = Signal(dict)
    failed = Signal(str)
    point_picked = Signal(str, float, float, str)
    circle_changed = Signal(float, float, float)
    polygon_changed = Signal(list, bool)
    polygon_closed = Signal(list)
    result_selected = Signal(int)
    log_message = Signal(str)

    @staticmethod
    def _loads(payload: str) -> dict[str, Any]:
        try:
            data = json.loads(payload or "{}")
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    @Slot(str)
    def reportStatus(self, payload: str) -> None:  # noqa: N802 - Qt 槽名
        data = self._loads(payload)
        if data.get("ready"):
            self.ready.emit(data)

    @Slot(str)
    def reportError(self, message: str) -> None:  # noqa: N802
        self.failed.emit(str(message)[:400])

    @Slot(str)
    def reportLog(self, message: str) -> None:  # noqa: N802
        self.log_message.emit(str(message)[:400])

    @Slot(str)
    def pointPicked(self, payload: str) -> None:  # noqa: N802
        data = self._loads(payload)
        try:
            self.point_picked.emit(
                str(data.get("user", "A")),
                float(data.get("lng", 0.0)),
                float(data.get("lat", 0.0)),
                str(data.get("source", "click")),
            )
        except (TypeError, ValueError):
            return

    @Slot(str)
    def circleChanged(self, payload: str) -> None:  # noqa: N802
        data = self._loads(payload)
        try:
            self.circle_changed.emit(
                float(data["lng"]), float(data["lat"]), float(data["radius"])
            )
        except (KeyError, TypeError, ValueError):
            return

    @Slot(str)
    def polygonChanged(self, payload: str) -> None:  # noqa: N802
        data = self._loads(payload)
        points = self._points(data.get("points"))
        if points:
            self.polygon_changed.emit(points, bool(data.get("closed", False)))

    @Slot(str)
    def polygonClosed(self, payload: str) -> None:  # noqa: N802
        data = self._loads(payload)
        points = self._points(data.get("points"))
        if points:
            self.polygon_closed.emit(points)

    @Slot(str)
    def selectResult(self, index: str) -> None:  # noqa: N802
        try:
            self.result_selected.emit(int(index))
        except (TypeError, ValueError):
            return

    @staticmethod
    def _points(raw: Any) -> list[list[float]]:
        points: list[list[float]] = []
        if not isinstance(raw, list):
            return points
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    points.append([float(item[0]), float(item[1])])
                except (TypeError, ValueError):
                    continue
        return points


class MapController(QObject):
    """负责装载 map.html 并向页面下发指令。"""

    load_timed_out = Signal(str)
    _js_queued = Signal(str)

    def __init__(
        self,
        view: QWebEngineView,
        bridge: MapBridge,
        settings: Settings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._view = view
        self._bridge = bridge
        self._settings = settings
        self._channel = QWebChannel(view.page())
        self._channel.registerObject("bridge", bridge)
        view.page().setWebChannel(self._channel)
        settings_obj = view.settings()
        settings_obj.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings_obj.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings_obj.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        # 所有 JS 调用都通过队列信号回到 GUI 线程执行，禁止跨线程直接操作 QWebEngineView
        self._js_queued.connect(self._run_js, Qt.QueuedConnection)

        self._ready = False
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(15000)
        self._watchdog.timeout.connect(self._on_timeout)

        self._bridge.ready.connect(self._on_ready)
        bridge.failed.connect(lambda msg: self._mark_failed(msg))

    # ------------------------------------------------------------------ #
    @property
    def ready(self) -> bool:
        return self._ready

    def _html_path(self) -> Path:
        candidates = [
            resource_root() / "ui" / "web" / "map.html",
            Path(__file__).resolve().parent / "web" / "map.html",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise FileNotFoundError("未找到 ui/web/map.html")

    def load(self) -> None:
        self._ready = False
        try:
            html = self._html_path().read_text(encoding="utf-8")
        except OSError as exc:
            self._mark_failed(f"读取地图页面失败：{exc}")
            return
        html = html.replace("__AMAP_KEY__", self._settings.amap_js_key)
        html = html.replace("__AMAP_SEC__", self._settings.amap_js_security_code)
        self._view.setHtml(html, QUrl("https://webapi.amap.com/"))
        self._watchdog.start()

    def reload(self) -> None:
        self._ready = False
        self.load()

    def update_settings(self, settings: Settings) -> None:
        """设置变更后必须把新配置交给地图：load() 用的是这里的 Key/安全密钥。"""
        self._settings = settings

    def _on_ready(self, info: dict[str, Any]) -> None:
        self._ready = True
        self._watchdog.stop()

    def _on_timeout(self) -> None:
        if not self._ready:
            self._mark_failed("高德 JS API 初始化超时（15 秒内未收到地图就绪事件）")
            self.load_timed_out.emit("地图初始化超时，请检查网络与 JS API Key / 安全密钥")

    def _mark_failed(self, message: str) -> None:
        self._ready = False
        self.load_timed_out.emit(message)

    # ------------------------------------------------------------------ #
    def js(self, code: str) -> None:
        """线程安全：任何线程调用都会排队到 GUI 线程执行。"""
        self._js_queued.emit(code)

    def _run_js(self, code: str) -> None:
        try:
            self._view.page().runJavaScript(code)
        except RuntimeError:
            pass

    def _call(self, func: str, *args: Any) -> None:
        payload = ", ".join(json.dumps(arg, ensure_ascii=False) for arg in args)
        self.js(f"window.MPAI && window.MPAI.{func}({payload});")

    def set_mode(self, mode: str) -> None:
        self._call("setMode", mode)

    def set_pending_radius(self, radius_m: float) -> None:
        self._call("setPendingRadius", float(radius_m))

    def set_point(self, user: str, lng: float, lat: float, name: str = "") -> None:
        self._call("setPoint", user, float(lng), float(lat), name)

    def clear_point(self, user: str) -> None:
        self._call("clearPoint", user)

    def set_circle(self, lng: float, lat: float, radius_m: float) -> None:
        self._call("setCircle", float(lng), float(lat), float(radius_m))

    def set_polygon(self, points: Sequence[Sequence[float]]) -> None:
        self._call("setPolygon", [[round(float(x), 6), round(float(y), 6)] for x, y in points])

    def close_polygon(self) -> None:
        self._call("closePolygon")

    def clear_region(self) -> None:
        self._call("clearRegion")

    def show_results(self, items: Sequence[dict[str, Any]]) -> None:
        self._call("showResults", list(items))

    def select_result(self, index: int) -> None:
        self._call("selectResult", int(index))

    def show_routes(
        self,
        a_path: Sequence[Sequence[float]],
        b_path: Sequence[Sequence[float]],
        poi: Sequence[float] | None = None,
        name: str = "",
        detail: str = "",
    ) -> None:
        payload = {
            "a": [[round(float(x), 6), round(float(y), 6)] for x, y in a_path],
            "b": [[round(float(x), 6), round(float(y), 6)] for x, y in b_path],
            "poi": [round(float(poi[0]), 6), round(float(poi[1]), 6)] if poi else None,
            "name": name,
            "detail": detail,
        }
        self._call("showRoutes", payload)

    def clear_results(self) -> None:
        self._call("clearResults")

    def fit_all(self) -> None:
        self._call("fitAll")

    def focus(self, lng: float, lat: float, zoom: int = 15) -> None:
        self._call("focus", float(lng), float(lat), int(zoom))

    def ping(self) -> None:
        self._call("ping")
