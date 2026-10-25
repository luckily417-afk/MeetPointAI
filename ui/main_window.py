"""主窗口：左侧控制面板 + 右侧高德地图 + 底部结果/分析，顶部三路服务状态。"""

from __future__ import annotations

import asyncio
import html
import json
import time
from datetime import datetime, timezone
from typing import Any, Sequence

from PySide6.QtCore import QLocale, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

from algorithms.candidates import CancelToken
from algorithms.pipeline import NO_RESULT_MESSAGE, RecommendRequest, RecommendationError
from algorithms.pipeline import RecommendationPipeline
from config.constants import (
    CATEGORY_PRESETS,
    REC_MODE_LABELS,
    REGION_MODE_LABELS,
    SERVICE_LABELS,
    SERVICE_ORDER,
    STATE_LABELS,
    TRAVEL_MODE_LABELS,
)
from config.settings import Settings, save_env
from geometry.crs import haversine
from geometry.regions import CircleRegion, PolygonRegion, Region, RegionError, build_region
from models.schemas import (
    HealthSnapshot,
    Recommendation,
    RecMode,
    RegionMode,
    RouteLeg,
    ServiceState,
    TravelMode,
    UserPoint,
    format_location,
    parse_location,
)
from monitoring.health import HealthMonitor
from services.amap_web import AMapWebService
from services.bootstrap import build_services
from services.deepseek import DeepSeekService
from services.errors import ServiceError
from storage.db import Database
from ui.async_runtime import AsyncRuntime
from ui.map_bridge import MapBridge, MapController
from ui.settings_dialog import SettingsDialog
from ui.theme import APP_QSS
from ui.widgets import Banner, RadiusSpinBox, ServicePill

__all__ = ["MainWindow"]


class MainWindow(QMainWindow):
    health_updated = Signal(str, object)
    map_failed = Signal(str)
    log_line = Signal(str)

    def __init__(
        self,
        settings: Settings,
        runtime: AsyncRuntime,
        db: Database,
        amap: AMapWebService,
        deepseek: DeepSeekService | None,
        monitor: HealthMonitor,
        pipeline: RecommendationPipeline,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 统一数字格式：部分系统区域设置会把 0-9 映射成苏州码子/全角数字，
        # 导致数值控件显示成「〢〦.〇」之类，这里强制使用 C 区域（ASCII 数字 + 小数点）。
        QLocale.setDefault(QLocale.c())
        self.settings = settings
        self.runtime = runtime
        self.db = db
        self.amap = amap
        self.deepseek = deepseek
        self.monitor = monitor
        self.pipeline = pipeline

        self._point_a: UserPoint | None = None
        self._point_b: UserPoint | None = None
        self._circle: CircleRegion | None = None
        self._polygon: PolygonRegion | None = None
        self._region_mode = RegionMode.NONE
        self._results: list[Recommendation] = []
        self._current_task: str | None = None
        self._cancel_token: CancelToken | None = None
        self._search_tasks: dict[str, str] = {}
        self._circle_radius_m = 3000.0
        self._last_radius_factor = 1000.0
        self._js_ready = False
        self._last_notice = ""

        self.setWindowTitle("MeetPoint AI · 双人智能见面地点推荐")
        self.resize(1480, 940)
        self.setMinimumSize(1100, 700)
        self.setStyleSheet(APP_QSS)

        self._build_ui()
        self._build_menu()
        self._wire_signals()

        self._metrics_timer = QTimer(self)
        self._metrics_timer.setInterval(2000)
        self._metrics_timer.timeout.connect(self._refresh_metrics)
        self._metrics_timer.start()

        interval = max(0, self.settings.health_interval_s)
        self._health_timer = QTimer(self)
        if interval:
            self._health_timer.setInterval(interval * 1000)
            self._health_timer.timeout.connect(self._periodic_health)
            self._health_timer.start()

        self._js_ping_timer = QTimer(self)
        self._js_ping_timer.setSingleShot(True)
        self._js_ping_timer.setInterval(6000)
        self._js_ping_timer.timeout.connect(self._on_js_ping_timeout)

        for name in SERVICE_ORDER:
            self._apply_health(name, self.monitor.snapshot(name))
        self._update_capabilities()
        self._log("程序启动，配置完成：" + json.dumps(self.settings.masked(), ensure_ascii=False))

    # ================================================================== #
    # 界面搭建
    # ================================================================== #
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_topbar())

        self.banner = Banner()
        self.result_banner = Banner()
        banner_host = QWidget()
        banner_layout = QVBoxLayout(banner_host)
        banner_layout.setContentsMargins(12, 8, 12, 0)
        banner_layout.setSpacing(6)
        banner_layout.addWidget(self.banner)
        banner_layout.addWidget(self.result_banner)
        outer.addWidget(banner_host)

        vertical = QSplitter(Qt.Vertical)
        vertical.setChildrenCollapsible(False)
        horizontal = QSplitter(Qt.Horizontal)
        horizontal.setChildrenCollapsible(False)
        horizontal.addWidget(self._build_left_panel())
        horizontal.addWidget(self._build_map_panel())
        horizontal.setStretchFactor(0, 0)
        horizontal.setStretchFactor(1, 1)
        horizontal.setSizes([400, 1080])
        vertical.addWidget(horizontal)
        vertical.addWidget(self._build_bottom_panel())
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        vertical.setSizes([620, 300])
        outer.addWidget(vertical, 1)

        self.statusBar().showMessage("就绪")
        self.metrics_label = QLabel("API 调用 0 次 · 失败 0 次 · Token 0")
        self.metrics_label.setStyleSheet("color:#6d7d90;font-size:11px;padding-right:8px;")
        self.statusBar().addPermanentWidget(self.metrics_label)

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)

        title = QLabel("MeetPoint AI")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        layout.addSpacing(6)

        short_titles = {"amap_web": "高德 Web", "amap_js": "高德 JS", "deepseek": "DeepSeek"}
        self.pills: dict[str, ServicePill] = {}
        for name in SERVICE_ORDER:
            pill = ServicePill(short_titles.get(name, name))
            pill.setToolTip(SERVICE_LABELS.get(name, name))
            self.pills[name] = pill
            layout.addWidget(pill)

        layout.addStretch(1)
        self.btn_check = QPushButton("立即检测")
        self.btn_check.setToolTip("分别检测高德 Web 服务、高德 JS API、DeepSeek API")
        layout.addWidget(self.btn_check)
        return bar

    def _build_left_panel(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(12, 10, 6, 10)
        outer.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(10)

        # ---- A/B 位置 ----
        box_points = QGroupBox("① 双方出发位置")
        points_layout = QVBoxLayout(box_points)
        points_layout.setSpacing(8)

        for user in ("A", "B"):
            row = QHBoxLayout()
            edit = QLineEdit()
            edit.setPlaceholderText(
                f"输入 {user} 的地点名称、详细地址，或直接输入 经度,纬度"
            )
            edit.setClearButtonEnabled(True)
            btn_search = QPushButton("搜索")
            btn_search.setFixedWidth(58)
            btn_map = QPushButton("地图选点")
            btn_map.setFixedWidth(78)
            btn_clear = QPushButton("清除")
            btn_clear.setFixedWidth(52)
            label = QLabel(f"{user} 出发地")
            label.setFixedWidth(60)
            label.setStyleSheet("font-weight:600;color:#1c3a5e;")
            row.addWidget(label)
            row.addWidget(edit, 1)
            points_layout.addLayout(row)
            actions = QHBoxLayout()
            actions.addStretch(1)
            actions.addWidget(btn_search)
            actions.addWidget(btn_map)
            actions.addWidget(btn_clear)
            points_layout.addLayout(actions)

            if user == "A":
                self.a_edit, self.a_btn_search, self.a_btn_map, self.a_btn_clear = (
                    edit,
                    btn_search,
                    btn_map,
                    btn_clear,
                )
            else:
                self.b_edit, self.b_btn_search, self.b_btn_map, self.b_btn_clear = (
                    edit,
                    btn_search,
                    btn_map,
                    btn_clear,
                )

        self.point_hint = QLabel("提示：可直接输入经纬度（GCJ-02），例如 117.210089,39.135951")
        self.point_hint.setWordWrap(True)
        self.point_hint.setStyleSheet("color:#7a8aa0;font-size:11px;")
        points_layout.addWidget(self.point_hint)
        layout.addWidget(box_points)

        # ---- 出行与偏好 ----
        box_pref = QGroupBox("② 出行方式与场所偏好")
        form = QFormLayout(box_pref)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.travel_combo = QComboBox()
        for value, label in TRAVEL_MODE_LABELS.items():
            self.travel_combo.addItem(label, value)
        form.addRow("出行方式", self.travel_combo)

        self.category_combo = QComboBox()
        for name in CATEGORY_PRESETS:
            self.category_combo.addItem(name, name)
        form.addRow("场所类型", self.category_combo)

        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("可选：自定义检索关键词（如 咖啡、火锅）")
        form.addRow("自定义关键词", self.keyword_edit)

        self.mode_combo = QComboBox()
        for value, label in REC_MODE_LABELS.items():
            self.mode_combo.addItem(label, value)
        form.addRow("推荐模式", self.mode_combo)

        self.topn_spin = QSpinBox()
        self.topn_spin.setRange(1, 10)
        self.topn_spin.setValue(max(1, min(10, self.settings.top_n)))
        self.topn_spin.setSuffix(" 个")
        self.topn_spin.setLocale(QLocale.c())
        self.topn_spin.setToolTip("最终展示的推荐条数（也可在「工具 → 设置」中批量调整检索参数）")

        self.prefs_edit = QPlainTextEdit()
        self.prefs_edit.setPlaceholderText(
            "自然语言偏好，例如：想找安静、适合聊天、人均 80 元以内的咖啡馆，最好离地铁近"
        )
        self.prefs_edit.setFixedHeight(76)
        form.addRow("偏好描述", self.prefs_edit)
        layout.addWidget(box_pref)

        # ---- 区域限制 ----
        box_region = QGroupBox("③ 推荐区域（硬约束）")
        region_layout = QVBoxLayout(box_region)
        region_layout.setSpacing(8)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("限制模式"))
        self.region_combo = QComboBox()
        for value, label in REGION_MODE_LABELS.items():
            self.region_combo.addItem(label, value)
        mode_row.addWidget(self.region_combo, 1)
        region_layout.addLayout(mode_row)

        radius_row = QHBoxLayout()
        radius_row.addWidget(QLabel("半径"))
        self.radius_spin = RadiusSpinBox()
        self.radius_spin.setValue(3.0)
        radius_row.addWidget(self.radius_spin, 1)
        self.radius_unit = QComboBox()
        self.radius_unit.addItem("千米", 1000.0)
        self.radius_unit.addItem("米", 1.0)
        self.radius_unit.setFixedWidth(78)
        self.radius_unit.setToolTip("切换半径单位；数值会自动换算，不改变实际范围")
        radius_row.addWidget(self.radius_unit)
        self.btn_circle = QPushButton("地图选圆心")
        radius_row.addWidget(self.btn_circle)
        region_layout.addLayout(radius_row)

        poly_row = QHBoxLayout()
        self.btn_polygon = QPushButton("绘制多边形")
        self.btn_close_poly = QPushButton("闭合多边形")
        self.btn_clear_region = QPushButton("清除区域")
        self.btn_clear_region.setObjectName("danger")
        poly_row.addWidget(self.btn_polygon)
        poly_row.addWidget(self.btn_close_poly)
        poly_row.addWidget(self.btn_clear_region)
        region_layout.addLayout(poly_row)

        self.region_label = QLabel("当前未限制区域（将以 A、B 中点为圆心按默认半径检索）")
        self.region_label.setWordWrap(True)
        self.region_label.setStyleSheet("color:#44576e;font-size:11px;")
        region_layout.addWidget(self.region_label)
        layout.addWidget(box_region)

        layout.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        self.btn_run = QPushButton("智能推荐见面地点")
        self.btn_run.setObjectName("primary")
        self.btn_run.setMinimumHeight(42)

        topn_row = QHBoxLayout()
        topn_row.addWidget(QLabel("推荐数量"))
        topn_row.addWidget(self.topn_spin)
        topn_row.addStretch(1)
        outer.addLayout(topn_row)
        outer.addWidget(self.btn_run)
        self.btn_cancel = QPushButton("取消当前任务")
        self.btn_cancel.setEnabled(False)
        outer.addWidget(self.btn_cancel)
        return container

    def _build_map_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 10, 12, 10)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        self.map_mode_label = QLabel("地图模式：普通浏览")
        self.map_mode_label.setStyleSheet("color:#44576e;font-size:11px;")
        btn_fit = QPushButton("全览")
        btn_fit.setFixedWidth(64)
        btn_reload = QPushButton("重新加载地图")
        toolbar.addWidget(self.map_mode_label)
        toolbar.addStretch(1)
        toolbar.addWidget(btn_fit)
        toolbar.addWidget(btn_reload)
        layout.addLayout(toolbar)

        self.map_view = QWebEngineView()
        self.map_view.setMinimumSize(320, 240)
        self.map_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.map_bridge = MapBridge(self)
        self.map = MapController(self.map_view, self.map_bridge, self.settings, self)
        layout.addWidget(self.map_view, 1)

        btn_fit.clicked.connect(self._fit_map)
        btn_reload.clicked.connect(self._reload_map)
        return panel

    def _build_bottom_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 0, 12, 10)
        layout.setSpacing(6)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setLocale(QLocale.c())
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.setFixedHeight(16)
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color:#44576e;font-size:12px;")
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.status_label, 2)
        layout.addLayout(progress_row)

        split = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "地点", "类型", "A 通勤", "B 通勤", "换乘", "来源"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        split.addWidget(self.table)

        self.tabs = QTabWidget()
        self.detail_view = QTextBrowser()
        self.detail_view.setOpenExternalLinks(True)
        self.ai_view = QTextBrowser()
        self.ai_view.setOpenExternalLinks(True)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("font-family:Consolas,monospace;font-size:11px;")
        self.tabs.addTab(self.detail_view, "地点详情")
        self.tabs.addTab(self.ai_view, "AI 分析")
        self.tabs.addTab(self.log_view, "服务日志")
        split.addWidget(self.tabs)
        split.setSizes([820, 560])
        layout.addWidget(split, 1)

        self.detail_view.setHtml(
            "<div style='color:#6d7d90;padding:10px'>选择上方推荐结果查看地点详情、双方路线与导航入口。</div>"
        )
        return panel

    def _build_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("文件")
        act_clear = QAction("清除历史数据与缓存", self)
        act_clear.triggered.connect(self._clear_history)
        act_export = QAction("导出当前推荐结果…", self)
        act_export.triggered.connect(self._export_results)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_clear)
        file_menu.addAction(act_export)
        file_menu.addSeparator()
        file_menu.addAction(act_quit)

        tool_menu = menu.addMenu("工具")
        act_settings = QAction("设置…", self)
        act_settings.setShortcut("Ctrl+,")
        act_settings.triggered.connect(self._open_settings)
        act_check = QAction("立即检测 API 状态", self)
        act_check.triggered.connect(self._check_services)
        act_reload = QAction("重新加载地图", self)
        act_reload.triggered.connect(self._reload_map)
        tool_menu.addAction(act_settings)
        tool_menu.addSeparator()
        tool_menu.addAction(act_check)
        tool_menu.addAction(act_reload)

        help_menu = menu.addMenu("帮助")
        act_about = QAction("关于 MeetPoint AI", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _wire_signals(self) -> None:
        self.a_btn_search.clicked.connect(lambda: self._search_point("A"))
        self.b_btn_search.clicked.connect(lambda: self._search_point("B"))
        self.a_btn_map.clicked.connect(lambda: self._arm_map_point("A"))
        self.b_btn_map.clicked.connect(lambda: self._arm_map_point("B"))
        self.a_btn_clear.clicked.connect(lambda: self._clear_point("A"))
        self.b_btn_clear.clicked.connect(lambda: self._clear_point("B"))
        self.a_edit.returnPressed.connect(lambda: self._search_point("A"))
        self.b_edit.returnPressed.connect(lambda: self._search_point("B"))

        self.region_combo.currentIndexChanged.connect(self._on_region_mode_changed)
        self.radius_spin.valueChanged.connect(self._on_radius_changed)
        self.radius_unit.currentIndexChanged.connect(self._on_radius_unit_changed)
        self.topn_spin.valueChanged.connect(self._on_topn_changed)
        self.btn_circle.clicked.connect(self._arm_circle_center)
        self.btn_polygon.clicked.connect(self._start_polygon)
        self.btn_close_poly.clicked.connect(self._close_polygon)
        self.btn_clear_region.clicked.connect(self._clear_region)

        self.btn_run.clicked.connect(self._run_recommendation)
        self.btn_cancel.clicked.connect(self._cancel_active)
        self.btn_check.clicked.connect(self._check_services)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.itemDoubleClicked.connect(self._open_selected_navigation)

        self.map_bridge.ready.connect(self._on_map_ready)
        self.map_bridge.failed.connect(self._on_map_failed)
        self.map_bridge.log_message.connect(self._log)
        self.map_bridge.point_picked.connect(self._on_map_point)
        self.map_bridge.circle_changed.connect(self._on_circle_changed)
        self.map_bridge.polygon_changed.connect(self._on_polygon_changed)
        self.map_bridge.polygon_closed.connect(self._on_polygon_closed)
        self.map_bridge.result_selected.connect(self._select_result_index)

        self.runtime.progress.connect(self._on_progress)
        self.health_updated.connect(self._apply_health)
        self.log_line.connect(self._log)
        self.map_failed.connect(self._on_map_failed_ui)
        self.monitor.add_listener(
            lambda name, snap: self.health_updated.emit(name, snap)
        )

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not getattr(self, "_map_loaded", False):
            self._map_loaded = True
            self.map.load()
            QTimer.singleShot(1500, self._startup_checks)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._cancel_active()
        # 事件循环由 main() 统一收尾（先关连接池，再停 asyncio 线程）
        try:
            self.db.purge_expired_cache()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)

    def _startup_checks(self) -> None:
        asyncio_coro = self._async_startup()
        self.runtime.submit(asyncio_coro, on_error=lambda msg: self._log(f"启动检测失败：{msg}"))

    async def _async_startup(self) -> None:
        await self.monitor.check_all(["amap_web", "deepseek"])
        self.map.ping()

    def _periodic_health(self) -> None:
        self.runtime.submit(self.monitor.check_due())
        if self._js_ready:
            self.map.ping()

    # ================================================================== #
    # 健康状态
    # ================================================================== #
    @Slot(str, object)
    def _apply_health(self, name: str, snap: HealthSnapshot) -> None:
        pill = self.pills.get(name)
        if pill is not None:
            pill.update_snapshot(snap, self.settings.latency_warn_ms)
        state = snap.state
        if state.is_failure:
            self.statusBar().showMessage(
                f"{SERVICE_LABELS.get(name, name)}：{STATE_LABELS.get(state.value, state.value)} — {snap.detail}"
            )
        self._update_capabilities()

    def _update_capabilities(self) -> None:
        amap_state = self.monitor.state_of("amap_web")
        js_state = self.monitor.state_of("amap_js")
        ds_state = self.monitor.state_of("deepseek")

        amap_ok = amap_state.is_usable
        self.btn_run.setEnabled(amap_ok)
        if not amap_ok:
            label = STATE_LABELS.get(amap_state.value, amap_state.value)
            detail = self.monitor.snapshot("amap_web").detail
            self.banner.show_message(
                f"地图数据服务不可用（{label}）：禁止生成未经验证的新地点或路线。"
                f"{('原因：' + detail) if detail else ''} 已缓存结果仅作历史参考，请手动重试。",
                "error",
            )
        elif ds_state.is_failure:
            self.banner.show_message(
                "AI 服务不可用，当前为基础推荐模式（本地规则排序，仍使用真实地图数据）。",
                "warn",
            )
        elif js_state.is_failure and not self._js_ready:
            self.banner.show_message(
                "高德 JS API 异常：地图交互受限（点击选点/绘图暂停），仍可使用文字地址输入进行推荐。",
                "warn",
            )
        else:
            self.banner.clear()

        if amap_state is ServiceState.NOT_CONFIGURED:
            self.banner.show_message("未配置高德 Web 服务 API Key，无法检索真实地点。", "error")

    def _check_services(self) -> None:
        self.btn_check.setEnabled(False)
        self.status_label.setText("正在检测三个服务…")
        self._log("手动触发 API 健康检测")
        self.runtime.submit(
            self._async_check(),
            on_done=lambda _: self._after_check(None),
            on_error=lambda msg: self._after_check(msg),
        )
        self.map.ping()
        self._js_ping_timer.start()

    async def _async_check(self) -> None:
        await asyncio.gather(
            self.monitor.check("amap_web"),
            self.monitor.check("deepseek"),
        )

    def _after_check(self, error: str | None) -> None:
        self.btn_check.setEnabled(True)
        if error:
            self.status_label.setText(f"检测失败：{error}")
        else:
            parts = []
            for name in SERVICE_ORDER:
                snap = self.monitor.snapshot(name)
                parts.append(f"{SERVICE_LABELS.get(name, name)} {STATE_LABELS.get(snap.state.value, snap.state.value)}")
            self.status_label.setText(" · ".join(parts))
        self._update_capabilities()

    @Slot(str)
    def _on_map_ready(self, info: dict) -> None:
        self._js_ready = True
        self._js_ping_timer.stop()
        latency = 0
        self.monitor.note_js_status(
            True,
            f"地图已初始化（版本 {info.get('version', '2.0')}，zoom={info.get('zoom')}）",
            latency_ms=latency,
        )
        self._log(f"地图就绪：{json.dumps(info, ensure_ascii=False)}")
        self._sync_map_state()

    @Slot(str)
    def _on_map_failed(self, message: str) -> None:
        self._js_ready = False
        self.monitor.note_js_status(False, message)
        self._log(f"地图异常：{message}")
        self.map_failed.emit(message)

    @Slot(str)
    def _on_map_failed_ui(self, message: str) -> None:
        if not self._js_ready:
            self.status_label.setText(f"地图不可用：{message}")

    def _on_js_ping_timeout(self) -> None:
        if not self._js_ready:
            self.monitor.note_js_status(False, "地图页面在 6 秒内未响应 ping")

    # ================================================================== #
    # 地图状态同步
    # ================================================================== #
    def _sync_map_state(self) -> None:
        if self._point_a:
            self.map.set_point("A", self._point_a.lng, self._point_a.lat, self._point_a.display)
        if self._point_b:
            self.map.set_point("B", self._point_b.lng, self._point_b.lat, self._point_b.display)
        self._redraw_region()
        if self._results:
            self._render_results_on_map()

    def _redraw_region(self) -> None:
        if self._region_mode is RegionMode.CIRCLE and self._circle:
            self.map.set_circle(self._circle.center_lng, self._circle.center_lat, self._circle.radius_m)
        elif self._region_mode is RegionMode.POLYGON and self._polygon:
            self.map.set_polygon([list(p) for p in self._polygon.points])
        else:
            self.map.clear_region()

    def _fit_map(self) -> None:
        self.map.fit_all()

    def _reload_map(self) -> None:
        self._js_ready = False
        self.monitor.reset("amap_js")
        self.map.reload()
        self._js_ping_timer.start()
        self._log("重新加载地图")

    # ================================================================== #
    # A/B 点
    # ================================================================== #
    def _arm_map_point(self, user: str) -> None:
        if not self._js_ready:
            self._warn("地图尚未就绪，暂时无法点击选点。可使用文字地址输入。")
            return
        self.map.set_mode("setA" if user == "A" else "setB")
        self.map_mode_label.setText(f"地图模式：设置 {user} 点（点击地图选择）")

    def _clear_point(self, user: str) -> None:
        if user == "A":
            self._point_a = None
            self.a_edit.clear()
        else:
            self._point_b = None
            self.b_edit.clear()
        self.map.clear_point(user)
        self._invalidate_results()
        self.status_label.setText(f"已清除 {user} 点")

    def _search_point(self, user: str) -> None:
        edit = self.a_edit if user == "A" else self.b_edit
        text = edit.text().strip()
        if not text:
            self._warn("请先输入地点名称、地址或经纬度")
            return
        try:
            lng, lat = parse_location(text)
        except ValueError:
            pass
        else:
            self._set_point(user, UserPoint(lng=lng, lat=lat, name=text, source="coord"))
            self.status_label.setText(f"{user} 点已按经纬度设置（GCJ-02）")
            return

        task_id = f"search-{user}-{int(time.time() * 1000)}"
        self._search_tasks[user] = task_id
        self.status_label.setText(f"正在搜索「{text}」…")
        self.runtime.submit(
            self._resolve_point_name(text),
            on_done=lambda points: self._on_search_done(user, task_id, points),
            on_error=lambda msg: self._on_search_error(user, task_id, msg),
            task_id=task_id,
        )

    async def _resolve_point_name(self, text: str) -> list[UserPoint]:
        results: list[UserPoint] = []
        try:
            page = await self.amap.place_text(text, offset=10)
            for poi in page.pois:
                results.append(
                    UserPoint(
                        lng=poi.lng,
                        lat=poi.lat,
                        name=poi.name,
                        address=poi.address or poi.cityname + poi.adname,
                        poi_id=poi.id,
                        adcode=poi.adcode,
                        city=poi.cityname,
                        source="search",
                    )
                )
        except ServiceError as exc:
            if exc.state in (ServiceState.AUTH_ERROR, ServiceState.QUOTA_LIMITED, ServiceState.NOT_CONFIGURED):
                raise
            self._async_log(f"POI 搜索失败，尝试地理编码：{exc.message}")
        if not results:
            hits = await self.amap.geocode(text)
            for hit in hits:
                results.append(
                    UserPoint(
                        lng=hit.lng,
                        lat=hit.lat,
                        name=text,
                        address=hit.formatted_address,
                        adcode=hit.adcode,
                        city=hit.city,
                        source="search",
                    )
                )
        return results[:10]

    def _async_log(self, message: str) -> None:
        # 协程运行在后台线程，通过队列信号回到 GUI 线程写日志
        self.log_line.emit(message)

    def _on_search_done(self, user: str, task_id: str, points: list[UserPoint]) -> None:
        if self._search_tasks.get(user) != task_id:
            return
        if not points:
            self.status_label.setText(f"未找到「{self.a_edit.text() if user == 'A' else self.b_edit.text()}」")
            self._warn("未搜索到匹配地点，请换一个更具体的名称或直接输入经纬度。")
            return
        if len(points) == 1:
            self._set_point(user, points[0])
            return
        labels = [
            f"{p.display} ｜ {p.address or '—'} ｜ {format_location(p.lng, p.lat)}" for p in points
        ]
        choice, ok = QInputDialog.getItem(
            self,
            "选择地点",
            f"「{points[0].name}」匹配到多个候选，请选择正确的位置：",
            labels,
            0,
            False,
        )
        if not ok or choice is None:
            self.status_label.setText("已取消地点选择")
            return
        self._set_point(user, points[labels.index(choice)])

    def _on_search_error(self, user: str, task_id: str, message: str) -> None:
        if self._search_tasks.get(user) != task_id:
            return
        self.status_label.setText(f"地点搜索失败：{message}")
        self._warn(f"地点搜索失败：{message}")

    def _set_point(self, user: str, point: UserPoint) -> None:
        if user == "A":
            self._point_a = point
            self.a_edit.setText(point.display)
        else:
            self._point_b = point
            self.b_edit.setText(point.display)
        self.map.set_point(user, point.lng, point.lat, point.display)
        self.status_label.setText(f"{user} 点：{point.display}（{point.location}）")
        self._invalidate_results()

    @Slot(str, float, float, str)
    def _on_map_point(self, user: str, lng: float, lat: float, source: str) -> None:
        point = UserPoint(lng=lng, lat=lat, source="map_click" if source != "drag" else "drag")
        self._set_point(user, point)
        self.map_mode_label.setText("地图模式：普通浏览")
        self.runtime.submit(
            self._reverse_geocode(lng, lat),
            on_done=lambda info: self._apply_regeo(user, lng, lat, info),
        )

    async def _reverse_geocode(self, lng: float, lat: float) -> dict[str, Any]:
        try:
            return await self.amap.regeocode(lng, lat)
        except ServiceError as exc:
            return {"error": exc.message}

    def _apply_regeo(self, user: str, lng: float, lat: float, info: dict[str, Any]) -> None:
        current = self._point_a if user == "A" else self._point_b
        if current is None or abs(current.lng - lng) > 1e-9 or abs(current.lat - lat) > 1e-9:
            return
        address = info.get("formatted_address") or ""
        if info.get("error"):
            self._log(f"逆地理编码失败：{info['error']}")
        updated = current.model_copy(
            update={
                "address": address or current.address,
                "name": current.name or address or format_location(lng, lat),
                "adcode": info.get("adcode") or current.adcode,
                "city": info.get("city") or current.city,
            }
        )
        if user == "A":
            self._point_a = updated
            self.a_edit.setText(updated.display)
        else:
            self._point_b = updated
            self.b_edit.setText(updated.display)
        self.map.set_point(user, updated.lng, updated.lat, updated.display)
        self.status_label.setText(f"{user} 点：{updated.display}")

    # ================================================================== #
    # 区域
    # ================================================================== #
    def _active_region(self) -> Region | None:
        if self._region_mode is RegionMode.CIRCLE:
            return self._circle
        if self._region_mode is RegionMode.POLYGON:
            return self._polygon
        return None

    def _on_region_mode_changed(self) -> None:
        mode = RegionMode(self.region_combo.currentData())
        self._region_mode = mode
        region = self._active_region()
        self._redraw_region()
        if mode is RegionMode.NONE:
            self.region_label.setText("当前未限制区域（将以 A、B 中点为圆心按默认半径检索）")
        elif mode is RegionMode.CIRCLE:
            if self._circle:
                self.region_label.setText(self._circle.describe())
            else:
                self.region_label.setText("圆形模式：请点击「地图选圆心」或直接输入半径后在图上选点")
            self.map.set_pending_radius(self._circle.radius_m if self._circle else self._radius_meters())
        else:
            if self._polygon:
                self.region_label.setText(self._polygon.describe())
            else:
                self.region_label.setText("多边形模式：点击「绘制多边形」后在图上连续点击顶点，双击闭合")
        self._invalidate_results()

    def _on_radius_changed(self, value: float = 0.0) -> None:
        radius = max(20.0, self._radius_meters())
        self._circle_radius_m = radius
        self.map.set_pending_radius(radius)
        if self._circle is None:
            self.status_label.setText(f"半径已设为 {radius / 1000:.2f} 千米，请在地图上选择圆心")
            return
        try:
            self._circle = CircleRegion(self._circle.center_lng, self._circle.center_lat, radius)
        except RegionError as exc:
            self._warn(str(exc))
            return
        self.region_label.setText(self._circle.describe())
        if self._region_mode is RegionMode.CIRCLE:
            self.map.set_circle(self._circle.center_lng, self._circle.center_lat, radius)
        self._invalidate_results()

    def _radius_meters(self) -> float:
        """当前单位下的输入换算成米。"""
        factor = float(self.radius_unit.currentData() or 1000.0)
        return float(self.radius_spin.value()) * factor

    def _on_radius_unit_changed(self) -> None:
        factor = float(self.radius_unit.currentData() or 1000.0)
        previous = float(getattr(self, "_last_radius_factor", 1000.0))
        meters = max(20.0, float(self.radius_spin.value()) * previous)
        self._last_radius_factor = factor
        self.radius_spin.blockSignals(True)
        if factor == 1.0:  # 米
            self.radius_spin.setRange(20.0, 50000.0)
            self.radius_spin.setDecimals(0)
            self.radius_spin.setSingleStep(50.0)
        else:  # 千米
            self.radius_spin.setRange(0.02, 50.0)
            self.radius_spin.setDecimals(2)
            self.radius_spin.setSingleStep(0.5)
        self.radius_spin.setValue(round(meters / factor, 2 if factor > 1 else 0))
        self.radius_spin.blockSignals(False)
        self._on_radius_changed()

    def _on_topn_changed(self, value: int) -> None:
        self.settings = self.settings.with_overrides(top_n=int(value))
        self.pipeline.settings = self.settings
        try:
            self.db.set_setting("top_n", int(value))
        except Exception:  # noqa: BLE001
            pass
        self.status_label.setText(f"推荐数量已设为 {value} 个")

    def _arm_circle_center(self) -> None:
        if self.region_combo.currentData() != RegionMode.CIRCLE.value:
            idx = self.region_combo.findData(RegionMode.CIRCLE.value)
            if idx >= 0:
                self.region_combo.setCurrentIndex(idx)
        if not self._js_ready:
            self._warn("地图尚未就绪，无法选择圆心")
            return
        self.map.set_pending_radius(self._radius_meters())
        self.map.set_mode("circleCenter")
        self.map_mode_label.setText("地图模式：选择圆心（点击地图）")
        self.status_label.setText("请在图上点击圆心位置")

    def _start_polygon(self) -> None:
        idx = self.region_combo.findData(RegionMode.POLYGON.value)
        if idx >= 0 and self.region_combo.currentIndex() != idx:
            self.region_combo.setCurrentIndex(idx)
        if not self._js_ready:
            self._warn("地图尚未就绪，无法绘制多边形")
            return
        self.map.clear_region()
        self._polygon = None
        self.map.set_mode("polygonDraw")
        self.map_mode_label.setText("地图模式：绘制多边形（连续点击，双击闭合）")
        self.status_label.setText("连续点击添加顶点，至少 3 个，双击闭合")

    def _close_polygon(self) -> None:
        if not self._js_ready:
            self._warn("地图尚未就绪")
            return
        self.map.close_polygon()

    def _clear_region(self) -> None:
        self._circle = None
        self._polygon = None
        self._region_mode = RegionMode.NONE
        self.region_combo.blockSignals(True)
        self.region_combo.setCurrentIndex(self.region_combo.findData(RegionMode.NONE.value))
        self.region_combo.blockSignals(False)
        self.map.clear_region()
        self.map.set_mode("browse")
        self.region_label.setText("当前未限制区域（将以 A、B 中点为圆心按默认半径检索）")
        self.map_mode_label.setText("地图模式：普通浏览")
        self._invalidate_results()

    @Slot(float, float, float)
    def _on_circle_changed(self, lng: float, lat: float, radius: float) -> None:
        try:
            circle = CircleRegion(lng, lat, radius)
        except RegionError as exc:
            self._warn(str(exc))
            return
        self._circle = circle
        self._region_mode = RegionMode.CIRCLE
        idx = self.region_combo.findData(RegionMode.CIRCLE.value)
        if idx >= 0 and self.region_combo.currentIndex() != idx:
            self.region_combo.blockSignals(True)
            self.region_combo.setCurrentIndex(idx)
            self.region_combo.blockSignals(False)
        self.radius_spin.blockSignals(True)
        factor = float(self.radius_unit.currentData() or 1000.0)
        self.radius_spin.setValue(max(self.radius_spin.minimum(), radius / factor))
        self.radius_spin.blockSignals(False)
        self.region_label.setText(circle.describe())
        self.map_mode_label.setText("地图模式：圆形区域（可拖动圆心/半径）")
        self._invalidate_results()

    @Slot(list, bool)
    def _on_polygon_changed(self, points: list, closed: bool) -> None:
        if not closed:
            self.region_label.setText(f"多边形绘制中：已添加 {len(points)} 个顶点（至少 3 个）")
            return
        try:
            polygon = PolygonRegion(points)
        except RegionError as exc:
            self.region_label.setText(f"多边形无效：{exc}")
            return
        self._polygon = polygon
        self.region_label.setText(polygon.describe())

    @Slot(list)
    def _on_polygon_closed(self, points: list) -> None:
        try:
            polygon = PolygonRegion(points)
        except RegionError as exc:
            self._warn(f"多边形无效：{exc}")
            self.status_label.setText(f"多边形无效：{exc}")
            self.map.set_mode("polygonDraw")
            return
        self._polygon = polygon
        self._region_mode = RegionMode.POLYGON
        idx = self.region_combo.findData(RegionMode.POLYGON.value)
        if idx >= 0 and self.region_combo.currentIndex() != idx:
            self.region_combo.blockSignals(True)
            self.region_combo.setCurrentIndex(idx)
            self.region_combo.blockSignals(False)
        self.region_label.setText(polygon.describe())
        self.map.set_mode("polygonEdit")
        self.map_mode_label.setText("地图模式：编辑多边形（可拖动顶点）")
        self.status_label.setText("多边形已生效，可拖动顶点微调")
        self._invalidate_results()

    # ================================================================== #
    # 推荐
    # ================================================================== #
    def _run_recommendation(self) -> None:
        if not self._point_a or not self._point_b:
            self._warn("请先设置 A、B 两人的出发位置")
            return
        if not self.monitor.usable("amap_web"):
            self._warn("地图数据服务当前不可用，无法检索真实地点；请先点击「立即检测」或稍后重试。")
            return

        region_mode = RegionMode(self.region_combo.currentData())
        region = self._active_region()
        if region_mode is RegionMode.CIRCLE and region is None:
            self._warn("已选择圆形区域，请先在地图上选择圆心")
            return
        if region_mode is RegionMode.POLYGON and region is None:
            self._warn("已选择多边形区域，请先完成绘制（至少 3 个顶点）")
            return

        rec_mode = RecMode(self.mode_combo.currentData())
        req = RecommendRequest(
            point_a=self._point_a,
            point_b=self._point_b,
            region=region,
            region_mode=region_mode,
            travel_mode=TravelMode(self.travel_combo.currentData()),
            category=self.category_combo.currentData(),
            keyword=self.keyword_edit.text().strip(),
            rec_mode=rec_mode,
            custom_prefs=self.prefs_edit.toPlainText().strip(),
            top_n=int(self.topn_spin.value()),
            city_a=self._point_a.adcode or self._point_a.city,
            city_b=self._point_b.adcode or self._point_b.city,
        )

        self._cancel_active(silent=True)
        self._results = []
        self.table.setRowCount(0)
        self.map.clear_results()
        self.detail_view.clear()
        self.ai_view.clear()
        self.result_banner.clear()

        self._cancel_token = CancelToken()
        task_id = f"rank-{int(time.time() * 1000)}"
        self._current_task = task_id
        self.progress.setValue(0)
        self.status_label.setText("开始推荐…")
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self._log(f"开始推荐：{json.dumps(self._request_snapshot(req), ensure_ascii=False)}")

        coro = self.pipeline.run(
            req,
            progress=self.runtime.make_progress(task_id),
            cancel=self._cancel_token,
        )
        self.runtime.submit(
            coro,
            on_done=self._on_results,
            on_error=self._on_pipeline_error,
            task_id=task_id,
        )

    @staticmethod
    def _request_snapshot(req: RecommendRequest) -> dict[str, Any]:
        return {
            "a": req.point_a.display,
            "b": req.point_b.display,
            "region": req.region.describe() if req.region else "不限制区域",
            "travel_mode": req.travel_mode.value,
            "category": req.category,
            "rec_mode": req.rec_mode.value,
            "top_n": req.top_n,
        }

    def _cancel_active(self, silent: bool = False) -> None:
        if self._cancel_token is not None:
            self._cancel_token.cancel("用户取消或输入变更")
            self._cancel_token = None
        if self._current_task:
            self.runtime.cancel(self._current_task)
            self._current_task = None
        self.btn_cancel.setEnabled(False)
        self.btn_run.setEnabled(self.monitor.usable("amap_web"))
        if not silent:
            self.progress.setValue(0)
            self.status_label.setText("已取消当前任务")

    @Slot(str, int, str)
    def _on_progress(self, task_id: str, percent: int, message: str) -> None:
        if task_id != self._current_task:
            return
        self.progress.setValue(max(0, min(100, percent)))
        self.status_label.setText(message)
        if percent in (0, 10, 48, 80, 100):
            self._log(f"[{percent}%] {message}")

    def _on_results(self, result) -> None:
        self._current_task = None
        self._cancel_token = None
        self.btn_cancel.setEnabled(False)
        self.btn_run.setEnabled(self.monitor.usable("amap_web"))
        self._results = list(result.recommendations)
        self.progress.setValue(100)

        if not self._results:
            self.status_label.setText(NO_RESULT_MESSAGE)
            self.result_banner.show_message(NO_RESULT_MESSAGE, "warn")
            return

        self.status_label.setText(
            f"完成：{len(self._results)} 个推荐 · 候选 {result.candidates_considered} 个 · "
            f"用时 {result.elapsed_s}s · 来源 {'AI' if result.source == 'ai' else '本地规则'}"
        )
        if result.notice:
            self.result_banner.show_message(result.notice, "warn")
        else:
            self.result_banner.clear()
        self._last_notice = result.notice

        self._fill_table(self._results)
        self._render_results_on_map()
        self._render_ai(result)
        self.table.selectRow(0)
        self._log(
            f"推荐完成：source={result.source}, candidates={result.candidates_considered}, "
            f"stats={json.dumps(result.stats, ensure_ascii=False)[:300]}"
        )

    def _on_pipeline_error(self, message: str) -> None:
        self._current_task = None
        self._cancel_token = None
        self.btn_cancel.setEnabled(False)
        self.btn_run.setEnabled(self.monitor.usable("amap_web"))
        if message == "__cancelled__":
            self.progress.setValue(0)
            self.status_label.setText("任务已取消")
            return
        self.progress.setValue(0)
        clean = message.split(":", 1)[-1].strip() if message.startswith("RecommendationError") else message
        self.status_label.setText(f"推荐失败：{clean}")
        self.result_banner.show_message(clean, "error")
        self._log(f"推荐失败：{message}")
        if NO_RESULT_MESSAGE in message:
            self._warn(NO_RESULT_MESSAGE)

    def _fill_table(self, items: Sequence[Recommendation]) -> None:
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            leg_a, leg_b = item.leg_a, item.leg_b
            values = [
                str(item.rank),
                item.poi.name,
                item.poi.type_name.split(";")[-1] if item.poi.type_name else "—",
                leg_a.text if leg_a else "—",
                leg_b.text if leg_b else "—",
                str((leg_a.transfers if leg_a and leg_a.ok else 0) + (leg_b.transfers if leg_b and leg_b.ok else 0)),
                "AI" if item.source == "ai" else "规则",
            ]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if col in (0, 3, 4, 5, 6):
                    cell.setTextAlignment(Qt.AlignCenter)
                cell.setToolTip(item.reason or item.poi.address)
                self.table.setItem(row, col, cell)

    def _render_results_on_map(self) -> None:
        items = [
            {
                "rank": r.rank,
                "name": r.poi.name,
                "lng": r.poi.lng,
                "lat": r.poi.lat,
                "reason": r.reason,
            }
            for r in self._results
        ]
        self.map.show_results(items)

    def _render_ai(self, result) -> None:
        parts: list[str] = []
        parts.append(
            f"<div style='font-family:Microsoft YaHei;font-size:13px;line-height:1.8'>"
            f"<h3 style='margin:4px 0;color:#14304f'>推荐分析</h3>"
            f"<p><b>来源：</b>{'DeepSeek AI 综合推荐' if result.source == 'ai' else '本地规则排序（降级模式）'}<br/>"
            f"<b>候选数量：</b>{result.candidates_considered}<br/>"
            f"<b>区域约束：</b>{html.escape(result.region_desc)}<br/>"
            f"<b>耗时：</b>{result.elapsed_s} 秒</p>"
        )
        if result.notice:
            parts.append(
                f"<p style='background:#fff6e5;border:1px solid #f0dcae;border-radius:6px;"
                f"padding:8px;color:#8a5a00'>{html.escape(result.notice)}</p>"
            )
        if result.ai_summary:
            parts.append(f"<p style='white-space:pre-wrap'>{html.escape(result.ai_summary)}</p>")
        if result.warnings:
            parts.append("<p><b>提示：</b>" + "；".join(html.escape(w) for w in result.warnings) + "</p>")
        stats = result.stats or {}
        if stats:
            parts.append(
                "<p style='color:#5b6b80;font-size:12px'>检索分块 {tiles} 块 · API 调用 {calls} 次 · "
                "原始 {raw} 条 · 去重后 {uniq} 条 · 区域内 {inside} 条 · 可达 {routed} 条</p>".format(
                    tiles=stats.get("tiles", 0),
                    calls=stats.get("api_calls", 0),
                    raw=stats.get("raw_pois", 0),
                    uniq=stats.get("unique_pois", 0),
                    inside=stats.get("in_region", 0),
                    routed=stats.get("routed", 0),
                )
            )
        for rec in self._results:
            parts.append(
                f"<h4 style='margin:14px 0 4px;color:#1b4c8f'>{rec.rank}. {html.escape(rec.poi.name)}</h4>"
                f"<div style='color:#33465e'>{html.escape(rec.reason)}</div>"
            )
        parts.append("</div>")
        self.ai_view.setHtml("".join(parts))

    def _on_row_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._results):
            return
        self._show_detail(row)
        self.map.select_result(row)

    def _show_detail(self, index: int) -> None:
        rec = self._results[index]
        poi = rec.poi
        nav_url = (
            f"https://uri.amap.com/marker?position={poi.lng:.6f},{poi.lat:.6f}"
            f"&name={poi.name}&src=meetpoint&coordinate=gaode&callnative=1"
        )
        route_url = (
            f"https://uri.amap.com/navigation?from={self._point_a.lng:.6f},{self._point_a.lat:.6f},A"
            f"&to={poi.lng:.6f},{poi.lat:.6f},{poi.name}"
            f"&mode={'bus' if (rec.leg_a and rec.leg_a.mode is TravelMode.TRANSIT) else 'car'}"
            f"&policy=1&src=meetpoint&coordinate=gaode&callnative=1"
        )
        rows = [
            f"<h3 style='margin:2px 0;color:#14304f'>{html.escape(poi.name)}</h3>",
            f"<p style='color:#5b6b80;margin:2px 0 8px'>{html.escape(poi.address or '地址未提供')}"
            f" · {html.escape(poi.type_name or '类型未知')}</p>",
            "<table cellspacing='0' cellpadding='4' style='font-size:12px;color:#33465e'>",
        ]
        rows.append(
            f"<tr><td style='color:#7a8aa0'>POI ID</td><td>{html.escape(poi.id)}</td></tr>"
            f"<tr><td style='color:#7a8aa0'>坐标 (GCJ-02)</td><td>{poi.lng:.6f}, {poi.lat:.6f}</td></tr>"
            f"<tr><td style='color:#7a8aa0'>商圈</td><td>{html.escape(poi.business_area or '—')}</td></tr>"
            f"<tr><td style='color:#7a8aa0'>高德评分</td><td>{poi.rating if poi.rating is not None else '—'}</td></tr>"
            f"<tr><td style='color:#7a8aa0'>人均消费</td><td>{poi.cost if poi.cost is not None else '—'}</td></tr>"
            f"<tr><td style='color:#7a8aa0'>电话</td><td>{html.escape(poi.tel or '—')}</td></tr>"
        )
        rows.append("</table>")

        for title, leg in (("A 出发", rec.leg_a), ("B 出发", rec.leg_b)):
            rows.append(f"<h4 style='margin:10px 0 4px;color:#1b4c8f'>{title}路线</h4>")
            rows.append(self._leg_html(leg))

        if rec.reason:
            rows.append(
                f"<h4 style='margin:10px 0 4px;color:#1b4c8f'>推荐理由</h4>"
                f"<p style='color:#33465e'>{html.escape(rec.reason)}</p>"
            )
        rows.append(
            f"<p style='margin-top:10px'><a href='{nav_url}'>在高德地图中查看该地点</a> · "
            f"<a href='{route_url}'>从 A 出发导航</a></p>"
        )
        self.detail_view.setHtml("".join(rows))

    @staticmethod
    def _leg_html(leg: RouteLeg | None) -> str:
        if leg is None:
            return "<p style='color:#a3261e'>无路线数据</p>"
        if not leg.ok:
            return (
                f"<p style='color:#a3261e'>路线不可达：{html.escape(leg.error or '未知原因')}</p>"
            )
        cache_note = (
            f"<span style='color:#8a5a00'>（缓存于 {leg.cached_at.astimezone().strftime('%H:%M:%S') if leg.cached_at else '未知'}，非实时）</span>"
            if leg.cached
            else ""
        )
        steps = "".join(f"<li>{html.escape(s)}</li>" for s in leg.steps[:8])
        return (
            f"<p style='color:#33465e;margin:2px 0'>{html.escape(leg.summary)} {cache_note}<br/>"
            f"距离 {leg.distance_km:.2f} 公里 · 预计 {leg.duration_min:.0f} 分钟 · "
            f"换乘 {leg.transfers} 次 · 步行 {leg.walk_m / 1000:.1f} 公里</p>"
            + (f"<ol style='font-size:12px;color:#5b6b80;margin:4px 0 4px 18px'>{steps}</ol>" if steps else "")
        )

    def _select_result_index(self, index: int) -> None:
        if 0 <= index < len(self._results):
            self.table.selectRow(index)
            self._show_detail(index)

    def _open_selected_navigation(self, *_: Any) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._results):
            poi = self._results[row].poi
            QDesktopServices.openUrl(
                QUrl(
                    f"https://uri.amap.com/marker?position={poi.lng:.6f},{poi.lat:.6f}"
                    f"&name={poi.name}&src=meetpoint&coordinate=gaode&callnative=1"
                )
            )

    def _invalidate_results(self) -> None:
        if self._current_task:
            self._cancel_active(silent=True)
        if self._results:
            self._results = []
            self.table.setRowCount(0)
            self.map.clear_results()
            self.detail_view.clear()
            self.ai_view.clear()
            self.status_label.setText("输入已变更，旧推荐已失效，请重新点击「智能推荐」")

    # ================================================================== #
    # 其他
    # ================================================================== #
    def _refresh_metrics(self) -> None:
        snap = self.amap._transport.metrics.snapshot()  # noqa: SLF001
        calls = sum(snap["calls"].values())
        fails = sum(snap["failures"].values())
        tokens = 0
        if self.deepseek is not None:
            tokens = self.deepseek.usage.total_tokens
        self.metrics_label.setText(f"API 调用 {calls} 次 · 失败 {fails} 次 · Token {tokens}")

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        if hasattr(self, "log_view"):
            self.log_view.appendPlainText(f"[{stamp}] {message}")

    def _warn(self, message: str) -> None:
        QMessageBox.warning(self, "MeetPoint AI", message)

    def first_run_setup(self) -> None:
        """首次运行（未配置 Key）时引导打开设置。"""
        if self.settings.amap_web_configured and self.settings.deepseek_configured:
            return
        self.banner.show_message(
            "尚未配置 API Key：请先在「工具 → 设置」中填写高德与 DeepSeek 的 Key（保存后立刻生效）。",
            "warn",
        )
        answer = QMessageBox.question(
            self,
            "首次使用",
            "检测到尚未配置 API Key。\n\n"
            "需要：高德 Web 服务 Key、高德 JS Key + 安全密钥、DeepSeek API Key。\n"
            "是否现在打开设置填写？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._open_settings()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != QDialog.Accepted:
            return
        changes = dialog.changed_values()
        if not changes:
            self.status_label.setText("设置未发生变化")
            return

        saved_path = None
        try:
            saved_path = save_env(changes)
        except OSError as exc:
            self._warn(f"写入 .env 失败：{exc}（本次修改仅在当前会话生效）")

        new_settings = self.settings.with_overrides(**changes)
        need_reload = SettingsDialog.requires_service_reload(changes)
        js_changed = SettingsDialog.js_changed(changes)
        self.settings = new_settings

        self.topn_spin.blockSignals(True)
        self.topn_spin.setValue(max(1, min(10, new_settings.top_n)))
        self.topn_spin.blockSignals(False)

        interval = max(0, new_settings.health_interval_s)
        if interval:
            self._health_timer.setInterval(interval * 1000)
            self._health_timer.start()
        else:
            self._health_timer.stop()

        if need_reload:
            self._rebuild_services(js_changed)
        else:
            self.pipeline.settings = new_settings
            self.status_label.setText("设置已应用（推荐数量等参数对下次推荐生效）")

        self._log(
            "设置已更新："
            + ", ".join(f"{k}={v if 'key' not in k and 'secret' not in k else '***'}" for k, v in changes.items())
        )
        if saved_path:
            self._log(f"配置已写入 {saved_path}")
            self.status_label.setText(f"设置已保存到 {saved_path.name}，正在按新配置重建服务…")

    def _rebuild_services(self, js_changed: bool) -> None:
        old_transport = getattr(self.amap, "_transport", None)
        if old_transport is not None:
            try:
                self.runtime.submit(old_transport.aclose())
            except Exception:  # noqa: BLE001
                pass

        transport, amap, deepseek, monitor = build_services(self.settings, self.db)
        self.transport = transport
        self.amap, self.deepseek, self.monitor = amap, deepseek, monitor
        self.pipeline = RecommendationPipeline(amap, deepseek, self.settings, monitor, self.db)
        monitor.add_listener(lambda name, snap: self.health_updated.emit(name, snap))
        for name in SERVICE_ORDER:
            self._apply_health(name, monitor.snapshot(name))

        if js_changed:
            self._js_ready = False
            self.monitor.reset("amap_js")
            self.map.reload()
            self._js_ping_timer.start()

        self._update_capabilities()
        self._check_services()

    def prepare_demo_state(self) -> None:
        """把界面重置为确定性状态（供 `--screenshot` 自检截图使用）。"""
        self.travel_combo.setCurrentIndex(0)
        self.category_combo.setCurrentIndex(0)
        self.mode_combo.setCurrentIndex(0)
        self.prefs_edit.clear()
        self.topn_spin.blockSignals(True)
        self.topn_spin.setValue(5)
        self.topn_spin.blockSignals(False)
        scroll = self.findChild(QScrollArea)
        if scroll is not None and scroll.verticalScrollBar() is not None:
            scroll.verticalScrollBar().setValue(0)
        self.region_combo.blockSignals(True)
        self.region_combo.setCurrentIndex(self.region_combo.findData(RegionMode.NONE.value))
        self.region_combo.blockSignals(False)
        self._region_mode = RegionMode.NONE
        self._circle = None
        self._polygon = None
        self.map.clear_region()
        self.map.clear_results()
        self.table.setRowCount(0)
        self._results = []
        self.region_label.setText("当前未限制区域（将以 A、B 中点为圆心按默认半径检索）")
        self.status_label.setText("就绪")

    def _clear_history(self) -> None:
        confirm = QMessageBox.question(
            self,
            "清除历史数据",
            "将清除本地缓存、历史会话与健康日志（不影响 API Key）。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.db.clear_all()
        self._results = []
        self.table.setRowCount(0)
        self.map.clear_results()
        self.detail_view.clear()
        self.ai_view.clear()
        self.status_label.setText("历史数据与缓存已清除")
        self._log("已清除本地历史数据与缓存")

    def _export_results(self) -> None:
        if not self._results:
            self._warn("当前没有可导出的推荐结果")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出推荐结果", "meetpoint_results.json", "JSON 文件 (*.json)"
        )
        if not path:
            return
        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "point_a": self._point_a.model_dump(mode="json") if self._point_a else None,
            "point_b": self._point_b.model_dump(mode="json") if self._point_b else None,
            "region": self._active_region().to_dict() if self._active_region() else None,
            "items": [
                {
                    "rank": r.rank,
                    "poi": r.poi.model_dump(mode="json"),
                    "leg_a": r.leg_a.model_dump(mode="json") if r.leg_a else None,
                    "leg_b": r.leg_b.model_dump(mode="json") if r.leg_b else None,
                    "reason": r.reason,
                    "source": r.source,
                    "score": r.score,
                }
                for r in self._results
            ],
        }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
            self.status_label.setText(f"已导出到 {path}")
        except OSError as exc:
            self._warn(f"导出失败：{exc}")

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于 MeetPoint AI",
            "MeetPoint AI 1.0\n\n"
            "• 高德地图提供真实 POI、坐标、路线与交通数据\n"
            "• DeepSeek 负责需求理解、候选比较与推荐理由\n"
            "• Python 负责几何硬约束校验与确定性排序\n\n"
            f"数据目录：{self.settings.data_dir}\n"
            f"高德 Key：{self.settings.masked()['amap_web_key']}\n"
            f"DeepSeek Key：{self.settings.masked()['deepseek_api_key']}\n"
            f"模型：{self.settings.deepseek_model}",
        )
