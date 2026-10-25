"""真实 API 联调脚本：用真实高德 / DeepSeek 接口跑通完整推荐流程。

用法：
    .venv\\Scripts\\python.exe scripts\\live_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from algorithms.pipeline import RecommendRequest, RecommendationPipeline  # noqa: E402
from config.settings import get_settings  # noqa: E402
from geometry.regions import CircleRegion  # noqa: E402
from models.schemas import RecMode, RegionMode, TravelMode, UserPoint  # noqa: E402
from monitoring.health import HealthMonitor  # noqa: E402
from services.amap_web import AMapWebService  # noqa: E402
from services.deepseek import DeepSeekService  # noqa: E402
from services.http_client import HttpMetrics, HttpTransport  # noqa: E402
from storage.db import Database  # noqa: E402


async def main() -> int:
    settings = get_settings()
    radius_km = 3.0
    top_n = 3
    for arg in sys.argv[1:]:
        if arg.startswith("--radius-km="):
            radius_km = float(arg.split("=", 1)[1])
        elif arg.startswith("--top-n="):
            top_n = int(arg.split("=", 1)[1])
    print(f"参数：半径 {radius_km} 千米，推荐 {top_n} 个")
    print("配置：", json.dumps(settings.masked(), ensure_ascii=False))
    db = Database(settings.data_dir / "live_check.sqlite3")
    transport = HttpTransport(
        timeout=settings.http_timeout,
        connect_timeout=settings.http_connect_timeout,
        metrics=HttpMetrics(),
    )
    amap = AMapWebService(settings.amap_web_key, transport, cache=db)
    deepseek = DeepSeekService(
        settings.deepseek_api_key,
        transport,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        cache=db,
    )
    monitor = HealthMonitor(db, latency_warn_ms=settings.latency_warn_ms)
    monitor.register("amap_web", amap.health_probe, configured=settings.amap_web_configured)
    monitor.register("deepseek", deepseek.health_probe, configured=settings.deepseek_configured)
    monitor.set_latency_threshold("deepseek", settings.llm_latency_warn_ms)

    print("\n=== 健康检测 ===")
    await monitor.check_all(["amap_web", "deepseek"])
    for name, snap in monitor.all_snapshots().items():
        print(f"{name:10s} {snap.state.value:14s} {snap.latency_ms} ms  {snap.detail}")

    print("\n=== 地点解析 ===")
    hits = await amap.place_text("天津站", offset=3)
    for poi in hits.pois[:3]:
        print(f"  {poi.id} {poi.name} {poi.lng:.6f},{poi.lat:.6f} {poi.adname}")
    geo = await amap.geocode("天津大学")
    for hit in geo[:1]:
        print(f"  geocode: {hit.formatted_address} {hit.location}")

    a = UserPoint(lng=hits.pois[0].lng, lat=hits.pois[0].lat, name="天津站", adcode=hits.pois[0].adcode)
    b_hit = geo[0] if geo else hits.pois[0]
    b = UserPoint(lng=b_hit.lng, lat=b_hit.lat, name="天津大学", adcode=b_hit.adcode or "")
    region = CircleRegion(117.2115, 39.1360, radius_km * 1000.0)

    pipeline = RecommendationPipeline(amap, deepseek, settings, monitor, db)
    req = RecommendRequest(
        point_a=a,
        point_b=b,
        region=region,
        region_mode=RegionMode.CIRCLE,
        travel_mode=TravelMode.TRANSIT,
        category="咖啡厅",
        rec_mode=RecMode.AI,
        custom_prefs="安静、适合聊天、人均 80 元以内",
        top_n=top_n,
        city_a=a.adcode,
        city_b=b.adcode,
    )

    def progress(pct: int, msg: str) -> None:
        print(f"  [{pct:3d}%] {msg}")

    print("\n=== 完整推荐 ===")
    try:
        result = await pipeline.run(req, progress=progress)
    except Exception as exc:  # noqa: BLE001
        print("推荐失败：", type(exc).__name__, exc)
        await transport.aclose()
        db.close()
        return 1

    print(f"来源：{result.source}  区域：{result.region_desc}  候选：{result.candidates_considered}")
    print(f"提示：{result.notice or '无'}")
    print(f"AI 摘要：{result.ai_summary or '（无）'}")
    for rec in result.recommendations:
        print(f"\n#{rec.rank} {rec.poi.name}  ({rec.poi.lng:.6f},{rec.poi.lat:.6f})  ID={rec.poi.id}")
        print(f"  地址：{rec.poi.address}")
        print(f"  A：{rec.leg_a.text if rec.leg_a else '—'}  换乘 {rec.leg_a.transfers if rec.leg_a else '—'}")
        print(f"  B：{rec.leg_b.text if rec.leg_b else '—'}  换乘 {rec.leg_b.transfers if rec.leg_b else '—'}")
        print(f"  理由：{rec.reason}")
        inside = region.contains(rec.poi.lng, rec.poi.lat)
        print(f"  区域硬约束校验：{'通过' if inside else '失败（不应出现）'}")

    print("\n统计：", json.dumps(result.stats, ensure_ascii=False))
    print("Token：", json.dumps(deepseek.usage.snapshot(), ensure_ascii=False))
    await transport.aclose()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
