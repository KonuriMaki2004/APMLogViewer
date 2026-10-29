# -*- coding: utf-8 -*-
"""tests/test_theme.py —— 界面主题：跟随系统 / 浅色 / 深色 / 自定义。

主题只换 QPalette（不装全局 QSS），并且和曲线配色分两层：
界面主题决定程序外壳；曲线配色默认跟着推导，但用户仍可在曲线页手动覆盖。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_theme.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtGui, QtWidgets                              # noqa: E402

from apmlog.ui import settings as st                              # noqa: E402
from apmlog.ui import theme as T                                  # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 20) -> None:
    for _ in range(times):
        app.processEvents()


def role(app: QtWidgets.QApplication, r: QtGui.QPalette.ColorRole) -> str:
    return app.palette().color(r).name()


class _PaletteCase(unittest.TestCase):
    """所有会改全局 palette 的用例都从这里继承，退出时把 palette 还回去。"""

    def setUp(self) -> None:
        self.app = qapp()
        st.install_tip_style(self.app)
        self._saved = QtGui.QPalette(self.app.palette())
        self.addCleanup(lambda: (self.app.setPalette(self._saved), pump(self.app, 3)))
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_theme_")
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "settings.json"
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(self.path))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        self.s = st.Settings.instance()


class TestPureHelpers(unittest.TestCase):
    def test_builtin_specs_are_valid_hex(self) -> None:
        for mode, spec in T.BUILTIN.items():
            for k, v in spec.items():
                self.assertTrue(T.is_hex_color(v), f"{mode}.{k}={v}")
        for k, v in T.CUSTOM_DEFAULTS.items():
            self.assertTrue(T.is_hex_color(v), f"{k}={v}")

    def test_is_hex_color_strict(self) -> None:
        for good in ("#fff", "#ffffff", "#101418", "#ABCDEF"):
            self.assertTrue(T.is_hex_color(good), good)
        for bad in ("", None, "red", "ffffff", "#gggggg", "#12", "#12345", "  "):
            self.assertFalse(T.is_hex_color(bad), repr(bad))

    def test_normalize_hex_falls_back_instead_of_raising(self) -> None:
        self.assertEqual(T.normalize_hex("#FF8F1F", "#ffffff"), "#ff8f1f")
        self.assertEqual(T.normalize_hex("garbage", "#ffffff"), "#ffffff")
        self.assertEqual(T.normalize_hex(None, "#123456"), "#123456")

    def test_luminance_orders_black_white(self) -> None:
        self.assertLess(T.luminance("#000000"), 0.01)
        self.assertGreater(T.luminance("#ffffff"), 0.99)
        self.assertAlmostEqual(T.luminance("#12"), 0.5, places=2)   # 脏值也不炸，给个中性值

    def test_curve_follows_base_lightness(self) -> None:
        self.assertEqual(T.curve_for({"base": "#101418"}), "dark", "暗底要亮线")
        self.assertEqual(T.curve_for({"base": "#ffffff"}), "light")
        self.assertEqual(T.curve_for({}), "light")

    def test_spec_for_known_modes(self) -> None:
        self.assertEqual(T.spec_for("light"), T.BUILTIN["light"])
        self.assertEqual(T.spec_for("dark"), T.BUILTIN["dark"])

    def test_spec_for_custom_uses_cleaned_values(self) -> None:
        spec = T.spec_for("custom", {"c_window": "#111111", "c_base": "nonsense",
                                     "c_text": "#eeeeee", "c_accent": "#ff0000"})
        self.assertEqual(spec["window"], "#111111")
        self.assertEqual(spec["base"], T.CUSTOM_DEFAULTS["c_base"], "脏颜色回默认，不能留脏值")
        self.assertEqual(spec["accent"], "#ff0000")

    def test_spec_for_unknown_mode_behaves_like_system(self) -> None:
        with mock.patch.object(T, "system_scheme", return_value="dark"):
            self.assertEqual(T.spec_for("purple"), T.BUILTIN["dark"])
            self.assertEqual(T.spec_for(""), T.BUILTIN["dark"])

    def test_system_scheme_survives_broken_stylehints(self) -> None:
        app = qapp()
        with mock.patch.object(app, "styleHints", side_effect=RuntimeError("no hints")):
            self.assertIn(T.system_scheme(app), ("light", "dark"))

    def test_system_scheme_reads_color_scheme(self) -> None:
        app = qapp()
        for cs, want in ((QtCore_Dark(), "dark"), (QtCore_Light(), "light")):
            with mock.patch.object(app.styleHints(), "colorScheme", return_value=cs):
                self.assertEqual(T.system_scheme(app), want)

    def test_palette_for_fills_the_roles_we_care_about(self) -> None:
        pal = T.palette_for({"window": "#2b2f36", "base": "#1e2227",
                             "text": "#e8eaed", "accent": "#5b9dff"})
        self.assertEqual(pal.color(QtGui.QPalette.ColorGroup.Active,
                                   QtGui.QPalette.ColorRole.Window).name(), "#2b2f36")
        self.assertEqual(pal.color(QtGui.QPalette.ColorGroup.Active,
                                   QtGui.QPalette.ColorRole.Base).name(), "#1e2227")
        self.assertEqual(pal.color(QtGui.QPalette.ColorGroup.Inactive,
                                   QtGui.QPalette.ColorRole.Highlight).name(), "#5b9dff")
        dis = pal.color(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.WindowText)
        act = pal.color(QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorRole.WindowText)
        self.assertNotEqual(dis.name(), act.name(), "禁用态文字要更淡，否则灰不掉")

    def test_highlighted_text_contrasts_with_accent(self) -> None:
        dark_accent = T.palette_for({"window": "#fff", "base": "#fff",
                                     "text": "#000", "accent": "#2f6fed"})
        light_accent = T.palette_for({"window": "#fff", "base": "#fff",
                                      "text": "#000", "accent": "#ffe066"})
        a = dark_accent.color(QtGui.QPalette.ColorRole.HighlightedText)
        b = light_accent.color(QtGui.QPalette.ColorRole.HighlightedText)
        self.assertGreater(T.luminance(a.name()), 0.9, "深蓝底要白字")
        self.assertLess(T.luminance(b.name()), 0.2, "亮黄底要黑字")


def QtCore_Dark():
    from PySide6 import QtCore
    return QtCore.Qt.ColorScheme.Dark


def QtCore_Light():
    from PySide6 import QtCore
    return QtCore.Qt.ColorScheme.Light


class TestApplyToApp(_PaletteCase):
    def test_apply_sets_app_palette_and_returns_curve(self) -> None:
        curve = T.apply(self.app, "dark")
        self.assertEqual(role(self.app, QtGui.QPalette.ColorRole.Window), T.BUILTIN["dark"]["window"])
        self.assertEqual(curve, "dark")

    def test_apply_with_none_app_is_noop(self) -> None:
        self.assertEqual(T.apply(None, "dark"), "light")

    def test_apply_settings_reads_settings_object(self) -> None:
        self.s.set("ui_theme", "custom")
        self.s.set("c_window", "#123456")
        self.s.set("c_base", "#000000")
        self.assertEqual(T.apply_settings(self.app, self.s), "dark")
        self.assertEqual(role(self.app, QtGui.QPalette.ColorRole.Window), "#123456")

    def test_custom_bad_color_does_not_break_palette(self) -> None:
        self.s.set("ui_theme", "custom")
        self.s.set("c_text", "not-a-color")
        T.apply_settings(self.app, self.s)
        self.assertEqual(role(self.app, QtGui.QPalette.ColorRole.Text),
                         T.CUSTOM_DEFAULTS["c_text"])


class TestSettingsValidation(_PaletteCase):
    def test_ui_theme_and_colors_clamped(self) -> None:
        self.assertEqual(self.s.get("ui_theme"), "system")
        self.s.set("ui_theme", "nope")
        self.assertEqual(self.s.get("ui_theme"), "system")
        self.s.set("c_accent", "rebeccapurple")
        self.assertEqual(self.s.get("c_accent"), T.CUSTOM_DEFAULTS["c_accent"])
        self.s.set("c_accent", "#FF8F1F")
        self.assertEqual(self.s.get("c_accent"), "#ff8f1f")

    def test_custom_colors_survive_reload(self) -> None:
        self.s.set("ui_theme", "custom")
        self.s.set("c_base", "#0b0e13")
        st.Settings.set_instance(None)
        again = st.Settings(self.path)
        self.assertEqual(again.get("c_base"), "#0b0e13")
        self.assertEqual(again.get("ui_theme"), "custom")


class TestDialog(_PaletteCase):
    def test_combo_lists_four_modes_in_chinese(self) -> None:
        dlg = st.SettingsDialog(s=self.s)
        self.addCleanup(dlg.deleteLater)
        got = [dlg.cb_ui_theme.itemText(i) for i in range(dlg.cb_ui_theme.count())]
        self.assertEqual(got, ["跟随系统", "浅色", "深色", "自定义"])
        self.assertEqual([dlg.cb_ui_theme.itemData(i) for i in range(dlg.cb_ui_theme.count())],
                         list(T.UI_THEMES))

    def test_color_buttons_enabled_only_for_custom(self) -> None:
        dlg = st.SettingsDialog(s=self.s)
        self.addCleanup(dlg.deleteLater)
        self.assertFalse(dlg._color_btns["c_base"].isEnabled())
        dlg.cb_ui_theme.setCurrentIndex(dlg.cb_ui_theme.findData("custom"))
        self.assertTrue(dlg._color_btns["c_base"].isEnabled())
        self.assertEqual(self.s.get("ui_theme"), "custom")

    def test_button_shows_label_and_hex(self) -> None:
        self.s.set("c_base", "#101418")
        dlg = st.SettingsDialog(s=self.s)
        self.addCleanup(dlg.deleteLater)
        txt = dlg._color_btns["c_base"].text()
        self.assertIn("输入区底色", txt)
        self.assertIn("#101418", txt)

    def test_picking_color_writes_settings(self) -> None:
        dlg = st.SettingsDialog(s=self.s)
        self.addCleanup(dlg.deleteLater)
        dlg.cb_ui_theme.setCurrentIndex(dlg.cb_ui_theme.findData("custom"))
        with mock.patch.object(QtWidgets.QColorDialog, "getColor",
                               staticmethod(lambda *a, **k: QtGui.QColor("#7f3fbf"))):
            dlg._pick_color("c_accent")
        self.assertEqual(self.s.get("c_accent"), "#7f3fbf")
        self.assertIn("#7f3fbf", dlg._color_btns["c_accent"].text())

    def test_cancelled_color_dialog_changes_nothing(self) -> None:
        dlg = st.SettingsDialog(s=self.s)
        self.addCleanup(dlg.deleteLater)
        dlg.cb_ui_theme.setCurrentIndex(dlg.cb_ui_theme.findData("custom"))
        before = self.s.get("c_accent")
        with mock.patch.object(QtWidgets.QColorDialog, "getColor",
                               staticmethod(lambda *a, **k: QtGui.QColor())):
            dlg._pick_color("c_accent")
        self.assertEqual(self.s.get("c_accent"), before)

    def test_external_change_syncs_widgets(self) -> None:
        dlg = st.SettingsDialog(s=self.s)
        self.addCleanup(dlg.deleteLater)
        self.s.set("ui_theme", "custom")
        self.s.set("c_window", "#010203")
        self.assertEqual(dlg.cb_ui_theme.currentData(), "custom")
        self.assertTrue(dlg._color_btns["c_window"].isEnabled())
        self.assertIn("#010203", dlg._color_btns["c_window"].text())

    def test_reset_button_restores_theme_widgets(self) -> None:
        dlg = st.SettingsDialog(s=self.s)
        self.addCleanup(dlg.deleteLater)
        dlg.cb_ui_theme.setCurrentIndex(dlg.cb_ui_theme.findData("custom"))
        self.s.set("c_text", "#123456")
        btn = next(b for b in dlg.findChildren(QtWidgets.QPushButton)
                   if "恢复默认" in b.text())
        with mock.patch.object(QtWidgets.QMessageBox, "exec", staticmethod(lambda *a, **k: 0)):
            btn.click()
        pump(self.app, 5)
        self.assertEqual(self.s.get("ui_theme"), "system")
        self.assertEqual(self.s.get("c_text"), T.CUSTOM_DEFAULTS["c_text"])
        self.assertEqual(dlg.cb_ui_theme.currentData(), "system")
        self.assertFalse(dlg._color_btns["c_text"].isEnabled())


class TestMainWindowIntegration(_PaletteCase):
    def _window(self):
        from apmlog.app import MainWindow
        w = MainWindow()
        w.show()
        pump(self.app)
        self.addCleanup(w.close)
        return w

    def test_startup_applies_palette_without_override_curve_theme(self) -> None:
        """界面走深色，但曲线配色尊重用户存下的 light：两层不能互相覆盖。"""
        self.s.set("ui_theme", "dark")
        self.s.set("theme", "light")
        w = self._window()
        self.assertEqual(role(self.app, QtGui.QPalette.ColorRole.Window), T.BUILTIN["dark"]["window"])
        self.assertEqual(w.plot.theme(), "light")
        self.assertEqual(self.s.get("theme"), "light")

    def test_changing_ui_theme_pushes_curve_theme(self) -> None:
        self.s.set("ui_theme", "light")
        self.s.set("theme", "light")
        w = self._window()
        self.s.set("ui_theme", "dark")
        pump(self.app)
        self.assertEqual(w.plot.theme(), "dark")
        self.assertEqual(self.s.get("theme"), "dark")
        self.assertEqual(role(self.app, QtGui.QPalette.ColorRole.Window), T.BUILTIN["dark"]["window"])

    def test_custom_dark_base_flips_curve_to_dark(self) -> None:
        self.s.set("ui_theme", "custom")
        self.s.set("c_base", "#0d1117")
        w = self._window()
        self.s.set("c_base", "#0d1117")
        self.s.set("c_window", "#161b22")
        pump(self.app)
        self.assertEqual(w.plot.theme(), "dark")
        self.assertEqual(role(self.app, QtGui.QPalette.ColorRole.Base), "#0d1117")

    def test_light_custom_keeps_light_curves(self) -> None:
        self.s.set("ui_theme", "custom")
        self.s.set("c_base", "#ffffff")
        w = self._window()
        self.s.set("c_window", "#f5f5f5")
        pump(self.app)
        self.assertEqual(w.plot.theme(), "light")

    def test_system_scheme_change_only_follows_in_system_mode(self) -> None:
        w = self._window()
        with mock.patch.object(w, "_apply_ui_theme") as called:
            self.s.set("ui_theme", "light")
            called.reset_mock()          # 上面那次是 set() 触发的，这里只关心系统信号
            w._on_system_scheme()
            self.assertFalse(called.called, "写死浅色时别被系统拽走")
            self.s.set("ui_theme", "system")
            called.reset_mock()
            w._on_system_scheme()
            self.assertTrue(called.called)

    def test_timeline_and_dialog_follow_the_palette(self) -> None:
        self.s.set("ui_theme", "dark")
        w = self._window()
        w.timeline.set_span(0.0, 100.0)
        w.timeline.grab()
        self.assertEqual(w.timeline._bg.name(), T.BUILTIN["dark"]["base"])
        dlg = st.SettingsDialog(w, s=self.s)
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(dlg.palette().color(QtGui.QPalette.ColorRole.Window).name(),
                         T.BUILTIN["dark"]["window"])

    def test_broken_theme_does_not_kill_the_window(self) -> None:
        w = self._window()
        with mock.patch.object(T, "apply_settings", side_effect=RuntimeError("boom")):
            self.s.set("ui_theme", "dark")
            pump(self.app)
        self.assertTrue(w.isVisible(), "换肤失败不能把主窗弄没")


class TestContrastHelpers(unittest.TestCase):
    """“部分位置文字颜不对”的根因都是写死颜色，这里钉住按底色算墨色的工具。"""

    def test_accepts_qcolor_not_only_strings(self) -> None:
        """str(QColor) 不是十六进制，回解会变无效色——以前这里会默默当成灰色算。"""
        self.assertAlmostEqual(T.rel_luminance(QtGui.QColor("#ffffff")), 1.0, places=3)
        self.assertAlmostEqual(T.rel_luminance(QtGui.QColor("#000000")), 0.0, places=3)
        self.assertAlmostEqual(T.luminance(QtGui.QColor("#808080")), 0.5019, places=3)
        self.assertGreater(T.contrast(QtGui.QColor("#000000"), QtGui.QColor("#ffffff")), 20)

    def test_contrast_matches_known_pairs(self) -> None:
        self.assertAlmostEqual(T.contrast("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(T.contrast("#777777", "#777777"), 1.0, places=2)

    def test_readable_keeps_hue_and_reaches_target(self) -> None:
        out = T.readable("#e8eaed", "#dfe9f5")          # 深色主题的白字 → 浅 pastel 带上
        self.assertGreaterEqual(T.contrast(out, "#dfe9f5"), 4.4, out)
        self.assertLess(T.rel_luminance(out), 0.45, "浅带上必须转成暗字，用户要的就是这个")
        h_in = QtGui.QColor("#e8eaed").getHsl()[0]
        h_out = QtGui.QColor(out).getHsl()[0]
        self.assertTrue(h_in == h_out or h_out == 0 or QtGui.QColor(out).getHsl()[1] < 30,
                        f"色相不该乱跳 {h_in}→{h_out}")

    def test_readable_leaves_already_legible_color_alone(self) -> None:
        self.assertEqual(T.readable("#1f2328", "#ffffff"), "#1f2328")
        self.assertEqual(T.readable("#e8eaed", "#1e2227"), "#e8eaed")

    def test_readable_lifts_dark_amber_on_dark_background(self) -> None:
        out = T.readable("#8a6d1b", "#1e2227")           # 跳过提示的暗黄字
        self.assertGreaterEqual(T.contrast(out, "#1e2227"), 4.4, out)
        self.assertGreater(T.rel_luminance(out), T.rel_luminance("#8a6d1b"))

    def test_mode_band_ink_is_dark_in_both_builtin_themes(self) -> None:
        """时间线模式色带是浅色 pastel：两套内置主题下都该算出暗字。"""
        for mode in ("light", "dark"):
            fg = T.BUILTIN[mode]["text"]
            for band in ("#dfe9f5", "#e8f0e3", "#f6e7d4", "#eae0f0", "#dcece9", "#f0e0e0"):
                ink = T.readable(fg, band)
                self.assertGreaterEqual(T.contrast(ink, band), 4.4, f"{mode} {band} → {ink}")
                self.assertLess(T.rel_luminance(ink), 0.5, f"{mode} {band} 上不该用亮字")

    def test_readable_survives_garbage(self) -> None:
        self.assertEqual(T.readable("nonsense", "#ffffff"), "nonsense")
        self.assertEqual(T.readable("#000000", "nonsense"), "#000000")

    def test_readable_on_uses_widget_background(self) -> None:
        app = qapp()
        w = QtWidgets.QWidget()
        pal = QtGui.QPalette()
        pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#1e2227"))
        w.setPalette(pal)
        self.assertGreaterEqual(T.contrast(T.readable_on(w, "#8a6d1b"), "#1e2227"), 4.4)


class TestStyleProxyBase(unittest.TestCase):
    """工具条/标签页不吃 palette 的根因：Windows 原生风格走 UxTheme，得把代理基风格定在 Fusion。"""

    def test_default_base_style_is_fusion(self) -> None:
        import inspect
        sig = inspect.signature(st.install_tip_style)
        self.assertEqual(sig.parameters["base_style"].default, "Fusion")
        self.assertIn("Fusion", QtWidgets.QStyleFactory.keys())

    def test_proxy_installed_and_still_overrides_wake_delay(self) -> None:
        app = qapp()
        self.addCleanup(st.install_tip_style, app)
        self.assertTrue(st.install_tip_style(app))
        self.assertIsInstance(app.style(), st._TipStyle)
        with mock.patch.object(st, "settings", return_value=mock.Mock(tip_wake_ms=lambda: 611)):
            self.assertEqual(app.style().styleHint(
                QtWidgets.QStyle.StyleHint.SH_ToolTip_WakeUpDelay), 611)

    def test_unknown_base_style_falls_back_without_crash(self) -> None:
        app = qapp()
        self.addCleanup(st.install_tip_style, app)
        self.assertTrue(st.install_tip_style(app, "nonexistent-style-xyz"))
        self.assertIsInstance(app.style(), st._TipStyle)


class TestToolbarInkIsVisible(_PaletteCase):
    """真像素检：浅色主题下工具条不能“浅底白字”。无字体环境自动跳过。"""

    def _ink_spread(self, widget) -> float:
        im = widget.grab().toImage()
        ls = [T.rel_luminance(im.pixelColor(x, y))
              for y in range(0, im.height(), 2) for x in range(0, im.width(), 2)]
        return max(ls) - min(ls) if ls else 0.0

    def test_toolbar_text_contrasts_with_theme(self) -> None:
        probe = QtWidgets.QLabel("打开日志 OPEN")
        probe.resize(140, 24)
        probe.show()
        pump(self.app, 6)
        if self._ink_spread(probe) < 0.2:
            self.skipTest("本环境没有可用字体，像素对比度测不了")
        probe.deleteLater()
        for mode, expect_ink in (("light", "dark"), ("dark", "light")):
            self.s.set("ui_theme", mode)
            T.apply_settings(self.app, self.s)
            bar = QtWidgets.QToolBar()
            bar.addAction("打开日志")
            bar.addAction("设置")
            bar.resize(400, 30)
            bar.show()
            pump(self.app, 8)
            im = bar.grab().toImage()
            ls = [T.rel_luminance(im.pixelColor(x, y))
                  for y in range(0, im.height(), 2) for x in range(0, im.width(), 2)]
            spread = max(ls) - min(ls)
            bg = T.rel_luminance(self.app.palette().color(QtGui.QPalette.ColorRole.Window))
            ink = min(ls) if expect_ink == "dark" else max(ls)
            self.assertGreater(spread, 0.35, f"{mode}: 工具条文字与底色同色（反差 {spread:.2f}）")
            self.assertTrue((ink < 0.35) if expect_ink == "dark" else (ink > 0.65),
                            f"{mode}: 底色亮度 {bg:.2f}，墨色亮度 {ink:.2f} 不对")
            bar.close()
            bar.deleteLater()
            pump(self.app, 4)


if __name__ == "__main__":
    unittest.main()
