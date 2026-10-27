"""meetpoint-debug 命令行入口。

示例：
    meetpoint-debug health --json
    meetpoint-debug search --config cases/tianjin.json --mock --json
    meetpoint-debug candidates --config cases/tianjin.json --json
    meetpoint-debug validate --config cases/tianjin.json --result out/result.json --json
    meetpoint-debug benchmark --suite suites/smoke.json --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine

from algorithms.candidates import CancelToken
from diagnostics.cases import CaseConfig, CaseError, load_case
from diagnostics.recording import RecordingError
from diagnostics.runner import RunOptions, RunOutcome, new_run_id, run_candidates, run_routes, run_search
from diagnostics.serialize import outcome_data, recommendation_list
from diagnostics.sources import ReplayMiss
from diagnostics.store import RunStore
from diagnostics.validate import validate_result
from cli.envelope import EXIT, TOOL, TOOL_VERSION, build_envelope, emit

__all__ = ["main", "build_parser"]

_COMMANDS = {
    "health", "search", "candidates", "routes", "diagnose",
    "validate", "benchmark", "history", "report",
}


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _bounded_int(minimum: int, maximum: int | None = None, *, label: str = "数值"):
    """argparse 类型：拒绝越界数字，而不是把它当成"未设置"静默忽略。"""

    def _parse(text: str) -> int:
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"{label}必须是整数：{text!r}") from None
        if value < minimum or (maximum is not None and value > maximum):
            span = f"{minimum}~{maximum}" if maximum is not None else f">= {minimum}"
            raise argparse.ArgumentTypeError(f"{label}超出范围（{span}）：{value}")
        return value

    return _parse


def _bounded_float(minimum: float, *, label: str = "数值"):
    def _parse(text: str) -> float:
        try:
            value = float(text)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"{label}必须是数字：{text!r}") from None
        if value <= minimum:
            raise argparse.ArgumentTypeError(f"{label}必须大于 {minimum}：{value}")
        return value

    return _parse


_ACTIVE_ARGV: list[str] = []


class _JsonArgumentParser(argparse.ArgumentParser):
    """用法错误时也要输出 JSON 信封（--json 模式下 stdout 永远只有 JSON）。"""

    def error(self, message: str) -> None:  # noqa: D401 - argparse 约定
        argv = _ACTIVE_ARGV or list(sys.argv[1:])
        if "--json" in argv:
            command = next((item for item in argv if item in _COMMANDS), "cli")
            payload = build_envelope(
                command,
                new_run_id("err"),
                "failed",
                {},
                errors=[{"code": "USAGE_ERROR", "message": message}],
            )
            emit(payload, as_json=True, out=None)
        super().error(message)


def _add_common(parser: argparse.ArgumentParser, *, need_config: bool = True) -> None:
    if need_config:
        parser.add_argument("--config", required=True, help="测试用例 JSON 路径")
    parser.add_argument("--json", action="store_true", help="标准输出只输出 JSON")
    parser.add_argument("--out", help="把 JSON 结果同时写入指定文件")
    parser.add_argument("--live", action="store_true", help="允许调用真实高德/DeepSeek API")
    parser.add_argument("--mock", action="store_true", help="使用内置合成数据（默认）")
    parser.add_argument("--replay", help="使用录制数据回放（文件或目录）")
    parser.add_argument("--offline", action="store_true", help="严格禁止任何外部网络请求")
    parser.add_argument("--record", help="把本次真实 API 交互录制到指定目录")
    parser.add_argument("--ai", choices=["recorded", "live", "mock"], help="AI 数据来源")
    parser.add_argument("--top-n", type=_bounded_int(1, 10, label="--top-n"), help="覆盖用例中的推荐数量（1~10）")
    parser.add_argument("--max-api-calls", type=_bounded_int(1, label="--max-api-calls"),
                        help="本次运行的最大 API 调用次数")
    parser.add_argument("--max-tokens", type=_bounded_int(0, label="--max-tokens"),
                        help="本次运行的最大 Token 消耗")
    parser.add_argument("--timeout", type=_bounded_float(0.0, label="--timeout"), help="整体超时（秒）")
    parser.add_argument("--runs-dir", help="运行记录目录（默认 ./data/runs）")
    parser.add_argument("--quiet", action="store_true", help="不输出进度信息")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志到 stderr")


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="meetpoint-debug",
        description="MeetPoint AI 调试与诊断命令行（与 GUI 共用推荐引擎）",
    )
    parser.add_argument("--version", action="version", version=f"{TOOL} {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_JsonArgumentParser)

    p_health = sub.add_parser("health", help="检测 API 与服务状态")
    p_health.add_argument("--json", action="store_true")
    p_health.add_argument("--live", action="store_true", help="执行真实网络探测")
    p_health.add_argument("--out")
    p_health.add_argument("--runs-dir")
    p_health.add_argument("--quiet", action="store_true")
    p_health.add_argument("--verbose", action="store_true")

    p_search = sub.add_parser("search", help="执行完整推荐流程")
    _add_common(p_search)

    p_cand = sub.add_parser("candidates", help="只做候选搜索与本地过滤")
    _add_common(p_cand)

    p_routes = sub.add_parser("routes", help="候选搜索 + 双方路线规划")
    _add_common(p_routes)

    p_diag = sub.add_parser("diagnose", help="完整诊断（候选池/路线/AI/缓存/耗时）")
    _add_common(p_diag)

    p_validate = sub.add_parser("validate", help="校验推荐结果是否满足硬约束")
    _add_common(p_validate)
    p_validate.add_argument("--result", required=True, help="待校验的推荐结果 JSON")

    p_bench = sub.add_parser("benchmark", help="批量执行测试套件")
    p_bench.add_argument("--suite", required=True, help="测试套件 JSON")
    p_bench.add_argument("--max-cases", type=_bounded_int(1, label="--max-cases"), help="最多执行多少个用例")
    p_bench.add_argument("--json", action="store_true")
    p_bench.add_argument("--out")
    p_bench.add_argument("--live", action="store_true")
    p_bench.add_argument("--mock", action="store_true")
    p_bench.add_argument("--replay")
    p_bench.add_argument("--offline", action="store_true")
    p_bench.add_argument("--ai", choices=["recorded", "live", "mock"])
    p_bench.add_argument("--max-api-calls", type=_bounded_int(1, label="--max-api-calls"))
    p_bench.add_argument("--max-tokens", type=_bounded_int(0, label="--max-tokens"))
    p_bench.add_argument("--timeout", type=_bounded_float(0.0, label="--timeout"))
    p_bench.add_argument("--runs-dir")
    p_bench.add_argument("--quiet", action="store_true")
    p_bench.add_argument("--verbose", action="store_true")

    p_hist = sub.add_parser("history", help="查看历史运行记录")
    p_hist.add_argument("--limit", type=_bounded_int(1, label="--limit"), default=20)
    p_hist.add_argument("--json", action="store_true")
    p_hist.add_argument("--runs-dir")
    p_hist.add_argument("--out")

    p_report = sub.add_parser("report", help="导出指定 Run 的完整报告")
    p_report.add_argument("--run-id", required=True)
    p_report.add_argument("--json", action="store_true")
    p_report.add_argument("--runs-dir")
    p_report.add_argument("--out")

    return parser


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _log(message: str, *, quiet: bool = False) -> None:
    if not quiet:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()


def _options(args: argparse.Namespace) -> RunOptions:
    return RunOptions(
        live=bool(getattr(args, "live", False)),
        mock=bool(getattr(args, "mock", False)),
        replay=getattr(args, "replay", None),
        offline=bool(getattr(args, "offline", False)),
        record=getattr(args, "record", None),
        ai_mode=getattr(args, "ai", None),
        max_api_calls=getattr(args, "max_api_calls", None),
        max_tokens=getattr(args, "max_tokens", None),
        timeout_s=getattr(args, "timeout", None),
        top_n=getattr(args, "top_n", None),
    )


def _store(args: argparse.Namespace) -> RunStore:
    return RunStore(getattr(args, "runs_dir", None))


def _run_async(factory: Callable[[], Coroutine[Any, Any, Any]], cancel: CancelToken) -> Any:
    def handler(signum, frame):  # noqa: ANN001, ARG001
        cancel.cancel("收到 Ctrl+C")
        sys.stderr.write("\n已请求取消，正在结束…\n")

    previous = None
    try:
        previous = signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        pass
    try:
        return asyncio.run(factory())
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except (ValueError, OSError):
                pass


def _exit_for(status: str) -> int:
    return EXIT.get(status, EXIT["failed"])


def _merge_errors(primary: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一条错误只保留一份（runner 会同时写 trace.errors 与 outcome.errors）。"""
    seen = {(item.get("code"), item.get("message")) for item in primary}
    merged = list(primary)
    for item in extra:
        key = (item.get("code"), item.get("message"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _fail(command: str, exc: Exception, *, as_json: bool, out: str | None, code: str = "USAGE_ERROR") -> int:
    payload = build_envelope(
        command,
        new_run_id("err"),
        "failed",
        {},
        errors=[{"code": code, "message": str(exc)}],
    )
    emit(payload, as_json=as_json, out=out, human=[f"[失败] {exc}"])
    usage_codes = {
        "USAGE_ERROR", "CASE_INVALID", "CASE_NOT_FOUND", "CASE_INVALID_JSON", "CASE_POINT_MISSING",
        "CASE_POINT_NOT_FOUND", "OFFLINE_CONFLICT", "USAGE_CONFLICT", "REPLAY_REQUIRED",
        "AI_MODE_CONFLICT", "AMAP_KEY_MISSING", "DEEPSEEK_KEY_MISSING", "CASE_CITY_NOT_FOUND", "SUITE_INVALID",
        "SUITE_NOT_FOUND", "RESULT_NOT_FOUND", "RESULT_INVALID", "RUN_INVALID_ID", "INVALID_REGION",
        "RECORDING_INVALID", "RECORDING_VERSION_MISMATCH", "RECORDING_SCHEMA_MISMATCH", "RECORDING_NOT_FOUND",
    }
    return EXIT["usage"] if code in usage_codes else EXIT["failed"]


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #
def _render_outcome(command: str, outcome: RunOutcome) -> list[str]:
    lines: list[str] = []
    snap = outcome.trace.snapshot()
    counts = snap["counts"]
    lines.append(f"命令: {command}  运行: {outcome.run_id}  模式: {outcome.data_mode}  状态: {outcome.status}")
    if hasattr(outcome.result, "recommendations"):
        lines.append(
            f"候选: 原始 {counts['raw']} → 去重 {counts['deduped']} → 区域内 {counts['in_region']}"
            f" → 可达 {counts['routed']} → 送 AI {counts['ai_input']} → 最终 {counts['final']}"
        )
        lines.append(f"来源: {outcome.result.source}  耗时: {outcome.elapsed_ms} ms")
        for rec in outcome.result.recommendations:
            leg_a = rec.leg_a
            leg_b = rec.leg_b
            a_text = f"{leg_a.duration_min:.0f}分/{leg_a.distance_km:.1f}km" if leg_a and leg_a.ok else "不可达"
            b_text = f"{leg_b.duration_min:.0f}分/{leg_b.distance_km:.1f}km" if leg_b and leg_b.ok else "不可达"
            lines.append(f"  #{rec.rank} {rec.poi.name}  A:{a_text}  B:{b_text}  —— {rec.reason[:60]}")
        if outcome.result.notice:
            lines.append(f"提示: {outcome.result.notice}")
    elif isinstance(outcome.result, dict):
        for key in ("search", "summary"):
            if key in outcome.result:
                lines.append(f"{key}: {json.dumps(outcome.result[key], ensure_ascii=False)}")
    notice = getattr(outcome.result, "notice", "") if outcome.result is not None else ""
    for warning in snap["warnings"]:
        if notice and warning["message"] == notice:
            continue  # 已在上方以"提示"输出，避免同一句话重复两遍
        repeat = int(warning.get("repeat", 1) or 1)
        suffix = f"（重复 {repeat} 次）" if repeat > 1 else ""
        lines.append(f"警告[{warning['code']}]: {warning['message']}{suffix}")
    for error in snap["errors"]:
        lines.append(f"错误[{error['code']}]: {error['message']}")
    return lines


def _emit_cancelled(command: str, *, as_json: bool, out: str | None, message: str) -> int:
    payload = build_envelope(
        command, new_run_id("cancel"), "cancelled", {},
        errors=[{"code": "CANCELLED", "message": message}],
    )
    emit(payload, as_json=as_json, out=out, human=["[已取消] 用户中断"])
    return EXIT["cancelled"]


def _run_command(command: str, args: argparse.Namespace, runner) -> int:  # noqa: ANN001
    case = load_case(args.config)
    options = _options(args)
    cancel = CancelToken()
    store = _store(args)

    if command != "validate":
        _log(f"[{command}] 开始：{case.name}（{options.data_mode(case)}）", quiet=args.quiet)
    try:
        outcome: RunOutcome = _run_async(lambda: runner(case, options, cancel), cancel)
    except asyncio.CancelledError:
        payload = build_envelope(
            command, new_run_id("cancel"), "cancelled", {"case_name": case.name},
            errors=[{"code": "CANCELLED", "message": "任务被 Ctrl+C 取消"}],
        )
        emit(payload, as_json=args.json, out=args.out, human=["[已取消] 用户中断"])
        return EXIT["cancelled"]
    except KeyboardInterrupt:
        return EXIT["cancelled"]

    data = outcome_data(outcome, include_candidates=(command == "diagnose"))
    if command == "diagnose":
        data.update(outcome.diagnostics())
    payload = build_envelope(
        command,
        outcome.run_id,
        outcome.status,
        data,
        warnings=outcome.trace.warnings + outcome.warnings,
        errors=_merge_errors(outcome.trace.errors, outcome.errors),
        started_at=outcome.started_at,
        elapsed_ms=outcome.elapsed_ms,
        stats=outcome.stats,
    )
    try:
        store.save(payload)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[warn] 运行记录保存失败：{exc}\n")
    emit(payload, as_json=args.json, out=args.out, human=_render_outcome(command, outcome))
    _log(f"[{command}] 结束：{outcome.status}（{outcome.elapsed_ms} ms）", quiet=args.quiet)
    return _exit_for(outcome.status)


async def _health_live(settings) -> dict[str, Any]:  # noqa: ANN001
    from services.bootstrap import build_services

    _transport, amap, deepseek, _monitor = build_services(settings, None)  # type: ignore[arg-type]

    async def probe(name: str, coro) -> tuple[str, dict[str, Any]]:  # noqa: ANN001
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(coro, timeout=20)
        except Exception as exc:  # noqa: BLE001
            return name, {
                "state": "service_error",
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "detail": f"{type(exc).__name__}: {exc}"[:200],
                "checked_at": _now(),
            }
        payload = asdict(result) if is_dataclass(result) else dict(result or {})
        state = payload.get("state")
        if hasattr(state, "value"):
            payload["state"] = state.value
        payload.setdefault("checked_at", _now())
        return name, payload

    pairs = await asyncio.gather(
        probe("amap_web", amap.health_probe()),
        probe("deepseek", deepseek.health_probe()),
    )
    return dict(pairs)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _cmd_health(args: argparse.Namespace) -> int:
    from config.settings import get_settings

    settings = get_settings()
    data: dict[str, Any] = {
        "live": bool(args.live),
        "model": settings.deepseek_model,
        "config": {
            "amap_web_key": settings.amap_web_configured,
            "amap_js_key": settings.amap_js_configured,
            "amap_js_security_code": bool(settings.amap_js_security_code),
            "deepseek_key": settings.deepseek_configured,
        },
        "services": {
            "amap_web": {"state": "not_tested", "detail": "未执行网络探测（加 --live 以检测）"},
            "amap_js": {
                "state": "not_tested",
                "detail": "无界面环境无法验证 JS API，请使用 GUI 或 scripts/js_probe.py",
            },
            "deepseek": {"state": "not_tested", "detail": "未执行网络探测（加 --live 以检测）"},
        },
        "notes": [
            "not_tested 表示未验证，不代表正常",
            "JS API 状态只能在地图实际加载后确认",
        ],
    }
    status = "ok"
    if args.live:
        if not settings.amap_web_configured and not settings.deepseek_configured:
            data["notes"].append("未配置任何 Key，--live 无法执行")
            status = "failed"
        else:
            try:
                services = _run_async(lambda: _health_live(settings), CancelToken())
                data["services"].update(services)
                failed = [n for n, s in services.items() if s.get("state") in {"auth_error", "quota_limited", "service_error", "connect_error", "timeout"}]
                if failed:
                    status = "failed"
            except Exception as exc:  # noqa: BLE001
                data["notes"].append(f"--live 检测失败：{type(exc).__name__}: {exc}")
                status = "failed"

    payload = build_envelope(
        "health", new_run_id("health"), status, data, elapsed_ms=0,
        stats={"api_calls": 2 if args.live else 0},
    )
    try:
        _store(args).save(payload)
    except Exception:  # noqa: BLE001
        pass
    human = [f"配置：高德Web={'✓' if settings.amap_web_configured else '×'} "
             f"高德JS={'✓' if settings.amap_js_configured else '×'} "
             f"DeepSeek={'✓' if settings.deepseek_configured else '×'} 模型={settings.deepseek_model}"]
    for name, snap in data["services"].items():
        human.append(f"  {name:10s} {snap.get('state')}  {snap.get('latency_ms', '-')}ms  {snap.get('detail', '')}")
    emit(payload, as_json=args.json, out=args.out, human=human)
    return _exit_for(status)


def _load_result_file(path: str) -> Any:
    file = Path(path)
    if not file.is_file():
        raise CaseError("RESULT_NOT_FOUND", f"找不到结果文件：{file}")
    try:
        payload = json.loads(file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise CaseError("RESULT_INVALID", f"结果文件不是合法 JSON：{exc}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]
        if isinstance(data.get("recommendations"), list):
            return data
        if isinstance(data.get("items"), list):
            return data
    return payload


def _cmd_validate(args: argparse.Namespace) -> int:
    case = load_case(args.config)
    result = _load_result_file(args.result)
    report = validate_result(case, result)
    status = "ok" if report["ok"] else "failed"
    payload = build_envelope(
        "validate", new_run_id("validate"), status, report,
        errors=[{"code": v["code"], "message": v["message"]} for v in report["violations"]],
        elapsed_ms=0,
    )
    try:
        _store(args).save(payload)
    except Exception:  # noqa: BLE001
        pass
    human = [f"校验：{report['checked']} 条，违规 {len(report['violations'])} 条"]
    for v in report["violations"]:
        human.append(f"  [{v['code']}] {v['message']} {v.get('poi_id') or ''} {v.get('location') or ''}")
    emit(payload, as_json=args.json, out=args.out, human=human)
    return _exit_for(status)


def _cmd_history(args: argparse.Namespace) -> int:
    entries = _store(args).history(args.limit)
    payload = build_envelope("history", new_run_id("history"), "ok", {"runs": entries, "count": len(entries)})
    human = [f"共 {len(entries)} 条运行记录"]
    for item in entries:
        human.append(
            f"  {item.get('run_id')}  {item.get('command')}  {item.get('status')}  "
            f"{item.get('elapsed_ms')}ms  {item.get('started_at')}"
        )
    emit(payload, as_json=args.json, out=args.out, human=human)
    return EXIT["ok"]


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        record = _store(args).load(args.run_id)
    except FileNotFoundError:
        payload = build_envelope("report", new_run_id("report"), "failed", {"run_id": args.run_id},
                                 errors=[{"code": "RUN_NOT_FOUND", "message": f"找不到运行记录 {args.run_id}"}])
        emit(payload, as_json=args.json, out=args.out, human=["找不到该运行记录"])
        return EXIT["failed"]
    except ValueError as exc:  # RunStore 拒绝非法 run_id（格式/字符集不合法）
        return _fail("report", exc, as_json=args.json, out=args.out, code="RUN_INVALID_ID")
    payload = record if isinstance(record, dict) else {}
    payload.setdefault("schema_version", "1.0")
    human = [f"运行 {args.run_id}: {payload.get('command')} / {payload.get('status')} / {payload.get('elapsed_ms')}ms"]
    emit(payload, as_json=args.json, out=args.out, human=human)
    return _exit_for(str(payload.get("status", "ok")))


def _cmd_benchmark(args: argparse.Namespace) -> int:
    from diagnostics.benchmark import load_suite, run_suite

    suite = load_suite(args.suite)
    options = RunOptions(
        live=args.live, mock=args.mock, replay=args.replay, offline=args.offline,
        ai_mode=args.ai, max_api_calls=args.max_api_calls, max_tokens=args.max_tokens,
        timeout_s=args.timeout,
    )
    cancel = CancelToken()
    started = time.perf_counter()
    try:
        result = _run_async(
            lambda: run_suite(suite, options, base_dir=Path(args.suite).resolve().parent,
                              max_cases=args.max_cases, cancel=cancel),
            cancel,
        )
    except (CaseError, FileNotFoundError) as exc:
        return _fail("benchmark", exc, as_json=args.json, out=args.out)
    except asyncio.CancelledError:
        return _emit_cancelled("benchmark", as_json=args.json, out=args.out,
                               message="批量基准被 Ctrl+C 取消")
    except KeyboardInterrupt:
        return _emit_cancelled("benchmark", as_json=args.json, out=args.out,
                               message="批量基准被 Ctrl+C 取消")

    summary = result.get("summary", {})
    status = "ok" if summary.get("failed", 0) == 0 else "failed"
    payload = build_envelope(
        "benchmark",
        new_run_id("bench"),
        status,
        result,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        stats={"api_calls": summary.get("api_calls", 0)},
        warnings=[
            {"code": "VALIDATION_VIOLATIONS", "message": f"{summary.get('validation_violations', 0)} 个用例存在违规"}
        ]
        if summary.get("validation_violations")
        else [],
    )
    try:
        _store(args).save(payload)
    except Exception:  # noqa: BLE001
        pass
    human = [f"套件 {result['suite']}：{result['cases_total']} 个用例"]
    for case in result.get("cases", []):
        human.append(
            f"  [{case.get('status')}] {case.get('name')}  "
            f"recs={len(case.get('recommendations', []))}  api={case.get('api_calls')}  {case.get('elapsed_ms')}ms"
        )
    emit(payload, as_json=args.json, out=args.out, human=human)
    return _exit_for(status)


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    _ACTIVE_ARGV[:] = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    command = args.command
    try:
        if command == "health":
            return _cmd_health(args)
        if command == "history":
            return _cmd_history(args)
        if command == "report":
            return _cmd_report(args)
        if command == "benchmark":
            return _cmd_benchmark(args)
        if command == "validate":
            return _cmd_validate(args)
        if command == "search":
            return _run_command("search", args, lambda c, o, k: run_search(c, o, k))
        if command == "candidates":
            return _run_command("candidates", args, lambda c, o, k: run_candidates(c, o, k))
        if command == "routes":
            return _run_command("routes", args, lambda c, o, k: run_routes(c, o, k))
        if command == "diagnose":
            return _run_command("diagnose", args, lambda c, o, k: run_search(c, o, k))
        parser.error(f"未知命令：{command}")
    except CaseError as exc:
        return _fail(command, exc, as_json=bool(getattr(args, "json", False)),
                     out=getattr(args, "out", None), code=exc.code)
    except RecordingError as exc:
        # 回放/录制类错误（版本不匹配、缺请求、找不到录制文件）必须给出 JSON 信封，
        # 不能把 traceback 甩到 stderr 上而 stdout 空着。
        return _fail(command, exc, as_json=bool(getattr(args, "json", False)),
                     out=getattr(args, "out", None), code=exc.code)
    except ReplayMiss as exc:
        return _fail(command, exc, as_json=bool(getattr(args, "json", False)),
                     out=getattr(args, "out", None), code=exc.code)
    except FileNotFoundError as exc:
        return _fail(command, exc, as_json=bool(getattr(args, "json", False)),
                     out=getattr(args, "out", None), code="FILE_NOT_FOUND")
    except KeyboardInterrupt:
        sys.stderr.write("已取消\n")
        return EXIT["cancelled"]
    return EXIT["ok"]
