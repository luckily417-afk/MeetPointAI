"""推荐结果表：两行地点单元 + 双端通勤对比条（本产品独有的主数据元素）。

每条推荐的两端通勤时间用同一条比例尺画成细条：A 端用唯一的数据色 #1d5fbf，
B 端用同组灰 #8a9098。谁更远一眼看得出来，不必逐格读数字。
"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
)

from models.schemas import Recommendation
from ui.theme import TOKENS, ui_font

__all__ = ["ResultTable"]

COL_RANK, COL_PLACE, COL_A, COL_B, COL_TOTAL, COL_TRANSFER = range(6)
HEADERS = ("#", "地点", "A 通勤", "B 通勤", "总时长", "换乘")

ROLE_MINUTES = Qt.ItemDataRole.UserRole + 11
ROLE_SCALE = Qt.ItemDataRole.UserRole + 12
ROLE_TONE = Qt.ItemDataRole.UserRole + 13  # "a" | "b" | "muted"
ROLE_TITLE = Qt.ItemDataRole.UserRole + 14
ROLE_SUB = Qt.ItemDataRole.UserRole + 15
ROLE_ALERT = Qt.ItemDataRole.UserRole + 16


class _CellDelegate(QStyledItemDelegate):
    """统一绘制：底色（选中/悬停/普通）+ 每列各自的排版。"""

    PAD = 14

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(120, 56)

    # -- 底色 ---------------------------------------------------------- #
    def _background(self, option) -> QColor:
        if option.state & QStyle.StateFlag.State_Selected:
            return QColor(TOKENS["active"])
        if option.state & QStyle.StateFlag.State_MouseOver:
            return QColor(TOKENS["hover"])
        return QColor(TOKENS["surface"])

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect: QRect = option.rect
        painter.fillRect(rect, self._background(option))
        column = index.column()
        if column == COL_PLACE:
            self._paint_place(painter, rect, index)
        elif column in (COL_A, COL_B):
            self._paint_commute(painter, rect, index, column)
        elif column == COL_TOTAL:
            self._paint_figure(painter, rect, index, weight=500, tone="text")
        elif column == COL_TRANSFER:
            self._paint_figure(painter, rect, index, weight=400, tone="muted")
        else:
            self._paint_rank(painter, rect, index)
        painter.restore()

    # -- 各列 ---------------------------------------------------------- #
    def _paint_rank(self, painter: QPainter, rect: QRect, index) -> None:
        selected = bool(index.flags() & Qt.ItemFlag.ItemIsSelectable) and index.data(
            Qt.ItemDataRole.UserRole + 99
        )
        painter.setFont(ui_font(12, 400))
        painter.setPen(QPen(QColor(TOKENS["muted"])))
        painter.drawText(
            QRect(rect.left() + 8, rect.top(), rect.width() - 8, rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
        )
        _ = selected

    def _paint_place(self, painter: QPainter, rect: QRect, index) -> None:
        title = str(index.data(ROLE_TITLE) or "")
        sub = str(index.data(ROLE_SUB) or "")
        alert = bool(index.data(ROLE_ALERT))
        left = rect.left() + self.PAD
        width = max(0, rect.width() - self.PAD - 8)

        painter.setFont(ui_font(13, 500))
        painter.setPen(QPen(QColor(TOKENS["text"])))
        title_text = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, width)
        painter.drawText(
            QRect(left, rect.top() + 10, width, 20),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            title_text,
        )

        painter.setFont(ui_font(12, 400))
        painter.setPen(QPen(QColor(TOKENS["negative"] if alert else TOKENS["muted"])))
        sub_text = painter.fontMetrics().elidedText(sub, Qt.TextElideMode.ElideRight, width)
        painter.drawText(
            QRect(left, rect.top() + 29, width, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            sub_text,
        )

    def _paint_commute(self, painter: QPainter, rect: QRect, index, column: int) -> None:
        minutes = index.data(ROLE_MINUTES)
        scale = index.data(ROLE_SCALE) or 0
        tone = index.data(ROLE_TONE) or ("a" if column == COL_A else "b")
        colour = QColor(TOKENS["accent"] if tone == "a" else TOKENS["bar_b"])

        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "—")
        painter.setFont(ui_font(13, 500))
        painter.setPen(QPen(QColor(TOKENS["text"] if minutes is not None else TOKENS["faint"])))
        painter.drawText(
            QRect(rect.left() + self.PAD, rect.top() + 8, rect.width() - self.PAD * 2, 22),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            text,
        )

        track_left = rect.left() + self.PAD
        track_width = max(0, rect.width() - self.PAD * 2)
        bar_y = rect.top() + 34
        painter.fillRect(QRect(track_left, bar_y, track_width, 3), QColor(TOKENS["rule"]))
        if minutes:
            ratio = min(1.0, float(minutes) / float(scale or 1))
            fill = max(2, int(round(track_width * ratio)))
            painter.fillRect(QRect(track_left, bar_y, fill, 3), colour)

    def _paint_figure(self, painter: QPainter, rect: QRect, index, weight: int, tone: str) -> None:
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "—")
        muted = text in {"—", ""}
        painter.setFont(ui_font(13, weight))
        painter.setPen(QPen(QColor(TOKENS["faint"] if muted else TOKENS[tone])))
        painter.drawText(
            QRect(rect.left() + 4, rect.top(), rect.width() - self.PAD - 4, rect.height()),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            text,
        )


class ResultTable(QTableWidget):
    """只读结果表；保持 QTableWidget 接口（currentRow / selectRow / itemSelectionChanged）。"""

    def __init__(self, parent=None) -> None:  # noqa: ANN001
        super().__init__(0, len(HEADERS), parent)
        self.setHorizontalHeaderLabels(list(HEADERS))
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFrameShape(QTableWidget.Shape.NoFrame)

        self._delegate = _CellDelegate(self)
        self.setItemDelegate(self._delegate)
        self._items: list[Recommendation] = []

        header = self.horizontalHeader()
        header.setMinimumSectionSize(58)
        header.setHighlightSections(False)
        header.setSectionsClickable(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setFixedHeight(34)
        header.setSectionResizeMode(COL_RANK, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_PLACE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_A, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_B, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_TOTAL, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_TRANSFER, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(COL_RANK, 34)
        self.setColumnWidth(COL_A, 126)
        self.setColumnWidth(COL_B, 126)
        self.setColumnWidth(COL_TOTAL, 86)
        self.setColumnWidth(COL_TRANSFER, 66)

        right = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for column in (COL_A, COL_B, COL_TOTAL, COL_TRANSFER):
            header_item = self.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setTextAlignment(right)
        self.verticalHeader().setDefaultSectionSize(56)

    # ------------------------------------------------------------------ #
    def set_compact(self, compact: bool) -> None:
        """窄窗口：缩掉两端通勤列的宽度，保证地点名不被省略到看不清。"""
        self.setColumnHidden(COL_TRANSFER, compact)
        self.setColumnHidden(COL_TOTAL, compact)
        if compact:
            self.setColumnWidth(COL_RANK, 28)
            self.setColumnWidth(COL_A, 96)
            self.setColumnWidth(COL_B, 96)
            self.setColumnWidth(COL_TOTAL, 72)
        else:
            self.setColumnWidth(COL_RANK, 34)
            self.setColumnWidth(COL_A, 126)
            self.setColumnWidth(COL_B, 126)
            self.setColumnWidth(COL_TOTAL, 86)
            self.setColumnWidth(COL_TRANSFER, 66)

    @property
    def items(self) -> list[Recommendation]:
        return self._items

    def set_results(self, items: Sequence[Recommendation]) -> None:
        self._items = list(items)
        self.setUpdatesEnabled(False)
        self.clearContents()
        self.setRowCount(len(self._items))

        scale = 1.0
        for item in self._items:
            for leg in (item.leg_a, item.leg_b):
                if leg is not None and leg.ok:
                    scale = max(scale, float(leg.duration_min))
        scale = max(scale, 10.0)

        for row, item in enumerate(self._items):
            self._fill_row(row, item, scale)
        self.setUpdatesEnabled(True)
        self.viewport().update()

    def _fill_row(self, row: int, item: Recommendation, scale: float) -> None:
        minutes_a = item.leg_a.duration_min if item.leg_a and item.leg_a.ok else None
        minutes_b = item.leg_b.duration_min if item.leg_b and item.leg_b.ok else None
        transfers = (item.leg_a.transfers if item.leg_a and item.leg_a.ok else 0) + (
            item.leg_b.transfers if item.leg_b and item.leg_b.ok else 0
        )
        total = None
        if minutes_a is not None and minutes_b is not None:
            total = minutes_a + minutes_b
        gap = abs((minutes_a or 0) - (minutes_b or 0)) if minutes_a is not None and minutes_b is not None else None

        rank_cell = QTableWidgetItem(str(item.rank))
        rank_cell.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        rank_cell.setToolTip(item.reason or item.poi.address)
        self.setItem(row, COL_RANK, rank_cell)

        place = QTableWidgetItem(item.poi.name)
        kind = (item.poi.type_name.split(";")[-1] if item.poi.type_name else "地点") or "地点"
        where = item.poi.business_area or item.poi.address or "地址未提供"
        sub = f"{kind} · {where}"
        if gap is not None:
            sub += f" · 相差 {gap:.0f} 分"
        place.setData(ROLE_TITLE, item.poi.name)
        place.setData(ROLE_SUB, sub)
        place.setData(ROLE_ALERT, bool(gap is not None and gap > 12))
        place.setToolTip(item.reason or item.poi.address)
        self.setItem(row, COL_PLACE, place)

        for column, minutes, tone in ((COL_A, minutes_a, "a"), (COL_B, minutes_b, "b")):
            cell = QTableWidgetItem(
                f"{minutes:.0f} 分" if minutes is not None else "不可达"
            )
            cell.setData(ROLE_MINUTES, minutes)
            cell.setData(ROLE_SCALE, scale)
            cell.setData(ROLE_TONE, tone)
            leg = item.leg_a if column == COL_A else item.leg_b
            if leg is not None and leg.ok:
                cell.setToolTip(
                    f"{leg.distance_km:.1f} 公里 · 换乘 {leg.transfers} 次 · 步行 {leg.walk_m / 1000:.1f} 公里\n"
                    f"{leg.summary}"
                )
            else:
                cell.setToolTip("该端路线不可达")
            cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.setItem(row, column, cell)

        total_cell = QTableWidgetItem(f"{total:.0f} 分" if total is not None else "—")
        total_cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        total_cell.setToolTip("两端通勤时间之和，推荐以它为主要排序依据")
        self.setItem(row, COL_TOTAL, total_cell)

        transfer_cell = QTableWidgetItem("直达" if transfers == 0 else f"{transfers} 次")
        transfer_cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setItem(row, COL_TRANSFER, transfer_cell)

    def current_index(self) -> int:
        return self.currentRow()
