"""Record / Replay 数据结构与文件格式。

所有录制内容均可安全提交/共享：不包含 API Key、不包含完整请求头。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["SCHEMA_VERSION", "DATA_VERSION", "Recording", "RecordingError", "request_key"]

SCHEMA_VERSION = "1.0"
DATA_VERSION = 1


class RecordingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def request_key(kind: str, **params: Any) -> str:
    """稳定的请求指纹：同一参数永远得到同一 key。"""
    payload = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


@dataclass
class Recording:
    case: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tool: str = "meetpoint-debug"
    schema_version: str = SCHEMA_VERSION
    data_version: int = DATA_VERSION
    amap: dict[str, dict[str, Any]] = field(default_factory=dict)
    deepseek: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    run_id: str = ""

    # ------------------------------------------------------------------ #
    def add_amap(self, kind: str, key: str, payload: dict[str, Any], params: dict[str, Any]) -> None:
        self.amap[key] = {
            "kind": kind,
            "params": params,
            "payload": payload,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

    def add_amap_error(self, kind: str, key: str, params: dict[str, Any], error: BaseException) -> None:
        self.amap[key] = {
            "kind": kind,
            "params": params,
            "error": {"type": type(error).__name__, "message": str(error)[:300]},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

    def add_deepseek(self, messages: list[dict[str, str]], result: dict[str, Any], usage: dict[str, Any]) -> None:
        self.deepseek.append(
            {
                "messages": messages,
                "response": result,
                "usage": usage,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def note(self, message: str) -> None:
        self.notes.append(message[:300])

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "data_version": self.data_version,
            "tool": self.tool,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "case": self.case,
            "amap": self.amap,
            "deepseek": self.deepseek,
            "timings_ms": self.timings_ms,
            "notes": self.notes,
            "errors": self.errors,
        }

    def save(self, directory: str | Path) -> Path:
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"recording-{self.run_id or stamp}.json"
        path = target_dir / name
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Recording":
        file = Path(path)
        if not file.is_file():
            raise RecordingError("RECORDING_NOT_FOUND", f"找不到录制文件：{file}")
        try:
            raw = json.loads(file.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise RecordingError("RECORDING_INVALID", f"录制文件不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise RecordingError("RECORDING_INVALID", "录制文件根节点必须是对象")
        version = int(raw.get("data_version", 0) or 0)
        if version != DATA_VERSION:
            raise RecordingError(
                "RECORDING_VERSION_MISMATCH",
                f"录制数据版本 {version} 与当前程序 {DATA_VERSION} 不兼容，请重新录制",
            )
        if str(raw.get("schema_version", "")) != SCHEMA_VERSION:
            raise RecordingError(
                "RECORDING_SCHEMA_MISMATCH",
                f"录制 schema {raw.get('schema_version')} 与当前 {SCHEMA_VERSION} 不兼容",
            )
        recording = cls(
            case=raw.get("case") or {},
            created_at=str(raw.get("created_at", "")),
            tool=str(raw.get("tool", "meetpoint-debug")),
            schema_version=str(raw.get("schema_version", SCHEMA_VERSION)),
            data_version=version,
            amap=raw.get("amap") or {},
            deepseek=raw.get("deepseek") or [],
            timings_ms=raw.get("timings_ms") or {},
            notes=raw.get("notes") or [],
            errors=raw.get("errors") or [],
            run_id=str(raw.get("run_id", "")),
        )
        if not isinstance(recording.amap, dict) or not isinstance(recording.deepseek, list):
            raise RecordingError("RECORDING_INVALID", "录制文件结构损坏")
        return recording


class Stopwatch:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)
