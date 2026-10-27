"""右侧详情面板：一条推荐的两端通勤对比 + 地点信息 + 导航入口。

用真实控件而不是 HTML：通勤条、kv 行、动作按钮都能按 1px 线与墨色精确排版。
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import quote as _urlquote

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from models.schemas import Recommendation, RouteLeg, TravelMode, UserPoint
from ui.theme import TOKENS, ui_font

__all__ = ["DetailPane"]


class Meter(QWidget):
    """4px 通勤条：同一比例尺下的实心条，底色为一条 1px 色阶。"""

    def __init__(self, ratio: float, colour: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ratio = max(0.0, min(1.0, ratio))
        self._colour = colour
        self.setFixedHeight(4)
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(TOKENS["rule"]))
        width = int(round(self.width() * self._ratio))
        if width > 0:
            painter.fillRect(QRectF(0, 0, width, self.height()), QColor(self._colour))
        painter.end()


def _label(
    text: str,
    size: int = 12,
    weight: int = 400,
    colour: str | None = None,
    wrap: bool = True,
) -> QLabel:
    label = QLabel(text)
    label.setFont(ui_font(size, weight))
    label.setStyleSheet(f"color:{colour or TOKENS['muted']};")
    label.setWordWrap(wrap)
    return label


def _elide(text: str, width: int, size: int = 11) -> str:
    from PySide6.QtGui import QFontMetrics

    return QFontMetrics(ui_font(size, 400)).elidedText(text, Qt.TextElideMode.ElideRight, width)


class DetailPane(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detailPane")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setStyleSheet("background:#ffffff;")
        self._host = QWidget()
        self._host.setStyleSheet("background:#ffffff;")
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(0, 2, 10, 12)
        self._layout.setSpacing(0)
        self.setWidget(self._host)
        self.show_empty()

    # ------------------------------------------------------------------ #
    def _reset(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    self._clear_layout(child)

    @staticmethod
    def _clear_layout(layout) -> None:  # noqa: ANN001
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                DetailPane._clear_layout(item.layout())

    def _gap(self, height: int) -> None:
        spacer = QWidget()
        spacer.setFixedHeight(height)
        self._layout.addWidget(spacer)

    def _rule(self) -> None:
        self._gap(12)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{TOKENS['border']};")
        self._layout.addWidget(line)
        self._gap(12)

    def show_empty(self, text: str = "推荐结果会显示在这里：两端通勤时间、路线步骤与导航入口。") -> None:
        self._reset()
        self._layout.addSpacing(4)
        self._layout.addWidget(_label(text, 12, 400, TOKENS["muted"]))
        self._layout.addStretch(1)
        self._layout.activate()

    # ------------------------------------------------------------------ #
    def show_recommendation(
        self,
        rec: Recommendation,
        point_a: UserPoint | None,
        point_b: UserPoint | None,
        open_url: Callable[[str], None],
    ) -> None:
        self._reset()
        poi = rec.poi

        self._layout.addWidget(_label(poi.name, 17, 600, TOKENS["text"]))
        kind = (poi.type_name.split(";")[-1] if poi.type_name else "地点") or "地点"
        where = poi.address or poi.business_area or "地址未提供"
        self._layout.addWidget(_label(f"{kind} · {where}", 12, 400, TOKENS["muted"]))
        self._gap(16)

        scale = max(
            10.0,
            float(rec.leg_a.duration_min) if rec.leg_a and rec.leg_a.ok else 0.0,
            float(rec.leg_b.duration_min) if rec.leg_b and rec.leg_b.ok else 0.0,
        )
        column_width = max(96, int((self.viewport().width() - 64) / 2))
        compare = QHBoxLayout()
        compare.setContentsMargins(0, 0, 0, 0)
        compare.setSpacing(24)
        compare.addWidget(
            self._leg_column(
                "A 出发",
                point_a.display if point_a else "A 未设置",
                rec.leg_a,
                scale,
                TOKENS["accent"],
                column_width,
            )
        )
        compare.addWidget(
            self._leg_column(
                "B 出发",
                point_b.display if point_b else "B 未设置",
                rec.leg_b,
                scale,
                TOKENS["bar_b"],
                column_width,
            )
        )
        self._layout.addLayout(compare)

        leg_a, leg_b = rec.leg_a, rec.leg_b
        if leg_a and leg_a.ok and leg_b and leg_b.ok:
            total = leg_a.duration_min + leg_b.duration_min
            gap = abs(leg_a.duration_min - leg_b.duration_min)
            tail = f"共 {total:.0f} 分钟" + (
                f" · 双方相差 {gap:.0f} 分钟" if gap >= 1 else " · 双方用时一致"
            )
            if leg_a.cached and leg_a.cached_at:
                tail += f" · 路线缓存于 {leg_a.cached_at.astimezone().strftime('%H:%M')}，非实时"
            self._gap(10)
            self._layout.addWidget(_label(tail, 12, 400, TOKENS["text"]))

        if rec.reason:
            self._rule()
            self._layout.addWidget(_label("推荐理由", 12, 400, TOKENS["muted"]))
            self._gap(4)
            self._layout.addWidget(_label(rec.reason, 12, 400, TOKENS["text"]))

        self._rule()
        self._layout.addWidget(_label("地点信息", 12, 400, TOKENS["muted"]))
        self._gap(6)
        rows = [
            ("高德评分", f"{poi.rating:.1f}" if poi.rating is not None else "—"),
            ("人均消费", f"{poi.cost:.0f} 元" if poi.cost is not None else "—"),
            ("商圈", poi.business_area or "—"),
            ("电话", poi.tel or "—"),
        ]
        for name, value in rows:
            self._layout.addWidget(self._kv_row(name, value))
        self._gap(8)
        self._layout.addWidget(
            _label(f"POI ID {poi.id} · GCJ-02 {poi.lng:.6f}, {poi.lat:.6f}", 11, 400, TOKENS["faint"])
        )

        self._rule()
        # URL 查询串必须用 percent-encoding：html.escape 会把 & 变成 &amp;、
        # 中文/空格原样塞进去，高德会当成非法参数。
        poi_name_q = _urlquote(poi.name, safe="")
        nav_url = (
            f"https://uri.amap.com/marker?position={poi.lng:.6f},{poi.lat:.6f}"
            f"&name={poi_name_q}&src=meetpoint&coordinate=gaode&callnative=1"
        )
        mode = "bus" if (rec.leg_a and rec.leg_a.mode is TravelMode.TRANSIT) else "car"
        from_lng = point_a.lng if point_a else poi.lng
        from_lat = point_a.lat if point_a else poi.lat
        route_url = (
            f"https://uri.amap.com/navigation?from={from_lng:.6f},{from_lat:.6f},A"
            f"&to={poi.lng:.6f},{poi.lat:.6f},{poi_name_q}"
            f"&mode={mode}&policy=1&src=meetpoint&coordinate=gaode&callnative=1"
        )
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(16)
        btn_open = QPushButton("在高德地图中查看")
        btn_open.setObjectName("textAction")
        btn_open.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open.clicked.connect(lambda: open_url(nav_url))
        btn_route = QPushButton("从 A 出发导航")
        btn_route.setObjectName("textAction")
        btn_route.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_route.clicked.connect(lambda: open_url(route_url))
        actions.addWidget(btn_open)
        actions.addWidget(btn_route)
        actions.addStretch(1)
        self._layout.addLayout(actions)
        self._layout.addStretch(1)
        self._layout.activate()

    # ------------------------------------------------------------------ #
    def _leg_column(
        self,
        title: str,
        origin: str,
        leg: RouteLeg | None,
        scale: float,
        colour: str,
        width: int = 132,
    ) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        head = _label(_elide(f"{title} · {origin}", width), 11, 400, TOKENS["muted"], wrap=False)
        head.setToolTip(origin)
        layout.addWidget(head)

        if leg is None or not leg.ok:
            reason = (leg.error if leg is not None else "") or "该端路线不可达"
            value = QLabel("不可达")
            value.setFont(ui_font(17, 400))
            value.setStyleSheet(f"color:{TOKENS['negative']};")
            layout.addWidget(value)
            layout.addWidget(_label(reason, 11, 400, TOKENS["muted"]))
            return host

        value = QLabel(f"{leg.duration_min:.0f} 分")
        value.setFont(ui_font(20, 400))
        value.setStyleSheet(f"color:{TOKENS['text']};")
        layout.addWidget(value)
        layout.addWidget(Meter(float(leg.duration_min) / scale, colour))
        meta = f"{leg.distance_km:.1f} 公里 · 换乘 {leg.transfers} 次"
        layout.addWidget(_label(_elide(meta, width), 11, 400, TOKENS["muted"], wrap=False))
        return host

    def _kv_row(self, name: str, value: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(8)
        layout.addWidget(_label(name, 12, 400, TOKENS["muted"]))
        layout.addStretch(1)
        value_label = QLabel(value)
        value_label.setFont(ui_font(12, 400))
        value_label.setStyleSheet(f"color:{TOKENS['text']};")
        layout.addWidget(value_label)
        return row
