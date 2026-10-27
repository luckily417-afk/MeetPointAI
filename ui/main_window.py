"""主窗口：左侧控制面板 + 右侧高德地图 + 底部结果/分析，顶部三路服务状态。"""

from __future__ import annotations

import asyncio
import html
import json
import time
from urllib.parse import quote
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
    QStackedWidget,
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
from ui.detail_pane import DetailPane
from ui.map_bridge import MapBridge, MapController
from ui.settings_dialog import SettingsDialog
from ui.results_table import ResultTable
from ui.theme import APP_QSS, TOKENS
from ui.widgets import (
    Banner,
    ChevronComboBox,
    ChevronSpinBox,
    HLine,
    RadiusSpinBox,
    SegmentedControl,
    ServicePill,
)

__all__ = ["MainWindow"]


def _strip_exception_prefix(message: str) -> str:
    """AsyncRuntime 回传的是「模块名.异常类: 说明」，界面只该显示说明本身。"""
    head, sep, tail = message.partition(": ")
    if sep and "Error" in head and "\uff1a" not in head:
        cleaned = tail.strip()
        if cleaned:
            return cleaned
    return message


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
        self._retired_transports: list[Any] = []

        self.setWindowTitle("MeetPoint AI")
        self.resize(1480, 940)
        self.setMinimumSize(1100, 700)
        self.setStyleSheet(APP_QSS)

        # 上次在界面上设置的推荐数量（sqlite 里存的）要真正生效，否则重启后悄悄变回 .env 的值
        self.settings = self._restore_session_settings()

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
    def _restore_session_settings(self) -> Settings:
        """把上次界面里设置的 top_n 找回来（与「设置」对话框写 .env 的行为保持一致）。"""
        try:
            stored = self.db.get_setting("top_n")
        except Exception:  # noqa: BLE001
            return self.settings
        if stored is None:
            return self.settings
        try:
            value = max(1, min(10, int(stored)))
        except (TypeError, ValueError):
            return self.settings
        restored = self.settings.with_overrides(top_n=value)
        self.pipeline.settings = restored
        return restored

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
        banner_layout.setContentsMargins(20, 8, 20, 0)
        banner_layout.setSpacing(6)
        banner_layout.addWidget(self.banner)
        banner_layout.addWidget(self.result_banner)
        outer.addWidget(banner_host)

        # 一栏表单 + 一栏地图/结果：左右两栏各自一个左边缘，中间 1px 分隔
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        left = self._build_left_panel()
        left.setFixedWidth(352)
        self._form_panel = left
        body_layout.addWidget(left)
        body_layout.addWidget(self._build_right_column(), 1)
        outer.addWidget(body, 1)

        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("muted")
        self.status_label.setMinimumWidth(0)
        bar.addWidget(self.status_label, 1)
        self.metrics_label = QLabel("API 调用 0 次 · 失败 0 次 · Token 0")
        self.metrics_label.setObjectName("meta")
        self.metrics_label.setStyleSheet("padding-right:12px;")
        bar.addPermanentWidget(self.metrics_label)

    # ------------------------------------------------------------------ #
    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(48)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(10)

        from ui.widgets import ProductMark

        layout.addWidget(ProductMark())
        title = QLabel("MeetPoint AI")
        title.setObjectName("wordmark")
        layout.addWidget(title)

        divider = QLabel()
        divider.setObjectName("vline")
        divider.setFixedHeight(16)
        layout.addWidget(divider)

        self.scope_chip = QLabel("双人见面地点推荐")
        self.scope_chip.setObjectName("scopeChip")
        layout.addWidget(self.scope_chip)

        layout.addStretch(1)

        short_titles = {"amap_web": "高德 Web", "amap_js": "高德 JS", "deepseek": "DeepSeek"}
        self.pills: dict[str, ServicePill] = {}
        for index, name in enumerate(SERVICE_ORDER):
            if index:
                spacer = QLabel("·")
                spacer.setObjectName("meta")
                layout.addWidget(spacer)
            pill = ServicePill(short_titles.get(name, name))
            pill.setToolTip(SERVICE_LABELS.get(name, name))
            self.pills[name] = pill
            layout.addWidget(pill)

        layout.addSpacing(4)
        self.btn_check = QPushButton("立即检测")
        self.btn_check.setObjectName("quietAction")
        self.btn_check.setToolTip("分别检测高德 Web 服务、高德 JS API、DeepSeek API")
        layout.addWidget(self.btn_check)
        return bar

    # ------------------------------------------------------------------ #
    def _section(self, title: str, note: str = "") -> tuple[QWidget, QVBoxLayout]:
        """左栏的一个表单区：小标题 + 内容，无卡片、无序号。"""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        head = QLabel(title)
        head.setObjectName("sectionTitle")
        layout.addWidget(head)
        if note:
            hint = QLabel(note)
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        return box, layout

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def _joined_field(self, placeholder: str) -> tuple[QFrame, QLineEdit]:
        """输入框 + 内嵌动作：共用一个 1px 边框，右侧方形搜索按钮。"""
        frame = QFrame()
        frame.setObjectName("joined")
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        edit = QLineEdit()
        edit.setObjectName("bare")
        edit.setPlaceholderText(placeholder)
        edit.setClearButtonEnabled(True)
        edit.setFrame(False)
        row.addWidget(edit, 1)
        return frame, edit

    def _field_block(self, label: str, widget: QWidget, stretch: int = 1) -> QWidget:
        """标签在上、控件在下（表单控件的高度统一为 32px）。"""
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        layout.addWidget(caption)
        layout.addWidget(widget)
        host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        _ = stretch
        return host

    def _build_left_panel(self) -> QWidget:
        container = QFrame()
        container.setObjectName("formPanel")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(20, 14, 14, 12)
        layout.setSpacing(14)

        # ---- 出发位置 ---------------------------------------------------- #
        box_points, points_layout = self._section("出发位置", "名称、地址或 GCJ-02 坐标。")
        for user in ("A", "B"):
            block = QWidget()
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 0, 0, 0)
            block_layout.setSpacing(4)

            btn_map = QPushButton("在地图上选点")
            btn_map.setObjectName("textAction")
            btn_map.setCursor(Qt.PointingHandCursor)
            btn_clear = QPushButton("清除")
            btn_clear.setObjectName("textAction")
            btn_clear.setCursor(Qt.PointingHandCursor)

            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(7)
            dot = QLabel()
            dot.setFixedSize(7, 7)
            dot.setStyleSheet(
                f"background:{'#1d5fbf' if user == 'A' else '#141414'};border-radius:3px;"
            )
            label = QLabel(f"出发地 {user}")
            label.setObjectName("fieldLabel")
            head.addWidget(dot)
            head.addWidget(label)
            head.addStretch(1)
            head.addWidget(btn_map)
            head.addSpacing(4)
            head.addWidget(btn_clear)
            block_layout.addLayout(head)

            frame, edit = self._joined_field(f"{user} 的名称、地址或经纬度")
            btn_search = QPushButton("搜索")
            btn_search.setObjectName("textAction")
            btn_search.setCursor(Qt.PointingHandCursor)
            btn_search.setFixedWidth(48)
            btn_search.setStyleSheet("border-left:1px solid #edf0f2;border-radius:0;")
            frame.layout().addWidget(btn_search)
            block_layout.addWidget(frame)

            points_layout.addWidget(block)

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
        layout.addWidget(box_points)

        # ---- 出行与偏好-------------------------------------------------- #
        box_pref, pref_layout = self._section("出行与偏好")

        self.travel_combo = SegmentedControl()
        for value, label in TRAVEL_MODE_LABELS.items():
            self.travel_combo.addItem(label, value)
        self.travel_combo.setFixedHeight(32)
        pref_layout.addWidget(self._field_block("出行方式", self.travel_combo))

        pair = QHBoxLayout()
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(10)
        self.category_combo = ChevronComboBox()
        for name in CATEGORY_PRESETS:
            self.category_combo.addItem(name, name)
        self.category_combo.setFixedHeight(32)
        pair.addWidget(self._field_block("场所类型", self.category_combo), 1)
        self.mode_combo = ChevronComboBox()
        for value, label in REC_MODE_LABELS.items():
            self.mode_combo.addItem(label, value)
        self.mode_combo.setFixedHeight(32)
        pair.addWidget(self._field_block("推荐模式", self.mode_combo), 1)
        pref_layout.addLayout(pair)

        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("可选，例如 咖啡、火锅")
        self.keyword_edit.setFixedHeight(32)
        pref_layout.addWidget(self._field_block("自定义关键词", self.keyword_edit))

        self.prefs_edit = QPlainTextEdit()
        self.prefs_edit.setPlaceholderText("例如：安静、适合聊天、人均 80 元以内，最好离地铁近")
        self.prefs_edit.setFixedHeight(58)
        pref_layout.addWidget(self._field_block("偏好描述", self.prefs_edit))
        layout.addWidget(box_pref)

        # ---- 推荐区域 ---------------------------------------------------- #
        box_region, region_layout = self._section("推荐区域")

        region_row = QHBoxLayout()
        region_row.setContentsMargins(0, 0, 0, 0)
        region_row.setSpacing(10)
        self.region_combo = ChevronComboBox()
        for value, label in REGION_MODE_LABELS.items():
            self.region_combo.addItem(label, value)
        self.region_combo.setFixedHeight(32)
        region_row.addWidget(self._field_block("区域限制", self.region_combo), 1)

        radius_host = QWidget()
        radius_layout = QVBoxLayout(radius_host)
        radius_layout.setContentsMargins(0, 0, 0, 0)
        radius_layout.setSpacing(4)
        radius_caption = QLabel("半径")
        radius_caption.setObjectName("fieldLabel")
        radius_layout.addWidget(radius_caption)
        radius_inputs = QHBoxLayout()
        radius_inputs.setContentsMargins(0, 0, 0, 0)
        radius_inputs.setSpacing(6)
        self.radius_spin = RadiusSpinBox()
        self.radius_spin.setValue(3.0)
        self.radius_spin.setFixedHeight(32)
        radius_inputs.addWidget(self.radius_spin, 1)
        self.radius_unit = ChevronComboBox()
        self.radius_unit.addItem("千米", 1000.0)
        self.radius_unit.addItem("米", 1.0)
        self.radius_unit.setFixedHeight(32)
        self.radius_unit.setFixedWidth(70)
        self.radius_unit.setToolTip("切换半径单位；数值会自动换算，不改变实际范围")
        radius_inputs.addWidget(self.radius_unit)
        radius_layout.addLayout(radius_inputs)
        radius_host.setFixedWidth(158)
        region_row.addWidget(radius_host)
        region_layout.addLayout(region_row)

        region_actions = QHBoxLayout()
        region_actions.setContentsMargins(0, 0, 0, 0)
        region_actions.setSpacing(2)
        self.btn_circle = QPushButton("在地图上选圆心")
        self.btn_circle.setObjectName("textAction")
        self.btn_circle.setCursor(Qt.PointingHandCursor)
        self.btn_polygon = QPushButton("绘制多边形")
        self.btn_polygon.setObjectName("textAction")
        self.btn_polygon.setCursor(Qt.PointingHandCursor)
        self.btn_close_poly = QPushButton("闭合")
        self.btn_close_poly.setObjectName("textAction")
        self.btn_close_poly.setCursor(Qt.PointingHandCursor)
        self.btn_clear_region = QPushButton("清除区域")
        self.btn_clear_region.setObjectName("textAction")
        self.btn_clear_region.setCursor(Qt.PointingHandCursor)
        self.btn_clear_region.setStyleSheet("color:#b3261e;")
        region_actions.addWidget(self.btn_circle)
        region_actions.addStretch(1)
        region_actions.addWidget(self.btn_polygon)
        region_actions.addWidget(self.btn_close_poly)
        region_actions.addWidget(self.btn_clear_region)
        region_layout.addLayout(region_actions)

        self.region_label = QLabel("当前未限制区域（将以 A、B 中点为圆心按默认半径检索）")
        self.region_label.setObjectName("meta")
        self.region_label.setWordWrap(True)
        region_layout.addWidget(self.region_label)
        layout.addWidget(box_region)

        layout.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # ---- 固定在底部的主操作 -------------------------------------------- #
        footer = QFrame()
        footer.setObjectName("formFooter")
        foot = QVBoxLayout(footer)
        foot.setContentsMargins(20, 10, 14, 14)
        foot.setSpacing(8)

        topn_row = QHBoxLayout()
        topn_row.setContentsMargins(0, 0, 0, 0)
        topn_row.setSpacing(8)
        topn_row.addWidget(self._field_label("推荐数量"))
        self.topn_spin = ChevronSpinBox()
        self.topn_spin.setRange(1, 10)
        self.topn_spin.setValue(max(1, min(10, self.settings.top_n)))
        self.topn_spin.setSuffix(" 个")
        self.topn_spin.setLocale(QLocale.c())
        self.topn_spin.setFixedWidth(78)
        self.topn_spin.setToolTip("最终展示的推荐条数（也可在「工具 → 设置」中批量调整检索参数）")
        topn_row.addWidget(self.topn_spin)
        topn_row.addStretch(1)
        self.btn_cancel = QPushButton("取消当前任务")
        self.btn_cancel.setObjectName("textAction")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setEnabled(False)
        topn_row.addWidget(self.btn_cancel)
        foot.addLayout(topn_row)

        self.btn_run = QPushButton("开始推荐")
        self.btn_run.setObjectName("primary")
        self.btn_run.setMinimumHeight(38)
        self.btn_run.setCursor(Qt.PointingHandCursor)
        self.btn_run.setToolTip("按当前的出发点、出行方式与区域约束检索见面地点")
        foot.addWidget(self.btn_run)

        outer.addWidget(footer)
        return container

    def _build_right_column(self) -> QWidget:
        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(12)
        title_block = QVBoxLayout()
        title_block.setContentsMargins(0, 0, 0, 0)
        title_block.setSpacing(4)
        h1 = QLabel("见面地点")
        h1.setObjectName("pageTitle")
        title_block.addWidget(h1)
        self.scope_label = QLabel("先填好 A、B 两个出发点，再点「开始推荐」。")
        self.scope_label.setObjectName("pageScope")
        title_block.addWidget(self.scope_label)
        title_row.addLayout(title_block, 1)
        layout.addLayout(title_row)

        self.map_split = QSplitter(Qt.Vertical)
        self.map_split.setChildrenCollapsible(False)
        self.map_split.setHandleWidth(1)
        self.map_split.addWidget(self._build_map_panel())
        self.map_split.addWidget(self._build_bottom_panel())
        self.map_split.setStretchFactor(0, 3)
        self.map_split.setStretchFactor(1, 2)
        self.map_split.splitterMoved.connect(lambda *_: setattr(self, "_splits_dragged", True))
        layout.addWidget(self.map_split, 1)
        return right

    def _size_map_split(self) -> None:
        """地图与结果、结果与详情按可用空间分配（布局稳定后再设定，避免首次挤出窗口）。"""
        split = getattr(self, "map_split", None)
        if split is not None:
            total = split.height()
            if total > 0:
                top = max(240, int(total * 0.52))
                split.setSizes([top, max(220, total - top)])
        results = getattr(self, "results_split", None)
        if results is not None:
            width = results.width()
            if width > 0:
                detail = max(264, min(420, int(width * 0.36)))
                results.setSizes([max(320, width - detail), detail])

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        narrow = self.width() < 1280
        panel = getattr(self, "_form_panel", None)
        if panel is not None:
            panel.setFixedWidth(316 if narrow else 352)
        table = getattr(self, "table", None)
        if table is not None and getattr(self, "_compact_table", None) != narrow:
            self._compact_table = narrow
            table.set_compact(narrow)
        if not getattr(self, "_splits_dragged", False):
            QTimer.singleShot(0, self._size_map_split)

    def _build_map_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        toolbar = QFrame()
        toolbar.setObjectName("panelHeader")
        toolbar.setFixedHeight(40)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(16, 0, 10, 0)
        row.setSpacing(8)
        self.map_mode_label = QLabel("地图模式：普通浏览")
        self.map_mode_label.setObjectName("muted")
        btn_fit = QPushButton("全览")
        btn_fit.setObjectName("quietAction")
        btn_fit.setCursor(Qt.PointingHandCursor)
        btn_reload = QPushButton("重新加载地图")
        btn_reload.setObjectName("quietAction")
        btn_reload.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.map_mode_label)
        row.addStretch(1)
        row.addWidget(btn_fit)
        row.addWidget(btn_reload)
        layout.addWidget(toolbar)

        self.map_view = QWebEngineView()
        self.map_view.setMinimumSize(320, 200)
        self.map_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.map_bridge = MapBridge(self)
        self.map = MapController(self.map_view, self.map_bridge, self.settings, self)
        layout.addWidget(self.map_view, 1)

        btn_fit.clicked.connect(self._fit_map)
        btn_reload.clicked.connect(self._reload_map)
        return panel

    def _build_bottom_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        head = QFrame()
        head.setObjectName("panelHeader")
        head_row = QHBoxLayout(head)
        head_row.setContentsMargins(16, 10, 12, 10)
        head_row.setSpacing(10)
        title = QLabel("推荐结果")
        title.setObjectName("sectionTitle")
        head_row.addWidget(title)
        self.summary_label = QLabel("尚未开始")
        self.summary_label.setObjectName("muted")
        head_row.addWidget(self.summary_label)
        head_row.addStretch(1)
        self.source_label = QLabel("")
        self.source_label.setObjectName("meta")
        head_row.addWidget(self.source_label)
        layout.addWidget(head)

        self.progress = QProgressBar()
        self.progress.setLocale(QLocale.c())
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(1)

        self.empty_hint = self._build_empty_hint()
        self.table = ResultTable()
        self.result_stack = QStackedWidget()
        self.result_stack.addWidget(self.empty_hint)
        self.result_stack.addWidget(self.table)
        self.result_stack.setCurrentWidget(self.empty_hint)
        split.addWidget(self.result_stack)

        detail_host = QWidget()
        detail_layout = QVBoxLayout(detail_host)
        detail_layout.setContentsMargins(16, 6, 12, 10)
        detail_layout.setSpacing(6)
        self.tabs = QTabWidget()
        self.detail_view = DetailPane()
        self.ai_view = QTextBrowser()
        self.ai_view.setOpenExternalLinks(True)
        self.ai_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.detail_view, "地点详情")
        self.tabs.addTab(self.ai_view, "AI 分析")
        self.tabs.addTab(self.log_view, "服务日志")
        detail_layout.addWidget(self.tabs, 1)
        detail_host.setMinimumWidth(264)
        split.addWidget(detail_host)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setCollapsible(0, False)
        split.setSizes([700, 360])
        self.results_split = split
        split.splitterMoved.connect(lambda *_: setattr(self, "_splits_dragged", True))
        layout.addWidget(split, 1)


        return panel

    def _build_empty_hint(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(24, 32, 24, 32)
        layout.setSpacing(8)
        layout.addStretch(1)
        title = QLabel("还没有推荐结果")
        title.setObjectName("sectionTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        hint = QLabel("填好 A、B 两个出发点，选好出行方式与区域，再点左下角的「开始推荐」。")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return host

    @staticmethod
    def _muted_html(text: str) -> str:
        return (
            f"<div style='color:#666c73;font-size:12px;line-height:160%'>{html.escape(text)}</div>"
        )

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
        self.category_combo.currentIndexChanged.connect(lambda _=0: self._update_scope())
        self.mode_combo.currentIndexChanged.connect(lambda _=0: self._update_scope())
        self.travel_combo.currentIndexChanged.connect(lambda _=0: self._update_scope())
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
        self.map.load_timed_out.connect(self._on_map_timeout)
        self.monitor.add_listener(
            lambda name, snap: self.health_updated.emit(name, snap)
        )

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not getattr(self, "_split_sized", False):
            self._split_sized = True
            QTimer.singleShot(0, self._size_map_split)
        if not getattr(self, "_map_loaded", False):
            self._map_loaded = True
            self._map_load_started = time.perf_counter()
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
        # 无条件 ping：页面若只是偶发脚本错误（ready 已上报过），ping 能让状态自动恢复，
        # 而不是必须由用户手动点「立即检测 / 重新加载地图」。
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

    def _amap_available(self) -> bool:
        """能否发起检索：健康（正常/延迟较高），或首次体检还没回来（UNKNOWN）。

        UNKNOWN 只是「尚未检测」，不是「不可用」：按钮在这期间是可点的
        （见 _update_capabilities），运行入口必须用同一套判断，否则用户点下去
        只会得到一句「服务当前不可用」。真正的故障会在调用时由服务层报错。
        """
        state = self.monitor.state_of("amap_web")
        return state.is_usable or state is ServiceState.UNKNOWN

    def _update_capabilities(self) -> None:
        amap_state = self.monitor.state_of("amap_web")
        js_state = self.monitor.state_of("amap_js")
        ds_state = self.monitor.state_of("deepseek")

        pending = amap_state is ServiceState.UNKNOWN  # 还没做过第一次体检
        amap_ok = amap_state.is_usable or pending
        # 运行中不能被健康回写「重新点亮」：否则用户在 AI 阶段点一下就把自己的任务取消了
        running = bool(self._current_task)
        self.btn_run.setEnabled(amap_ok and not running)
        self.btn_cancel.setEnabled(running)
        if amap_state.is_failure:
            label = STATE_LABELS.get(amap_state.value, amap_state.value)
            detail = self.monitor.snapshot("amap_web").detail
            self.banner.show_message(
                f"地图数据服务不可用（{label}）：禁止生成未经验证的新地点或路线。"
                f"{('原因：' + detail) if detail else ''} 已缓存结果仅作历史参考，请手动重试。",
                "error",
            )
        elif amap_state is ServiceState.NOT_CONFIGURED:
            self.banner.show_message("未配置高德 Web 服务 API Key，无法检索真实地点。", "error")
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
        elif pending:
            self.banner.show_message("正在检测高德服务状态，稍候即可开始推荐…", "info")
        else:
            self.banner.clear()

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
        started = getattr(self, "_map_load_started", None)
        latency = int((time.perf_counter() - started) * 1000) if started else None
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

    @Slot(str)
    def _on_map_timeout(self, message: str) -> None:
        """地图初始化超时 / 页面读取失败：以前这个信号没人接收，用户完全看不到原因。"""
        self._js_ready = False
        self.monitor.note_js_status(False, message)
        self.status_label.setText(f"地图不可用：{message}")
        self.banner.show_message(f"{message}。仍可用文字地址输入继续推荐。", "warn")
        self._log(f"地图异常：{message}")

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
        self._map_load_started = time.perf_counter()
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
        # 清掉在飞的地点搜索：否则几百毫秒后它的结果回调会把这个点又填回来
        self._search_tasks.pop(user, None)
        self.map.clear_point(user)
        self._invalidate_results()
        self._update_scope()
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
            on_done=lambda points, tid=task_id: self._on_search_done(user, tid, points),
            on_error=lambda msg, tid=task_id: self._on_search_error(user, tid, msg),
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
        self._update_scope()

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
        self._update_scope()

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
        QTimer.singleShot(0, self._update_scope)
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
        # 协程运行在后台线程，通过队列信号回到 GUI 线程写日志
        self._invalidate_results()

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
        if not self._amap_available():
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
        self.table.set_results([])
        self.result_stack.setCurrentWidget(self.empty_hint)
        self.map.clear_results()
        self.detail_view.show_empty("输入变更后旧结果已失效，重新推荐后这里会显示两端路线。")
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
            # 回调必须带上 task_id：旧任务的 settle 是异步到达的，
            # 不能让它把刚开始的新任务状态清掉。
            on_done=lambda result, tid=task_id: self._on_results(tid, result),
            on_error=lambda msg, tid=task_id: self._on_pipeline_error(tid, msg),
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
        self.btn_run.setEnabled(self._amap_available())
        if not silent:
            self.progress.setValue(0)
            self.progress.setVisible(False)
            self.status_label.setText("已取消当前任务")

    def _hide_progress_if_idle(self) -> None:
        """900ms 后收起进度条；期间若已开始新任务，不能把新进度条藏起来。"""
        if not self._current_task:
            self.progress.hide()

    @Slot(str, int, str)
    def _on_progress(self, task_id: str, percent: int, message: str) -> None:
        if task_id != self._current_task:
            return
        self.progress.setVisible(True)
        self.progress.setValue(max(0, min(100, percent)))
        self.status_label.setText(message)
        if percent in (0, 10, 48, 80, 100):
            self._log(f"[{percent}%] {message}")

    def _on_results(self, task_id: str, result) -> None:
        if task_id != self._current_task:
            return  # 旧任务的迟到结果，不能覆盖当前任务
        self._current_task = None
        self._cancel_token = None
        self.btn_cancel.setEnabled(False)
        self.btn_run.setEnabled(self._amap_available())
        self._results = list(result.recommendations)
        self.progress.setValue(100)
        QTimer.singleShot(900, self._hide_progress_if_idle)

        if not self._results:
            self.status_label.setText(NO_RESULT_MESSAGE)
            self.result_banner.show_message(NO_RESULT_MESSAGE, "warn")
            return

        pool_detail = f"候选 {result.candidates_considered} 个 · 双方可达 {result.stats.get('routed', 0)} 个"
        if result.source == "ai":
            pool_detail += f" · AI 分批比较（每批至多 {self.settings.ai_candidate_limit} 个）"
        self.status_label.setText(
            f"完成：{len(self._results)} 个推荐 · {pool_detail} · "
            f"用时 {result.elapsed_s}s · 来源 {'AI' if result.source == 'ai' else '本地规则'}"
        )
        self.summary_label.setText(
            f"{len(self._results)} 个 · {pool_detail} · 用时 {result.elapsed_s}s"
        )
        self.source_label.setText("AI 综合推荐" if result.source == "ai" else "本地规则 · 降级")
        self._update_scope()
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

    def _on_pipeline_error(self, task_id: str, message: str) -> None:
        if task_id != self._current_task:
            return  # 旧任务的取消/失败回调，忽略
        self._current_task = None
        self._cancel_token = None
        self.btn_cancel.setEnabled(False)
        self.btn_run.setEnabled(self._amap_available())
        if message == "__cancelled__":
            self.progress.setValue(0)
            self.progress.setVisible(False)
            self.status_label.setText("任务已取消")
            return
        self.progress.setValue(0)
        self.progress.setVisible(False)
        clean = _strip_exception_prefix(message)
        self.status_label.setText(f"推荐失败：{clean}")
        self.result_banner.show_message(clean, "error")
        self._log(f"推荐失败：{message}")
        if NO_RESULT_MESSAGE in message:
            self._warn(NO_RESULT_MESSAGE)

    def _fill_table(self, items: Sequence[Recommendation]) -> None:
        self.table.set_results(items)
        self.result_stack.setCurrentWidget(self.table if items else self.empty_hint)

    def _update_scope(self) -> None:
        """标题下那一行：没有结果时说清本次检索范围，有结果时说出最值得看的一条发现。"""
        if self._results:
            best = self._results[0]
            leg_a, leg_b = best.leg_a, best.leg_b
            parts = [f"最佳 {best.poi.name}"]
            if leg_a and leg_a.ok and leg_b and leg_b.ok:
                total = leg_a.duration_min + leg_b.duration_min
                gap = abs(leg_a.duration_min - leg_b.duration_min)
                parts.append(f"两端共 {total:.0f} 分钟")
                parts.append(f"相差 {gap:.0f} 分钟" if gap >= 1 else "双方用时一致")
            parts.append(f"{len(self._results)} 个候选")
            self.scope_label.setText(" · ".join(parts))
            return
        a = self._point_a.display if self._point_a else "A 未设置"
        b = self._point_b.display if self._point_b else "B 未设置"
        travel = TRAVEL_MODE_LABELS.get(str(self.travel_combo.currentData()), "公共交通")
        category = self.category_combo.currentData() or "任意类型"
        self.scope_label.setText(f"{a} → {b} · {travel} · {category} · {self._region_brief()}")

    def _region_brief(self) -> str:
        """标题行里的区域描述要短：细节留给左栏的区域说明。"""
        if self._region_mode is RegionMode.CIRCLE and self._circle is not None:
            return f"{self._circle.radius_m / 1000:.1f} 公里圆形区域"
        if self._region_mode is RegionMode.POLYGON and self._polygon is not None:
            return f"多边形区域（{len(self._polygon.points)} 个顶点）"
        return "不限制区域"

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
        stats = result.stats or {}
        source = "DeepSeek AI 综合推荐" if result.source == "ai" else "本地规则排序（降级模式）"
        cells = [
            (str(result.candidates_considered), "个候选"),
            (str(stats.get("routed", 0)), "个双方可达"),
            (f"{result.elapsed_s:g}", "秒"),
            (str(stats.get("api_calls", 0)), "次 API 调用"),
        ]
        parts = ["<div style='font-size:12px;color:#666c73'>"]
        parts.append(
            "<table cellspacing='0' cellpadding='0' width='100%'><tr>"
            + "".join(
                f"<td width='25%'><span style='font-size:20px;color:#0a0a0a'>{value}</span>"
                f"<span style='font-size:11px;color:#666c73'> {unit}</span></td>"
                for value, unit in cells
            )
            + "</tr></table>"
        )
        parts.append(f"<p style='margin:6px 0 0'>来源：{source}</p>")
        parts.append(f"<p style='margin:2px 0 16px'>区域约束：{html.escape(result.region_desc)}</p>")

        if result.notice:
            parts.append(
                "<p style='background:#fffbeb;border:1px solid #f3e2ad;padding:8px;"
                f"color:#92400e'>{html.escape(result.notice)}</p>"
            )
        if result.ai_summary:
            parts.append("<p style='margin:0 0 4px;color:#666c73'>分析摘要</p>")
            parts.append(
                f"<p style='margin:0 0 16px;color:#0a0a0a;font-size:12px'>{html.escape(result.ai_summary)}</p>"
            )
        if result.warnings:
            parts.append(
                "<p style='margin:0 0 12px;color:#92400e'>"
                + "<br>".join(html.escape(w) for w in result.warnings)
                + "</p>"
            )
        if stats:
            parts.append(
                "<p style='margin:0 0 16px'>检索分块 {tiles} 块 · 原始 {raw} 条 · 去重后 {uniq} 条 · "
                "区域内 {inside} 条 · 可达 {routed} 条</p>".format(
                    tiles=stats.get("tiles", 0),
                    raw=stats.get("raw_pois", 0),
                    uniq=stats.get("unique_pois", 0),
                    inside=stats.get("in_region", 0),
                    routed=stats.get("routed", 0),
                )
            )

        if self._results:
            parts.append("<p style='margin:0 0 6px;color:#666c73'>逐条理由</p>")
            for rec in self._results:
                leg_a, leg_b = rec.leg_a, rec.leg_b
                meta = ""
                if leg_a and leg_a.ok and leg_b and leg_b.ok:
                    meta = (
                        f"A {leg_a.duration_min:.0f} 分 / B {leg_b.duration_min:.0f} 分 · "
                        f"共 {leg_a.duration_min + leg_b.duration_min:.0f} 分"
                    )
                parts.append(
                    "<table cellspacing='0' cellpadding='0' width='100%'><tr>"
                    f"<td width='22' valign='top'><span style='font-size:12px'>{rec.rank}</span></td>"
                    "<td><span style='font-size:13px;color:#0a0a0a'>"
                    f"{html.escape(rec.poi.name)}</span><br/>"
                    f"<span style='font-size:11px;color:#666c73'>{html.escape(meta)}</span><br/>"
                    f"<span style='font-size:12px;color:#0a0a0a'>{html.escape(rec.reason)}</span>"
                    "</td></tr><tr><td height='14'></td><td></td></tr></table>"
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
        self.detail_view.show_recommendation(
            rec, self._point_a, self._point_b, lambda url: QDesktopServices.openUrl(QUrl(url))
        )

    def _select_result_index(self, index: int) -> None:
        if 0 <= index < len(self._results):
            self.table.selectRow(index)
            self._show_detail(index)

    def _open_selected_navigation(self, *_: Any) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self._results):
            poi = self._results[row].poi
            name = quote(poi.name, safe="")
            QDesktopServices.openUrl(
                QUrl(
                    f"https://uri.amap.com/marker?position={poi.lng:.6f},{poi.lat:.6f}"
                    f"&name={name}&src=meetpoint&coordinate=gaode&callnative=1"
                )
            )

    def _invalidate_results(self) -> None:
        if self._current_task:
            self._cancel_active(silent=True)
            # 旧任务的完成回调会因为 task_id 不匹配被丢弃，进度条必须在这里自己收回去
            self.progress.setValue(0)
            self.progress.setVisible(False)
        if self._results:
            self._results = []
            self.table.set_results([])
            self.result_stack.setCurrentWidget(self.empty_hint)
            self.map.clear_results()
            self.detail_view.show_empty()
            self.ai_view.clear()
            self.summary_label.setText("输入已变更，请重新推荐")
            self.source_label.setText("")
            self._update_scope()
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
            + ", ".join(f"{k}={self._mask_setting(k, v)}" for k, v in changes.items())
        )
        if saved_path:
            self._log(f"配置已写入 {saved_path}")
            self.status_label.setText(f"设置已保存到 {saved_path.name}，正在按新配置重建服务…")

    @staticmethod
    def _mask_setting(name: str, value: Any) -> Any:
        """日志脱敏：凭据字段一律不落盘（amap_js_security_code 这种名字也含 code/key）。"""
        lowered = str(name).lower()
        if any(token in lowered for token in ("key", "secret", "code", "token", "password")):
            return "***"
        return value

    def _rebuild_services(self, js_changed: bool) -> None:
        # 先停掉在跑的推荐：它还在用旧连接池，直接关掉会让请求报
        # "Cannot send a request, as the client has been closed"
        self._cancel_active(silent=True)
        old_transport = getattr(self.amap, "transport", None)
        if old_transport is not None:
            # 旧连接池不立刻关：可能还有排队的请求，留到退出时统一收尾
            self._retired_transports.append(old_transport)

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
            self.map.update_settings(self.settings)  # 新 Key/安全密钥必须注入到页面
            self.map.reload()
            self._js_ping_timer.start()
        else:
            self.map.update_settings(self.settings)

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
        self.table.set_results([])
        self.result_stack.setCurrentWidget(self.empty_hint)
        self._results = []
        self.summary_label.setText("尚未开始")
        self.source_label.setText("")
        self.detail_view.show_empty()
        self.region_label.setText("当前未限制区域（将以 A、B 中点为圆心按默认半径检索）")
        self._update_scope()
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
        self.table.set_results([])
        self.result_stack.setCurrentWidget(self.empty_hint)
        self.map.clear_results()
        self.detail_view.show_empty()
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
            "MeetPoint AI 1.0 · 双人智能见面地点推荐\n\n"
            "• 高德地图提供真实 POI、坐标、路线与交通数据\n"
            "• DeepSeek 负责需求理解、候选比较与推荐理由\n"
            "• Python 负责几何硬约束校验与确定性排序\n"
            "• 命令行：meetpoint-debug（--help 查看全部命令）\n\n"
            f"数据目录：{self.settings.data_dir}\n"
            f"高德 Key：{self.settings.masked()['amap_web_key']}\n"
            f"DeepSeek Key：{self.settings.masked()['deepseek_api_key']}\n"
            f"模型：{self.settings.deepseek_model}",
        )
