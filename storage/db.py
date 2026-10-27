"""SQLite 本地存储：API 响应缓存、健康日志、会话历史、应用设置。

安全约定：
* 不写入任何 API Key；
* 默认不永久保存精确位置（会话历史可通过 `clear_all()` 一键清除，且默认保留 7 天）；
* 缓存条目带时间戳，过期即失效，展示时必须标注缓存时间。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

__all__ = ["Database"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS health_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    service       TEXT NOT NULL,
    ts            REAL NOT NULL,
    latency_ms    INTEGER,
    http_status   INTEGER,
    biz_code      TEXT,
    state         TEXT,
    error_type    TEXT,
    detail        TEXT,
    retries       INTEGER DEFAULT 0,
    recovered_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_health_service_ts ON health_log(service, ts DESC);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    title       TEXT,
    request     TEXT,
    region      TEXT,
    result      TEXT,
    source      TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ #
    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except BaseException:
                # 中途失败必须回滚：否则这条未提交事务会被下一次 commit 顺手落盘
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                cur.close()

    # ------------------------------------------------------------------ #
    # 通用 JSON 缓存
    # ------------------------------------------------------------------ #
    def put_json(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO kv_cache(key, payload, created_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, created_at=excluded.created_at",
                (key, payload, time.time()),
            )

    def get_json(self, key: str, ttl_s: float | None = None) -> Any | None:
        with self._lock:
            cur = self._conn.execute("SELECT payload, created_at FROM kv_cache WHERE key=?", (key,))
            row = cur.fetchone()
        if row is None:
            return None
        created_at = float(row["created_at"])
        if ttl_s is not None and (ttl_s <= 0 or time.time() - created_at > ttl_s):
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def cache_age_s(self, key: str) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT created_at FROM kv_cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return max(0.0, time.time() - float(row["created_at"]))

    # ------------------------------------------------------------------ #
    # 健康日志
    # ------------------------------------------------------------------ #
    def log_health(
        self,
        service: str,
        state: str,
        *,
        latency_ms: int | None = None,
        http_status: int | None = None,
        biz_code: str | None = None,
        error_type: str = "",
        detail: str = "",
        retries: int = 0,
        recovered_at: float | None = None,
        ts: float | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO health_log(service, ts, latency_ms, http_status, biz_code, state, "
                "error_type, detail, retries, recovered_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    service,
                    ts if ts is not None else time.time(),
                    latency_ms,
                    http_status,
                    biz_code,
                    state,
                    error_type,
                    detail[:500],
                    retries,
                    recovered_at,
                ),
            )

    def recent_health(self, limit: int = 50, service: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM health_log"
        args: list[Any] = []
        if service:
            sql += " WHERE service=?"
            args.append(service)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def health_summary(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT service, state, MAX(ts) AS ts, COUNT(*) AS n FROM health_log GROUP BY service, state"
            ).fetchall()
        summary: dict[str, dict[str, Any]] = {}
        for row in rows:
            summary.setdefault(row["service"], {})[row["state"]] = row["n"]
        return summary

    # ------------------------------------------------------------------ #
    # 会话历史
    # ------------------------------------------------------------------ #
    def save_session(
        self,
        *,
        title: str,
        request: dict[str, Any],
        region: dict[str, Any] | None,
        result: dict[str, Any],
        source: str = "ai",
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO sessions(created_at, title, request, region, result, source) "
                "VALUES(?,?,?,?,?,?)",
                (
                    time.time(),
                    title[:200],
                    json.dumps(request, ensure_ascii=False, default=str),
                    json.dumps(region, ensure_ascii=False, default=str) if region else None,
                    json.dumps(result, ensure_ascii=False, default=str),
                    source,
                ),
            )
            return int(cur.lastrowid or 0)

    def recent_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, title, source FROM sessions ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_session(self, session_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        for field in ("request", "region", "result"):
            if data.get(field):
                try:
                    data[field] = json.loads(data[field])
                except json.JSONDecodeError:
                    data[field] = None
        return data

    # ------------------------------------------------------------------ #
    # 设置
    # ------------------------------------------------------------------ #
    def set_setting(self, key: str, value: Any) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO app_settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False, default=str)),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #
    def clear_all(self, *, keep_settings: bool = True) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM kv_cache")
            cur.execute("DELETE FROM sessions")
            cur.execute("DELETE FROM health_log")
            if not keep_settings:
                cur.execute("DELETE FROM app_settings")

    def purge_expired_cache(self, ttl_s: float = 7 * 24 * 3600) -> int:
        cutoff = time.time() - ttl_s
        with self._cursor() as cur:
            cur.execute("DELETE FROM kv_cache WHERE created_at < ?", (cutoff,))
            return cur.rowcount or 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            cache_n = self._conn.execute("SELECT COUNT(*) FROM kv_cache").fetchone()[0]
            session_n = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            health_n = self._conn.execute("SELECT COUNT(*) FROM health_log").fetchone()[0]
        return {
            "cache_entries": cache_n,
            "sessions": session_n,
            "health_entries": health_n,
            "path": str(self._path),
        }
