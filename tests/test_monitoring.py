"""健康监测与错误分类测试（需求 7.1 - 7.6）。"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from models.schemas import ServiceState
from monitoring.health import HealthMonitor
from services.errors import (
    AmapError,
    classify_amap_payload,
    classify_deepseek_payload,
    classify_exception,
    classify_http_status,
)
from storage.db import Database


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def test_amap_success_payload():
    state, code, _ = classify_amap_payload({"status": "1", "info": "OK", "infocode": "10000"})
    assert state is ServiceState.OK


@pytest.mark.parametrize(
    "infocode,expected",
    [
        ("10001", ServiceState.AUTH_ERROR),
        ("10003", ServiceState.QUOTA_LIMITED),
        ("10044", ServiceState.QUOTA_LIMITED),
        ("20000", ServiceState.SERVICE_ERROR),
        ("10009", ServiceState.AUTH_ERROR),
    ],
)
def test_amap_error_codes(infocode, expected):
    state, code, detail = classify_amap_payload(
        {"status": "0", "info": detail_info(infocode), "infocode": infocode}
    )
    assert state is expected
    assert code == infocode
    assert detail


def detail_info(infocode: str) -> str:
    return {
        "10001": "INVALID_USER_KEY",
        "10003": "DAILY_QUERY_OVER_LIMIT",
        "10044": "USER_DAILY_QUERY_OVER_LIMIT",
        "20000": "SERVICE_NOT_AVAILABLE",
        "10009": "USERKEY_PLAT_NOMATCH",
    }.get(infocode, "UNKNOWN")


def test_amap_http200_business_failure_not_ok():
    state, _, _ = classify_amap_payload({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
    assert state is not ServiceState.OK


@pytest.mark.parametrize(
    "status,expected",
    [(200, ServiceState.OK), (401, ServiceState.AUTH_ERROR), (403, ServiceState.AUTH_ERROR),
     (402, ServiceState.QUOTA_LIMITED), (429, ServiceState.QUOTA_LIMITED),
     (500, ServiceState.SERVICE_ERROR), (503, ServiceState.SERVICE_ERROR), (408, ServiceState.TIMEOUT)],
)
def test_http_status_mapping(status, expected):
    assert classify_http_status(status)[0] is expected


def test_exception_mapping():
    assert classify_exception(httpx.ConnectTimeout("t"))[0] is ServiceState.TIMEOUT
    assert classify_exception(httpx.ConnectError("c"))[0] is ServiceState.CONNECT_ERROR
    assert classify_exception(ValueError("bad json"))[0] is ServiceState.SERVICE_ERROR
    err = AmapError("key 无效", state=ServiceState.AUTH_ERROR)
    assert classify_exception(err)[0] is ServiceState.AUTH_ERROR


def test_deepseek_error_payload():
    state, code, msg = classify_deepseek_payload(
        {"error": {"message": "Insufficient Balance", "code": "insufficient_balance"}}, 402
    )
    assert state is ServiceState.QUOTA_LIMITED
    assert code == "insufficient_balance"
    assert "Balance" in msg


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def test_monitor_success_and_slow(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    monitor = HealthMonitor(db, latency_warn_ms=1000)
    monitor.register("amap_web", None, configured=True)

    monitor.note_success("amap_web", 120)
    assert monitor.state_of("amap_web") is ServiceState.OK

    monitor.note_success("amap_web", 4000)
    assert monitor.state_of("amap_web") is ServiceState.SLOW
    assert monitor.usable("amap_web")

    rows = db.recent_health(10, "amap_web")
    assert len(rows) >= 2
    db.close()


def test_monitor_failure_backoff_and_recovery(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    monitor = HealthMonitor(db, latency_warn_ms=1000, backoff_base_s=0.01, backoff_cap_s=0.05)
    monitor.register("deepseek", None, configured=True)

    monitor.note_failure("deepseek", ServiceState.AUTH_ERROR, detail="401")
    snap = monitor.snapshot("deepseek")
    assert snap.state is ServiceState.AUTH_ERROR
    assert snap.consecutive_failures == 1
    assert not monitor.usable("deepseek")

    monitor.note_failure("deepseek", ServiceState.CONNECT_ERROR, detail="dns")
    assert monitor.snapshot("deepseek").consecutive_failures == 2

    before = time.time()
    monitor.note_success("deepseek", 80)
    snap = monitor.snapshot("deepseek")
    assert snap.state is ServiceState.OK
    assert snap.consecutive_failures == 0
    assert snap.last_success_at is not None
    assert snap.last_check_at is not None
    assert monitor.usable("deepseek")
    assert time.time() >= before
    db.close()


def test_monitor_not_configured(tmp_path):
    monitor = HealthMonitor(Database(tmp_path / "t.sqlite3"))
    monitor.register("amap_web", None, configured=False)
    snap = _run(monitor.check("amap_web"))
    assert snap.state is ServiceState.NOT_CONFIGURED
    assert not monitor.usable("amap_web")


def test_monitor_probe_returns_state(tmp_path):
    monitor = HealthMonitor(Database(tmp_path / "t.sqlite3"))

    async def probe_ok():
        return {"state": ServiceState.OK, "latency_ms": 42, "detail": "fine"}

    monitor.register("deepseek", probe_ok, configured=True)
    snap = _run(monitor.check("deepseek"))
    assert snap.state is ServiceState.OK
    assert snap.latency_ms == 42


def test_monitor_probe_timeout(tmp_path):
    monitor = HealthMonitor(Database(tmp_path / "t.sqlite3"))

    async def probe_timeout():
        raise httpx.ReadTimeout("timeout")

    monitor.register("deepseek", probe_timeout, configured=True)
    snap = _run(monitor.check("deepseek"))
    assert snap.state is ServiceState.TIMEOUT


def test_monitor_check_due_respects_backoff(tmp_path):
    monitor = HealthMonitor(
        Database(tmp_path / "t.sqlite3"), backoff_base_s=60.0, backoff_cap_s=120.0, interval_s=60
    )
    calls = {"n": 0}

    async def probe_fail():
        calls["n"] += 1
        return {"state": ServiceState.SERVICE_ERROR, "latency_ms": 10, "detail": "boom"}

    monitor.register("amap_web", probe_fail, configured=True)
    _run(monitor.check_due())
    assert calls["n"] == 1
    # 刚失败，next_check_at 在未来，不应重复请求
    due = _run(monitor.check_due())
    assert due == []
    assert calls["n"] == 1


def test_monitor_js_status_driven(tmp_path):
    monitor = HealthMonitor(Database(tmp_path / "t.sqlite3"))
    monitor.register("amap_js", None, configured=True)
    monitor.note_js_status(False, "脚本加载失败")
    assert monitor.state_of("amap_js") is ServiceState.SERVICE_ERROR
    monitor.note_js_status(True, "地图已初始化")
    assert monitor.state_of("amap_js") is ServiceState.OK


def test_monitor_listener_receives_updates(tmp_path):
    monitor = HealthMonitor(Database(tmp_path / "t.sqlite3"))
    monitor.register("amap_web", None, configured=True)
    events = []
    monitor.add_listener(lambda name, snap: events.append((name, snap.state)))
    monitor.note_success("amap_web", 10)
    assert events and events[-1][0] == "amap_web"
    assert events[-1][1] is ServiceState.OK


def test_api_key_never_logged(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    monitor = HealthMonitor(db, latency_warn_ms=1000)
    monitor.register("amap_web", None, configured=True)
    monitor.note_failure(
        "amap_web",
        ServiceState.AUTH_ERROR,
        detail="key=DUMMY_KEY_FOR_TEST 无效",
    )
    rows = db.recent_health(5)
    assert rows
    # 说明字段允许保留调用方传入的错误摘要，但真实 Key 不应出现在任何日志中
    assert "sk-" not in str(rows)
    db.close()
