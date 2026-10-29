# -*- coding: utf-8 -*-
"""tests/test_settings.py —— 设置存储、悬浮提示留存、设置对话框、主窗接线。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_settings.py -q
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtWidgets                       # noqa: E402

from apmlog.ui import settings as st                              # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 3) -> None:
    for _ in range(times):
        app.processEvents()


class _TempSettings(unittest.TestCase):
    """把单例指到临时文件，绝不碰用户真实的 settings.json。"""

    def setUp(self):
        self.app = qapp()
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_settings_")
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "settings.json"
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(self.path))
        self.s = st.Settings.instance()

    def tearDown(self):
        st.Settings.set_instance(self._prev)


class TestSettingsStore(_TempSettings):
    def test_defaults_without_file(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(self.s.get("tip_seconds"), st.DEFAULTS["tip_seconds"])
        self.assertEqual(self.s.get("lane_min_h"), 132)
        self.assertEqual(self.s.get("theme"), "light")

    def test_save_and_reload_roundtrip(self):
        self.s.set("tip_seconds", 25)
        self.s.set("theme", "dark")
        self.s.set("badge_tol_px", 14.5)
        self.assertTrue(self.path.exists())
        st.Settings.set_instance(None)
        again = st.Settings(self.path)
        self.assertEqual(again.get("tip_seconds"), 25)
        self.assertEqual(again.get("theme"), "dark")
        self.assertAlmostEqual(again.get("badge_tol_px"), 14.5)

    def test_out_of_range_is_clamped_not_rejected(self):
        self.s.set("lane_min_h", 99999)
        self.assertEqual(self.s.get("lane_min_h"), 400)
        self.s.set("lane_min_h", -50)
        self.assertEqual(self.s.get("lane_min_h"), 70)
        self.s.set("dl_blocks", "abc")
        self.assertEqual(self.s.get("dl_blocks"), st.DEFAULTS["dl_blocks"])

    def test_illegal_theme_and_broken_file_fall_back(self):
        self.s.set("theme", "nonsense")
        self.assertEqual(self.s.get("theme"), "light")
        self.path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.s.load()
        self.assertEqual(self.s.get("tip_seconds"), st.DEFAULTS["tip_seconds"])

    def test_unknown_future_keys_survive(self):
        self.path.write_text(json.dumps({"tip_seconds": 9, "next_version_opt": {"a": 1}}),
                             encoding="utf-8")
        self.s.load()
        self.assertEqual(self.s.get("tip_seconds"), 9)
        self.s.set("theme", "dark")
        self.s.save()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["next_version_opt"], {"a": 1}, "不能把不认识的键写丢")

    def test_same_value_does_not_emit_again(self):
        seen: list[tuple[str, object]] = []
        self.s.changed.connect(lambda k, v: seen.append((k, v)))
        self.s.set("tip_seconds", 7)
        self.s.set("tip_seconds", 7)
        self.assertEqual(seen, [("tip_seconds", 7)], "同值回环会让控件反复抖动")

    def test_reset_restores_defaults(self):
        self.s.set("tip_seconds", 33)
        self.s.set("lane_min_h", 300)
        self.s.reset()
        for k, v in st.DEFAULTS.items():
            self.assertEqual(self.s.get(k), v, k)

    def test_save_leaves_no_temp_files(self):
        self.s.set("tip_seconds", 3)
        leftovers = [p.name for p in Path(self._dir.name).iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_tip_msec_mapping(self):
        self.s.set("tip_seconds", 0)
        self.assertEqual(self.s.tip_msec(), st.TIP_FOREVER_MS, "0 秒＝不自动消失")
        self.s.set("tip_seconds", 8)
        self.assertEqual(self.s.tip_msec(), 8000)
        self.s.set("tip_delay_ms", 120)
        self.assertEqual(self.s.tip_wake_ms(), 120)


class TestShowTip(_TempSettings):
    def _capture(self):
        calls: list[tuple] = []
        fake = staticmethod(lambda *a, **k: calls.append(a))
        return calls, mock.patch.object(QtWidgets.QToolTip, "showText", fake)

    def test_msec_comes_from_settings(self):
        self.s.set("tip_seconds", 8)
        calls, patcher = self._capture()
        with patcher:
            st.show_tip(None, "提示内容")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "提示内容")
        self.assertEqual(calls[0][4], 8000)

    def test_zero_means_stay_until_mouse_leaves(self):
        self.s.set("tip_seconds", 0)
        calls, patcher = self._capture()
        with patcher:
            st.show_tip(None, "多行\n事件列表")
        self.assertEqual(calls[0][4], st.TIP_FOREVER_MS)

    def test_empty_text_is_not_shown(self):
        calls, patcher = self._capture()
        with patcher:
            st.show_tip(None, "")
            st.show_tip(None, "   ")      # 空白也算内容，交给调用方决定；只测真空
        self.assertEqual(len(calls), 1)

    def test_hide_only_when_visible(self):
        hidden: list[int] = []
        with mock.patch.object(QtWidgets.QToolTip, "isVisible", staticmethod(lambda: False)), \
                mock.patch.object(QtWidgets.QToolTip, "hideText",
                                  staticmethod(lambda: hidden.append(1))):
            st.hide_tip()
        self.assertEqual(hidden, [])
        with mock.patch.object(QtWidgets.QToolTip, "isVisible", staticmethod(lambda: True)), \
                mock.patch.object(QtWidgets.QToolTip, "hideText",
                                  staticmethod(lambda: hidden.append(1))):
            st.hide_tip()
        self.assertEqual(hidden, [1])


class TestTipStyle(_TempSettings):
    def test_proxy_honours_wake_up_delay(self):
        style = st._TipStyle()          # 不 setStyle，避免影响同进程其它用例
        self.s.set("tip_delay_ms", 1234)
        got = style.styleHint(QtWidgets.QStyle.StyleHint.SH_ToolTip_WakeUpDelay)
        self.assertEqual(int(got), 1234)

    def test_proxy_passes_other_hints_through(self):
        style = st._TipStyle()
        a = style.styleHint(QtWidgets.QStyle.StyleHint.SH_ToolTip_WakeUpDelay)
        b = style.styleHint(QtWidgets.QStyle.StyleHint.SH_ScrollBar_Transient)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)

    def test_proxy_never_raises_on_garbage_hint(self):
        style = st._TipStyle()
        with mock.patch.object(st, "settings", side_effect=RuntimeError("boom")):
            self.assertIsNotNone(style.styleHint(QtWidgets.QStyle.StyleHint.SH_ToolTip_WakeUpDelay))


class TestSettingsDialog(_TempSettings):
    def setUp(self):
        super().setUp()
        self.dlg = st.SettingsDialog()
        pump(self.app)

    def tearDown(self):
        d, self.dlg = self.dlg, None
        d.deleteLater()
        pump(self.app, 4)
        super().tearDown()

    def test_all_controls_present(self):
        for key in ("tip_seconds", "tip_delay_ms", "tip_font_pt", "lane_min_h",
                    "theme", "badge_tol_px", "refine_debounce_ms", "dl_blocks"):
            self.assertIn(key, self.dlg._widgets, key)
        self.assertEqual(self.dlg._widgets["tip_seconds"].specialValueText(),
                         "不自动消失（移开才关）")

    def test_editing_a_widget_writes_the_file(self):
        self.dlg._widgets["tip_seconds"].setValue(17)
        pump(self.app)
        self.assertEqual(self.s.get("tip_seconds"), 17)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["tip_seconds"], 17)

    def test_reset_button_restores_widgets(self):
        self.dlg._widgets["lane_min_h"].setValue(300)
        self.dlg._widgets["theme"].setCurrentIndex(max(0, self.dlg._widgets["theme"].findData("dark")))
        pump(self.app)
        self.assertEqual(self.s.get("lane_min_h"), 300)
        for btn in self.dlg.findChildren(QtWidgets.QPushButton):
            if btn.text() == "恢复默认":
                btn.click()
                break
        pump(self.app)
        self.assertEqual(self.s.get("lane_min_h"), st.DEFAULTS["lane_min_h"])
        self.assertEqual(self.dlg._widgets["lane_min_h"].value(), st.DEFAULTS["lane_min_h"])
        self.assertEqual(self.s.get("theme"), "light")

    def test_external_change_refreshes_dialog(self):
        self.s.set("theme", "dark")
        pump(self.app)
        self.assertEqual(self.dlg._widgets["theme"].currentData(), "dark")
        self.s.set("lane_min_h", 210)
        pump(self.app)
        self.assertEqual(self.dlg._widgets["lane_min_h"].value(), 210)

    def test_cache_size_line_is_filled(self):
        self.assertIn("当前占用", self.dlg.lbl_cache.text())

    def test_clear_cache_asks_first(self):
        cache = Path(self._dir.name) / "cache" / "abc123"
        cache.mkdir(parents=True)
        (cache / "ANG.npz").write_bytes(b"x" * 4096)
        with mock.patch.object(st, "cache_dir", lambda: cache.parent), \
                mock.patch.object(QtWidgets.QMessageBox, "exec", staticmethod(lambda: 0)):
            self.dlg._clear_cache()
        self.assertTrue(cache.exists(), "没点“是”之前一个文件都不能删")

    def test_clear_cache_removes_log_cache_dirs(self):
        cache = Path(self._dir.name) / "cache"
        for name in ("aaa", "bbb"):
            d = cache / name
            d.mkdir(parents=True)
            (d / "ANG.npz").write_bytes(b"y" * 2048)
        (cache / "loose.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(st, "cache_dir", lambda: cache):
            n, b = st.clear_parse_cache()
        self.assertEqual((n, b), (2, 4096))
        self.assertFalse((cache / "aaa").exists())
        self.assertTrue((cache / "loose.json").exists(), "只清每日志一个目录的解析缓存")


class TestMainWindowWiring(_TempSettings):
    def setUp(self):
        super().setUp()
        from apmlog.app import MainWindow
        self.w = MainWindow()
        pump(self.app)

    def tearDown(self):
        w, self.w = self.w, None
        w.map.close()            # 不先关 TileStore 会在退出时段错误（/map 实测）
        w.close()
        w.deleteLater()
        pump(self.app, 6)
        super().tearDown()

    def test_settings_action_in_toolbar(self):
        tb = self.w.findChildren(QtWidgets.QToolBar)[0]
        self.assertIn("设置", [a.text() for a in tb.actions() if a.text()])

    def test_settings_push_into_widgets(self):
        self.s.set("lane_min_h", 200)
        pump(self.app)
        self.assertEqual(self.w.lane_h_box.value(), 200)
        self.assertEqual(self.w.plot.lane_min_height(), 200)
        self.s.set("theme", "dark")
        pump(self.app)
        self.assertEqual(self.w.plot.theme(), "dark")
        self.s.set("badge_tol_px", 22.0)
        self.assertAlmostEqual(self.w.plot.badge_tol_px(), 22.0)
        self.s.set("refine_debounce_ms", 900)
        self.assertEqual(self.w.plot.refine_debounce_ms(), 900)

    def test_spinbox_and_theme_write_back(self):
        self.w.lane_h_box.setValue(148)
        pump(self.app)
        self.assertEqual(self.s.get("lane_min_h"), 148)
        self.w.plot.set_theme("dark")
        self.assertEqual(self.s.get("theme"), "dark")

    def test_plot_starts_from_settings(self):
        self.s.set("lane_min_h", 233)
        self.s.set("theme", "dark")
        self.s.set("badge_tol_px", 15.0)
        st.Settings.set_instance(None)
        fresh = st.Settings(self.path)
        st.Settings.set_instance(fresh)
        from apmlog.app import MainWindow
        w2 = MainWindow()
        pump(self.app)
        try:
            self.assertEqual(w2.plot.lane_min_height(), 233)
            self.assertEqual(w2.plot.theme(), "dark")
            self.assertAlmostEqual(w2.plot.badge_tol_px(), 15.0)
            self.assertEqual(w2.lane_h_box.value(), 233, "曲线页的 spinbox 初值也要跟着设置")
        finally:
            w2.map.close()
            w2.close()
            w2.deleteLater()
            pump(self.app, 6)

    def test_leaving_the_chart_hides_a_sticky_tip(self):
        """留存设成“不自动消失”后，鼠标离开组件必须主动收掉，否则读数会挂在图上。"""
        hidden: list[int] = []
        with mock.patch.object(QtWidgets.QToolTip, "isVisible", staticmethod(lambda: True)), \
                mock.patch.object(QtWidgets.QToolTip, "hideText",
                                  staticmethod(lambda: hidden.append(1))):
            QtWidgets.QApplication.sendEvent(self.w.plot,
                                             QtCore.QEvent(QtCore.QEvent.Type.Leave))
            pump(self.app)
        self.assertEqual(hidden, [1])

    def test_legacy_views_json_lane_h_migrates_once(self):
        views = Path(self._dir.name) / "views.json"
        self.w._views_path = lambda: views          # type: ignore[assignment]
        views.write_text(json.dumps({"preset": self.w._preset, "lane_h": 240}), encoding="utf-8")
        self.w._restore_view()
        pump(self.app)
        self.assertEqual(self.s.get("lane_min_h"), 240)
        self.assertEqual(self.w.lane_h_box.value(), 240)

    def test_forget_view_keeps_global_lane_height(self):
        self.s.set("lane_min_h", 190)
        pump(self.app)
        self.w._forget_view()
        pump(self.app)
        self.assertEqual(self.w.lane_h_box.value(), 190,
                         "每道最小高是全局设置，不该被“清除视图记忆”顺手改掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
