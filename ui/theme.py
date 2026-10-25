"""界面主题（QSS）。"""

from __future__ import annotations

APP_QSS = """
QWidget {
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #22303f;
}
QMainWindow, QWidget#root { background: #f2f5f9; }

QFrame#topbar {
    background: #ffffff;
    border-bottom: 1px solid #dde4ee;
}
QLabel#appTitle {
    font-size: 16px;
    font-weight: 700;
    color: #14304f;
    padding-right: 6px;
}
QLabel#appSubtitle { color: #7a8aa0; font-size: 12px; }

QGroupBox {
    background: #ffffff;
    border: 1px solid #dde4ee;
    border-radius: 10px;
    margin-top: 14px;
    padding: 10px 10px 8px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #14304f;
}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDoubleSpinBox, QSpinBox {
    background: #fbfcfe;
    border: 1px solid #ccd6e4;
    border-radius: 7px;
    padding: 5px 8px;
    selection-background-color: #cfe4ff;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #2f6fd0;
    background: #ffffff;
}
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    border: 1px solid #ccd6e4;
    selection-background-color: #e6f0ff;
    selection-color: #14304f;
    background: #ffffff;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #c8d4e4;
    border-radius: 7px;
    padding: 5px 12px;
}
QPushButton:hover { background: #f1f6ff; border-color: #9dbde8; }
QPushButton:pressed { background: #e2ecfb; }
QPushButton:disabled { color: #9aa7b8; background: #f3f5f8; border-color: #e2e7ef; }

QPushButton#primary {
    background: #1b64d1;
    color: #ffffff;
    border: 1px solid #1b64d1;
    font-weight: 700;
    padding: 9px 14px;
    font-size: 14px;
}
QPushButton#primary:hover { background: #1757b8; }
QPushButton#primary:disabled { background: #a8bcd9; border-color: #a8bcd9; color: #eef3fa; }

QPushButton#danger { color: #b3261e; border-color: #e6b4b0; }
QPushButton#danger:hover { background: #fdf2f1; }

QTableWidget {
    background: #ffffff;
    border: 1px solid #dde4ee;
    border-radius: 8px;
    gridline-color: #edf1f7;
    selection-background-color: #e6f0ff;
    selection-color: #14304f;
}
QHeaderView::section {
    background: #f6f9fd;
    border: none;
    border-bottom: 1px solid #dde4ee;
    padding: 6px;
    font-weight: 600;
    color: #3b4d63;
}
QTabWidget::pane { border: 1px solid #dde4ee; border-radius: 8px; background: #ffffff; }
QTabBar::tab {
    background: #eef2f8; border: 1px solid #dde4ee; border-bottom: none;
    padding: 6px 14px; border-top-left-radius: 7px; border-top-right-radius: 7px;
    color: #44576e;
}
QTabBar::tab:selected { background: #ffffff; color: #14304f; font-weight: 600; }

QProgressBar {
    border: 1px solid #dde4ee; border-radius: 6px; background: #f5f8fc;
    text-align: center; height: 16px;
}
QProgressBar::chunk { background: #1b64d1; border-radius: 5px; }

QTextBrowser { background: #ffffff; border: 1px solid #dde4ee; border-radius: 8px; }
QMenuBar { background: #ffffff; }
QStatusBar { background: #ffffff; border-top: 1px solid #dde4ee; color: #5b6b80; }
QSplitter::handle { background: #e6ebf3; }
QSplitter::handle:horizontal { width: 5px; }
QSplitter::handle:vertical { height: 5px; }

QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c9d4e3; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #a9bad0; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QToolTip {
    background: #1e2a3a; color: #f4f7fb; border: none; padding: 6px 8px; border-radius: 6px;
}
"""

PILL_STYLES = {
    "ok": ("#e8f6ec", "#b7e0c2", "#1a7f37"),
    "slow": ("#fdf6e3", "#eee0b4", "#8a6d1f"),
    "error": ("#fdeceb", "#f0c4c0", "#a3261e"),
    "idle": ("#f2f4f7", "#dde2ea", "#63707f"),
}
