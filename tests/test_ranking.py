"""确定性排序测试（需求 5.2）。"""

from __future__ import annotations

from algorithms.ranking import RankContext, rank_candidates, score_candidates, to_recommendations
from models.schemas import Candidate, Poi, RecMode, RouteLeg, TravelMode


def cand(pid: str, dur_a: float, dur_b: float, *, ta: int = 0, tb: int = 0,
         walk_a: float = 0.0, speed: str = "transit", code: str = "050500") -> Candidate:
    poi = Poi(id=pid, name=pid, lng=117.2, lat=39.1, type_code=code, type_name="咖啡厅")
    leg_a = RouteLeg(mode=TravelMode(speed), ok=True, duration_s=dur_a, transfers=ta, walk_m=walk_a)
    leg_b = RouteLeg(mode=TravelMode(speed), ok=True, duration_s=dur_b, transfers=tb)
    return Candidate(poi=poi, leg_a=leg_a, leg_b=leg_b)


def test_total_time_ranking():
    items = [cand("slow", 1800, 1800), cand("fast", 600, 900), cand("mid", 1200, 1000)]
    ordered = rank_candidates(items, RankContext(rec_mode=RecMode.TOTAL_TIME), top_n=3)
    assert [c.poi.id for c in ordered] == ["fast", "mid", "slow"]
    assert ordered[0].score < ordered[-1].score


def test_fewest_transfers_ranking():
    items = [cand("t2", 600, 600, ta=2, tb=1), cand("t0", 1500, 1500, ta=0, tb=0)]
    ordered = rank_candidates(items, RankContext(rec_mode=RecMode.FEWEST_TRANSFERS), top_n=2)
    assert ordered[0].poi.id == "t0"


def test_category_priority():
    items = [cand("park", 900, 900, code="110101"), cand("cafe", 1200, 1200, code="050500")]
    ordered = rank_candidates(
        items, RankContext(rec_mode=RecMode.CATEGORY_PRIORITY, category="咖啡厅"), top_n=2
    )
    assert ordered[0].poi.id == "cafe"


def test_unreachable_excluded():
    poi = Poi(id="dead", name="dead", lng=117.2, lat=39.1)
    bad = Candidate(
        poi=poi,
        leg_a=RouteLeg(ok=False, error="no route"),
        leg_b=RouteLeg(ok=False, error="no route"),
    )
    good = cand("ok", 600, 600)
    ordered = rank_candidates([bad, good], RankContext(), top_n=5)
    assert [c.poi.id for c in ordered] == ["ok"]


def test_long_walk_penalty():
    normal = cand("normal", 1200, 1200)
    long_walk = cand("walk", 1200, 1200, walk_a=3000)
    ordered = score_candidates([long_walk, normal], RankContext())
    assert ordered[0].poi.id == "normal"


def test_to_recommendations_ranks_and_reason():
    ordered = rank_candidates([cand("a", 600, 700), cand("b", 800, 900)], RankContext(), top_n=2)
    recs = to_recommendations(ordered, source="rule")
    assert [r.rank for r in recs] == [1, 2]
    assert all(r.reason for r in recs)
    assert recs[0].source == "rule"
    assert "分钟" in recs[0].reason


def test_no_fairness_metric_in_reason():
    # 需求明确取消"公平性评分"，理由中不得出现公平性表述
    ordered = rank_candidates([cand("a", 600, 900)], RankContext(), top_n=1)
    recs = to_recommendations(ordered)
    assert "公平" not in recs[0].reason
