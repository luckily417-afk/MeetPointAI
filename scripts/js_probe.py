"""高德 JS API 探针：在 QWebEngineView 中真实加载地图并回报状态。

用法：
    set QT_QPA_PLATFORM=offscreen
    .venv\\Scripts\\python.exe scripts\\js_probe.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-software-rasterizer")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402

from config.settings import get_settings  # noqa: E402
from ui.map_bridge import MapBridge, MapController  # noqa: E402


class LoggingPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print(f"[console:{level}] {message} ({source}:{line})")


def main() -> int:
    settings = get_settings()
    print("JS Key:", settings.masked()["amap_js_key"], " 安全密钥:", settings.masked()["amap_js_security_code"])

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    view = QWebEngineView()
    page = LoggingPage(view)
    view.setPage(page)
    bridge = MapBridge()
    controller = MapController(view, bridge, settings, parent=view)

    state = {"ready": False, "failed": "", "logs": [], "started": None}

    import time

    def on_ready(info: dict) -> None:
        state["ready"] = True
        elapsed = round(time.time() - (state["started"] or time.time()), 2)
        print(f"READY in {elapsed}s: {json.dumps(info, ensure_ascii=False)}")
        app.quit()

    def on_failed(message: str) -> None:
        state["failed"] = message
        print("FAILED:", message)
        app.quit()

    bridge.ready.connect(on_ready)
    bridge.failed.connect(on_failed)
    bridge.log_message.connect(lambda m: print("LOG:", m))
    controller.load_timed_out.connect(on_failed)

    view.resize(1200, 800)
    view.show()
    state["started"] = time.time()
    controller.load()
    QTimer.singleShot(25000, lambda: (print("TIMEOUT waiting for map"), app.quit()))
    app.exec()

    print(json.dumps({"ready": state["ready"], "failed": state["failed"]}, ensure_ascii=False))
    return 0 if state["ready"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
