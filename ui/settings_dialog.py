"""应用内设置对话框：调整 API 凭据、模型、推荐数量与运行参数。

保存后由主窗口负责：
1. 写回 `.env`（保留注释，仅覆盖相关键）；
2. 重建 HTTP/高德/DeepSeek/监测服务；
3. 若 JS Key 变更则重新加载地图；
4. 立即触发一次健康检测。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config.settings import Settings

__all__ = ["SettingsDialog"]

MODEL_CHOICES = ["deepseek-v4-pro", "deepseek-flash"]
EFFORT_CHOICES = [("低（更快更省）", "low"), ("高（更细致）", "high"), ("最高", "max"), ("不传该参数", "")]


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置 · MeetPoint AI")
        self.setMinimumWidth(620)
        self._settings = settings
        self._widgets: dict[str, Any] = {}
        self._build_ui()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        tabs = QTabWidget()
        tabs.addTab(self._tab_search(), "推荐与检索")
        tabs.addTab(self._tab_credentials(), "API 凭据")
        tabs.addTab(self._tab_runtime(), "网络与监测")
        layout.addWidget(tabs, 1)

        hint = QLabel(
            "保存后会写入程序目录的 .env 并立即重建服务；Key 只保存在本机文件中，"
            "界面与日志均只显示脱敏结果。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#6d7d90;font-size:11px;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox()
        self.btn_save = buttons.addButton("保存并应用", QDialogButtonBox.AcceptRole)
        self.btn_cancel = buttons.addButton("取消", QDialogButtonBox.RejectRole)
        self.btn_reset = buttons.addButton("恢复默认", QDialogButtonBox.ResetRole)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        self.btn_reset.clicked.connect(self._on_reset)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ #
    def _spin(self, key: str, value: int, lo: int, hi: int, suffix: str = "") -> QSpinBox:
        box = QSpinBox()
        box.setRange(lo, hi)
        box.setValue(int(value))
        if suffix:
            box.setSuffix(suffix)
        self._widgets[key] = box
        return box

    def _dspin(
        self, key: str, value: float, lo: float, hi: float, decimals: int = 1, suffix: str = ""
    ) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setDecimals(decimals)
        box.setSingleStep(0.5 if decimals else 50)
        box.setValue(float(value))
        if suffix:
            box.setSuffix(suffix)
        self._widgets[key] = box
        return box

    def _tab_search(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        box = QGroupBox("推荐结果")
        form = QFormLayout(box)
        form.addRow("推荐数量（1-10）", self._spin("top_n", self._settings.top_n, 1, 10, " 个"))
        form.addRow("参与路线规划的候选上限", self._spin("max_candidates", self._settings.max_candidates, 3, 80, " 个"))
        form.addRow("送入 AI 的候选上限", self._spin("ai_candidate_limit", self._settings.ai_candidate_limit, 3, 30, " 个"))
        note = QLabel("候选越多，AI 比较越充分但耗时与 Token 越高；推荐数量即最终展示条数。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6d7d90;font-size:11px;")
        form.addRow("", note)
        outer.addWidget(box)

        box2 = QGroupBox("区域检索分块")
        form2 = QFormLayout(box2)
        form2.addRow("分块目标边长", self._dspin("tile_target_km", self._settings.tile_target_km, 0.5, 10.0, 1, " 千米"))
        form2.addRow("最大分块数", self._spin("max_tiles", self._settings.max_tiles, 1, 100, " 块"))
        form2.addRow(
            "无区域时默认半径",
            self._spin("search_radius_default_m", self._settings.search_radius_default_m, 500, 50000, " 米"),
        )
        note2 = QLabel("分块越大调用越少但单块可能超过接口上限；建议 2 千米 / 36 块。")
        note2.setWordWrap(True)
        note2.setStyleSheet("color:#6d7d90;font-size:11px;")
        form2.addRow("", note2)
        outer.addWidget(box2)
        outer.addStretch(1)
        return page

    def _tab_credentials(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        box = QGroupBox("高德地图")
        form = QFormLayout(box)
        self._widgets["amap_web_key"] = self._secret_edit(self._settings.amap_web_key)
        self._widgets["amap_js_key"] = self._secret_edit(self._settings.amap_js_key)
        self._widgets["amap_js_security_code"] = self._secret_edit(self._settings.amap_js_security_code)
        form.addRow("Web 服务 Key", self._widgets["amap_web_key"])
        form.addRow("JS API Key", self._widgets["amap_js_key"])
        form.addRow("JS 安全密钥", self._widgets["amap_js_security_code"])
        outer.addWidget(box)

        box2 = QGroupBox("DeepSeek")
        form2 = QFormLayout(box2)
        self._widgets["deepseek_api_key"] = self._secret_edit(self._settings.deepseek_api_key)
        form2.addRow("API Key", self._widgets["deepseek_api_key"])

        self._widgets["deepseek_base_url"] = QLineEdit(self._settings.deepseek_base_url)
        self._widgets["deepseek_base_url"].setPlaceholderText("https://api.deepseek.com")
        form2.addRow("Base URL", self._widgets["deepseek_base_url"])

        model = QComboBox()
        model.setEditable(True)
        for choice in MODEL_CHOICES:
            model.addItem(choice)
        model.setCurrentText(self._settings.deepseek_model or MODEL_CHOICES[0])
        self._widgets["deepseek_model"] = model
        form2.addRow("模型", model)
        outer.addWidget(box2)

        show_row = QHBoxLayout()
        show = QCheckBox("显示明文 Key")
        show.toggled.connect(self._toggle_secret)
        show_row.addWidget(show)
        show_row.addStretch(1)
        outer.addLayout(show_row)
        outer.addStretch(1)
        return page

    def _secret_edit(self, value: str) -> QLineEdit:
        edit = QLineEdit(value)
        edit.setEchoMode(QLineEdit.Password)
        edit.setClearButtonEnabled(True)
        edit.setPlaceholderText("留空表示未配置")
        return edit

    def _toggle_secret(self, checked: bool) -> None:
        mode = QLineEdit.Normal if checked else QLineEdit.Password
        for key in ("amap_web_key", "amap_js_key", "amap_js_security_code", "deepseek_api_key"):
            widget = self._widgets.get(key)
            if isinstance(widget, QLineEdit):
                widget.setEchoMode(mode)

    def _tab_runtime(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        box = QGroupBox("网络与超时")
        form = QFormLayout(box)
        form.addRow("高德请求超时", self._dspin("http_timeout", self._settings.http_timeout, 3, 60, 1, " 秒"))
        form.addRow("DeepSeek 请求超时", self._dspin("llm_timeout", self._settings.llm_timeout, 30, 300, 0, " 秒"))
        effort = QComboBox()
        for label, value in EFFORT_CHOICES:
            effort.addItem(label, value)
        index = effort.findData(self._settings.llm_effort)
        effort.setCurrentIndex(index if index >= 0 else 0)
        self._widgets["llm_effort"] = effort
        form.addRow("推理强度", effort)
        form.addRow("并发请求数", self._spin("max_concurrency", self._settings.max_concurrency, 1, 8, " 个"))
        outer.addWidget(box)

        box2 = QGroupBox("健康监测")
        form2 = QFormLayout(box2)
        form2.addRow("周期检测间隔", self._spin("health_interval_s", self._settings.health_interval_s, 0, 600, " 秒"))
        form2.addRow("高延迟阈值", self._spin("latency_warn_ms", self._settings.latency_warn_ms, 200, 60000, " ms"))
        form2.addRow(
            "DeepSeek 高延迟阈值",
            self._spin("llm_latency_warn_ms", self._settings.llm_latency_warn_ms, 1000, 300000, " ms"),
        )
        tip = QLabel("周期检测填 0 表示关闭；故障时会自动指数退避，不会频繁打点。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#6d7d90;font-size:11px;")
        form2.addRow("", tip)
        outer.addWidget(box2)
        outer.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    def _collect(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, widget in self._widgets.items():
            if isinstance(widget, QSpinBox):
                values[key] = int(widget.value())
            elif isinstance(widget, QDoubleSpinBox):
                values[key] = float(widget.value())
            elif isinstance(widget, QComboBox):
                values[key] = widget.currentData() if widget.currentData() is not None else widget.currentText()
            elif isinstance(widget, QLineEdit):
                values[key] = widget.text().strip()
        return values

    def values(self) -> dict[str, Any]:
        return self._collect()

    def changed_values(self) -> dict[str, Any]:
        collected = self._collect()
        return {
            key: value
            for key, value in collected.items()
            if getattr(self._settings, key, None) != value
        }

    @staticmethod
    def requires_service_reload(changes: dict[str, Any]) -> bool:
        heavy = {
            "amap_web_key",
            "amap_js_key",
            "amap_js_security_code",
            "deepseek_api_key",
            "deepseek_base_url",
            "deepseek_model",
            "llm_effort",
            "http_timeout",
            "llm_timeout",
            "max_concurrency",
            "health_interval_s",
            "latency_warn_ms",
            "llm_latency_warn_ms",
        }
        return bool(heavy & set(changes))

    @staticmethod
    def js_changed(changes: dict[str, Any]) -> bool:
        return bool({"amap_js_key", "amap_js_security_code"} & set(changes))

    # ------------------------------------------------------------------ #
    def _on_accept(self) -> None:
        values = self._collect()
        if not (1 <= values["top_n"] <= 10):
            QMessageBox.warning(self, "参数错误", "推荐数量必须在 1-10 之间")
            return
        if values["ai_candidate_limit"] < values["top_n"]:
            QMessageBox.warning(self, "参数错误", "送入 AI 的候选上限不能小于推荐数量")
            return
        if values["max_candidates"] < values["top_n"]:
            QMessageBox.warning(self, "参数错误", "候选上限不能小于推荐数量")
            return
        base = values.get("deepseek_base_url", "")
        if base and not base.startswith(("http://", "https://")):
            QMessageBox.warning(self, "参数错误", "Base URL 需以 http:// 或 https:// 开头")
            return
        self.accept()

    def _on_reset(self) -> None:
        confirm = QMessageBox.question(
            self,
            "恢复默认",
            "将把本页参数恢复为默认值（不会清空已填写的 Key）。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        defaults = Settings()
        for key, widget in self._widgets.items():
            if key.endswith("_key") or key in {"amap_js_security_code"}:
                continue
            value = getattr(defaults, key, None)
            if isinstance(widget, QSpinBox) and isinstance(value, int):
                widget.setValue(value)
            elif isinstance(widget, QDoubleSpinBox) and isinstance(value, (int, float)):
                widget.setValue(float(value))
            elif isinstance(widget, QLineEdit) and isinstance(value, str):
                widget.setText(value)
            elif isinstance(widget, QComboBox):
                if key == "deepseek_model":
                    widget.setCurrentText(str(value))
                elif key == "llm_effort":
                    idx = widget.findData(value)
                    if idx >= 0:
                        widget.setCurrentIndex(idx)
