"""推荐结果校验：区域、坐标、POI ID、数量等硬性条件。"""

from __future__ import annotations

import math
from typing import Any

from diagnostics.cases import CaseConfig

__all__ = ["validate_result"]


def _iter_items(result: dict[str, Any] | list[Any]) -> list[Any]:
    if isinstance(result, list):
        return result
    for key in ("recommendations", "items", "data"):
        value = result.get(key)
        if isinstance(value, list):
            return value
    return []


def _item_poi(item: dict[str, Any]) -> dict[str, Any]:
    poi = item.get("poi")
    if isinstance(poi, dict):
        return poi
    return item


def validate_result(case: CaseConfig, result: dict[str, Any] | list[Any]) -> dict[str, Any]:
    items = _iter_items(result)
    violations: list[dict[str, Any]] = []
    region = None
    region_error = ""
    try:
        region = case.region.build() if case.region.mode != "none" else None
    except Exception as exc:  # noqa: BLE001
        region_error = str(exc)

    if not items:
        violations.append(
            {"code": "EMPTY_RESULT", "message": "结果中没有推荐条目", "severity": "error"}
        )

    seen_ids: set[str] = set()
    ranks: list[int] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            violations.append(
                {"code": "INVALID_ITEM", "message": "推荐条目必须是对象", "index": index}
            )
            continue
        poi = _item_poi(item)
        poi_id = str(item.get("poi_id") or poi.get("id") or "")
        name = str(poi.get("name") or item.get("name") or "")
        lng = poi.get("lng", item.get("lng"))
        lat = poi.get("lat", item.get("lat"))
        rank = item.get("rank", index)

        if not poi_id:
            violations.append(
                {"code": "MISSING_POI_ID", "message": "条目缺少 poi_id", "index": index, "name": name}
            )
        elif poi_id in seen_ids:
            violations.append(
                {"code": "DUPLICATE_POI_ID", "message": "同一 poi_id 重复出现", "poi_id": poi_id}
            )
        else:
            seen_ids.add(poi_id)

        if isinstance(rank, int):
            ranks.append(rank)

        if lng is None or lat is None:
            violations.append(
                {"code": "MISSING_COORD", "message": "条目缺少经纬度", "poi_id": poi_id or None}
            )
            continue
        try:
            lng_f = float(lng)
            lat_f = float(lat)
        except (TypeError, ValueError):
            violations.append(
                {"code": "INVALID_COORD", "message": "经纬度不是数字", "poi_id": poi_id or None}
            )
            continue

        if not (math.isfinite(lng_f) and math.isfinite(lat_f)):
            violations.append(
                {"code": "INVALID_COORD", "message": "经纬度不是有限数值", "poi_id": poi_id or None}
            )
            continue
        if not (-180.0 <= lng_f <= 180.0 and -90.0 <= lat_f <= 90.0):
            violations.append(
                {
                    "code": "OUT_OF_RANGE_COORD",
                    "message": "经纬度超出合法范围",
                    "poi_id": poi_id or None,
                    "location": [lng_f, lat_f],
                }
            )
            continue
        if abs(lng_f) < 1e-9 and abs(lat_f) < 1e-9:
            violations.append(
                {
                    "code": "NULL_ISLAND",
                    "message": "坐标位于 (0,0)，通常是缺失值或坐标系错误",
                    "poi_id": poi_id or None,
                    "location": [lng_f, lat_f],
                }
            )
            continue

        if region is not None and not region.contains(lng_f, lat_f):
            violations.append(
                {
                    "code": "OUT_OF_REGION",
                    "message": "推荐地点位于允许区域之外",
                    "poi_id": poi_id or None,
                    "name": name,
                    "location": [round(lng_f, 6), round(lat_f, 6)],
                    "region": case.region.model_dump(),
                }
            )

    if len(items) > case.top_n:
        violations.append(
            {
                "code": "TOO_MANY_RESULTS",
                "message": f"推荐数量 {len(items)} 超过 top_n={case.top_n}",
                "count": len(items),
                "top_n": case.top_n,
            }
        )
    if ranks and (len(set(ranks)) != len(ranks) or min(ranks) < 1):
        violations.append(
            {"code": "INVALID_RANK", "message": "rank 重复或小于 1", "ranks": ranks}
        )
    if region_error:
        violations.append(
            {"code": "INVALID_REGION", "message": f"用例区域无法构建：{region_error}", "severity": "error"}
        )

    return {
        "ok": not violations,
        "checked": len(items),
        "violations": violations,
        "region_mode": case.region.mode,
        "top_n": case.top_n,
        "poi_ids": sorted(seen_ids),
    }
