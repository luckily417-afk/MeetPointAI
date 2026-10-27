"""运行记录存储：每次执行产生唯一 Run ID，可在 history / report 中回看。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["RunStore", "default_runs_dir"]

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{4,80}$")


def default_runs_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "runs"


class RunStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else default_runs_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.jsonl"

    # ------------------------------------------------------------------ #
    def save(self, payload: dict[str, Any]) -> Path:
        run_id = str(payload.get("run_id") or "")
        if not _SAFE_ID.match(run_id):
            raise ValueError(f"非法 run_id：{run_id!r}")
        path = self.root / f"{run_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "run_id": run_id,
            "command": payload.get("command"),
            "status": payload.get("status"),
            "started_at": payload.get("started_at"),
            "elapsed_ms": payload.get("elapsed_ms"),
            "data_mode": (payload.get("data") or {}).get("data_mode")
            if isinstance(payload.get("data"), dict)
            else None,
            "case": (payload.get("data") or {}).get("case_name")
            if isinstance(payload.get("data"), dict)
            else None,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
        return path

    def load(self, run_id: str) -> dict[str, Any]:
        if not _SAFE_ID.match(run_id):
            raise ValueError(f"非法 run_id：{run_id!r}")
        path = self.root / f"{run_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"找不到运行记录：{run_id}")
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.index_path.is_file():
            return []
        lines = self.index_path.read_text(encoding="utf-8-sig").splitlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(entries))
