"""无界面 UI 功能校验：半径输入换算、推荐数量、设置对话框。

用法：
    .venv\\Scripts\\python.exe scripts\\ui_check.py
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
from config.settings import Settings  # noqa: E402
from services.bootstrap import build_services  # noqa: E402
from storage.db import Database  # noqa: E402
from ui.async_runtime import AsyncRuntime  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    settings = Settings.load()
    db = Database(settings.data_dir / "ui_check.sqlite3")
    runtime = AsyncRuntime()
    transport, amap, deepseek, monitor = build_services(settings, db)
    pipeline = RecommendationPipeline(amap, deepseek, settings, monitor, db)
    window = MainWindow(settings, runtime, db, amap, deepseek, monitor, pipeline)
    window.show()

    failures: list[str] = []

    def check(name: str, condition: bool, extra: str = "") -> None:
        print(f"[{'OK ' if condition else 'FAIL'}] {name}{(' -> ' + extra) if extra else ''}")
        if not condition:
            failures.append(name)

    def run_checks() -> None:
        spin = window.radius_spin
        spin.setValue(spin.valueFromText("〢〦.〇"))
        check("苏州码子半径 26 -> 26000 m", abs(window._radius_meters() - 26000) < 1, f"{window._radius_meters():.0f}")
        spin.setValue(spin.valueFromText("２６．５"))
        check("全角 26.5 -> 26500 m", abs(window._radius_meters() - 26500) < 1, f"{window._radius_meters():.0f}")

        window.radius_unit.setCurrentIndex(window.radius_unit.findData(1.0))
        check("切换到米：读数=26500", abs(spin.value() - 26500) < 1, f"{spin.value():.0f}")
        spin.setValue(820)
        check("米制 820 -> 820 m", abs(window._radius_meters() - 820) < 1, f"{window._radius_meters():.0f}")
        window.radius_unit.setCurrentIndex(window.radius_unit.findData(1000.0))
        check("切回千米：读数=0.82", abs(spin.value() - 0.82) < 0.01, f"{spin.value():.2f}")

        # 圆形区域联动
        from geometry.regions import CircleRegion

        window._circle = CircleRegion(117.2115, 39.1360, 26000)
        window._region_mode = window._region_mode.__class__.CIRCLE
        window._on_radius_changed()
        check("圆形半径刷新无异常", window._circle is not None, window.region_label.text()[:40])

        # 推荐数量
        window.topn_spin.setValue(7)
        check("推荐数量联动 settings", window.settings.top_n == 7, str(window.settings.top_n))

        # 设置对话框
        dlg = SettingsDialog(window.settings, window)
        check("对话框默认无改动", dlg.changed_values() == {}, str(dlg.changed_values()))
        dlg._widgets["top_n"].setValue(4)  # noqa: SLF001
        changes = dlg.changed_values()
        check("修改 top_n 被识别", changes.get("top_n") == 4, str(changes))
        check("仅改 top_n 不需重建服务", not SettingsDialog.requires_service_reload(changes))
        dlg.deleteLater()

        # 运行时重建服务（设置里改 Key/模型后会走这条路径）
        old_monitor = window.monitor
        window.settings = window.settings.with_overrides(top_n=4)
        window._rebuild_services(False)  # noqa: SLF001
        check("服务重建成功", window.monitor is not old_monitor)
        QTimer.singleShot(4500, finish)

    def finish() -> None:
        snap = window.monitor.snapshot("amap_web")
        check("重建后健康检测有结果", snap.state.value != "unknown", f"{snap.state.value} / {snap.latency_ms} ms")
        print("\n结果：", "全部通过" if not failures else f"失败 {len(failures)} 项: {failures}")
        app.quit()

    QTimer.singleShot(4000, run_checks)
    code = app.exec()
    runtime.stop()
    db.close()
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
