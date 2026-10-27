"""候选覆盖、去重和限额选取的回归测试。"""

from __future__ import annotations

import asyncio

from algorithms import candidates
from geometry.regions import CircleRegion, CircleTile
from models.schemas import Poi, UserPoint
from services.amap_web import PoiPage


def test_pages_are_shared_across_tiles_before_budget_runs_out(monkeypatch):
    centers = [(117.20 + i * 0.005, 39.13) for i in range(4)]
    monkeypatch.setattr(
        candidates,
        "tile_circle",
        lambda *args, **kwargs: [CircleTile(x, y, 1000) for x, y in centers],
    )

    class PagedAMap:
        def __init__(self):
            self.calls = []

        async def place_around(self, center, radius, *, page, offset, **kwargs):
            index = centers.index(center)
            self.calls.append((index, page))
            poi = Poi(id=f"T{index}P{page}", name=f"店{index}-{page}", lng=center[0], lat=center[1])
            return PoiPage(total=4, pois=[poi], page=page, offset=offset)

    amap = PagedAMap()
    a = UserPoint(name="A", lng=117.20, lat=39.13)
    b = UserPoint(name="B", lng=117.22, lat=39.13)
    pois, stats = asyncio.run(
        candidates.discover_pois(
            amap, CircleRegion(117.21, 39.13, 3000), point_a=a, point_b=b,
            page_size=1, max_pages=4, max_tiles=4, max_candidates=100,
        )
    )
    assert amap.calls == [(i, 1) for i in range(4)] + [(i, 2) for i in range(4)]
    assert stats.budget_hit
    assert len(pois) == 8


def test_preselection_uses_rating_and_coverage_not_start_distance():
    a = UserPoint(name="A", lng=117.19, lat=39.13)
    b = UserPoint(name="B", lng=117.23, lat=39.13)
    pois = [
        Poi(id=f"P{i:02d}", name=f"店{i}", lng=117.21 + i * 0.001, lat=39.13 + i * 0.001)
        for i in range(10)
    ]
    pois[-1].rating = 4.9
    selected = candidates._preselect_pois(pois, a, b, 4)
    assert "P09" in {p.id for p in selected}
    assert len({p.id for p in selected}) == 4


def test_dedupe_uses_actual_distance_not_coordinate_grid():
    pois = [
        Poi(id="A", name="同一家店", lng=117.21, lat=39.13),
        Poi(id="B", name="同一家店", lng=117.2102, lat=39.13),
        Poi(id="C", name="同一家店", lng=117.211, lat=39.13),
    ]
    dropped = []
    kept = candidates._dedupe(pois, dropped)
    assert [p.id for p in kept] == ["A", "C"]
    assert [p.id for p in dropped] == ["B"]
