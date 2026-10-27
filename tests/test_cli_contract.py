"""CLI 契约测试：JSON 纯净性、退出码、错误区分、历史与报告。"""

from __future__ import annotations

import json
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "tests" / "data" / "small_case.json"


def run_cli(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(ROOT / "meetpoint_debug.py"), *args]
    return subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )


def _json(result: subprocess.CompletedProcess) -> dict:
    return json.loads(result.stdout)


def test_search_json_contract(tmp_path):
    result = run_cli("search", "--config", str(CASE), "--json", "--runs-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr[-1000:]
    payload = _json(result)
    for key in ("schema_version", "run_id", "status", "command", "started_at", "elapsed_ms", "data", "warnings", "errors"):
        assert key in payload
    assert payload["status"] == "ok"
    assert payload["command"] == "search"
    assert payload["data"]["recommendations"]
    assert isinstance(payload["stats"].get("api_calls"), int)
    assert result.stderr.strip(), "进度/日志应写入 stderr"
    assert "sk-" not in result.stdout
    assert "44d3f1a4" not in result.stdout


def test_out_file_written(tmp_path):
    out = tmp_path / "result.json"
    result = run_cli("search", "--config", str(CASE), "--json", "--out", str(out), "--runs-dir", str(tmp_path))
    assert result.returncode == 0
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8"))["run_id"] == _json(result)["run_id"]


def test_candidates_reports_pool_counts(tmp_path):
    result = run_cli("candidates", "--config", str(CASE), "--json", "--runs-dir", str(tmp_path))
    assert result.returncode == 0
    data = _json(result)["data"]
    assert data["search"]["raw_pois"] >= data["search"]["in_region"] >= 0
    assert "returned" in data["search"]


def test_health_offline_reports_not_tested(tmp_path):
    result = run_cli("health", "--json", "--runs-dir", str(tmp_path))
    assert result.returncode == 0
    data = _json(result)["data"]
    assert data["services"]["amap_js"]["state"] == "not_tested"
    assert data["services"]["amap_web"]["state"] == "not_tested"
    assert "config" in data


def test_offline_live_conflict_is_usage_error(tmp_path):
    result = run_cli("search", "--config", str(CASE), "--live", "--offline", "--json", "--runs-dir", str(tmp_path))
    assert result.returncode == 2
    payload = _json(result)
    assert payload["status"] == "failed"
    assert payload["errors"][0]["code"] == "OFFLINE_CONFLICT"


def test_missing_config_is_usage_error(tmp_path):
    result = run_cli("search", "--config", str(tmp_path / "nope.json"), "--json", "--runs-dir", str(tmp_path))
    assert result.returncode == 2
    assert _json(result)["errors"][0]["code"] == "CASE_NOT_FOUND"


def test_validate_detects_region_violation(tmp_path):
    bad = tmp_path / "bad_result.json"
    bad.write_text(
        json.dumps(
            {
                "recommendations": [
                    {"rank": 1, "poi_id": "OUT", "name": "区域外", "lng": 118.0, "lat": 40.0}
                ]
            }
        ),
        encoding="utf-8",
    )
    result = run_cli(
        "validate", "--config", str(CASE), "--result", str(bad), "--json", "--runs-dir", str(tmp_path)
    )
    assert result.returncode == 1
    violations = _json(result)["data"]["violations"]
    assert any(v["code"] == "OUT_OF_REGION" for v in violations)


def test_history_and_report_roundtrip(tmp_path):
    first = run_cli("search", "--config", str(CASE), "--json", "--runs-dir", str(tmp_path))
    run_id = _json(first)["run_id"]

    history = run_cli("history", "--json", "--runs-dir", str(tmp_path))
    assert history.returncode == 0
    runs = _json(history)["data"]["runs"]
    assert any(item["run_id"] == run_id for item in runs)

    report = run_cli("report", "--run-id", run_id, "--json", "--runs-dir", str(tmp_path))
    assert report.returncode == 0
    assert _json(report)["run_id"] == run_id


def test_benchmark_suite(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps(
            {
                "name": "cli-suite",
                "budget": {"max_cases": 2, "max_api_calls": 500},
                "cases": [
                    {"name": "small-1", "config": str(CASE)},
                    {"name": "small-2", "config": str(CASE)},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = run_cli("benchmark", "--suite", str(suite), "--json", "--runs-dir", str(tmp_path))
    assert result.returncode == 0, result.stderr[-800:]
    data = _json(result)["data"]
    assert data["cases_total"] == 2
    assert data["summary"]["ok"] == 2


def test_no_result_exit_code(tmp_path):
    case = tmp_path / "empty.json"
    case.write_text(
        json.dumps(
            {
                "name": "empty-region",
                "a": {"name": "A", "lng": 117.20, "lat": 39.13},
                "b": {"name": "B", "lng": 117.20, "lat": 39.13},
                "region": {"mode": "circle", "center": [117.2115, 39.136], "radius_m": 500},
            }
        ),
        encoding="utf-8",
    )
    result = run_cli("search", "--config", str(case), "--json", "--runs-dir", str(tmp_path))
    # A/B 相同点属于用例错误 → 失败；这里仅确认不是崩溃，且返回结构化 JSON
    payload = _json(result)
    assert payload["status"] in {"failed", "no_result"}
    assert result.returncode in {1, 2, 3}


def test_live_transit_resolves_missing_cities_once(monkeypatch):
    from diagnostics import runner
    from diagnostics.cases import load_case
    from diagnostics.sources import Budget

    class AMap:
        def __init__(self):
            self.calls = []

        async def regeocode(self, lng, lat):
            self.calls.append((lng, lat))
            return {"citycode": "022", "adcode": "120105"}

    amap = AMap()
    monkeypatch.setattr(
        runner, "_make_sources",
        lambda case, options: (amap, object(), Budget(), None, "live"),
    )
    prepared = asyncio.run(runner._prepare(load_case(CASE), runner.RunOptions(live=True), "test"))
    assert len(amap.calls) == 2
    assert prepared.request.city_a == "022"
    assert prepared.request.city_b == "022"
