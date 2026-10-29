# -*- coding: utf-8 -*-
"""tests/test_gauges.py —— 地图左下角仪表：航向罗盘 + 姿态球。

覆盖三件最容易出错的事：
1. 姿态查表按自己的时间轴走（和航迹不同长），拿不到就近点宁可显示“无”；
2. 浮层几何：贴左下角、不出视口、不和右下角版权条打架；
3. 形状真画到像素上（fillPath 在 PySide6 6.12 静默不画，只测“不崩”抱不住）。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_gauges.py -q
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                              # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets                    # noqa: E402

from apmlog.ui import settings as st                            # noqa: E402
from apmlog.ui import theme as T                                # noqa: E402
from apmlog.ui import trackmap as TM                            # noqa: E402
from apmlog.ui.gauges import (AltitudeTape, AttitudeDial, CompassDial, GaugePanel,   # noqa: E402
                              FACE, alt_step, compass_ink, wrap_deg)

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 20) -> None:
    for _ in range(times):
        app.processEvents()


def settle(app: QtWidgets.QApplication, seconds: float = 0.25) -> None:
    """跑真实事件循环：浮层定位挂在 60ms 防抖的图层刷新上。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.004)


def _pixels(pm: QtGui.QPixmap, pred) -> int:
    im = pm.toImage()
    return sum(1 for y in range(0, im.height(), 2)
               for x in range(0, im.width(), 2) if pred(im.pixel(x, y)))


def _light_in(img: QtGui.QImage, x0: int, y0: int, x1: int, y1: int) -> int:
    """数矩形里的亮像素（高度带读数框是浅底深字，盘面是深底）。"""
    return sum(1 for y in range(y0, y1) for x in range(x0, x1)
               if QtGui.qRed(img.pixel(x, y)) > 200
               and QtGui.qGreen(img.pixel(x, y)) > 200
               and QtGui.qBlue(img.pixel(x, y)) > 200)


def _red(c: int) -> bool:
    return QtGui.qRed(c) > 150 and QtGui.qGreen(c) < 90 and QtGui.qBlue(c) < 90


def _yellow(c: int) -> bool:
    return QtGui.qRed(c) > 180 and QtGui.qGreen(c) > 150 and QtGui.qBlue(c) < 120


def _sky(c: int) -> bool:
    return QtGui.qBlue(c) > 150 and QtGui.qRed(c) < 110


class TestAltitudeTape(unittest.TestCase):
    """高度带：自适应刻度 + 中间读数框，都得真画到像素上。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_step_boundaries(self) -> None:
        for v, step in ((0.0, 5), (29.9, 5), (30.0, 10), (99.0, 10), (100.0, 20),
                        (299.0, 20), (300.0, 50), (999.0, 50), (1000.0, 100),
                        (-0.6, 5), (-29.0, 5), (-1000.0, 100)):
            self.assertEqual(alt_step(v), step, f"{v} m 的主刻度不该是 {alt_step(v)}")

    def test_value_roundtrip_nan_and_clear(self) -> None:
        t = AltitudeTape()
        self.addCleanup(t.deleteLater)
        self.assertTrue(math.isnan(t.altitude()))
        t.set_altitude(12.3, "航迹")
        self.assertAlmostEqual(t.altitude(), 12.3)
        self.assertEqual(t.source(), "航迹")
        t.set_altitude(float("nan"))
        self.assertTrue(math.isnan(t.altitude()), "nan 要能显式清掉")
        t.set_altitude(5.0, "气压")
        t.clear()
        self.assertTrue(math.isnan(t.altitude()))

    def test_dedupes_identical_updates(self) -> None:
        t = AltitudeTape()
        self.addCleanup(t.deleteLater)
        t.set_altitude(30.0, "航迹")
        paints = [0]
        t.update = lambda *a: paints.__setitem__(0, paints[0] + 1)   # type: ignore[assignment]
        t.set_altitude(30.0, "航迹")
        self.assertEqual(paints[0], 0, "播放时每帧都推同一个值，不该重绘")
        t.set_altitude(31.0, "航迹")
        self.assertEqual(paints[0], 1)

    def test_readout_band_is_light_only_with_data(self) -> None:
        t = AltitudeTape()
        self.addCleanup(t.deleteLater)
        t.set_altitude(100.0)
        img = t.grab().toImage()
        cy = img.height() // 2
        self.assertGreater(_light_in(img, 3, cy - 7, img.width() - 3, cy + 7), 80,
                           "中间读数框必须真画出来")
        t.clear()
        img2 = t.grab().toImage()
        self.assertEqual(_light_in(img2, 3, cy - 7, img2.width() - 3, cy + 7), 0,
                         "无数据时不能还挂着一个旧读数框")

    def test_scale_rolls_with_altitude(self) -> None:
        t = AltitudeTape()
        self.addCleanup(t.deleteLater)
        t.set_altitude(100.0)
        pump(self.app, 3)
        a = t.grab().toImage()
        t.set_altitude(108.0)
        pump(self.app, 3)
        b = t.grab().toImage()
        w = a.width()
        diff = sum(1 for y in range(14, a.height() // 2 - 12)
                   for x in range(w - 18, w - 1) if a.pixel(x, y) != b.pixel(x, y))
        self.assertGreater(diff, 20, "刻度必须随高度滚动，否则就是张死图")


class TestPaintedShapes(unittest.TestCase):
    """把“形状到底画没画出来”钉在像素上。

    起因：PySide6 6.12 的 QPainter.fillPath(path, QColor) 不报错也不画东西，
    罗盘箭头一度只剩白描边。光测数据/不崩是抱不住这类问题的。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_compass_dart_has_red_fill(self) -> None:
        on = CompassDial(diameter=120)
        self.addCleanup(on.deleteLater)
        on.set_value(20.0, "飞控")
        off = CompassDial(diameter=120)          # 无数据：只有 N 字母是红的
        self.addCleanup(off.deleteLater)
        gain = _pixels(on.grab(), _red) - _pixels(off.grab(), _red)
        self.assertGreater(gain, 20, f"机头箭头红填充没画出来（只多了 {gain} 个像素）")

    def test_attitude_roll_pointer_is_yellow(self) -> None:
        on = AttitudeDial(diameter=120)
        self.addCleanup(on.deleteLater)
        on.set_pose(-5.0, 15.0)
        off = AttitudeDial(diameter=120)
        self.addCleanup(off.deleteLater)
        gain = _pixels(on.grab(), _yellow) - _pixels(off.grab(), _yellow)
        self.assertGreater(gain, 10, "横滚指针/地平线机标的黄色没画出来")

    def test_horizon_is_clipped_to_the_disc(self) -> None:
        d = AttitudeDial(diameter=120)
        self.addCleanup(d.deleteLater)
        d.set_pose(0.0, 0.0)
        pm = d.grab()
        self.assertFalse(_sky(pm.toImage().pixel(3, 3)), "左上角应该是盘面色，天空溢出来就是裁切坏了")
        self.assertTrue(_sky(pm.toImage().pixel(60, 20)), "盘心上方应该是天空蓝")


class TestMath(unittest.TestCase):
    def test_wrap_deg(self) -> None:
        self.assertAlmostEqual(wrap_deg(370.0), 10.0)
        self.assertAlmostEqual(wrap_deg(-190.0), 170.0)
        self.assertAlmostEqual(wrap_deg(180.0), 180.0)
        self.assertTrue(math.isnan(wrap_deg(float("nan"))))

    def test_face_ink_is_readable_on_face(self) -> None:
        # 盘面上不许出现“白底白字”那类同色问题，这里反过来：深盘配亮墨
        self.assertGreaterEqual(T.contrast(compass_ink(), FACE), 4.5)


class TestDials(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_compass_value_and_clear(self) -> None:
        c = CompassDial()
        self.addCleanup(c.deleteLater)
        self.assertTrue(math.isnan(c.value()))
        c.set_value(155.7, "飞控")
        self.assertAlmostEqual(c.value(), 155.7)
        c.set_value(float("nan"))
        self.assertTrue(math.isnan(c.value()), "nan 要能显式清掉，而不是停在旧值")

    def test_compass_dedupes_identical_updates(self) -> None:
        c = CompassDial()
        self.addCleanup(c.deleteLater)
        c.set_value(90.0, "飞控")
        paints = [0]
        c.update = lambda *a: paints.__setitem__(0, paints[0] + 1)   # type: ignore[assignment]
        c.set_value(90.0, "飞控")
        self.assertEqual(paints[0], 0, "播放时每帧都推同一个值，重复值不该再触发重绘")
        c.set_value(91.0, "飞控")
        self.assertEqual(paints[0], 1)

    def test_attitude_pose_roundtrip_and_nan(self) -> None:
        a = AttitudeDial()
        self.addCleanup(a.deleteLater)
        a.set_pose(-12.5, 30.0)
        self.assertEqual(a.pose(), (-12.5, 30.0))
        a.set_pose(float("nan"), 5.0)
        pi, ro = a.pose()
        self.assertTrue(math.isnan(pi), "nan 俯仰要能单独清掉")
        self.assertEqual(ro, 5.0)

    def test_dials_paint_without_crashing(self) -> None:
        for cls, args in ((CompassDial, (155.7,)), (AttitudeDial, None)):
            d = cls()
            self.addCleanup(d.deleteLater)
            if args:
                d.set_value(*args, "飞控")
            else:
                d.set_pose(*(-4.0, 12.0))
            img = d.grab()
            self.assertGreater(img.width(), 40)
            d.clear()
            self.assertFalse(img.isNull())


class TestPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_readout_text_includes_source_tag(self) -> None:
        g = GaugePanel()
        self.addCleanup(g.deleteLater)
        g.set_pose(-3.4, 2.4, 188.1, "飞控")
        self.assertIn("航向 188°", g.lbl.text())
        self.assertIn("（飞控）", g.lbl.text())
        self.assertIn("俯仰 -3.4°", g.lbl.text())
        self.assertIn("横滚 2.4°", g.lbl.text())

    def test_no_data_state(self) -> None:
        g = GaugePanel()
        self.addCleanup(g.deleteLater)
        g.set_pose(1.0, 2.0, 3.0)
        g.set_no_data("该日志没有姿态报文")
        self.assertEqual(g.lbl.text(), "该日志没有姿态报文")
        self.assertTrue(math.isnan(g.compass.value()))

    def test_collapse_hides_dials_and_emits(self) -> None:
        g = GaugePanel()
        self.addCleanup(g.deleteLater)
        fired: list[bool] = []
        g.collapseToggled.connect(fired.append)
        full = g.sizeHint().height()
        g.btn_fold.click()
        self.assertTrue(g.collapsed())
        self.assertFalse(g.compass.isVisibleTo(g))
        self.assertLess(g.sizeHint().height(), full)
        self.assertEqual(fired, [True])
        g.btn_fold.click()
        self.assertFalse(g.collapsed())
        self.assertEqual(fired, [True, False])

    def test_no_3d_leftovers(self) -> None:
        """3D 预览已按讨论砍掉：控件、信号、设置键都不应再存在。"""
        g = GaugePanel()
        self.addCleanup(g.deleteLater)
        self.assertFalse(hasattr(g, "btn_3d"))
        self.assertFalse(hasattr(g, "threeDToggled"))
        self.assertFalse(hasattr(g, "three_d"))
        self.assertFalse(hasattr(g, "set_3d_enabled"))
        self.assertNotIn("gauge_3d", st.DEFAULTS)
        import apmlog.ui.trackmap as tm
        self.assertFalse(hasattr(tm.TrackMapWidget, "threeDToggled"))


class TestPanelAltitude(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_tape_is_in_the_row_and_counts_in_size(self) -> None:
        p = GaugePanel()
        self.addCleanup(p.deleteLater)
        self.assertIsInstance(p.altitude, AltitudeTape)
        sh = p.sizeHint()
        self.assertGreaterEqual(sh.width(),
                                p.compass.width() + p.attitude.width() + p.altitude.width())

    def test_set_altitude_forwards_without_touching_the_readout(self) -> None:
        p = GaugePanel()
        self.addCleanup(p.deleteLater)
        p.set_pose(-3.0, 5.0, 156.0, "飞控")
        before = p.lbl.text()
        p.set_altitude(88.5, "航迹")
        self.assertAlmostEqual(p.altitude.altitude(), 88.5)
        self.assertEqual(p.lbl.text(), before, "高度显示在带子上，不该挤占读数行")

    def test_pose_missing_keeps_altitude(self) -> None:
        """没姿态不等于没高度：set_pose_missing 只能动罗盘和姿态球。"""
        p = GaugePanel()
        self.addCleanup(p.deleteLater)
        p.set_altitude(42.0, "航迹")
        p.set_pose_missing()
        self.assertTrue(math.isnan(p.compass.value()))
        self.assertAlmostEqual(p.altitude.altitude(), 42.0, msg="高度带被误清了")
        self.assertIn("没有姿态", p.lbl.text())

    def test_set_no_data_clears_everything(self) -> None:
        p = GaugePanel()
        self.addCleanup(p.deleteLater)
        p.set_pose(-3.0, 5.0, 156.0, "飞控")
        p.set_altitude(42.0, "航迹")
        p.set_no_data()
        self.assertTrue(math.isnan(p.compass.value()))
        self.assertTrue(math.isnan(p.altitude.altitude()))

    def test_collapse_hides_the_tape_too(self) -> None:
        p = GaugePanel()
        p.show()
        self.addCleanup(p.deleteLater)
        settle(self.app, 0.1)
        p.set_collapsed(True)
        self.assertFalse(p.altitude.isVisible())
        p.set_collapsed(False)
        self.assertTrue(p.altitude.isVisible())


class _MapCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.m = TM.TrackMapWidget()
        self.m.resize(920, 620)
        self.m.show()
        settle(self.app)
        self.addCleanup(self._kill)

    def _kill(self) -> None:
        self.m.close()
        pump(self.app, 5)
        self.m.deleteLater()
        pump(self.app, 5)

    def feed(self, n: int = 101, t1: float = 100.0) -> None:
        t = np.linspace(0.0, t1, n)
        self.m.set_attitude(t, np.full(n, -5.0), np.full(n, 8.0), np.linspace(0, 359, n))


class TestMapIntegration(_MapCase):
    def test_panel_sits_bottom_left_inside_viewport(self) -> None:
        vp = self.m.view.viewport().size()
        g = self.m.gauges
        self.assertEqual(g.pos().x(), self.m.GAUGE_MARGIN)
        self.assertEqual(g.pos().y(), vp.height() - g.height() - self.m.GAUGE_MARGIN)
        self.assertLessEqual(g.geometry().bottom(), vp.height())
        # 右下角版权条不能被仪表压住
        self.assertLess(g.geometry().right(), self.m._attr.geometry().left())

    def test_attitude_lookup_follows_its_own_time_axis(self) -> None:
        self.feed(n=101, t1=100.0)
        self.m.set_time(50.0)
        self.assertAlmostEqual(self.m.gauges.compass.value(), 180.0, places=0)
        self.assertAlmostEqual(self.m.gauges.attitude.pose()[0], -5.0)
        self.assertIn("（飞控）", self.m.gauges.lbl.text())
        self.m.set_time(10.0)
        self.assertAlmostEqual(self.m.gauges.compass.value(), 36.0, places=0)

    def test_stale_lookup_falls_back_instead_of_lying(self) -> None:
        self.feed(n=101, t1=100.0)
        self.m.set_time(999.0)                    # 远超姿态时间线覆盖范围
        self.assertNotIn("（飞控）", self.m.gauges.lbl.text())

    def test_empty_attitude_shows_note(self) -> None:
        self.m.set_attitude([], [], [], [])
        self.assertEqual(self.m._att_t.size, 0)
        self.assertIn("没有姿态", self.m.gauges.lbl.text())

    def test_ragged_arrays_truncate_to_shortest(self) -> None:
        t = np.linspace(0, 100, 51)
        self.m.set_attitude(t, np.full(51, 1.0), np.full(40, 2.0), np.full(51, 3.0))
        self.assertEqual(self.m._att_t.size, 40)
        self.assertEqual(self.m._att_roll.size, 40)

    def test_hidden_panel_skips_updates(self) -> None:
        self.feed()
        self.m.set_gauges_visible(False)
        settle(self.app, 0.1)
        before = self.m.gauges.lbl.text()
        self.m.set_time(20.0)
        self.assertEqual(self.m.gauges.lbl.text(), before)
        self.m.set_gauges_visible(True)
        settle(self.app, 0.1)
        self.m.set_time(20.0)
        self.assertIn("航向", self.m.gauges.lbl.text())

    def test_set_time_after_attitude_arrives_uses_last_time(self) -> None:
        self.m.set_time(30.0)                     # 先给时间，再喂数据（加载顺序反过来也要对）
        self.feed()
        self.assertAlmostEqual(self.m.gauges.compass.value(), 108.0, places=0)

    def test_no_3d_signal_on_the_map(self) -> None:
        self.assertFalse(hasattr(self.m, "threeDToggled"))
        self.assertFalse(hasattr(self.m.gauges, "btn_3d"))

    def test_scale_bar_moves_above_panel(self) -> None:
        """比例尺不能钻到仪表底下：可见时它要抬高一个面板高。"""
        vp = self.m.view.viewport().rect()
        y_shown = self.m.scale_bar_y(vp)
        self.m.set_gauges_visible(False)
        settle(self.app, 0.1)
        y_hidden = self.m.scale_bar_y(vp)
        self.m.set_gauges_visible(True)
        settle(self.app, 0.1)
        step = self.m.gauges.sizeHint().height() + 2 * self.m.GAUGE_MARGIN
        self.assertGreater(y_hidden, y_shown, "隐藏仪表时比例尺要回到底部")
        self.assertEqual(y_hidden - y_shown, step)
        self.assertGreater(y_shown, 0)


class TestMapAltitude(_MapCase):
    """高度从哪来：有航迹用航迹（和地图游标读数同源），没航迹才用备胎。"""

    def put_track(self, t, alt, idx) -> None:
        self.m._t = np.asarray(t, float)
        self.m._alt = np.asarray(alt, float)
        self.m._cursor_idx = idx

    def test_track_altitude_wins_over_the_fallback(self) -> None:
        self.m.set_altitude(np.linspace(0, 100, 11), np.full(11, 999.0), "气压")
        self.put_track([0, 1, 2, 3, 4], [1.0, 2.0, 3.0, 4.0, 5.0], 2)
        self.m._push_gauge(2.0)
        self.assertAlmostEqual(self.m.gauges.altitude.altitude(), 3.0, places=6)
        self.assertEqual(self.m.gauges.altitude.source(), "航迹",
                         "两处报不一样的数用户只会觉得其中一个错了")

    def test_fallback_used_when_no_track(self) -> None:
        t = np.linspace(0.0, 100.0, 11)
        self.m.set_altitude(t, t * 0.5, "气压")
        self.m._push_gauge(40.0)
        self.assertAlmostEqual(self.m.gauges.altitude.altitude(), 20.0, places=6)
        self.assertEqual(self.m.gauges.altitude.source(), "气压")

    def test_stale_fallback_shows_nothing(self) -> None:
        t = np.linspace(0.0, 100.0, 11)
        self.m.set_altitude(t, np.full(11, 50.0), "气压")
        self.m._push_gauge(400.0)
        self.assertTrue(math.isnan(self.m.gauges.altitude.altitude()),
                        "时间差太远宁可显示无，不能拿半分钟前的数骗人")

    def test_ragged_arrays_truncate(self) -> None:
        self.m.set_altitude(np.linspace(0, 10, 5), np.linspace(0, 10, 8), "气压")
        self.assertEqual(self.m._alt_t.size, 5)
        self.assertEqual(self.m._alt_v.size, 5)

    def test_empty_feed_clears_the_tape(self) -> None:
        self.m.set_altitude(np.linspace(0, 10, 5), np.full(5, 7.0), "气压")
        self.m.set_altitude([], [])
        self.assertTrue(math.isnan(self.m.gauges.altitude.altitude()))

    def test_missing_pose_does_not_kill_altitude(self) -> None:
        self.put_track([0, 1, 2, 3, 4], [10.0, 11.0, 12.0, 13.0, 14.0], 2)
        self.m._heading = np.empty(0)
        self.m._push_gauge(2.0)
        self.assertAlmostEqual(self.m.gauges.altitude.altitude(), 12.0, places=6)
        self.assertIn("没有姿态", self.m.gauges.lbl.text())

    def test_nan_track_altitude_falls_through(self) -> None:
        self.put_track([0, 1, 2], [1.0, float("nan"), 3.0], 1)
        self.m.set_altitude(np.linspace(0, 2, 3), np.array([1.0, 6.5, 3.0]), "气压")
        self.m._push_gauge(1.0)
        self.assertAlmostEqual(self.m.gauges.altitude.altitude(), 6.5, places=6,
                               msg="航迹高度是 nan 时必须落到备胎，不能报 nan")


class _NoTrack:
    """空航迹：让 _on_time 里“查高度/速度”那段安全跳过。"""

    n_points = 0
    t = []


class _Key:
    """真内核用 dataclass ChannelKey 做键；这里只要可哈希且带 field 就够。"""

    __slots__ = ("field",)

    def __init__(self, field: str) -> None:
        self.field = field

    def __hash__(self) -> int:
        return hash(self.field)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Key) and other.field == self.field


class _Store:
    """只实现 series_many 的假内核：按字段名给回 Series。"""

    def __init__(self, have_ang: bool = True, have_att: bool = False,
                 have_baro: bool = True, have_ctun: bool = True) -> None:
        self.have_ang, self.have_att = have_ang, have_att
        self.have_baro, self.have_ctun = have_baro, have_ctun
        self.calls: list[tuple[str, ...]] = []

    def series_many(self, keys, t0=None, t1=None, max_points=None):
        self.calls.append(tuple(str(k) for k in keys))
        out = {}
        for k in keys:
            ty, field = str(k).split(".")
            if ty == "ANG" and not self.have_ang:
                continue
            if ty == "ATT" and not self.have_att:
                continue
            if ty == "BARO" and not self.have_baro:
                continue
            if ty == "CTUN" and not self.have_ctun:
                continue
            t = np.linspace(0.0, 100.0, 11)
            v = np.linspace(0.0, 100.0, 11) + (1.0 if field == "Roll" else 0.0)
            out[_Key(field)] = SimpleNamespace(t=t, v=v)
        return out

    def close(self) -> None:
        pass

    def track(self):
        return _NoTrack()


class TestVisibilityCatchUp(unittest.TestCase):
    """“先加载、后显示”的补推：exe 自检、批量、从对话框选完文件都是这条路。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_load_while_hidden_then_show_catches_up(self) -> None:
        m = TM.TrackMapWidget()
        self.addCleanup(m.deleteLater)
        m.resize(900, 600)                       # 故意不 show
        t = np.linspace(0.0, 100.0, 101)
        m.set_attitude(t, np.full(101, -5.0), np.full(101, 8.0), np.linspace(0, 359, 101))
        m.set_time(50.0)
        self.assertTrue(math.isnan(m.gauges.compass.value()), "隐藏期间不必白做功")
        m.show()
        settle(self.app, 0.2)
        self.assertAlmostEqual(m.gauges.compass.value(), 180.0, places=0,
                               msg="显示出来必须补上当前时刻的姿态")
        self.assertIn("（飞控）", m.gauges.lbl.text())
        m.close()
        pump(self.app, 5)


class TestMainWindowFeed(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()
        st.install_tip_style(cls.app)

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_gauge_")
        self.addCleanup(self._dir.cleanup)
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self._dir.name) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.resize(1300, 860)
        self.w.show()
        settle(self.app, 0.2)
        self.seen = None
        self.real_set_attitude = self.w.map.set_attitude      # 留一份真的，给下面那条用例用
        self.w.map.set_attitude = lambda t, p, r, y: setattr(   # type: ignore[assignment]
            self, "seen", (np.asarray(t).size, np.asarray(y).size))
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.w.close()
        pump(self.app, 5)
        self.w.deleteLater()
        pump(self.app, 5)

    def test_prefers_ang(self) -> None:
        self.w.store = _Store(have_ang=True, have_att=True)
        self.w._feed_attitude()
        self.assertEqual(self.seen, (11, 11))
        self.assertEqual(self.w.store.calls[0][0], "ANG.Pitch", "ANG 优先于 ATT")

    def test_falls_back_to_att(self) -> None:
        self.w.store = _Store(have_ang=False, have_att=True)
        self.w._feed_attitude()
        self.assertEqual(self.seen, (11, 11))
        self.assertEqual(self.w.store.calls[0][0], "ANG.Pitch", "先试 ANG")
        self.assertEqual(self.w.store.calls[-1][0], "ATT.Pitch", "ANG 缺字段才退到 ATT")

    def test_no_attitude_message_clears_gauges(self) -> None:
        self.w.store = _Store(have_ang=False, have_att=False)
        self.w._feed_attitude()
        self.assertEqual(self.seen, (0, 0))

    def test_gauge_show_setting_controls_the_overlay(self) -> None:
        s = st.Settings.instance()
        self.assertTrue(bool(s.get("gauge_show")))
        s.set("gauge_show", False)
        pump(self.app)
        self.assertFalse(self.w.map.gauges.isVisible())
        s.set("gauge_show", True)
        pump(self.app)
        self.assertTrue(self.w.map.gauges.isVisible())

    def test_fresh_load_shows_attitude_not_a_false_no_data(self) -> None:
        """刚加载完、还没点过任何时刻：仪表不能谎说“没有姿态报文”。"""
        self.w.map.set_attitude = self.real_set_attitude
        self.w.store = _Store(have_ang=True)
        self.w._feed_attitude()
        pump(self.app)
        txt = self.w.map.gauges.lbl.text()
        self.assertIn("航向", txt)
        self.assertNotIn("没有姿态", txt)
        self.assertTrue(math.isfinite(self.w.map.gauges.compass.value()))

    def test_no_3d_handler_left_in_main_window(self) -> None:
        self.assertFalse(hasattr(self.w, "_on_3d_toggled"))
        s = st.Settings.instance()
        s.set("gauge_3d", True)                      # 旧配置残留也不能弄坏主窗
        pump(self.app)
        self.assertTrue(self.w.map.gauges.isVisible())


class TestMainWindowAltitude(unittest.TestCase):
    """主窗取高度：BARO 优先，没 BARO 用 CTUN，都没有就干净地空着。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()
        st.install_tip_style(cls.app)

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_alt_")
        self.addCleanup(self._dir.cleanup)
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self._dir.name) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.resize(1300, 860)
        self.w.show()
        settle(self.app, 0.2)
        self.seen = None
        self.w.map.set_altitude = lambda t, a, s="": setattr(   # type: ignore[assignment]
            self, "seen", (np.asarray(t).size, np.asarray(a).size, s))
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.w.close()
        pump(self.app, 5)
        self.w.deleteLater()
        pump(self.app, 5)

    def test_prefers_baro(self) -> None:
        self.w.store = _Store()
        self.w._feed_altitude()
        self.assertEqual(self.seen[:2], (11, 11))
        self.assertEqual(self.seen[2], "气压")
        self.assertEqual(self.w.store.calls[0][0], "BARO.Alt")

    def test_falls_back_to_ctun(self) -> None:
        self.w.store = _Store(have_baro=False)
        self.w._feed_altitude()
        self.assertEqual(self.seen[2], "EKF")
        self.assertEqual(self.w.store.calls[-1][0], "CTUN.Alt", "BARO 取不到才试 CTUN")

    def test_no_altitude_channel_clears_the_fallback(self) -> None:
        self.w.store = _Store(have_baro=False, have_ctun=False)
        self.w._feed_altitude()
        self.assertEqual(self.seen, (0, 0, ""), "没高度源就要把旧的清掉")

    def test_no_store_clears(self) -> None:
        self.w.store = None
        self.w._feed_altitude()
        self.assertEqual(self.seen, (0, 0, ""))

    def test_fresh_load_fills_the_tape_without_any_click(self) -> None:
        """加载完不点任何东西，高度带也得有数（和罗盘同一个修复）。"""
        self.w.map.set_altitude = TM.TrackMapWidget.set_altitude.__get__(self.w.map)
        self.w.store = _Store()
        self.w._feed_altitude()
        self.w._feed_attitude()
        pump(self.app)
        tape = self.w.map.gauges.altitude
        self.assertFalse(math.isnan(tape.altitude()), "刚加载完高度带不应为空")
        self.assertEqual(tape.source(), "气压", "假内核没航迹，应落到备胎源")


class TestSettingsKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_gauge_s_")
        self.addCleanup(self._dir.cleanup)
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self._dir.name) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        self.s = st.Settings.instance()

    def test_defaults(self) -> None:
        self.assertTrue(bool(self.s.get("gauge_show")))
        self.assertNotIn("gauge_3d", st.DEFAULTS)

    def test_retired_key_is_washed_but_unknown_keys_survive(self) -> None:
        import json
        p = Path(self._dir.name) / "retired.json"
        p.write_text(json.dumps({"gauge_3d": True, "gauge_show": False,
                                 "someone_future_key": 7}), encoding="utf-8")
        s2 = st.Settings(p)
        self.addCleanup(lambda: s2.deleteLater())
        self.assertFalse(bool(s2.get("gauge_show")), "正常键照旧读回")
        after = json.loads(p.read_text(encoding="utf-8"))
        self.assertNotIn("gauge_3d", after, "退役键要当场从文件里洗掉，不能下次写盘时又带回去")
        self.assertEqual(after.get("someone_future_key"), 7, "未知键仍要前向兼容地留着")

    def test_bool_coercion_is_truthiness(self) -> None:
        self.s.set("gauge_show", 0)
        self.assertFalse(bool(self.s.get("gauge_show")))
        self.s.set("gauge_show", "非空")
        self.assertTrue(bool(self.s.get("gauge_show")))

    def test_dialog_has_only_the_gauge_switch(self) -> None:
        d = st.SettingsDialog()
        self.addCleanup(d.deleteLater)
        self.assertIsInstance(d._widgets["gauge_show"], QtWidgets.QCheckBox)
        self.assertNotIn("gauge_3d", d._widgets, "设置页不应当还摆一个已被砍掉的功能")
        d._widgets["gauge_show"].setChecked(False)
        self.assertFalse(bool(self.s.get("gauge_show")))
        d._reload_widgets()
        self.assertFalse(d._widgets["gauge_show"].isChecked())


if __name__ == "__main__":
    unittest.main()


class TestBatteryReadout(unittest.TestCase):
    """仪表里的电压/电流行。

    用户 2026-10-24 的决策：地图上不再标“剩余电量偏低”（RemPct 是按电压推的估计值），
    改为在左下角直接把电压/电流这两个**原始测量量**摆出来。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _panel(self):
        g = GaugePanel()
        g.show()
        pump(self.app)
        self.addCleanup(lambda: (g.close(), pump(self.app, 4)))
        return g

    def test_shows_voltage_and_current(self) -> None:
        g = self._panel()
        g.set_battery(55.16, 28.7)
        pump(self.app, 4)
        self.assertTrue(g.lbl_elec.isVisible())
        self.assertEqual(g.lbl_elec.text(), "电压 55.16 V　电流 28.7 A")

    def test_voltage_only_when_current_missing(self) -> None:
        g = self._panel()
        g.set_battery(22.4, None)
        self.assertIn("22.40 V", g.lbl_elec.text())
        self.assertNotIn("电流", g.lbl_elec.text())

    def test_current_only_when_voltage_missing(self) -> None:
        g = self._panel()
        g.set_battery(None, 12.0)
        self.assertIn("12.0 A", g.lbl_elec.text())
        self.assertNotIn("电压", g.lbl_elec.text())

    def test_no_data_hides_the_row(self) -> None:
        g = self._panel()
        g.set_battery(50.0, 10.0)
        self.assertTrue(g.lbl_elec.isVisible())
        g.set_battery(None, None)
        self.assertFalse(g.lbl_elec.isVisible(), "没数就不要留一行占位")
        self.assertEqual(g.lbl_elec.text(), "")

    def test_nan_is_treated_as_missing(self) -> None:
        g = self._panel()
        g.set_battery(float("nan"), float("nan"))
        self.assertFalse(g.lbl_elec.isVisible())
        self.assertEqual(g.lbl_elec.text(), "")

    def test_collapsed_hides_and_expand_restores(self) -> None:
        g = self._panel()
        g.set_battery(50.0, 10.0)
        g.set_collapsed(True)
        self.assertFalse(g.lbl_elec.isVisible())
        g.set_collapsed(False)
        self.assertTrue(g.lbl_elec.isVisible())
        self.assertIn("50.00 V", g.lbl_elec.text())

    def test_expand_without_data_does_not_show_an_empty_row(self) -> None:
        g = self._panel()
        g.set_collapsed(True)
        g.set_collapsed(False)
        self.assertFalse(g.lbl_elec.isVisible())

    def test_no_data_clears_the_row(self) -> None:
        g = self._panel()
        g.set_battery(50.0, 10.0)
        g.set_no_data("该日志没有姿态报文")
        self.assertEqual(g.lbl_elec.text(), "")
        self.assertFalse(g.lbl_elec.isVisible())

    def test_panel_grows_to_fit_the_row(self) -> None:
        g = self._panel()
        before = g.sizeHint().height()
        g.set_battery(50.0, 10.0)
        self.assertGreater(g.sizeHint().height(), before, "多一行就要多给高度")

    def test_battery_does_not_disturb_pose_or_altitude(self) -> None:
        g = self._panel()
        g.set_pose(1.5, 7.1, 186.0, "飞控")
        g.set_altitude(120.0, "航迹")
        g.set_battery(55.0, 20.0)
        self.assertIn("航向", g.lbl.text())
        self.assertIn("186", g.lbl.text())
        self.assertIn("55.00", g.lbl_elec.text())
