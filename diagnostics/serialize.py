"""把运行结果转换成稳定的 JSON 结构（CLI 与 benchmark 共用）。"""

from __future__ import annotations

from typing import Any

from diagnostics.runner import RunOutcome
from models.schemas import Recommendation

__all__ = ["recommendation_list", "outcome_data", "RECOMMENDATION_SCHEMA"]


RECOMMENDATION_SCHEMA = {
    "rank": int,
    "poi_id": str,
    "name": str,
    "lng": float,
    "lat": float,
    "address": str,
    "type": str,
    "reason": str,
    "source": str,
    "leg_a": dict | None,
    "leg_b": dict | None,
}


def _leg(leg) -> dict[str, Any] | None:  # noqa: ANN001
    if leg is None:
        return None
    return {
        "mode": leg.mode.value,
        "ok": leg.ok,
        "duration_min": leg.duration_min,
        "distance_km": leg.distance_km,
        "transfers": leg.transfers,
        "walk_m": round(leg.walk_m),
        "cached": leg.cached,
        "error": leg.error or None,
    }


def recommendation_list(items: list[Recommendation]) -> list[dict[str, Any]]:
    return [
        {
            "rank": rec.rank,
            "poi_id": rec.poi.id,
            "name": rec.poi.name,
            "lng": round(rec.poi.lng, 6),
            "lat": round(rec.poi.lat, 6),
            "address": rec.poi.address,
            "type": rec.poi.type_name,
            "reason": rec.reason,
            "source": rec.source,
            "leg_a": _leg(rec.leg_a),
            "leg_b": _leg(rec.leg_b),
        }
        for rec in items
    ]


def outcome_data(outcome: RunOutcome, *, include_candidates: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "case_name": outcome.case.name,
        "data_mode": outcome.data_mode,
        "case": outcome.case.describe(),
        "points": outcome.points,
        "warnings": outcome.trace.warnings + outcome.warnings,
    }
    result = outcome.result
    if outcome.status in ("ok", "no_result") and hasattr(result, "recommendations"):
        data.update(
            {
                "source": result.source,
                "notice": result.notice,
                "ai_summary": result.ai_summary,
                "region": result.region_desc,
                "candidates_considered": result.candidates_considered,
                "elapsed_s": result.elapsed_s,
                "recommendations": recommendation_list(result.recommendations),
            }
        )
    elif isinstance(result, dict):
        data.update(result)
    if include_candidates:
        data["candidates"] = outcome.trace.snapshot()["candidates"]
    return data
