# -*- coding: utf-8 -*-
"""tests/test_theme_plugins.py —— 主题插件（注册 / 选择 / 应用 / 失效回退）。

用户 2026-10-28：在插件机制里增加主题插件类型与入口，允许加载、选择并应用自定义主题；
主题不受支持或插件在加载/应用/运行中异常时，自动回退内置默认主题、记录错误、
只停用该主题插件，不影响主程序启动运行和其它插件。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtGui, QtWidgets                             # noqa: E402

from apmlog.plugins import api as papi                           # noqa: E402
from apmlog.plugins import manager as mgr_mod                    # noqa: E402
from apmlog.plugins.manager import PluginManager                 # noqa: E402
from apmlog.ui import settings as st                             # noqa: E402
from apmlog.ui import theme as th                                # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(times: int = 6) -> None:
    app = qapp()
    for _ in range(times):
        app.processEvents()


def window_color() -> str:
    return qapp().palette().color(QtGui.QPalette.ColorRole.Window).name().lower()


# --------------------------------------------------------------------------- #
# 插件源码
# --------------------------------------------------------------------------- #
THEME_PLUGIN = r'''
PLUGIN_NAME = "主题插件"
__version__ = "1.0"
from apmlog.plugins import ThemeSpec


def register(ctx):
    ctx.register_theme(ThemeSpec(id="neon", name="霓虹", colors={
        "window": "#101820", "base": "#0b1218", "text": "#e6f1ff", "accent": "#00d4ff"}))
    ctx.register_theme({"id": "paper", "name": "纸张", "curve": "light",
                        "colors": {"c_window": "#f5f2e8", "c_base": "#fffdf6",
                                   "c_text": "#2b2b2b", "c_accent": "#b5651d"}})
'''

BAD_COLORS_PLUGIN = r'''
PLUGIN_NAME = "坏主题"
def register(ctx):
    ctx.register_theme({"id": "broken", "name": "缺色",
                        "colors": {"window": "#101010", "base": "#000000"}})
'''

BAD_HEX_PLUGIN = r'''
PLUGIN_NAME = "非法颜色"
def register(ctx):
    ctx.register_theme({"id": "t1", "name": "红色字符串",
                        "colors": {"window": "red", "base": "#fff",
                                   "text": "#111", "accent": "#222"}})
'''

MIXED_PLUGIN = r'''
PLUGIN_NAME = "好坏参半"
def register(ctx):
    ctx.register_theme({"id": "ok", "name": "好主题",
                        "colors": {"window": "#203040", "base": "#101820",
                                   "text": "#eeeeee", "accent": "#ff8800"}})
    ctx.register_theme({"id": "bad", "name": "坏主题", "colors": {"window": "#000000"}})
'''


class FakeHost:
    """主题测试用的最小宿主：只实现 plugin_apply_theme。"""

    def __init__(self) -> None:
        self.store = None
        self.applied: list[str] = []
        self.answer = True
        self.status: list[str] = []

    def set_status(self, text: str, temporary: bool = False) -> None:
        self.status.append(str(text))

    def plugin_apply_theme(self, value: str) -> bool:
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer:
            self.applied.append(str(value))
        return bool(self.answer)


class ThemeCase(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmp = Path(tempfile.mkdtemp(prefix="apmlog_theme_"))
        self.dir = self.tmp / "plugins"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings = st.Settings(self.tmp / "settings.json")
        st.Settings.set_instance(self.settings)
        self._p1 = mock.patch.object(mgr_mod, "default_plugin_dir", lambda: self.dir)
        self._p1.start()
        self.addCleanup(self._p1.stop)
        self.host = FakeHost()

    def tearDown(self) -> None:
        try:
            th.apply(qapp(), th.DEFAULT_THEME, {})
        except Exception:                              # noqa: BLE001
            pass
        st.Settings.set_instance(None)

    def write(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def manager(self) -> PluginManager:
        m = PluginManager(self.host)
        m.load_all()
        return m


# --------------------------------------------------------------------------- #
# 1. 注册与校验
# --------------------------------------------------------------------------- #
class TestThemeRegistration(ThemeCase):
    def test_registers_two_themes(self) -> None:
        self.write("tp.py", THEME_PLUGIN)
        m = self.manager()
        info = m.plugins()[0]
        self.assertEqual(info.status, "loaded")
        self.assertIn("主题", info.provides)
        avail = m.themes_available()
        self.assertEqual([t["id"] for t in avail], ["neon", "paper"])
        self.assertEqual(avail[0]["value"], "plugin:tp:neon")
        self.assertEqual(avail[0]["name"], "霓虹")
        self.assertEqual(avail[0]["colors"]["accent"], "#00d4ff")
        self.assertEqual(avail[1]["curve"], "light")

    def test_missing_colors_are_rejected(self) -> None:
        self.write("bad.py", BAD_COLORS_PLUGIN)
        m = self.manager()
        self.assertEqual(m.themes(), [], "颜色不全的主题不许进注册表")
        info = m.plugins()[0]
        self.assertEqual(info.status, "failed")
        self.assertIn("不受支持", info.error)

    def test_non_hex_color_is_rejected(self) -> None:
        self.write("badhex.py", BAD_HEX_PLUGIN)
        m = self.manager()
        self.assertEqual(m.themes(), [])
        self.assertIn("不受支持", m.plugins()[0].error)

    def test_partial_plugin_keeps_the_good_theme(self) -> None:
        self.write("mixed.py", MIXED_PLUGIN)
        m = self.manager()
        info = m.plugins()[0]
        self.assertEqual(info.status, "loaded", "一个好主题 + 一个坏主题不该整插件判死")
        self.assertEqual([t.id for t in m.themes()], ["ok"])
        self.assertIn("主题", info.provides)
        self.assertGreaterEqual(info.errors, 0)

    def test_bad_id_and_curve_are_rejected(self) -> None:
        m = self.manager()
        self.assertFalse(m.add_theme("x", {"id": "有空格 的", "name": "x",
                                           "colors": {k: "#ffffff" for k in
                                                      ("window", "base", "text", "accent")}}))
        self.assertFalse(m.add_theme("x", {"id": "t", "name": "x", "curve": "sepia",
                                           "colors": {k: "#ffffff" for k in
                                                      ("window", "base", "text", "accent")}}))
        self.assertFalse(m.add_theme("x", {"id": "", "name": "x", "colors": {}}))
        self.assertEqual(m.themes(), [])

    def test_duplicate_id_rejected(self) -> None:
        cols = {k: "#123456" for k in ("window", "base", "text", "accent")}
        m = self.manager()
        self.assertTrue(m.add_theme("x", {"id": "same", "name": "一", "colors": cols}))
        self.assertFalse(m.add_theme("x", {"id": "same", "name": "二", "colors": cols}))
        self.assertEqual(len(m.themes()), 1)

    def test_same_id_across_plugins_is_allowed(self) -> None:
        cols = {k: "#123456" for k in ("window", "base", "text", "accent")}
        m = self.manager()
        self.assertTrue(m.add_theme("a", {"id": "same", "name": "A", "colors": cols}))
        self.assertTrue(m.add_theme("b", {"id": "same", "name": "B", "colors": cols}))
        self.assertEqual(len(m.themes()), 2)
        self.assertIsNotNone(m.theme_by_value("plugin:a:same"))
        self.assertIsNotNone(m.theme_by_value("plugin:b:same"))

    def test_theme_spec_is_immutable_for_plugins(self) -> None:
        self.write("tp.py", THEME_PLUGIN)
        m = self.manager()
        spec = m.themes()[0]
        with self.assertRaises(TypeError):
            spec.colors["window"] = "#ff0000"          # MappingProxyType
        with self.assertRaises(Exception):
            spec.name = "改名"                          # frozen dataclass


# --------------------------------------------------------------------------- #
# 2. 应用（manager → host）与失败归因
# --------------------------------------------------------------------------- #
class TestThemeApply(ThemeCase):
    def test_apply_calls_host(self) -> None:
        self.write("tp.py", THEME_PLUGIN)
        m = self.manager()
        self.assertTrue(m.apply_theme("plugin:tp:neon"))
        self.assertEqual(self.host.applied, ["plugin:tp:neon"])
        self.assertEqual(m.plugins()[0].status, "loaded")

    def test_apply_from_plugin_accepts_short_id(self) -> None:
        self.write("tp.py", THEME_PLUGIN)
        m = self.manager()
        self.assertTrue(m.apply_theme_from_plugin("tp", "paper"))
        self.assertEqual(self.host.applied, ["plugin:tp:paper"])
        self.assertTrue(m.apply_theme_from_plugin("tp", "dark"))
        self.assertEqual(self.host.applied[-1], "dark")

    def test_host_false_disables_owner(self) -> None:
        self.write("tp.py", THEME_PLUGIN)
        m = self.manager()
        self.host.answer = False
        self.assertFalse(m.apply_theme("plugin:tp:neon"))
        info = m.plugins()[0]
        self.assertEqual(info.status, "disabled")
        self.assertIn("应用失败", info.error)
        self.assertEqual(m.themes(), [], "停用后主题要摘干净")

    def test_host_exception_is_contained(self) -> None:
        self.write("tp.py", THEME_PLUGIN)
        m = self.manager()
        self.host.answer = RuntimeError("palette boom")
        self.assertFalse(m.apply_theme("plugin:tp:neon"))
        self.assertEqual(m.plugins()[0].status, "disabled")

    def test_unknown_theme_value_returns_none(self) -> None:
        m = self.manager()
        self.assertIsNone(m.theme_by_value("plugin:ghost:x"))
        self.assertIsNone(m.theme_by_value("dark"))
        self.assertIsNone(m.theme_by_value("plugin:onlypid"))


# --------------------------------------------------------------------------- #
# 3. 主题值 / 设置项
# --------------------------------------------------------------------------- #
class TestThemeValues(unittest.TestCase):
    def test_value_roundtrip(self) -> None:
        v = th.theme_value("pid", "neon")
        self.assertEqual(v, "plugin:pid:neon")
        self.assertTrue(th.is_plugin_theme(v))
        self.assertEqual(th.parse_plugin_theme(v), ("pid", "neon"))
        self.assertIsNone(th.parse_plugin_theme("dark"))
        self.assertIsNone(th.parse_plugin_theme("plugin:noColon"))
        self.assertIsNone(th.parse_plugin_theme("plugin::x"))

    def test_spec_from_theme_accepts_both_key_styles(self) -> None:
        a = th.spec_from_theme({"colors": {"window": "#111111", "base": "#222222",
                                          "text": "#eeeeee", "accent": "#00ff00"}})
        b = th.spec_from_theme({"colors": {"c_window": "#111111", "c_base": "#222222",
                                          "c_text": "#eeeeee", "c_accent": "#00ff00"}})
        self.assertEqual(a, b)
        self.assertEqual(a["window"], "#111111")

    def test_spec_from_theme_raises_on_shorthand_gap(self) -> None:
        with self.assertRaises(ValueError):
            th.spec_from_theme({"colors": {"window": "#111111"}})
        with self.assertRaises(ValueError):
            th.spec_from_theme({"colors": {"window": "#111", "base": "#222",
                                          "text": "#333", "accent": "nope"}})

    def test_settings_clean_accepts_plugin_value(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="apmlog_thset_"))
        s = st.Settings(tmp / "s.json")
        st.Settings.set_instance(s)
        try:
            s.set("ui_theme", "plugin:tp:neon")
            self.assertEqual(s.get("ui_theme"), "plugin:tp:neon")
            s.set("ui_theme", "随便写的")
            self.assertEqual(s.get("ui_theme"), th.DEFAULT_THEME)
        finally:
            st.Settings.set_instance(None)


# --------------------------------------------------------------------------- #
# 4. 主窗：选择、应用、回退
# --------------------------------------------------------------------------- #
class TestMainWindowTheme(ThemeCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("tp.py", THEME_PLUGIN)
        views = self.tmp / "views.json"
        self._p2 = mock.patch("apmlog.app.MainWindow._views_path", lambda self_: views)
        self._p2.start()
        self.addCleanup(self._p2.stop)
        from apmlog.app import MainWindow
        self.win = MainWindow()
        pump()
        self.addCleanup(self.win.close)

    def _combo(self) -> QtWidgets.QComboBox:
        dlg = QtWidgets.QDialog(self.win)
        from apmlog.ui.settings import SettingsDialog
        d = SettingsDialog(self.win, cells_provider=None,
                           themes_provider=self.win.plugins.themes_available)
        self.addCleanup(d.close)
        return d.cb_ui_theme

    def test_theme_extends_settings_combo(self) -> None:
        cb = self._combo()
        values = [cb.itemData(i) for i in range(cb.count())]
        self.assertIn("plugin:tp:neon", values)
        self.assertIn("plugin:tp:paper", values)
        self.assertIn("system", values)
        self.assertIn("custom", values)
        for i in range(cb.count()):
            if cb.itemData(i) == "plugin:tp:neon":
                self.assertIn("插件：", cb.itemText(i))

    def test_selecting_plugin_theme_applies_palette(self) -> None:
        cb = self._combo()
        cb.setCurrentIndex(cb.findData("plugin:tp:neon"))
        pump()
        self.assertEqual(str(self.settings.get("ui_theme")), "plugin:tp:neon")
        self.assertEqual(window_color(), "#101820")
        self.assertEqual(str(self.settings.get("theme")), "dark",
                         "霓虹底色很暗，曲线应自动切深色调色板")

    def test_theme_changed_signal(self) -> None:
        got: list[str] = []
        self.win.plugins.signals.theme_changed.connect(got.append)
        self.win.plugin_apply_theme("plugin:tp:paper")
        pump()
        self.assertIn("plugin:tp:paper", got)

    def test_disabling_plugin_falls_back_to_default(self) -> None:
        cb = self._combo()
        cb.setCurrentIndex(cb.findData("plugin:tp:neon"))
        pump()
        self.assertEqual(window_color(), "#101820")
        self.win.plugins.disable("tp")
        pump()
        self.assertEqual(str(self.settings.get("ui_theme")), th.DEFAULT_THEME)
        self.assertEqual(window_color(), th.BUILTIN[th.system_scheme()]["window"].lower()
                         if th.DEFAULT_THEME != "light" else "#f2f3f5")
        self.assertEqual(self.win.plugins.themes(), [])

    def test_unsupported_theme_at_apply_time_falls_back(self) -> None:
        """注册时合法、应用时被改坏（模拟插件运行期改了 colors）→ 回退 + 停用。"""
        cb = self._combo()
        bad = papi.ThemeSpec(id="neon", name="霓虹",
                             colors={"window": "not-a-color", "base": "#000000",
                                     "text": "#ffffff", "accent": "#00d4ff"})
        self.win.plugins._themes = [("tp", bad)]
        ok = self.win.plugin_apply_theme("plugin:tp:neon")
        pump()
        self.assertFalse(ok)
        self.assertEqual(str(self.settings.get("ui_theme")), th.DEFAULT_THEME)
        self.assertEqual([i.status for i in self.win.plugins.plugins()], ["disabled"])
        self.assertIn("主题不可用", self.win.statusBar().currentMessage())

    def test_host_exception_during_apply_is_contained(self) -> None:
        with mock.patch.object(self.win, "plugin_apply_theme",
                               side_effect=RuntimeError("boom")):
            ok = self.win.plugins.apply_theme("plugin:tp:neon")
        pump()
        self.assertFalse(ok)
        self.assertTrue(self.win.centralWidget() is not None, "主窗必须还活着")
        self.assertEqual(self.win.plugins.plugins()[0].status, "disabled")

    def test_stale_plugin_theme_at_startup_falls_back(self) -> None:
        """设置里存着一个已经不存在的插件主题 → 启动时回退，窗口照常。"""
        from apmlog.app import MainWindow
        self.settings.set("ui_theme", "plugin:ghost:neon")
        win2 = MainWindow()
        try:
            pump()
            self.assertEqual(str(self.settings.get("ui_theme")), th.DEFAULT_THEME)
            self.assertIsNotNone(win2.centralWidget())
        finally:
            win2.close()

    def test_plugin_dialog_lists_and_applies_theme(self) -> None:
        from apmlog.ui.plugindialog import PluginDialog
        dlg = PluginDialog(self.win.plugins, self.win)
        try:
            dlg.show()
            pump()
            values = [dlg.theme_list.item(i).data(QtCore_UserRole())
                      for i in range(dlg.theme_list.count())]
            self.assertIn("plugin:tp:neon", values)
            dlg.theme_list.setCurrentRow(values.index("plugin:tp:neon"))
            dlg._apply_theme()
            pump()
            self.assertEqual(window_color(), "#101820")
            self.assertEqual(str(self.settings.get("ui_theme")), "plugin:tp:neon")
        finally:
            dlg.close()

    def test_plugin_without_theme_keeps_settings_clean(self) -> None:
        cb = self._combo()
        values = [cb.itemData(i) for i in range(cb.count())]
        self.assertNotIn("plugin:", " ".join(str(v) for v in values if v is None))


def QtCore_UserRole():
    from PySide6 import QtCore
    return QtCore.Qt.ItemDataRole.UserRole


if __name__ == "__main__":
    unittest.main()
