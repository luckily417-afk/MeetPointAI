"""运行期配置：从环境变量 / .env 读取，绝不硬编码任何凭据。

应用根目录解析同时兼容源码运行与 PyInstaller 单文件/单目录打包。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_TRUE = {"1", "true", "yes", "on"}


def app_root() -> Path:
    """返回程序所在根目录（源码目录或 EXE 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """返回只读资源根目录（打包后为 _MEIPASS）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_root()


def default_data_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "MeetPointAI"


def _load_env_files() -> None:
    candidates = [
        default_data_dir() / ".env",   # 用户级配置（安装目录只读时的首选）
        app_root() / ".env",
        app_root() / "config" / ".env",
        resource_root() / ".env",
    ]
    for path in candidates:
        try:
            if path.is_file():
                load_dotenv(path, override=False)
        except OSError:
            continue


_load_env_files()


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw.strip()))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


@dataclass(frozen=True)
class Settings:
    """不可变运行配置。`with_overrides()` 可派生修改副本。"""

    # --- 凭据 ---
    amap_js_key: str = ""
    amap_js_security_code: str = ""
    amap_web_key: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"

    # --- 网络 ---
    http_timeout: float = 12.0
    http_connect_timeout: float = 5.0
    llm_timeout: float = 120.0
    llm_effort: str = "low"
    max_retries: int = 2
    max_concurrency: int = 3

    # --- 监控 ---
    latency_warn_ms: int = 2500
    llm_latency_warn_ms: int = 60000
    health_interval_s: int = 60
    backoff_base_s: float = 5.0
    backoff_cap_s: float = 300.0

    # --- 推荐 ---
    top_n: int = 5
    max_candidates: int = 20
    ai_candidate_limit: int = 12
    poi_page_size: int = 25
    poi_max_pages: int = 4
    tile_target_km: float = 2.0
    max_tiles: int = 36
    search_radius_default_m: int = 5000

    # --- 路径 ---
    data_dir: Path = field(default_factory=default_data_dir)
    verbose_log: bool = False

    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        data_dir_raw = _env_str("MPAI_DATA_DIR")
        data_dir = Path(data_dir_raw).expanduser() if data_dir_raw else default_data_dir()
        return cls(
            amap_js_key=_env_str("AMAP_JS_KEY"),
            amap_js_security_code=_env_str("AMAP_JS_SECURITY_CODE"),
            amap_web_key=_env_str("AMAP_WEB_KEY"),
            deepseek_api_key=_env_str("DEEPSEEK_API_KEY"),
            deepseek_base_url=_env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
            or "https://api.deepseek.com",
            deepseek_model=_env_str("DEEPSEEK_MODEL", "deepseek-v4-pro") or "deepseek-v4-pro",
            http_timeout=_env_float("MPAI_HTTP_TIMEOUT", 12.0),
            http_connect_timeout=_env_float("MPAI_CONNECT_TIMEOUT", 5.0),
            llm_timeout=_env_float("MPAI_LLM_TIMEOUT", 120.0),
            llm_effort=_env_str("MPAI_LLM_EFFORT", "low"),
            max_retries=max(0, _env_int("MPAI_MAX_RETRIES", 2)),
            max_concurrency=max(1, _env_int("MPAI_MAX_CONCURRENCY", 4)),
            latency_warn_ms=max(200, _env_int("MPAI_LATENCY_WARN_MS", 2500)),
            llm_latency_warn_ms=max(1000, _env_int("MPAI_LLM_LATENCY_WARN_MS", 60000)),
            health_interval_s=max(0, _env_int("MPAI_HEALTH_INTERVAL", 60)),
            top_n=max(1, min(10, _env_int("MPAI_TOP_N", 5))),
            max_candidates=max(3, min(80, _env_int("MPAI_MAX_CANDIDATES", 20))),
            ai_candidate_limit=max(3, min(30, _env_int("MPAI_AI_CANDIDATES", 12))),
            poi_page_size=max(1, min(25, _env_int("MPAI_POI_PAGE_SIZE", 25))),
            poi_max_pages=max(1, _env_int("MPAI_POI_MAX_PAGES", 4)),
            tile_target_km=max(0.5, _env_float("MPAI_TILE_KM", 2.0)),
            max_tiles=max(1, _env_int("MPAI_MAX_TILES", 36)),
            search_radius_default_m=max(500, _env_int("MPAI_SEARCH_RADIUS", 5000)),
            data_dir=data_dir,
            verbose_log=_env_bool("MPAI_VERBOSE", False),
        )

    # ------------------------------------------------------------------
    def with_overrides(self, **kwargs: Any) -> "Settings":
        return replace(self, **kwargs)

    # --- 配置完整性 ---
    @property
    def amap_web_configured(self) -> bool:
        return bool(self.amap_web_key)

    @property
    def amap_js_configured(self) -> bool:
        return bool(self.amap_js_key)

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.deepseek_api_key)

    # --- 路径 ---
    @property
    def db_path(self) -> Path:
        return self.data_dir / "meetpoint.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def masked(self) -> dict[str, str]:
        """用于诊断输出的脱敏信息（永不返回完整 Key）。"""

        def mask(value: str) -> str:
            if not value:
                return "<未配置>"
            if len(value) <= 8:
                return value[:2] + "*" * (len(value) - 2)
            return f"{value[:4]}***{value[-4:]}"

        return {
            "amap_js_key": mask(self.amap_js_key),
            "amap_js_security_code": mask(self.amap_js_security_code),
            "amap_web_key": mask(self.amap_web_key),
            "deepseek_api_key": mask(self.deepseek_api_key),
            "deepseek_model": self.deepseek_model,
            "data_dir": str(self.data_dir),
        }


_settings: Settings | None = None


# 字段名 -> 环境变量名（用于界面内设置写回 .env）
ENV_KEY_BY_FIELD: dict[str, str] = {
    "amap_js_key": "AMAP_JS_KEY",
    "amap_js_security_code": "AMAP_JS_SECURITY_CODE",
    "amap_web_key": "AMAP_WEB_KEY",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "deepseek_base_url": "DEEPSEEK_BASE_URL",
    "deepseek_model": "DEEPSEEK_MODEL",
    "http_timeout": "MPAI_HTTP_TIMEOUT",
    "llm_timeout": "MPAI_LLM_TIMEOUT",
    "llm_effort": "MPAI_LLM_EFFORT",
    "latency_warn_ms": "MPAI_LATENCY_WARN_MS",
    "llm_latency_warn_ms": "MPAI_LLM_LATENCY_WARN_MS",
    "health_interval_s": "MPAI_HEALTH_INTERVAL",
    "top_n": "MPAI_TOP_N",
    "max_candidates": "MPAI_MAX_CANDIDATES",
    "ai_candidate_limit": "MPAI_AI_CANDIDATES",
    "max_tiles": "MPAI_MAX_TILES",
    "tile_target_km": "MPAI_TILE_KM",
    "max_concurrency": "MPAI_MAX_CONCURRENCY",
    "search_radius_default_m": "MPAI_SEARCH_RADIUS",
}


def save_env(overrides: dict[str, Any], path: Path | None = None) -> Path:
    """把设置写回 `.env`（保留注释与其他行），返回被写入的文件路径。

    仅写入 `ENV_KEY_BY_FIELD` 中登记过的字段；布尔/数值统一转字符串。
    """
    target = Path(path) if path else (app_root() / ".env")
    lines: list[str] = []
    if target.is_file():
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    pending = {ENV_KEY_BY_FIELD[k]: v for k, v in overrides.items() if k in ENV_KEY_BY_FIELD}
    remaining = dict(pending)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# —— 由程序内「设置」写入 ——")
        for key, value in remaining.items():
            out.append(f"{key}={value}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError:
        if path is not None:
            raise
        # 安装目录只读（例如装到 Program Files）时退回用户数据目录
        target = default_data_dir() / ".env"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return target


def get_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings.load()
        try:
            _settings.ensure_dirs()
        except OSError:
            pass
    return _settings


def reset_settings() -> Settings:
    global _settings
    _settings = None
    return get_settings()
