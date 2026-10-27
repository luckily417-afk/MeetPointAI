"""界面主题：设计 tokens + 全局 QSS（Achromatic ledger · app screen 规则）。

方向单：`.taste-skill/direction.json`（seed 1288702729, achromatic-ledger）。
- 界面骨架不带品牌色：主操作近黑，次级为白底描边；
- 唯一的数据色 #1d5fbf 只用在「A 端通勤」这一条主数据上（B 端为同组灰）；
- 其余全部是墨色 / 三级灰，靠 1px 边与一层色阶区分表面，不用阴影。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["TOKENS", "APP_QSS", "PILL_STYLES", "install_fonts", "ui_font"]


TOKENS: dict[str, str] = {
    # 表面
    "ground": "#f3f5f7",
    "surface": "#ffffff",
    "raised": "#eef1f4",
    "active": "#e8ecef",
    "hover": "#f5f7f8",
    # 线
    "border": "#e3e7ea",
    "rule": "#edf0f2",
    "control": "#d3d9de",
    # 字
    "text": "#0a0a0a",
    "muted": "#666c73",
    "faint": "#7f868d",
    # 动作
    "primary": "#141414",
    "primary_hover": "#2c2c2c",
    # 数据色（只有 A 端通勤 / 地图 A 点使用）
    "accent": "#1d5fbf",
    "accent_soft": "#9db6d8",
    "bar_b": "#9aa0a6",
    # 状态
    "positive": "#2e7d32",
    "negative": "#b3261e",
    "warning": "#92400e",
    "notice_bg": "#fffbeb",
    "notice_border": "#f3e2ad",
}

PILL_STYLES = {
    "ok": ("#edf3ec", "#cfdccd", "#2e7d32"),
    "slow": ("#fdf7e8", "#ecdcb2", "#8a5a00"),
    "error": ("#fcefee", "#efc9c5", "#a3261e"),
    "idle": ("#f0f2f4", "#dde2e6", "#666c73"),
}

_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_FAMILY: str | None = None


def install_fonts() -> str:
    """加载随程序分发的 Geist（正文/数字）与 Geist Mono（ID、日志）。"""
    global _FAMILY
    if _FAMILY is not None:
        return _FAMILY
    family = "Geist"
    try:
        from PySide6.QtGui import QFontDatabase

        loaded = 0
        for path in sorted(_FONT_DIR.glob("*.ttf")):
            if QFontDatabase.addApplicationFont(str(path)) >= 0:
                loaded += 1
        if loaded == 0 or family not in QFontDatabase.families():
            family = "Microsoft YaHei UI"
    except Exception:  # noqa: BLE001 - 字体缺失时退回系统中文字体
        family = "Microsoft YaHei UI"
    _FAMILY = family
    return family


def ui_font(size: int = 13, weight: int = 400):
    """给需要在代码里指定字号的控件用（QSS 管不到委托绘制）。"""
    from PySide6.QtGui import QFont

    family = install_fonts()
    font = QFont()
    font.setFamilies([family, "Microsoft YaHei UI", "Segoe UI"])
    font.setPixelSize(size)
    font.setWeight(QFont.Weight(weight))
    return font


APP_QSS = """
QWidget {
    font-family: "Geist", "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #0a0a0a;
}
QMainWindow, QWidget#root, QDialog { background: #f3f5f7; }

/* ---------------------------------------------------------------- 菜单/状态栏 */
QMenuBar {
    background: #f3f5f7;
    border-bottom: 1px solid #e3e7ea;
    padding: 2px 8px;
    color: #666c73;
}
QMenuBar::item { padding: 4px 10px; background: transparent; border-radius: 4px; }
QMenuBar::item:selected { background: #e8ecef; color: #0a0a0a; }
QMenu { background: #ffffff; border: 1px solid #e3e7ea; padding: 6px; }
QMenu::item { padding: 6px 22px 6px 14px; border-radius: 4px; }
QMenu::item:selected { background: #eef1f4; }
QMenu::separator { height: 1px; background: #edf0f2; margin: 6px 4px; }
QStatusBar {
    background: #f3f5f7; border-top: 1px solid #e3e7ea; color: #666c73; font-size: 11px;
}
QStatusBar::item { border: none; }

/* ---------------------------------------------------------------- 顶栏 */
QFrame#topbar { background: #ffffff; border-bottom: 1px solid #e3e7ea; }
QLabel#wordmark { font-size: 15px; font-weight: 600; letter-spacing: -0.2px; color: #0a0a0a; }
QLabel#scopeChip { color: #666c73; font-size: 12px; }
QLabel#vline { background: #e3e7ea; min-width: 1px; max-width: 1px; }

/* ---------------------------------------------------------------- 标题行 */
QLabel#pageTitle { font-size: 22px; font-weight: 600; letter-spacing: -0.4px; color: #0a0a0a; }
QLabel#pageScope { font-size: 12px; color: #666c73; }
QLabel#sectionTitle { font-size: 14px; font-weight: 600; color: #0a0a0a; }
QLabel#fieldLabel { font-size: 12px; color: #666c73; }
QLabel#muted { font-size: 12px; color: #666c73; }
QLabel#meta { font-size: 11px; color: #7f868d; }
QLabel#mono { font-family: "Geist Mono", "Consolas", monospace; font-size: 12px; color: #666c73; }
QLabel#finding { font-size: 12px; color: #0a0a0a; }

/* ---------------------------------------------------------------- 面板 */
QFrame#panel { background: #ffffff; border: 1px solid #e3e7ea; border-radius: 8px; }
QFrame#panelHeader { background: #ffffff; border-bottom: 1px solid #e3e7ea; }
QFrame#formPanel { background: #f3f5f7; border-right: 1px solid #e3e7ea; }
QFrame#formFooter { background: #f3f5f7; border-top: 1px solid #e3e7ea; }
QWidget#mapHost { background: #e9edf0; }

QGroupBox {
    background: #ffffff; border: 1px solid #e3e7ea; border-radius: 8px;
    margin-top: 24px; padding: 18px 14px 14px 14px; font-weight: 500;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 14px; top: 2px; padding: 0 4px; color: #0a0a0a;
}

/* ---------------------------------------------------------------- 输入 */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #ffffff;
    border: 1px solid #d3d9de;
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 22px;
    selection-background-color: #dbe6f4;
    selection-color: #0a0a0a;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #bfc7cd; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #141414; }
QLineEdit:disabled, QComboBox:disabled { color: #8b9298; background: #f3f5f7; border-color: #e3e7ea; }
QLineEdit#bare { border: none; background: transparent; padding: 4px 8px; }
QLineEdit#bare:focus { border: none; }
QFrame#joined { background: #ffffff; border: 1px solid #d3d9de; border-radius: 6px; }
QFrame#joined[state="focus"] { border-color: #141414; }

QSpinBox, QDoubleSpinBox { padding-right: 20px; }
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: padding; subcontrol-position: top right; width: 18px;
    background: transparent; border: none; margin-top: 2px;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: padding; subcontrol-position: bottom right; width: 18px;
    background: transparent; border: none; margin-bottom: 2px;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { image: none; width: 0px; height: 0px; }
QComboBox QAbstractItemView {
    border: 1px solid #e3e7ea; background: #ffffff; padding: 4px;
    selection-background-color: #eef1f4; selection-color: #0a0a0a; outline: none;
}

/* ---------------------------------------------------------------- 按钮 */
QPushButton {
    background: #ffffff;
    border: 1px solid #d3d9de;
    border-radius: 6px;
    padding: 4px 12px;
    min-height: 22px;
    color: #0a0a0a;
}
QPushButton:hover { background: #f5f7f8; border-color: #bfc7cd; }
QPushButton:focus { border: 1px solid #141414; }
QPushButton#textAction:focus { text-decoration: underline; }
QPushButton:pressed { background: #e8ecef; }
QPushButton:disabled { color: #9aa1a7; background: #f3f5f7; border-color: #e3e7ea; }

QPushButton#primary {
    background: #141414; color: #ffffff; border: 1px solid #141414;
    font-weight: 500; padding: 9px 16px; min-height: 22px;
}
QPushButton#primary:hover { background: #2c2c2c; border-color: #2c2c2c; }
QPushButton#primary:focus { border: 1px solid #141414; outline: none; }
QPushButton#primary:pressed { background: #000000; }
QPushButton#primary:disabled { background: #c9ced3; border-color: #c9ced3; color: #ffffff; }

QPushButton#textAction {
    background: transparent; border: none; padding: 2px 4px; color: #0a0a0a; font-size: 12px;
}
QPushButton#textAction:hover { color: #0a0a0a; text-decoration: underline; }
QPushButton#textAction:disabled { color: #9aa1a7; text-decoration: none; }

QPushButton#quietAction {
    background: transparent; border: 1px solid #e3e7ea; padding: 3px 10px; color: #0a0a0a; font-size: 12px;
}
QPushButton#quietAction:hover { background: #ffffff; border-color: #d3d9de; }

QPushButton#seg {
    background: transparent; border: none; border-radius: 5px; padding: 5px 8px;
    color: #666c73; font-size: 12px; min-height: 20px;
}
QPushButton#seg:hover { color: #0a0a0a; }
QPushButton#seg:checked {
    background: #ffffff; border: 1px solid #e3e7ea; color: #0a0a0a; font-weight: 500;
}
QFrame#segTrack { background: #eaeef1; border-radius: 6px; }

QPushButton#danger { color: #b3261e; }
QPushButton#danger:hover { background: #fcefee; border-color: #efc9c5; }

/* ---------------------------------------------------------------- 表格 */
QTableWidget, QTableView {
    background: #ffffff;
    border: none;
    gridline-color: transparent;
    selection-background-color: #e8ecef;
    selection-color: #0a0a0a;
    outline: none;
}
QTableWidget::item, QTableView::item { border: none; padding: 0px; }
QHeaderView { background: #ffffff; }
QHeaderView::section {
    background: #ffffff;
    border: none;
    border-bottom: 1px solid #e3e7ea;
    padding: 0px 12px;
    height: 34px;
    font-size: 12px;
    font-weight: 400;
    color: #666c73;
}
QHeaderView::section:first { padding-left: 16px; }
QTableCornerButton::section { background: #ffffff; border: none; }

/* ---------------------------------------------------------------- 标签页 */
QTabWidget::pane { border: none; background: #ffffff; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent; border: none; border-bottom: 2px solid transparent;
    padding: 8px 2px; margin-right: 20px; color: #666c73; font-size: 12px;
}
QTabBar::tab:hover { color: #0a0a0a; }
QTabBar::tab:selected { color: #0a0a0a; font-weight: 500; border-bottom: 2px solid #141414; }

/* ---------------------------------------------------------------- 进度 */
QProgressBar {
    border: none; background: #eaeef1; max-height: 3px; min-height: 3px;
    text-align: center; border-radius: 0px; color: transparent;
}
QProgressBar::chunk { background: #141414; border-radius: 0px; }

/* ---------------------------------------------------------------- 文本区 */
QTextBrowser, QTextEdit, QPlainTextEdit#log {
    background: #ffffff; border: none; padding: 0px;
}
QPlainTextEdit#log {
    font-family: "Geist Mono", "Consolas", monospace; font-size: 11px; color: #4a5057;
}

/* ---------------------------------------------------------------- 分隔/滚动 */
QSplitter::handle { background: #e3e7ea; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QSplitter::handle:hover { background: #cfd6dc; }

QScrollArea { border: none; background: transparent; }
QScrollArea#detailPane, QScrollArea#detailPane > QWidget > QWidget { background: #ffffff; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0px; }
QScrollBar::handle:vertical { background: #c9d0d6; border-radius: 5px; min-height: 40px; }
QScrollBar::handle:vertical:hover { background: #aeb6bd; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0px; }
QScrollBar::handle:horizontal { background: #c9d0d6; border-radius: 5px; min-width: 40px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QToolTip {
    background: #141414; color: #ffffff; border: none; padding: 6px 8px; border-radius: 4px;
    font-size: 12px;
}

QCheckBox { spacing: 8px; color: #0a0a0a; }
QDialogButtonBox QPushButton { min-width: 96px; }
"""
