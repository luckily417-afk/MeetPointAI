"""MeetPoint AI 桌面程序入口。

运行：
    python main.py
打包：
    pyinstaller meetpoint.spec --noconfirm      # 生成 dist/MeetPointAI/MeetPointAI.exe
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import Settings, get_settings


def _install_privacy_filter() -> None:
    """日志脱敏：任何形如 sk-xxxx / key=xxxx 的内容都不落盘。"""

    import re

    secret = re.compile(r"(sk-[A-Za-z0-9]{6,}|(?:key|token|secret)=[^&\s]+)", re.IGNORECASE)

    class _Filter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001
                return True
            record.msg = secret.sub("***", message)
            record.args = ()
            return True

    for handler in logging.getLogger().handlers:
        handler.addFilter(_Filter())


def _setup_logging(settings: Settings) -> None:
    try:
        settings.ensure_dirs()
        log_file = settings.log_dir / "meetpoint.log"
        handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stderr)  # type: ignore[assignment]
    logging.basicConfig(
        level=logging.DEBUG if settings.verbose_log else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[handler],
    )
    _install_privacy_filter()
    logging.getLogger("PySide6").setLevel(logging.WARNING)


def build_services(settings: Settings, db):
    """向后兼容的再导出：实际实现位于 services/bootstrap.py。"""
    from services.bootstrap import build_services as _build

    return _build(settings, db)


def self_test(settings: Settings) -> int:
    """无界面自检（打包后验证用）：真实调用三路 API 并写报告文件。"""
    import asyncio
    import json

    from monitoring.health import HealthMonitor
    from storage.db import Database

    report: dict[str, object] = {"ok": False, "checks": {}}
    db = Database(settings.db_path)
    try:
        transport, amap, deepseek, monitor = build_services(settings, db)

        async def run() -> dict[str, object]:
            checks: dict[str, object] = {}
            await monitor.check_all(["amap_web", "deepseek"])
            for name in ("amap_web", "deepseek"):
                snap = monitor.snapshot(name)
                checks[name] = {
                    "state": snap.state.value,
                    "latency_ms": snap.latency_ms,
                    "detail": snap.detail,
                }
            try:
                page = await amap.place_text("天津站", offset=3)
                checks["amap_poi"] = {"count": len(page.pois), "first": page.pois[0].name if page.pois else None}
            except Exception as exc:  # noqa: BLE001
                checks["amap_poi"] = {"error": str(exc)[:200]}
            try:
                from models.schemas import TravelMode

                leg = await amap.route(
                    TravelMode.DRIVING,
                    (117.210089, 39.135951),
                    (117.2115, 39.1360),
                )
                checks["amap_route"] = {"ok": leg.ok, "summary": leg.summary}
            except Exception as exc:  # noqa: BLE001
                checks["amap_route"] = {"error": str(exc)[:200]}
            try:
                llm = await deepseek.chat_json(
                    [
                        {"role": "system", "content": '只输出 JSON：{"summary":"ok","items":[]}'},
                        {"role": "user", "content": "ping"},
                    ],
                    max_tokens=1500,
                    timeout_s=60,
                )
                checks["deepseek_chat"] = {"model": llm.model, "latency_ms": llm.latency_ms}
            except Exception as exc:  # noqa: BLE001
                checks["deepseek_chat"] = {"error": str(exc)[:200]}
            await transport.aclose()
            return checks

        report["checks"] = asyncio.run(run())
        checks = report["checks"]  # type: ignore[assignment]
        report["ok"] = bool(
            isinstance(checks, dict)
            and isinstance(checks.get("amap_web"), dict)
            and checks["amap_web"].get("state") in {"ok", "slow"}
            and isinstance(checks.get("deepseek"), dict)
            and checks["deepseek"].get("state") in {"ok", "slow"}
        )
    finally:
        db.close()

    try:
        settings.ensure_dirs()
        (settings.data_dir / "self_test.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 1


def main() -> int:
    settings = get_settings()
    _setup_logging(settings)

    if "--self-test" in sys.argv:
        return self_test(settings)

    try:
        from PySide6.QtCore import QLocale, Qt
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtWebEngineCore import QWebEngineProfile
    except ImportError as exc:  # pragma: no cover - 环境缺失提示
        print("缺少 PySide6 依赖，请先执行：pip install -r requirements.txt", file=sys.stderr)
        print(f"详细错误：{exc}", file=sys.stderr)
        return 2

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    QLocale.setDefault(QLocale.c())
    app = QApplication(sys.argv)
    app.setApplicationName("MeetPoint AI")
    app.setApplicationDisplayName("MeetPoint AI")
    app.setOrganizationName("MeetPoint")

    # 隐私：不持久化浏览数据、不保留地图页缓存
    try:
        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("WebEngine Profile 配置跳过", exc_info=True)

    from storage.db import Database
    from ui.async_runtime import AsyncRuntime
    from ui.main_window import MainWindow
    from algorithms.pipeline import RecommendationPipeline

    db = Database(settings.db_path)
    runtime = AsyncRuntime()
    transport, amap, deepseek, monitor = build_services(settings, db)
    pipeline = RecommendationPipeline(amap, deepseek, settings, monitor, db)

    window = MainWindow(settings, runtime, db, amap, deepseek, monitor, pipeline)

    def _excepthook(exc_type, exc_value, exc_tb):  # pragma: no cover
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logging.getLogger("meetpoint").error("未捕获异常：%s", text)
        QMessageBox.critical(window, "程序异常", f"{exc_value}\n\n详细信息已写入日志。")

    sys.excepthook = _excepthook
    window.show()

    if not (settings.amap_web_configured and settings.deepseek_configured):
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1200, window.first_run_setup)

    # 打包后自检截图：--screenshot <path>，6 秒后抓取窗口并退出
    if "--screenshot" in sys.argv:
        index = sys.argv.index("--screenshot")
        target = sys.argv[index + 1] if len(sys.argv) > index + 1 else "screenshot.png"
        from PySide6.QtCore import QTimer

        def _grab() -> None:
            try:
                window.prepare_demo_state()
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                window.grab().save(target)
                logging.getLogger("meetpoint").info("screenshot saved: %s", target)
            finally:
                app.quit()

        QTimer.singleShot(9000, _grab)

    try:
        return app.exec()
    finally:
        try:
            if runtime.is_running():
                runtime.run_sync(transport.aclose(), timeout=5.0)
        except Exception:  # noqa: BLE001
            pass
        runtime.stop()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
