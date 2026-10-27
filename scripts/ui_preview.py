"""确定性界面预览：注入演示数据、抓多张截图，不调用任何外部 API。

用法：
    .venv\\Scripts\\python.exe scripts\\ui_preview.py --out docs/before --size 1440x900

产物：
    <out>/main_1440.png    推荐结果 + 地图 + 详情
    <out>/detail_1440.png  详情标签
    <out>/ai_1440.png      AI 分析标签
    <out>/main_1100.png    最小宽度下的布局
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from config.settings import get_settings  # noqa: E402
from geometry.regions import CircleRegion  # noqa: E402
from models.schemas import (  # noqa: E402
    HealthSnapshot,
    Poi,
    RecMode,
    Recommendation,
    RegionMode,
    RouteLeg,
    ServiceName,
    ServiceState,
    TravelMode,
    UserPoint,
)
from storage.db import Database  # noqa: E402
from ui.async_runtime import AsyncRuntime  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

POINT_A = UserPoint(lng=117.210089, lat=39.135951, name="天津站", adcode="120105", source="search")
POINT_B = UserPoint(lng=117.311568, lat=38.992672, name="天津大学（卫津路校区）", adcode="120112", source="search")
CENTER = (117.2480, 39.1180)
RADIUS = 3000.0

# (名称, 类型, 地址, 商圈, 评分, 人均, A 分钟, B 分钟, A 公里, B 公里, A 换乘, B 换乘)
DEMO = [
    ("星巴克臻选（恒隆广场店）", "咖啡厅", "和平区南京路 219 号恒隆广场 L1", "南京路", 4.7, 45, 14, 21, 4.2, 6.8, 0, 1),
    ("西西弗书店·矢量咖啡", "咖啡厅", "和平区南京路 189 号和平大悦城 4F", "南京路", 4.5, 52, 12, 23, 3.6, 7.1, 0, 1),
    ("Manner Coffee（小白楼店）", "咖啡厅", "河西区南京路 39 号小白楼", "小白楼", 4.4, 28, 17, 19, 5.1, 5.9, 1, 1),
    ("Tims 天好咖啡（津湾广场店）", "咖啡厅", "和平区解放北路 津湾广场 1 号楼", "津湾广场", 4.3, 32, 9, 26, 2.4, 8.3, 0, 1),
    ("Seesaw Coffee（万象城店）", "咖啡厅", "河西区乐园道 9 号万象城 L2", "乐园道", 4.6, 48, 22, 16, 7.4, 4.8, 1, 0),
    ("漫咖啡（五大道店）", "咖啡厅", "和平区成都道 32 号", "五大道", 4.2, 40, 16, 18, 4.9, 5.4, 1, 1),
]

REASONS = [
    "双方都是零换乘或一次换乘，A 从天津站出发 14 分钟直达，B 从天大骑行接地铁 21 分钟，总通勤 35 分钟为全部候选中最低；人均 45 元在预算内。",
    "书店与咖啡同层，安静度评价最高，符合「适合聊天」的偏好；A 12 分钟直达，B 需在营口道换乘一次。",
    "价格最低，双方通勤时间接近，但 A 需换乘一次，步行段 480 米稍长。",
    "A 的通勤最短（9 分钟），但 B 需 26 分钟，双方差距 17 分钟，公平性一般。",
    "B 的通勤最短，A 需 22 分钟跨河，总时长 38 分钟，作为备选保留。",
    "五大道环境安静，双方时长接近，但步行段合计 1.2 公里，雨天体验一般。",
]


def _leg(minutes: float, km: float, transfers: int, walking_km: float, mode: TravelMode) -> RouteLeg:
    steps = [
        f"步行 {walking_km * 0.35 * 1000:.0f} 米至地铁站",
        "地铁 3 号线 · 2 站",
        "出站步行 210 米到达",
    ] if transfers else ["步行 320 米至公交站", "公交 8 路 · 4 站", "下车步行 180 米到达"]
    return RouteLeg(
        mode=mode,
        ok=True,
        distance_m=km * 1000,
        duration_s=minutes * 60,
        transfers=transfers,
        walk_m=walking_km * 1000,
        summary=f"地铁 3 号线 · {int(minutes)} 分钟",
        steps=steps,
        cached=True,
        cached_at=datetime.now(timezone.utc) - timedelta(minutes=4),
    )


def build_results() -> list[Recommendation]:
    results: list[Recommendation] = []
    for rank, row in enumerate(DEMO, start=1):
        name, kind, address, area, rating, cost, a_min, b_min, a_km, b_km, a_tr, b_tr = row
        poi = Poi(
            id=f"B0FFH{rank:02d}Q7X{rank}",
            name=name,
            lng=CENTER[0] + (rank - 3.5) * 0.0105,
            lat=CENTER[1] + (rank - 3.5) * 0.0068,
            address=address,
            type_name=f"餐饮服务;咖啡厅;{kind}",
            tel=f"022-2711{rank:02d}{rank}8",
            cityname="天津市",
            adname="和平区",
            business_area=area,
            adcode="120101",
            rating=rating,
            cost=cost,
        )
        results.append(
            Recommendation(
                rank=rank,
                poi=poi,
                leg_a=_leg(a_min, a_km, a_tr, a_km * 0.12, TravelMode.TRANSIT),
                leg_b=_leg(b_min, b_km, b_tr, b_km * 0.14, TravelMode.TRANSIT),
                reason=REASONS[rank - 1],
                source="ai",
                score=round(100 - rank * 4.5, 1),
            )
        )
    return results


class _ResultStub:
    def __init__(self) -> None:
        self.recommendations = build_results()
        self.candidates_considered = 27
        self.stats = {
            "tiles": 4,
            "api_calls": 11,
            "raw_pois": 96,
            "unique_pois": 61,
            "in_region": 38,
            "routed": 27,
        }
        self.source = "ai"
        self.elapsed_s = 6.4
        self.region_desc = "圆形区域 · 中心 117.248000,39.118000 · 半径 3.0 公里"
        self.notice = ""
        self.ai_summary = (
            "27 个双方可达候选中，南京路一带的 3 家咖啡馆总通勤时间都在 40 分钟以内，"
            "且与两人出发地形成近似等腰三角形。若更看重安静度，优先第 2 条（书店咖啡）；"
            "若更看重准时，优先第 1 条（零换乘）。"
        )
        self.warnings = []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/preview")
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--delay", type=int, default=9000)
    parser.add_argument("--empty", action="store_true")
    args = parser.parse_args()

    width, height = (int(v) for v in args.size.lower().split("x"))
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)

    from PySide6.QtGui import QFont

    from ui.theme import install_fonts

    app.setStyle("Fusion")
    ui_font = QFont()
    ui_font.setFamilies([install_fonts(), "Microsoft YaHei UI", "Segoe UI"])
    ui_font.setPixelSize(13)
    app.setFont(ui_font)

    settings = get_settings()
    db = Database(settings.data_dir / "ui_preview.sqlite3")
    runtime = AsyncRuntime()
    from services.bootstrap import build_services

    from algorithms.pipeline import RecommendationPipeline

    transport, amap, deepseek, monitor = build_services(settings, db)
    pipeline = RecommendationPipeline(amap, deepseek, settings, monitor, db)
    window = MainWindow(settings, runtime, db, amap, deepseek, monitor, pipeline)
    window.resize(width, height)
    window.show()

    # 跳过启动时真实体检：只注入演示用的健康快照
    window._map_loaded = True  # noqa: SLF001
    window.map.load()

    def fake_health() -> None:
        now = datetime.now(timezone.utc)
        for name, latency in (("amap_web", 81), ("amap_js", 64), ("deepseek", 121)):
            state = ServiceState.OK if name != "amap_js" else ServiceState.OK
            window._apply_health(  # noqa: SLF001
                name,
                HealthSnapshot(
                    service=ServiceName(name),
                    state=state,
                    last_check_at=now,
                    last_success_at=now,
                    latency_ms=latency,
                    http_status=200,
                    detail="演示快照",
                ),
            )

    def inject() -> None:
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
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(RecMode.AI.value))
        window.prefs_edit.setPlainText("安静、适合聊天、人均 80 元以内，最好离地铁近")
        window.map.set_circle(CENTER[0], CENTER[1], RADIUS)
        fake_health()
        if args.empty:
            window.summary_label.setText("尚未开始")
            window._update_scope()  # noqa: SLF001
            return

        result = _ResultStub()
        window._results = result.recommendations  # noqa: SLF001
        window._fill_table(window._results)  # noqa: SLF001
        window._render_ai(result)  # noqa: SLF001
        window._render_results_on_map()  # noqa: SLF001
        window.progress.setValue(100)
        window.progress.setVisible(False)
        window.summary_label.setText("6 个 · 候选 27 个 · 双方可达 27 个 · 用时 6.4s")
        window.source_label.setText("AI 综合推荐")
        window._update_scope()  # noqa: SLF001
        window.status_label.setText(
            "完成：6 个推荐 · 候选 27 个 · 双方可达 27 个 · 用时 6.4s · 来源 AI"
        )
        window.table.selectRow(0)

    def shots() -> None:
        window.grab().save(str(out / f"main_{width}.png"))
        for index, name in ((1, "ai"), (2, "log")):
            window.tabs.setCurrentIndex(index)
            app.processEvents()
            window.grab().save(str(out / f"{name}_{width}.png"))
        window.tabs.setCurrentIndex(0)
        app.processEvents()
        window.table.selectRow(2)
        app.processEvents()
        window.grab().save(str(out / f"detail2_{width}.png"))

        window.resize(1100, 760)
        app.processEvents()
        QTimer.singleShot(1200, shots_narrow)

    def shots_narrow() -> None:
        window.grab().save(str(out / "main_1100.png"))
        print("saved:", out)
        app.quit()

    QTimer.singleShot(2500, inject)
    QTimer.singleShot(args.delay, shots)
    code = app.exec()
    runtime.stop()
    db.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
