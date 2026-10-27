"""预算、超时与取消。"""

from __future__ import annotations

import asyncio

import pytest

from algorithms.candidates import CancelToken, plan_routes
from diagnostics.cases import CaseConfig
from diagnostics.runner import RunOptions, run_candidates, run_search
from diagnostics.sources import BudgetExceeded
from models.schemas import Poi, UserPoint


def _case() -> CaseConfig:
    return CaseConfig.model_validate(
        {
            "name": "budget",
            "a": {"name": "A", "lng": 117.210089, "lat": 39.135951},
            "b": {"name": "B", "lng": 117.216, "lat": 39.14},
            "region": {"mode": "circle", "center": [117.2115, 39.136], "radius_m": 600},
            "top_n": 3,
        }
    )


def test_api_budget_aborts():
    outcome = asyncio.run(run_candidates(_case(), RunOptions(mock=True, max_api_calls=1)))
    assert outcome.status == "failed"
    assert outcome.errors and outcome.errors[0]["code"] == "BUDGET_EXCEEDED"
    assert outcome.stats["api_calls"] >= 1


def test_timeout_aborts():
    outcome = asyncio.run(run_search(_case(), RunOptions(mock=True, timeout_s=0.001)))
    assert outcome.status == "failed"
    assert outcome.errors[0]["code"] == "TIMEOUT"


def test_pre_cancelled_token():
    token = CancelToken()
    token.cancel("测试取消")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_search(_case(), RunOptions(mock=True), cancel=token))


def test_route_budget_error_is_not_reported_as_unreachable():
    class ExhaustedAMap:
        async def route(self, *args, **kwargs):
            raise BudgetExceeded("api_calls", 1, 2)

    a = UserPoint(name="A", lng=117.2, lat=39.13)
    b = UserPoint(name="B", lng=117.3, lat=39.14)
    poi = Poi(id="P1", name="测试点", lng=117.21, lat=39.13)
    with pytest.raises(BudgetExceeded):
        asyncio.run(plan_routes(ExhaustedAMap(), [poi], point_a=a, point_b=b))
