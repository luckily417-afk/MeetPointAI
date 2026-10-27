"""命令行层：稳定 JSON 信封、退出码与人类可读输出。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["SCHEMA_VERSION", "TOOL", "TOOL_VERSION", "EXIT", "build_envelope", "emit"]

SCHEMA_VERSION = "1.0"
TOOL = "meetpoint-debug"
TOOL_VERSION = "1.0.0"

EXIT = {
    "ok": 0,
    "failed": 1,
    "usage": 2,
    "no_result": 3,
    "cancelled": 130,
}


def build_envelope(
    command: str,
    run_id: str,
    status: str,
    data: Any,
    *,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    started_at: str | None = None,
    elapsed_ms: int = 0,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "run_id": run_id,
        "status": status,
        "command": command,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": int(elapsed_ms),
        "data": data,
        "warnings": warnings or [],
        "errors": errors or [],
        "stats": stats or {},
    }


def emit(payload: dict[str, Any], *, as_json: bool, out: str | None = None, human: list[str] | None = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if as_json:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    else:
        for line in human or []:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
    if out:
        # 写 --out 失败不应把一次成功运行变成 traceback：stdout 已经给出了结果，
        # 这里只把原因写到 stderr，退出码保持不变。
        try:
            target = Path(out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"[out] 写入 {out} 失败：{exc}\n")
            sys.stderr.flush()
