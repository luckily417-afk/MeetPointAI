"""生成设置对话框截图（docs/screenshot_settings.png）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QLocale, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from config.settings import get_settings  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402

QLocale.setDefault(QLocale.c())
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
app = QApplication(sys.argv)
dialog = SettingsDialog(get_settings())
dialog.show()


def shoot() -> None:
    out = ROOT / "docs"
    dialog.grab().save(str(out / "screenshot_settings.png"))
    tabs = dialog.findChild(QTabWidget)
    if tabs is not None:
        tabs.setCurrentIndex(1)
        QTimer.singleShot(400, lambda: _shot_credentials(out, tabs))
    else:
        app.quit()


def _shot_credentials(out: Path, tabs: QTabWidget) -> None:
    dialog.grab().save(str(out / "screenshot_settings_api.png"))
    print("saved settings screenshots")
    app.quit()


QTimer.singleShot(1200, shoot)
app.exec()
