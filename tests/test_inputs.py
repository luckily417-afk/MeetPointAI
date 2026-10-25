"""输入健壮性测试：半径数值的各种写法、设置写回 .env。"""

from __future__ import annotations

import os

import pytest

from config.settings import ENV_KEY_BY_FIELD, save_env
from ui.widgets import normalize_number_text, parse_number_text


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("26", "26"),
        ("26.5", "26.5"),
        (" 26 ", "26"),
        ("〢〦.〇", "26.0"),
        ("〢〦", "26"),
        ("２６．０", "26.0"),
        ("２６", "26"),
        ("26千米", "26"),
        ("26km", "26"),
        ("26 米", "26"),
        ("1,5", "1.5"),
        ("〇.〥", "0.5"),
    ],
)
def test_normalize_number_text(raw, expected):
    assert normalize_number_text(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "二十六", "--", "km"])
def test_parse_number_text_invalid(raw):
    assert parse_number_text(raw) is None


def test_parse_number_text_valid():
    assert parse_number_text("〢〦.〇") == pytest.approx(26.0)
    assert parse_number_text("０．５") == pytest.approx(0.5)


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_radius_spin_accepts_suzhou_and_fullwidth(qt_app):
    from PySide6.QtGui import QValidator

    from ui.widgets import RadiusSpinBox

    spin = RadiusSpinBox()
    state, _, _ = spin.validate("〢〦.〇", 0)
    assert state != QValidator.State.Invalid
    spin.setValue(spin.valueFromText("〢〦.〇"))
    assert spin.value() == pytest.approx(26.0)
    assert spin.valueFromText("２６．５") == pytest.approx(26.5)
    assert spin.valueFromText("26千米") == pytest.approx(26.0)


def test_radius_spin_rejects_text(qt_app):
    from PySide6.QtGui import QValidator

    from ui.widgets import RadiusSpinBox

    spin = RadiusSpinBox()
    state, _, _ = spin.validate("abc", 0)
    assert state == QValidator.State.Invalid
    # 非法文本不应改变既有值
    spin.setValue(5.0)
    assert spin.valueFromText("abc") == pytest.approx(5.0)


def test_radius_spin_text_is_ascii(qt_app):
    from ui.widgets import RadiusSpinBox

    spin = RadiusSpinBox()
    assert spin.textFromValue(26.0) == "26"
    assert spin.textFromValue(2.5) == "2.5"


def test_save_env_updates_and_preserves(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 顶部注释\nAMAP_WEB_KEY=old\nDEEPSEEK_API_KEY=sk-old\nMPAI_TOP_N=5\n# 尾部注释\n",
        encoding="utf-8",
    )
    save_env({"amap_web_key": "newkey", "top_n": 7, "deepseek_model": "deepseek-flash"}, path=env)
    text = env.read_text(encoding="utf-8")
    assert "AMAP_WEB_KEY=newkey" in text
    assert "MPAI_TOP_N=7" in text
    assert "DEEPSEEK_MODEL=deepseek-flash" in text
    assert "# 顶部注释" in text and "# 尾部注释" in text
    assert "sk-old" in text  # 未变更的键保持原样
    assert text.endswith("\n")


def test_save_env_creates_file(tmp_path):
    env = tmp_path / "sub" / ".env"
    save_env({"top_n": 3}, path=env)
    assert env.is_file()
    assert "MPAI_TOP_N=3" in env.read_text(encoding="utf-8")


def test_env_key_mapping_covers_core_fields():
    for field in (
        "amap_web_key",
        "amap_js_key",
        "amap_js_security_code",
        "deepseek_api_key",
        "deepseek_base_url",
        "deepseek_model",
        "top_n",
        "max_candidates",
        "ai_candidate_limit",
    ):
        assert field in ENV_KEY_BY_FIELD


def test_settings_dialog_reload_flags():
    from ui.settings_dialog import SettingsDialog

    assert SettingsDialog.requires_service_reload({"deepseek_api_key": "sk-x"})
    assert SettingsDialog.requires_service_reload({"deepseek_model": "deepseek-flash"})
    assert not SettingsDialog.requires_service_reload({"top_n": 4})
    assert SettingsDialog.js_changed({"amap_js_key": "k"})
    assert not SettingsDialog.js_changed({"top_n": 4})
