"""生成带真实推荐结果的界面截图（真实调用高德 + DeepSeek）。

用法：
    .venv\\Scripts\\python.exe scripts\\capture_demo.py
产物：
    docs/screenshot_main.png      # 含推荐结果的完整界面
    docs/screenshot_map.png       # 地图区域（含 A/B 标记、圆形区域、推荐点与路线）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from algorithms.pipeline import RecommendationPipeline  # noqa: E402
from config.settings import get_settings  # noqa: E402
from geometry.regions import CircleRegion  # noqa: E402
from main import build_services  # noqa: E402
from models.schemas import RegionMode, UserPoint  # noqa: E402
from storage.db import Database  # noqa: E402
from ui.async_runtime import AsyncRuntime  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

POINT_A = UserPoint(lng=117.210089, lat=39.135951, name="天津站", adcode="120105", source="search")
POINT_B = UserPoint(lng=117.311568, lat=38.992672, name="天津大学", adcode="120112", source="search")
CENTER = (117.2115, 39.1360)
RADIUS = 3000.0


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    settings = get_settings()
    db = Database(settings.data_dir / "capture_demo.sqlite3")
    runtime = AsyncRuntime()
    transport, amap, deepseek, monitor = build_services(settings, db)
    pipeline = RecommendationPipeline(amap, deepseek, settings, monitor, db)

    window = MainWindow(settings, runtime, db, amap, deepseek, monitor, pipeline)
    window.resize(1460, 900)
    window.show()

    deadline = time.time() + 180

    def start() -> None:
        window._set_point("A", POINT_A)  # noqa: SLF001
        window._set_point("B", POINT_B)  # noqa: SLF001
        window._circle = CircleRegion(CENTER[0], CENTER[1], RADIUS)  # noqa: SLF001
        window._region_mode = RegionMode.CIRCLE  # noqa: SLF001
        idx = window.region_combo.findData(RegionMode.CIRCLE.value)
        window.region_combo.blockSignals(True)
        window.region_combo.setCurrentIndex(idx)
        window.region_combo.blockSignals(False)
        window.radius_spin.blockSignals(True)
        window.radius_spin.setValue(RADIUS / 1000)
        window.radius_spin.blockSignals(False)
        window.region_label.setText(window._circle.describe())  # noqa: SLF001
        window.category_combo.setCurrentIndex(window.category_combo.findData("咖啡厅"))
        window.prefs_edit.setPlainText("安静、适合聊天、人均 80 元以内，最好离地铁近")
        window.map.set_circle(CENTER[0], CENTER[1], RADIUS)
        window._run_recommendation()  # noqa: SLF001

    def poll() -> None:
        finished = window._current_task is None and bool(window._results)  # noqa: SLF001
        if finished:
            QTimer.singleShot(2500, capture)
            return
        if time.time() > deadline:
            print("超时：未在 180 秒内得到推荐结果，仍保存当前界面")
            capture()
            return
        QTimer.singleShot(700, poll)

    def capture() -> None:
        out = ROOT / "docs"
        out.mkdir(exist_ok=True)
        window.map.fit_all()
        QTimer.singleShot(1200, lambda: _save(out))

    def _save(out: Path) -> None:
        window.grab().save(str(out / "screenshot_main.png"))
        window.map_view.grab().save(str(out / "screenshot_map.png"))
        print("results:", len(window._results))  # noqa: SLF001
        for r in window._results:  # noqa: SLF001
            print(f"  #{r.rank} {r.poi.name} | {r.reason[:40]}")
        print("saved screenshots")
        app.quit()

    QTimer.singleShot(3000, start)
    QTimer.singleShot(8000, poll)
    code = app.exec()
    runtime.stop()
    db.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
