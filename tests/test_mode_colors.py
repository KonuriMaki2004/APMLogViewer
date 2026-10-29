# -*- coding: utf-8 -*-
"""tests/test_mode_colors.py —— 时间线飞行模式配色（按模式而不是按段）。

用户 2026-10-24：「为每种模式分配专属颜色：同一模式在所有时间段同色、不同模式可区分；
默认按操作难度由低到高在色相与明度上递进；色块/分段边界/当前选中状态都要清晰；
设置里按模式列名字+颜色，点色块可自定义、立即生效并持久化，另有恢复默认；
自定义只影响该模式，相邻太像时靠分段边界或描边保持可读。」
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

from PySide6 import QtGui, QtWidgets                            # noqa: E402

from apmlog.ui import modecolors as mc                          # noqa: E402
from apmlog.ui import settings as st                            # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app, times: int = 6) -> None:
    for _ in range(times):
        app.processEvents()


def _timeline(modes, colors=None):
    from apmlog.app import MiniTimeline
    tl = MiniTimeline()
    tl.resize(900, 110)
    tl.show()
    tl.set_span(0.0, 100.0)
    tl.set_modes(modes)
    tl.set_mode_colors(colors if colors is not None else mc.palette())
    pump(qapp())
    return tl


class TestDefaultPalette(unittest.TestCase):
    """默认方案：按难度阶梯，色相 + 明度递进，两两可区分。"""

    def test_ladder_and_colors_line_up(self) -> None:
        self.assertEqual(len(mc.MODE_LADDER), len(mc.MODE_LADDER_COLORS))
        self.assertEqual([n for n, _c in mc.MODE_LADDER_COLORS], list(mc.MODE_LADDER))

    def test_every_mode_has_a_unique_color(self) -> None:
        cols = [mc.default_color(m) for m in mc.MODE_LADDER]
        self.assertEqual(len(set(cols)), len(cols), "不同模式必须不同色")

    def test_neighbours_are_far_enough_apart(self) -> None:
        ds = [mc.color_distance(mc.default_color(a), mc.default_color(b))
              for a, b in zip(mc.MODE_LADDER, mc.MODE_LADDER[1:])]
        self.assertGreaterEqual(min(ds), mc.TOO_CLOSE, "默认相邻两色不该触发「太像」的加重描边")

    def test_hue_and_lightness_progress(self) -> None:
        """难度由低到高：色相整体从青绿走向红（h 递减）。"""
        def h_of(hexv: str) -> float:
            c = QtGui.QColor(hexv)
            return float(c.hue())
        hs = [h_of(mc.default_color(m)) for m in mc.MODE_LADDER]
        # 允许明度交替带来的小波动，但首尾必须明显不同且总体递减
        self.assertGreater(hs[0], 150.0, "最简单（LOITER）应在青绿一侧")
        self.assertLess(hs[-1], 30.0, "最难（TURTLE）应在红一侧")
        drops = sum(1 for a, b in zip(hs, hs[1:]) if b <= a + 40)
        self.assertGreaterEqual(drops, len(hs) - 2, f"色相应整体递减: {hs}")

    def test_unknown_mode_is_stable_and_off_ladder(self) -> None:
        a = mc.default_color("ZIGZAG")
        self.assertEqual(a, mc.default_color("zigzag"), "同一未知模式必须恒定")
        self.assertIn(a, mc.EXTRA_COLORS)
        self.assertNotIn(a, [mc.default_color(m) for m in mc.MODE_LADDER][:3])

    def test_ink_contrast(self) -> None:
        self.assertEqual(mc.ink_on("#ffffff"), "#101418", "亮底写深字")
        self.assertEqual(mc.ink_on("#101418"), "#ffffff", "暗底写白字")


class TestOverrides(unittest.TestCase):
    def test_override_only_affects_that_mode(self) -> None:
        ov = {"LOITER": "#123456"}
        self.assertEqual(mc.resolve("LOITER", ov), "#123456")
        self.assertEqual(mc.resolve("AUTO", ov), mc.default_color("AUTO"))

    def test_bad_override_falls_back(self) -> None:
        self.assertEqual(mc.resolve("LOITER", {"LOITER": "nope"}), mc.default_color("LOITER"))

    def test_roundtrip_and_garbage(self) -> None:
        txt = mc.dump_overrides({"ACRO": "#ABCDEF", "LOITER": "#112233"})
        self.assertEqual(mc.parse_overrides(txt), {"ACRO": "#abcdef", "LOITER": "#112233"})
        self.assertEqual(mc.parse_overrides("{not json"), {})
        # 注意："bad" 是合法的 3 位简写（#bbaadd），真正非法的要像 "nope" 这样
        self.assertEqual(mc.parse_overrides('{"X": "nope"}'), {})
        self.assertEqual(mc.normalize_hex("bad"), "#bbaadd", "支持 3 位简写")

    def test_too_close_detects_user_mistakes(self) -> None:
        self.assertTrue(mc.too_close("#88cfdd", "#88cfdd"))
        self.assertFalse(mc.too_close(mc.default_color("LOITER"), mc.default_color("AUTO")))


class TestSettingsPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mcs_"))
        self.path = self.dir / "settings.json"

    def test_set_persist_reload_reset(self) -> None:
        s = st.Settings(self.path)
        self.assertEqual(s.mode_color("LOITER"), mc.default_color("LOITER"))
        s.set_mode_color("LOITER", "#123456")
        self.assertEqual(s.mode_color("LOITER"), "#123456")
        self.assertEqual(st.Settings(self.path).mode_color("LOITER"), "#123456",
                         "重启后（重新读设置文件）必须还在")
        again = st.Settings(self.path)
        again.reset_mode_colors()
        self.assertEqual(again.mode_color("LOITER"), mc.default_color("LOITER"))
        self.assertEqual(st.Settings(self.path).mode_color("LOITER"),
                         mc.default_color("LOITER"), "恢复默认也要落盘")

    def test_invalid_color_is_ignored(self) -> None:
        s = st.Settings(self.path)
        s.set_mode_color("AUTO", "#28ae4a")
        s.set_mode_color("AUTO", "不是颜色")
        self.assertEqual(s.mode_color("AUTO"), "#28ae4a", "非法输入不该把颜色清掉")

    def test_palette_includes_log_modes(self) -> None:
        s = st.Settings(self.path)
        pal = s.mode_color_palette(["ZIGZAG", "LOITER"])
        self.assertIn("ZIGZAG", pal)
        self.assertIn("LOITER", pal)
        self.assertEqual(pal["LOITER"], mc.default_color("LOITER"))


class TestTimelineBand(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_same_mode_same_color_any_segment(self) -> None:
        tl = _timeline([(0, 20, "LOITER"), (20, 55, "AUTO"),
                        (55, 70, "LOITER"), (70, 100, "TURTLE")])
        self.assertEqual(tl.mode_color_of("LOITER"), mc.default_color("LOITER"))
        hit_names = [n for _x0, _x1, n, _a, _b in tl._mode_hits]
        self.assertEqual(hit_names.count("LOITER"), 2, "LOITER 出现两段")

    def test_different_modes_differ(self) -> None:
        tl = _timeline([(0, 50, "LOITER"), (50, 100, "TURTLE")])
        self.assertNotEqual(tl.mode_color_of("LOITER"), tl.mode_color_of("TURTLE"))

    def test_custom_color_applies_immediately(self) -> None:
        tl = _timeline([(0, 50, "AUTO"), (50, 100, "LOITER")])
        tl.set_mode_colors(mc.palette({"AUTO": "#123456"}))
        self.assertEqual(tl.mode_color_of("AUTO"), "#123456")
        self.assertEqual(tl.mode_color_of("LOITER"), mc.default_color("LOITER"),
                         "只改指定模式")

    def test_unknown_mode_gets_a_color(self) -> None:
        tl = _timeline([(0, 50, "ZIGZAG")])
        self.assertIn(tl.mode_color_of("ZIGZAG"), mc.EXTRA_COLORS)

    def test_current_mode_tracks_the_cursor(self) -> None:
        tl = _timeline([(0, 20, "LOITER"), (20, 55, "AUTO"), (55, 100, "TURTLE")])
        self.assertEqual(tl.current_mode(10.0), "LOITER")
        self.assertEqual(tl.current_mode(30.0), "AUTO")
        tl.set_time(80.0)
        self.assertEqual(tl.current_mode(), "TURTLE", "不给参数时用播放指针")
        self.assertEqual(tl.current_mode(500.0), "")

    def test_boundary_is_always_drawn_and_thickened_when_too_close(self) -> None:
        tl = _timeline([(0, 50, "AUTO"), (50, 100, "LOITER")])
        self.assertEqual(tl.mode_boundary_widths(), [1.0, 1.0], "默认配色不需要加粗")
        same = "#88cfdd"
        tl.set_mode_colors({"AUTO": same, "LOITER": same})
        w = tl.mode_boundary_widths()
        self.assertEqual(w[0], 1.0, "第一段没有左侧邻居")
        self.assertEqual(w[1], 2.0, "两段同色时必须加粗边界，否则分不清")

    def test_paint_does_not_raise(self) -> None:
        tl = _timeline([(0, 33, "AUTO"), (33, 66, "ZIGZAG"), (66, 100, "TURTLE")])
        tl.set_time(50.0)
        pump(self.app)
        pm = tl.grab()
        self.assertFalse(pm.isNull())


class TestSettingsDialogGroup(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="mcdlg_")
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self.dir) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        from apmlog.ui.settings import SettingsDialog
        self.dlg = SettingsDialog()
        self.dlg.show()
        pump(self.app)

    def tearDown(self) -> None:
        self.dlg.close()
        pump(self.app)

    def test_all_modes_listed_with_swatches(self) -> None:
        self.assertEqual(len(self.dlg._mode_btns), len(mc.MODE_LADDER))
        for name in mc.MODE_LADDER:
            self.assertIn(mc.default_color(name), self.dlg._mode_btns[name].styleSheet())
        self.assertFalse(self.dlg.btn_mode_reset.isEnabled(), "还没自定义时无需恢复默认")

    def test_pick_color_updates_swatch_and_settings(self) -> None:
        col = QtGui.QColor("#123456")
        with mock.patch.object(QtWidgets.QColorDialog, "getColor",
                               staticmethod(lambda *a, **k: col)):
            self.dlg._pick_mode_color("AUTO")
        pump(self.app)
        s = st.Settings.instance()
        self.assertEqual(s.mode_color("AUTO"), "#123456")
        self.assertIn("#123456", self.dlg._mode_btns["AUTO"].styleSheet())
        self.assertTrue(self.dlg.btn_mode_reset.isEnabled())
        self.assertIn("1", self.dlg.lbl_mode_custom.text())
        self.assertEqual(s.mode_color("LOITER"), mc.default_color("LOITER"),
                         "只改被点的那一个")

    def test_cancel_keeps_original(self) -> None:
        prev = st.Settings.instance().mode_color("AUTO")
        with mock.patch.object(QtWidgets.QColorDialog, "getColor",
                               staticmethod(lambda *a, **k: QtGui.QColor())):
            self.dlg._pick_mode_color("AUTO")
        self.assertEqual(st.Settings.instance().mode_color("AUTO"), prev)

    def test_reset_button_restores_defaults(self) -> None:
        st.Settings.instance().set_mode_color("ACRO", "#123456")
        pump(self.app)
        self.assertTrue(self.dlg.btn_mode_reset.isEnabled())
        self.dlg._reset_mode_colors()
        pump(self.app)
        self.assertEqual(st.Settings.instance().mode_color("ACRO"), mc.default_color("ACRO"))
        self.assertFalse(self.dlg.btn_mode_reset.isEnabled())
        self.assertIn(mc.default_color("ACRO"), self.dlg._mode_btns["ACRO"].styleSheet())

    def test_grid_is_multi_column_and_has_no_explainer(self) -> None:
        """用户 2026-10-24：色块要多列排；原来那段说明文字要删掉。"""
        rows = {b.y() for b in self.dlg._mode_btns.values()}
        per_row = len(self.dlg._mode_btns) // max(1, len(rows))
        self.assertGreaterEqual(per_row, 3, f"每行至少 3 个色块（现在 {per_row}）")
        self.assertLessEqual(len(rows), 6, "18 个模式最多 6 行")
        from apmlog.ui.settings import SettingsDialog as _SD
        texts = [l.text() for l in self.dlg.findChildren(QtWidgets.QLabel)]
        self.assertFalse(any("难度" in (t or "") for t in texts),
                         "模式颜色的说明文字应已删除")
        self.assertTrue(_SD is not None)

    def test_external_change_refreshes_the_panel(self) -> None:
        st.Settings.instance().set_mode_color("LAND", "#abcdef")
        pump(self.app)
        self.assertIn("#abcdef", self.dlg._mode_btns["LAND"].styleSheet())


class TestAppWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_setting_change_pushes_colors_to_timeline(self) -> None:
        from apmlog.app import MainWindow
        d = tempfile.mkdtemp(prefix="mcap_")
        prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(d) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(prev))
        w = MainWindow()
        self.addCleanup(w.close)
        w.timeline.set_span(0.0, 10.0)
        w.timeline.set_modes([(0, 10, "AUTO")])
        st.Settings.instance().set_mode_color("AUTO", "#112233")   # 会走 _apply_setting
        pump(self.app)
        self.assertEqual(w.timeline.mode_color_of("AUTO"), "#112233")


if __name__ == "__main__":
    unittest.main()
