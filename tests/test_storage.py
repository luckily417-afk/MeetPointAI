"""本地存储测试：缓存 TTL、会话、健康日志、清理。"""

from __future__ import annotations

import time

from storage.db import Database


def test_cache_roundtrip_and_ttl(tmp_path):
    db = Database(tmp_path / "cache.sqlite3")
    db.put_json("k1", {"v": 1})
    assert db.get_json("k1") == {"v": 1}
    assert db.get_json("k1", ttl_s=60) == {"v": 1}
    assert db.get_json("k1", ttl_s=0) is None
    assert db.get_json("missing") is None
    db.close()


def test_cache_overwrite(tmp_path):
    db = Database(tmp_path / "c.sqlite3")
    db.put_json("k", {"v": 1})
    db.put_json("k", {"v": 2})
    assert db.get_json("k") == {"v": 2}
    assert db.cache_age_s("k") is not None


def test_health_log_and_summary(tmp_path):
    db = Database(tmp_path / "h.sqlite3")
    db.log_health("amap_web", "ok", latency_ms=100)
    db.log_health("amap_web", "auth_error", latency_ms=90, http_status=401, error_type="AmapError")
    rows = db.recent_health(5, "amap_web")
    assert len(rows) == 2
    summary = db.health_summary()
    assert summary["amap_web"]["ok"] == 1
    assert summary["amap_web"]["auth_error"] == 1
    db.close()


def test_sessions(tmp_path):
    db = Database(tmp_path / "s.sqlite3")
    sid = db.save_session(
        title="A ↔ B",
        request={"a": "x", "b": "y"},
        region={"mode": "circle", "center_lng": 1, "center_lat": 2, "radius_m": 100},
        result={"items": [{"name": "地点"}]},
        source="ai",
    )
    assert sid > 0
    loaded = db.load_session(sid)
    assert loaded is not None
    assert loaded["region"]["mode"] == "circle"
    assert loaded["result"]["items"][0]["name"] == "地点"
    assert db.recent_sessions(5)[0]["id"] == sid
    db.close()


def test_settings_and_clear(tmp_path):
    db = Database(tmp_path / "p.sqlite3")
    db.set_setting("theme", "light")
    assert db.get_setting("theme") == "light"
    db.put_json("x", 1)
    db.save_session(title="t", request={}, region=None, result={})
    db.clear_all()
    assert db.get_json("x") is None
    assert db.recent_sessions() == []
    assert db.get_setting("theme") == "light"  # keep_settings=True
    db.close()


def test_purge_expired(tmp_path):
    db = Database(tmp_path / "e.sqlite3")
    db.put_json("old", 1)
    with db._cursor() as cur:  # noqa: SLF001 - 测试直接改时间戳
        cur.execute("UPDATE kv_cache SET created_at=?", (time.time() - 10 * 24 * 3600,))
    removed = db.purge_expired_cache(ttl_s=7 * 24 * 3600)
    assert removed == 1
    assert db.get_json("old") is None
    db.close()
