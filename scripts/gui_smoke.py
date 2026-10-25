"""GUI 冒烟测试：以 offscreen 方式构建主窗口，截图并退出。

用法：
    .venv\\Scripts\\python.exe scripts\\gui_smoke.py
产物：
    docs/screenshot_main.png
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox --disable-software-rasterizer")

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from algorithms.pipeline import RecommendationPipeline  # noqa: E402
from config.settings import get_settings  # noqa: E402
from main import build_services  # noqa: E402
from storage.db import Database  # noqa: E402
from ui.async_runtime import AsyncRuntime  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    print("[1] QApplication", flush=True)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    settings = get_settings()
    print("[2] settings", settings.masked()["data_dir"], flush=True)
    db = Database(settings.data_dir / "gui_smoke.sqlite3")
    runtime = AsyncRuntime()
    print("[3] services", flush=True)
    transport, amap, deepseek, monitor = build_services(settings, db)
    pipeline = RecommendationPipeline(amap, deepseek, settings, monitor, db)
    print("[4] MainWindow", flush=True)
    window = MainWindow(settings, runtime, db, amap, deepseek, monitor, pipeline)
    print("[5] show", flush=True)
    window.show()

    out_dir = ROOT / "docs"
    out_dir.mkdir(exist_ok=True)

    def capture() -> None:
        print("[6] capture", flush=True)
        map_path = out_dir / "screenshot_map.png"
        window.map_view.grab().save(str(map_path))
        win_path = out_dir / "screenshot_main.png"
        window.grab().save(str(win_path))
        print("saved:", map_path.name, win_path.name, flush=True)
        print("pills:", {k: v.toolTip().splitlines()[1] for k, v in window.pills.items()}, flush=True)
        print("map ready:", window.map.ready, flush=True)
        app.quit()

    QTimer.singleShot(8000, capture)
    code = app.exec()
    try:
        runtime.stop()
    except Exception:  # noqa: BLE001
        pass
    db.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
