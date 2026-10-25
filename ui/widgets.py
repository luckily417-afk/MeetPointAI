"""可复用界面控件。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from config.constants import STATE_COLORS, STATE_LABELS
from models.schemas import HealthSnapshot, ServiceState
from ui.theme import PILL_STYLES

__all__ = ["ServicePill", "Banner", "RadiusSpinBox", "normalize_number_text", "parse_number_text"]

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


class ServicePill(QFrame):
    """顶部服务状态胶囊：状态色 + 状态文案 + 最近延迟/成功时间。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("servicePill")
        self._title = title
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(8)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(12)
        self._title_label = QLabel(title)
        self._title_label.setStyleSheet("font-weight:600;color:#1c2f45;")
        self._state_label = QLabel("状态未知")
        self._state_label.setStyleSheet("font-weight:600;")
        self._meta = QLabel("—")
        self._meta.setStyleSheet("color:#6d7d90;font-size:11px;")
        for label in (self._title_label, self._state_label, self._meta):
            label.setMinimumWidth(0)

        layout.addWidget(self._dot)
        layout.addWidget(self._title_label)
        layout.addWidget(self._state_label)
        layout.addWidget(self._meta)
        self._apply("unknown", "状态未知", "尚未检测")
        self.setToolTip("尚未检测")

    def _apply(self, tone: str, state_text: str, meta: str) -> None:
        bg, border, fg = PILL_STYLES.get(tone, PILL_STYLES["idle"])
        self.setStyleSheet(
            f"QFrame#servicePill {{ background:{bg}; border:1px solid {border}; border-radius:14px; }}"
        )
        self._dot.setStyleSheet(f"color:{fg};")
        self._state_label.setText(state_text)
        self._state_label.setStyleSheet(f"font-weight:600;color:{fg};")
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
        latency_text = f"{snap.latency_ms} ms" if snap.latency_ms is not None else "—"
        self._apply(tone, state_text, latency_text)

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
    """可显示/隐藏的提示条（用于降级/故障提示）。"""

    TONES = {
        "info": ("#eaf2ff", "#bcd4f6", "#1b4c8f"),
        "warn": ("#fff6e5", "#f0dcae", "#8a5a00"),
        "error": ("#fdecea", "#f3c4bf", "#a3261e"),
        "ok": ("#e9f7ee", "#bfe3cb", "#1a7f37"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setVisible(False)

    def show_message(self, text: str, tone: str = "info") -> None:
        bg, border, fg = self.TONES.get(tone, self.TONES["info"])
        self.setStyleSheet(
            f"background:{bg};border:1px solid {border};border-radius:8px;"
            f"padding:7px 10px;color:{fg};"
        )
        self.setText(text)
        self.setVisible(bool(text))

    def clear(self) -> None:
        self.setText("")
        self.setVisible(False)


class RadiusSpinBox(QDoubleSpinBox):
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
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
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
