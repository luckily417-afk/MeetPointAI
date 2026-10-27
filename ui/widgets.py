"""可复用界面控件（Achromatic ledger 风格：无填充、1px 线、墨色为主）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QValidator
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.constants import STATE_COLORS, STATE_LABELS
from models.schemas import HealthSnapshot, ServiceState
from ui.theme import PILL_STYLES, TOKENS

__all__ = [
    "ChevronComboBox",
    "ChevronSpinBox",
    "ChevronDoubleSpinBox",
    "ServicePill",
    "Banner",
    "RadiusSpinBox",
    "ProductMark",
    "SegmentedControl",
    "HLine",
    "normalize_number_text",
    "parse_number_text",
]

# 苏州码子 / 常见异体数字 -> ASCII
_NUMERAL_MAP = {
    "〡": "1", "〢": "2", "〣": "3", "〤": "4", "〥": "5",
    "〦": "6", "〧": "7", "〨": "8", "〩": "9",
    "〇": "0", "○": "0", "零": "0", "Ο": "0", "O": "0",
}

_UNIT_TOKENS = ("平方公里", "平方千米", "千米", "公里", "km", "KM", "Km", "米", "m", "M", " ", "\u3000", "、")


def normalize_number_text(text: str) -> str:
    """把各种“看起来是数字”的输入规范成可被 float() 解析的 ASCII 串。

    覆盖：全角数字（０-９）、全角/中文句号与逗号、苏州码子（〡〢〣…〇）、
    以及误输入的单位后缀（千米/km/米）。
    """
    if not text:
        return ""
    cleaned = str(text).strip()
    cleaned = cleaned.replace("，", ".").replace(",", ".").replace("．", ".").replace("。", ".")
    cleaned = "".join(
        chr(ord(ch) - 0xFEE0) if "０" <= ch <= "９" else ch for ch in cleaned
    )
    cleaned = cleaned.replace("．", ".").replace("－", "-").replace("＋", "+")
    for src, dst in _NUMERAL_MAP.items():
        cleaned = cleaned.replace(src, dst)
    for token in _UNIT_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned.strip()


def parse_number_text(text: str) -> float | None:
    cleaned = normalize_number_text(text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fmt_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    try:
        return value.astimezone().strftime("%H:%M:%S")
    except (ValueError, OSError):
        return value.strftime("%H:%M:%S")


class HLine(QFrame):
    """1px 分隔线（app screen：靠一层色阶分面，不用阴影）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setStyleSheet(f"background:{TOKENS['border']};")


class ProductMark(QWidget):
    """产品标记：两个出发点收束到同一个见面地点。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(TOKENS["text"]))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        left = QPointF(5.0, 16.5)
        right = QPointF(17.0, 5.5)
        path = QPainterPath(left)
        path.lineTo(QPointF(11.0, 11.0))
        path.lineTo(right)
        painter.drawPath(path)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(TOKENS["accent"]))
        painter.drawEllipse(left, 3.0, 3.0)
        painter.setBrush(QColor(TOKENS["text"]))
        painter.drawEllipse(right, 3.0, 3.0)

        painter.setBrush(QColor(TOKENS["surface"]))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(8.4, 8.4, 5.2, 5.2))
        painter.end()


class ChevronComboBox(QComboBox):
    """无填充的下拉框：自己画 8px 弯折箭头，避免各平台默认箭头粗细不一。"""

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(TOKENS["muted"]))
        pen.setWidthF(1.2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        right = self.width() - 13.0
        middle = self.height() / 2.0
        path = QPainterPath(QPointF(right - 4.0, middle - 2.0))
        path.lineTo(QPointF(right, middle + 2.4))
        path.lineTo(QPointF(right + 4.0, middle - 2.0))
        painter.drawPath(path)
        painter.end()


class _SpinChevrons:
    """上下箭头自己画：QSS 把原生按钮涂空以后，仍能看清哪里可以点。"""

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(TOKENS["faint"]))
        pen.setWidthF(1.1)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        x = self.width() - 12.0
        middle = self.height() / 2.0
        up = QPainterPath(QPointF(x - 3.4, middle - 4.6))
        up.lineTo(QPointF(x, middle - 8.2))
        up.lineTo(QPointF(x + 3.4, middle - 4.6))
        painter.drawPath(up)
        down = QPainterPath(QPointF(x - 3.4, middle + 4.6))
        down.lineTo(QPointF(x, middle + 8.2))
        down.lineTo(QPointF(x + 3.4, middle + 4.6))
        painter.drawPath(down)
        painter.end()


class ChevronSpinBox(_SpinChevrons, QSpinBox):
    pass


class ServicePill(QFrame):
    """顶栏服务状态：6px 状态点 + 名称 + 最近延迟，无填充胶囊。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("servicePill")
        self._title = title
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._dot = QLabel()
        self._dot.setFixedSize(6, 6)
        self._title_label = QLabel(title)
        self._title_label.setObjectName("muted")
        self._state_label = QLabel("检测中")
        self._state_label.setObjectName("meta")
        self._meta = QLabel("—")
        self._meta.setObjectName("meta")

        layout.addWidget(self._dot)
        layout.addWidget(self._title_label)
        layout.addWidget(self._state_label)
        layout.addWidget(self._meta)
        self._apply("idle", "检测中", "")
        self.setToolTip("尚未检测")

    def _apply(self, tone: str, state_text: str, meta: str) -> None:
        _, _, fg = PILL_STYLES.get(tone, PILL_STYLES["idle"])
        self._dot.setStyleSheet(f"background:{fg};border-radius:3px;")
        self._state_label.setText(state_text)
        self._state_label.setStyleSheet(f"color:{fg};font-size:11px;")
        self._meta.setText(meta)

    def update_snapshot(self, snap: HealthSnapshot, latency_warn_ms: int = 2500) -> None:
        state = snap.state
        if state in (ServiceState.OK, ServiceState.SLOW):
            tone = "ok" if state is ServiceState.OK else "slow"
        elif state in (ServiceState.UNKNOWN, ServiceState.NOT_CONFIGURED):
            tone = "idle"
        else:
            tone = "error"

        state_text = STATE_LABELS.get(state.value, state.value)
        # 正常态不重复写「正常」：状态点 + 延迟已经说明问题
        short_state = "" if state is ServiceState.OK else state_text
        latency_text = f"{snap.latency_ms} ms" if snap.latency_ms is not None else "—"
        self._apply(tone, short_state, latency_text)

        tooltip_lines = [
            f"{self._title}",
            f"状态：{state_text}",
            f"最近检测：{_fmt_time(snap.last_check_at)}",
            f"最近成功：{_fmt_time(snap.last_success_at)}",
            f"最近延迟：{snap.latency_ms if snap.latency_ms is not None else '—'} ms",
        ]
        if snap.http_status:
            tooltip_lines.append(f"HTTP：{snap.http_status}")
        if snap.biz_code:
            tooltip_lines.append(f"业务码：{snap.biz_code}")
        if snap.error_type:
            tooltip_lines.append(f"错误类型：{snap.error_type}")
        if snap.detail:
            tooltip_lines.append(f"说明：{snap.detail}")
        if snap.consecutive_failures:
            tooltip_lines.append(f"连续失败：{snap.consecutive_failures} 次")
        self.setToolTip("\n".join(tooltip_lines))


class Banner(QLabel):
    """可显示/隐藏的提示条（用于降级/故障提示），12px 静音提示色。"""

    TONES = {
        "info": ("#f1f5fb", "#dce5ef", "#1f3b5b"),
        "warn": ("#fffbeb", "#f3e2ad", "#92400e"),
        "error": ("#fcefee", "#efc9c5", "#a3261e"),
        "ok": ("#f0f5f0", "#d6e3d6", "#2e7d32"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setVisible(False)

    def show_message(self, text: str, tone: str = "info") -> None:
        bg, border, fg = self.TONES.get(tone, self.TONES["info"])
        self.setStyleSheet(
            f"background:{bg};border:1px solid {border};border-radius:6px;"
            f"padding:6px 10px;color:{fg};font-size:12px;"
        )
        self.setText(text)
        self.setVisible(bool(text))

    def clear(self) -> None:
        self.setText("")
        self.setVisible(False)


class SegmentedControl(QFrame):
    """分段控件：与 QComboBox 兼容的最小接口（currentData / setCurrentIndex / findData）。"""

    currentIndexChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("segTrack")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._data: list[Any] = []

    def addItem(self, label: str, data: Any = None) -> None:  # noqa: N802
        button = QPushButton(label)
        button.setObjectName("seg")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        index = len(self._data)
        self._group.addButton(button, index)
        self._data.append(data if data is not None else label)
        self.layout().addWidget(button, 1)
        button.clicked.connect(lambda _=False, i=index: self._on_clicked(i))
        if index == 0:
            button.setChecked(True)

    def _on_clicked(self, index: int) -> None:
        self.currentIndexChanged.emit(index)

    def count(self) -> int:
        return len(self._data)

    def currentIndex(self) -> int:  # noqa: N802
        return self._group.checkedId()

    def currentData(self) -> Any:  # noqa: N802
        index = self.currentIndex()
        return self._data[index] if 0 <= index < len(self._data) else None

    def findData(self, value: Any) -> int:  # noqa: N802
        for index, data in enumerate(self._data):
            if data == value:
                return index
        return -1

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        button = self._group.button(index)
        if button is not None:
            button.setChecked(True)
            self.currentIndexChanged.emit(index)

    def set_current_value(self, value: Any) -> None:
        self.setCurrentIndex(self.findData(value))

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        for button in self._group.buttons():
            button.setEnabled(enabled)


class ChevronDoubleSpinBox(_SpinChevrons, QDoubleSpinBox):
    pass


class RadiusSpinBox(ChevronDoubleSpinBox):
    """半径输入框：强制 C 数字格式，并兼容全角/苏州码子等“看起来是数字”的输入。

    修复点：中文输入法可能把「26」上屏成「〢〦」或全角「２６」，原生
    QDoubleSpinBox 校验器会直接拒绝，表现为"输入不进去/值不生效"。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtCore import QLocale

        self.setLocale(QLocale.c())
        self.setDecimals(2)
        self.setRange(0.02, 50.0)
        self.setSingleStep(0.5)
        self.setAccelerated(True)
        self.setKeyboardTracking(True)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setToolTip(
            "半径：可直接输入 26 或 26.5；也接受全角数字、苏州码子（〢〦）\n"
            "单位可切换为「千米 / 米」，范围 20 米 ~ 50 千米"
        )

    # -- QDoubleSpinBox overrides -------------------------------------- #
    def textFromValue(self, value: float) -> str:  # noqa: N802
        decimals = self.decimals()
        text = f"{value:.{decimals}f}"
        if decimals > 0:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    def valueFromText(self, text: str) -> float:  # noqa: N802
        parsed = parse_number_text(text)
        return float(parsed) if parsed is not None else self.value()

    def validate(self, text: str, pos: int):  # noqa: N802
        cleaned = normalize_number_text(text)
        if cleaned in {"", "-", ".", "-.", "+"}:
            return (QValidator.State.Intermediate, text, pos)
        try:
            float(cleaned)
        except ValueError:
            return (QValidator.State.Invalid, text, pos)
        return (QValidator.State.Acceptable, text, pos)

    def fixup(self, text: str) -> str:  # noqa: N802
        cleaned = normalize_number_text(text)
        try:
            value = float(cleaned)
        except ValueError:
            return self.textFromValue(self.value())
        lo, hi = self.minimum(), self.maximum()
        return self.textFromValue(min(hi, max(lo, value)))
