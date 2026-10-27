"""批量基准测试：一次运行多个用例并汇总。"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from algorithms.candidates import CancelToken
from diagnostics.cases import CaseConfig, CaseError, load_case
from diagnostics.runner import RunOptions, run_search
from diagnostics.serialize import outcome_data, recommendation_list
from diagnostics.validate import validate_result

__all__ = ["run_suite", "load_suite"]


def load_suite(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    if not file.is_file():
        raise CaseError("SUITE_NOT_FOUND", f"找不到测试套件：{file}")
    import json

    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise CaseError("SUITE_INVALID", f"套件不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise CaseError("SUITE_INVALID", "套件需要 cases 数组")
    return data


def _case_from_entry(entry: dict[str, Any], base_dir: Path) -> CaseConfig:
    if isinstance(entry.get("config"), str):
        return load_case(base_dir / entry["config"])
    if isinstance(entry.get("config"), dict):
        payload = entry["config"]
        if "name" not in payload and entry.get("name"):
            payload = {**payload, "name": entry["name"]}
        return CaseConfig.model_validate(payload)
    if isinstance(entry.get("case"), dict):
        return CaseConfig.model_validate(entry["case"])
    raise CaseError("SUITE_INVALID", "每个用例需要 config（路径或对象）")


async def run_suite(
    suite: dict[str, Any],
    options: RunOptions,
    *,
    base_dir: Path | None = None,
    max_cases: int | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    base = base_dir or Path.cwd()
    entries = suite.get("cases") or []
    limit = max_cases or int((suite.get("budget") or {}).get("max_cases") or 20)
    selected = entries[: max(1, limit)]
    total_api_budget = int((suite.get("budget") or {}).get("max_api_calls") or 0) or None

    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    api_calls = 0
    stop_reason = ""

    for index, entry in enumerate(selected, start=1):
        if cancel:
            cancel.raise_if_cancelled()
        # 预算要在「开跑之前」检查：事后检查等于白白多烧掉一整个用例的配额
        if total_api_budget is not None and api_calls >= total_api_budget:
            stop_reason = (
                f"套件累计 API 调用已达预算上限 {total_api_budget}，剩余 {len(selected) - index + 1} 个用例未执行"
            )
            break
        if not isinstance(entry, dict):
            results.append({"index": index, "status": "failed", "error": "用例条目必须是对象"})
            continue
        try:
            case = _case_from_entry(entry, base)
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "index": index,
                    "name": entry.get("name") or f"case-{index}",
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        case_options = options
        if total_api_budget is not None:
            # 把「剩余额度」交给单个用例，避免每例都用满自己的预算导致整体超支
            remaining = max(1, total_api_budget - api_calls)
            per_case = options.max_api_calls
            case_options = replace(options, max_api_calls=min(per_case, remaining) if per_case else remaining)

        outcome = await run_search(case, case_options, cancel)
        api_calls += int(outcome.stats.get("api_calls", 0) or 0)
        validation = None
        if outcome.status in ("ok", "no_result") and hasattr(outcome.result, "recommendations"):
            validation = validate_result(
                case,
                {"recommendations": recommendation_list(outcome.result.recommendations)},
            )
        entry_result = {
            "index": index,
            "run_id": outcome.run_id,
            "name": case.name,
            "status": outcome.status,
            "elapsed_ms": outcome.elapsed_ms,
            "data_mode": outcome.data_mode,
            "api_calls": int(outcome.stats.get("api_calls", 0) or 0),
            "tokens": int(outcome.stats.get("tokens", 0) or 0),
            "counts": outcome.trace.snapshot()["counts"],
            "recommendations": (
                recommendation_list(outcome.result.recommendations)
                if hasattr(outcome.result, "recommendations")
                else []
            ),
            "validation": validation,
            "warnings": outcome.trace.warnings + outcome.warnings,
            "errors": outcome.trace.errors + outcome.errors,
            "ai": outcome.trace.snapshot()["ai"],
        }
        results.append(entry_result)

        if total_api_budget is not None and api_calls > total_api_budget:
            entry_result["status"] = "aborted"
            entry_result["error"] = f"套件累计 API 调用 {api_calls} 超过预算 {total_api_budget}"
            stop_reason = entry_result["error"]
            break

    ok = sum(1 for r in results if r.get("status") == "ok")
    no_result = sum(1 for r in results if r.get("status") == "no_result")
    failed = sum(1 for r in results if r.get("status") in ("failed", "aborted"))
    violations = sum(
        1 for r in results if isinstance(r.get("validation"), dict) and not r["validation"].get("ok")
    )
    summary: dict[str, Any] = {
        "ok": ok,
        "no_result": no_result,
        "failed": failed,
        "validation_violations": violations,
        "api_calls": api_calls,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "executed": len(results),
        "selected": len(selected),
    }
    if stop_reason:
        summary["aborted"] = True
        summary["abort_reason"] = stop_reason
    return {
        "suite": suite.get("name") or "suite",
        # cases_total 表示「套件里计划执行的用例数」，而不是「已经跑出结果的行数」
        "cases_total": len(selected),
        "summary": summary,
        "cases": results,
    }
