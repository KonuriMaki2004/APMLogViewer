# -*- coding: utf-8 -*-
"""tests/test_seriesplot.py —— C 工作流自测（unittest，venv 里不依赖 pytest）。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m unittest discover -s tests -v
"""
from __future__ import annotations

import math
import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                              # noqa: E402

from apmlog.ui._decim import (EnvTower, bucket_envelope, clean_series, finite_stats,  # noqa: E402
                              is_monotonic, lttb, parse_key_tolerant)
from apmlog.ui.seriesplot import PALETTES, LaneSpec, SeriesPlot, _rng_bounds  # noqa: E402

from PySide6 import QtCore, QtGui, QtWidgets                    # noqa: E402

# 单测产物统一进 selftest/_unittest/，不污染共享的 selftest/ 交付目录
OUT = ROOT / "selftest" / "_unittest"
OUT.mkdir(parents=True, exist_ok=True)

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 3) -> None:
    for _ in range(times):
        app.processEvents()


def lane_viewport_point(w: SeriesPlot, idx: int, fx: float = 0.5, fy: float = 0.5) -> QtCore.QPoint:
    """把某泳道 ViewBox 内的相对位置换算成 viewport 坐标（发合成鼠标事件用）。"""
    vb = w._lanes[idx].plot.getViewBox()
    xr, yr = vb.viewRange()
    xv = float(xr[0]) + (float(xr[1]) - float(xr[0])) * fx
    yv = float(yr[0]) + (float(yr[1]) - float(yr[0])) * fy
    scene = vb.mapViewToScene(QtCore.QPointF(xv, yv))
    return w._graphics.mapFromScene(scene)


def send_mouse(w: SeriesPlot, kind: QtCore.QEvent.Type, pos: QtCore.QPoint,
               button: QtCore.Qt.MouseButton, mods: QtCore.Qt.KeyboardModifier,
               held: QtCore.Qt.MouseButton | None = None) -> bool:
    """向 GraphicsView 的 viewport 派发合成鼠标事件。

    held 是“当前按住的键”：拖动必须传 LeftButton，否则 Qt 当 hover 处理，
    pyqtgraph 的 mouseDragEvent（平移/框选）根本不会被调用。
    """
    vp = w._graphics.viewport()
    lp = QtCore.QPointF(pos)
    gp = vp.mapToGlobal(pos)
    if kind == QtCore.QEvent.MouseMove:
        buttons = held if held is not None else QtCore.Qt.MouseButton.NoButton
    elif kind == QtCore.QEvent.MouseButtonRelease:
        buttons = QtCore.Qt.MouseButton.NoButton
    else:
        buttons = button
    ev = QtGui.QMouseEvent(kind, lp, gp, button, buttons, mods)
    return QtWidgets.QApplication.sendEvent(vp, ev)


def send_wheel(w: SeriesPlot, pos: QtCore.QPoint, delta: int = 120) -> bool:
    """派发滚轮事件（角度增量>0 为向上，pyqtgraph 会缩小时间窗）。"""
    vp = w._graphics.viewport()
    ev = QtGui.QWheelEvent(QtCore.QPointF(pos), QtCore.QPointF(vp.mapToGlobal(pos)),
                           QtCore.QPoint(0, 0), QtCore.QPoint(0, delta),
                           QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
                           QtCore.Qt.ScrollPhase.ScrollUpdate, False)
    return QtWidgets.QApplication.sendEvent(vp, ev)


def view_x_range(w: SeriesPlot, idx: int = 0) -> tuple[float, float]:
    return tuple(_rng_bounds(w._lanes[idx].plot.getViewBox().viewRange()[0]))


def make_widget(lanes, t_span=None, size=(1400, 800)):
    w = SeriesPlot(theme="light")
    w.resize(*size)
    w.show()
    pump(qapp())
    w.set_lanes(lanes, t_span=t_span)
    pump(qapp())
    w._refresh()
    pump(qapp())
    return w


def synth(n=40_000, t_end=200.0):
    t = np.linspace(0.0, t_end, n)
    y = np.sin(2 * np.pi * 0.5 * t) + 0.05 * np.random.default_rng(1).standard_normal(n)
    return t, y


# --------------------------------------------------------------------------- #
# 1. 抽稀/分桶工具
# --------------------------------------------------------------------------- #


class TestDecim(unittest.TestCase):

    def test_bucket_envelope_matches_naive(self):
        t = np.linspace(0.0, 100.0, 50_000)
        y = np.sin(t) + 0.3 * np.sin(13 * t)
        env = bucket_envelope(t, y, 0.0, 100.0, 300)
        self.assertGreater(env.n, 100)
        self.assertEqual(env.t.size, env.vmin.size)
        # 逐个桶用暴力法核对 min/max/mean/count
        edges = np.linspace(0.0, 100.0, 301)
        bi = 0
        for k in range(300):
            m = (t >= edges[k]) & (t < edges[k + 1] if k < 299 else t <= edges[k + 1])
            if not m.any():
                continue
            ref = y[m]
            self.assertAlmostEqual(float(env.vmin[bi]), float(ref.min()), places=6)
            self.assertAlmostEqual(float(env.vmax[bi]), float(ref.max()), places=6)
            self.assertAlmostEqual(float(env.vmean[bi]), float(ref.mean()), places=6)
            self.assertEqual(int(env.count[bi]), int(ref.size))
            bi += 1
        self.assertGreater(bi, 100)

    def test_bucket_keeps_spikes(self):
        """分桶必须保得住尖峰（振动毛刺不能丢）——这是选 min/max 包络而不是均值抽稀的理由。"""
        t = np.linspace(0.0, 100.0, 100_000)
        y = np.zeros_like(t)
        y[50_000] = 99.0
        env = bucket_envelope(t, y, 0.0, 100.0, 500)
        self.assertAlmostEqual(float(np.nanmax(env.vmax)), 99.0, places=6)

    def test_bucket_nan_and_empty(self):
        t = np.linspace(0.0, 10.0, 1000)
        y = np.full(1000, np.nan)
        env = bucket_envelope(t, y, 0.0, 10.0, 50)
        self.assertEqual(env.n, 0)
        env2 = bucket_envelope(t, np.arange(1000.0), 20.0, 30.0, 50)   # 完全在范围外
        self.assertEqual(env2.n, 0)
        mixed = np.arange(1000.0)
        mixed[400:600] = np.nan
        env3 = bucket_envelope(t, mixed, 0.0, 10.0, 40)
        self.assertGreater(env3.n, 0)
        self.assertTrue(np.isfinite(env3.vmin).all())

    def test_clean_series_copies_and_sorts(self):
        t = np.array([3.0, 1.0, 2.0, 5.0, 4.0])
        y = np.array([30.0, 10.0, 20.0, 50.0, 40.0])
        t_orig, y_orig = t.copy(), y.copy()
        ts, ys = clean_series(t, y)
        self.assertTrue(is_monotonic(ts))
        np.testing.assert_allclose(ts, [1, 2, 3, 4, 5])
        np.testing.assert_allclose(ys, [10, 20, 30, 40, 50])
        np.testing.assert_allclose(t, t_orig)         # 输入数组绝不能被改动
        np.testing.assert_allclose(y, y_orig)

    def test_lttb_and_finite_stats_and_keys(self):
        t = np.linspace(0, 100, 10_000)
        y = np.sin(t)
        ot, oy = lttb(t, y, 500)
        self.assertLessEqual(ot.size, 502)
        self.assertGreater(ot.size, 400)
        self.assertEqual(ot[0], t[0])
        self.assertEqual(ot[-1], t[-1])
        self.assertEqual(finite_stats(np.array([np.nan, np.inf, 2.0, 4.0])), (2.0, 4.0, 3.0))
        self.assertEqual(parse_key_tolerant("VIBE#1.VibeX"), ("VIBE", 1, "VibeX"))
        self.assertEqual(parse_key_tolerant("VIBE[1].VibeX"), ("VIBE", 1, "VibeX"))
        self.assertEqual(parse_key_tolerant("BAT.Volt"), ("BAT", None, "Volt"))


# --------------------------------------------------------------------------- #
# 2. 组件构造与信号
# --------------------------------------------------------------------------- #


class TestEnvelopeTower(unittest.TestCase):
    """分层桶塔：结果要和直接分桶一致，且不丢尖峰。"""

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(7)
        cls.n = 300_000
        cls.t = np.linspace(0.0, 3000.0, cls.n)
        cls.y = np.sin(cls.t) + 0.1 * rng.standard_normal(cls.n)
        cls.k = int(cls.n * 0.63)
        cls.y[cls.k] += 900.0                     # 单点毛刺，包络必须留得住
        cls.tower = EnvTower(cls.t, cls.y, ppb=128)

    def test_matches_direct_bucketing(self):
        """桶塔结果与直接分桶在同一个量级：极值一致、点数基本一致。"""
        for x0, x1 in ((0.0, 3000.0), (500.0, 1500.0)):
            direct = bucket_envelope(self.t, self.y, x0, x1, 800)
            tow = self.tower.envelope(x0, x1, 800)
            self.assertIsNotNone(tow, f"[{x0},{x1}] 应该用得上塔")
            self.assertGreater(tow.n, 0)
            self.assertAlmostEqual(float(np.nanmax(tow.vmax)), float(np.nanmax(direct.vmax)),
                                   delta=0.5, msg="桶塔峰值包络不能丢毛刺")
            self.assertAlmostEqual(float(np.nanmin(tow.vmin)), float(np.nanmin(direct.vmin)),
                                   delta=0.5)
            dsum = float(np.nansum(direct.count))
            tsum = float(np.nansum(tow.count))
            # 塔按点数分桶、边界桶会多/少算几个点，相对误差应在 1% 内
            self.assertLess(abs(tsum - dsum) / max(dsum, 1.0), 0.01,
                            f"点数不一致 {tsum} vs {dsum}")

    def test_spike_preserved(self):
        """中等窗口：单点毛刺必须出现在包络里（降采样不能把尖峰洗掉）。"""
        spike_t = float(self.t[self.k])
        ea = self.tower.envelope(spike_t - 200.0, spike_t + 200.0, 400)
        self.assertIsNotNone(ea)
        self.assertGreater(float(np.nanmax(ea.vmax)), 800.0)

    def test_returns_none_when_too_fine(self):
        """窗口细到桶数不够 -> None，调用方该走原始点。"""
        self.assertIsNone(self.tower.envelope(0.0, 0.0001, 800))
        self.assertIsNone(self.tower.envelope(1000.0, 1000.5, 800))

    def test_all_nan_and_empty(self):
        tw = EnvTower(np.linspace(0, 100, 50_000), np.full(50_000, np.nan))
        ea = tw.envelope(0.0, 100.0, 500)
        self.assertTrue(ea is None or ea.n == 0)
        self.assertIsNotNone(EnvTower(np.linspace(0, 100, 50_000), np.arange(50_000.0)))

    def test_query_cost_independent_of_data_size(self):
        """全览查询应是毫秒级（直接分桶是 O(可见点)）。"""
        t0 = time.perf_counter()
        for _ in range(50):
            self.tower.envelope(0.0, self.t[-1], 1200)
        ms = (time.perf_counter() - t0) / 50 * 1000
        self.assertLess(ms, 5.0, f"300k 点全览包络查询 {ms:.2f}ms 太慢")


class TestSeriesPlot(unittest.TestCase):

    def setUp(self):
        self.app = qapp()
        t, y = synth()
        t2, y2 = synth(8000, 200.0)
        self.lanes = [
            LaneSpec(label="VIBE[0].VibeX", color=PALETTES["light"][0], unit=" m/s²",
                     t=t, y=y, threshold=0.3, band=(-0.2, 0.2), note="VIBE#0.VibeX"),
            LaneSpec(label="RCOU[0].C1", color=PALETTES["light"][1], unit=" us",
                     t=t2, y=np.clip(1500 + y2 * 200, 1000, 2000),
                     y_min=1000, y_max=2000, style="step", note="RCOU#0.C1"),
            LaneSpec(label="BAT[0].Volt", color=PALETTES["light"][3], unit=" V",
                     t=t2, y=np.where(np.arange(t2.size) % 97 == 0, np.nan, 12.4 + y2 * 0.1),
                     note="BAT#0.Volt"),
            LaneSpec(label="空道(全nan)", color=PALETTES["light"][4], unit="",
                     t=np.linspace(0, 200, 100), y=np.full(100, np.nan), note="BAT#0.Temp"),
        ]
        self.w = make_widget(self.lanes, t_span=(0.0, 200.0))

    def tearDown(self):
        self.w.deleteLater()
        self.w = None

    def test_build_and_labels(self):
        self.assertEqual(self.w.lanes(), ["VIBE[0].VibeX", "RCOU[0].C1", "BAT[0].Volt", "空道(全nan)"])
        self.assertTrue(self.w._lanes[0].has_data)
        self.assertFalse(self.w._lanes[3].has_data)          # 全 nan -> 无数据 + 占位
        self.w._refresh()
        pump(self.app, 4)
        self.assertTrue(self.w._lanes[3].empty_txt.isVisible())
        # 共享 X 轴
        vb0 = self.w._lanes[0].plot.getViewBox()
        for lane in self.w._lanes[1:]:
            self.assertIs(lane.plot.getViewBox().linkedView(0), vb0)   # X 轴链接到首泳道

    def test_set_time_does_not_emit(self):
        got = []
        self.w.timeChanged.connect(lambda t: got.append(t))
        self.w.set_time(55.5)
        pump(self.app)
        self.assertEqual(got, [])
        self.assertAlmostEqual(self.w._cursor_t, 55.5, places=6)
        for lane in self.w._lanes:
            self.assertAlmostEqual(lane.cursor.value(), 55.5, places=3)

    def test_drag_cursor_emits_and_syncs(self):
        got = []
        self.w.timeChanged.connect(lambda t: got.append(t))
        self.w._lanes[0].cursor.setValue(123.0)              # 模拟用户拖动第一道游标
        pump(self.app)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0], 123.0, places=3)
        self.assertAlmostEqual(self.w._lanes[2].cursor.value(), 123.0, places=3)

    def test_click_sets_cursor(self):
        got = []
        self.w.timeChanged.connect(lambda t: got.append(t))
        p = lane_viewport_point(self.w, 1, fx=0.7, fy=0.5)
        send_mouse(self.w, QtCore.QEvent.MouseButtonPress, p, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        send_mouse(self.w, QtCore.QEvent.MouseButtonRelease, p, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        pump(self.app)
        self.assertEqual(len(got), 1, f"单击应发一次 timeChanged，实得 {got}")
        self.assertTrue(0.0 <= got[0] <= 200.0)

    def test_ctrl_drag_region(self):
        got = []
        self.w.regionChanged.connect(lambda a, b: got.append((a, b)))
        p0 = lane_viewport_point(self.w, 0, fx=0.3, fy=0.4)
        p1 = lane_viewport_point(self.w, 0, fx=0.6, fy=0.4)
        ctrl = QtCore.Qt.ControlModifier
        send_mouse(self.w, QtCore.QEvent.MouseButtonPress, p0, QtCore.Qt.LeftButton, ctrl)
        send_mouse(self.w, QtCore.QEvent.MouseMove, p1, QtCore.Qt.NoButton, ctrl)
        send_mouse(self.w, QtCore.QEvent.MouseButtonRelease, p1, QtCore.Qt.LeftButton, ctrl)
        pump(self.app)
        self.assertEqual(len(got), 1, "Ctrl+拖拽松手应发一次 regionChanged")
        a, b = got[0]
        self.assertLess(a, b)
        self.assertTrue(0.0 <= a < b <= 200.0)
        ra, rb = self.w.get_region()
        self.assertAlmostEqual(ra, a, places=3)
        # 所有泳道都要有选区图元
        self.assertTrue(all(l.selection is not None for l in self.w._lanes if l.has_data))

    def test_wheel_zoom_centered_on_mouse(self):
        self.w.zoom_to(0.0, 200.0)
        pump(self.app, 4)
        before = view_x_range(self.w)
        p = lane_viewport_point(self.w, 0, fx=0.8, fy=0.5)
        target_x = self.w._view_x(self.w._lanes[0], p)
        self.assertTrue(math.isfinite(target_x))
        self.assertTrue(send_wheel(self.w, p))
        pump(self.app, 5)
        after = view_x_range(self.w)
        self.assertLess(after[1] - after[0], before[1] - before[0],
                        f"滚轮应缩小时间窗：before={before} after={after}")
        # 鼠标点处的数据位置应基本不动（以鼠标为中心缩放）
        self.assertAlmostEqual(target_x, self.w._view_x(self.w._lanes[0], p), delta=8.0,
                               msg="缩放应以鼠标位置为中心")

    def test_left_drag_pans_x_axis_without_signals(self):
        """左键拖拽平移：X 范围平移、宽度不变、不发任何信号（事件交回 pyqtgraph）。"""
        self.w.zoom_to(0.0, 200.0)
        pump(self.app, 4)
        before = view_x_range(self.w)
        sig: list = []
        self.w.timeChanged.connect(lambda t: sig.append(("t", t)))
        self.w.regionChanged.connect(lambda a, b: sig.append(("r", a, b)))
        self.w.laneVisibilityChanged.connect(lambda s_, on: sig.append(("v", s_, on)))
        held = QtCore.Qt.LeftButton
        send_mouse(self.w, QtCore.QEvent.MouseButtonPress, lane_viewport_point(self.w, 0, 0.75),
                   held, QtCore.Qt.NoModifier)
        for fx in (0.65, 0.5, 0.35):
            send_mouse(self.w, QtCore.QEvent.MouseMove, lane_viewport_point(self.w, 0, fx),
                       QtCore.Qt.NoButton, QtCore.Qt.NoModifier, held=held)
        during = view_x_range(self.w)
        send_mouse(self.w, QtCore.QEvent.MouseButtonRelease,
                   lane_viewport_point(self.w, 0, 0.35), held, QtCore.Qt.NoModifier)
        pump(self.app, 5)
        after = view_x_range(self.w)
        self.assertGreater(during[0], before[0], f"拖拽中视图应已平移：{before} -> {during}")
        self.assertGreater(after[0], before[0], f"松手后应停在平移位置：{before} -> {after}")
        self.assertAlmostEqual(after[1] - after[0], before[1] - before[0], places=1,
                               msg="平移不能改变窗口宽度")
        self.assertEqual(sig, [], f"平移不应发信号，实得 {sig}")
        # 组件内部范围要跟实际视图一致（主窗联动依赖这一点）
        self.assertAlmostEqual(self.w._view[0], after[0], delta=1.0)
        self.assertAlmostEqual(self.w._view[1], after[1], delta=1.0)

    def test_double_click_resets_view(self):
        """双击复位到全时间跨。"""
        self.w.zoom_to(50.0, 60.0)
        pump(self.app, 4)
        lo, hi = view_x_range(self.w)
        self.assertAlmostEqual(hi - lo, 10.0, delta=1.5)
        send_mouse(self.w, QtCore.QEvent.MouseButtonDblClick, lane_viewport_point(self.w, 1, 0.5),
                   QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        pump(self.app, 5)
        lo, hi = view_x_range(self.w)
        self.assertLess(lo, 5.0, f"双击应复位到全览，实得 ({lo}, {hi})")
        self.assertGreater(hi, 195.0, f"双击应复位到全览，实得 ({lo}, {hi})")

    def test_hover_move_does_not_raise(self):
        """悬停读数不能抛异常；悬停也不能冒出 pyqtgraph 的自动量程“A”按钮。"""
        with self.assertNoLogs("apmlog.ui.seriesplot", level="ERROR"):
            for fx in (0.2, 0.4, 0.6, 0.8):
                for idx in range(len(self.w._lanes)):
                    send_mouse(self.w, QtCore.QEvent.MouseMove,
                               lane_viewport_point(self.w, idx, fx, 0.5),
                               QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
            pump(self.app, 4)
        for lane in self.w._lanes:
            self.assertFalse(lane.plot.autoBtn.isVisible(),
                             f"{lane.label} 不应出现 pyqtgraph 自动量程按钮")

    def test_viewport_fallthrough_events_are_silent(self):
        """非鼠标事件落到 fallthrough 分支必须安静返回 False。

        回归点：这里曾写 super().eventFilter(obj, ev)，而 obj 不在该函数作用域，
        任何走到末尾的事件都 NameError + 整串 traceback（被外层 try 兜住但噪声巨大）。
        注意不能发 QEvent(Paint)：没有真实 paint device 会直接段错误，那是 Qt 的约束。
        """
        kinds = [QtCore.QEvent.Type[k] for k in
                 ("HoverEnter", "HoverMove", "HoverLeave", "Enter", "Leave",
                  "FocusIn", "FocusOut", "UpdateRequest", "EnabledChange",
                  "WindowActivate", "StatusTip", "ToolTip", "WhatsThis", "Resize")]
        vp = self.w._graphics.viewport()
        with self.assertNoLogs("apmlog.ui.seriesplot", level="ERROR"):
            for k in kinds:
                QtWidgets.QApplication.sendEvent(vp, QtCore.QEvent(k))
            pump(self.app, 4)
        # fallthrough 之后交互必须照旧
        got: list[float] = []
        self.w.timeChanged.connect(lambda t: got.append(t))
        p = lane_viewport_point(self.w, 0, fx=0.5)
        send_mouse(self.w, QtCore.QEvent.MouseButtonPress, p, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        send_mouse(self.w, QtCore.QEvent.MouseButtonRelease, p, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        pump(self.app)
        self.assertEqual(len(got), 1, "fallthrough 后单击仍应发一次 timeChanged")
        before = view_x_range(self.w)
        send_wheel(self.w, lane_viewport_point(self.w, 0, fx=0.5))
        pump(self.app, 4)
        after = view_x_range(self.w)
        self.assertLess(after[1] - after[0], before[1] - before[0], "fallthrough 后滚轮缩放仍应工作")
        self.assertTrue(self.w.export_png(str(OUT / "unittest_after_fallthrough.png"), 900, 500))

    def test_keyboard_cursor_steps(self):
        self.w.set_time(50.0)
        pump(self.app)
        got = []
        self.w.timeChanged.connect(lambda t: got.append(t))
        for key, exp in ((QtCore.Qt.Key_Right, 50.1), (QtCore.Qt.Key_Left, 50.0)):
            ev = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, key, QtCore.Qt.NoModifier)
            QtWidgets.QApplication.sendEvent(self.w._graphics.viewport(), ev)
            pump(self.app)
            self.assertAlmostEqual(self.w._cursor_t, exp, places=2, msg=f"按键后游标应为 {exp}")
        self.assertEqual(len(got), 2, "键盘移动游标应发 timeChanged")
        ev = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Right, QtCore.Qt.ShiftModifier)
        QtWidgets.QApplication.sendEvent(self.w._graphics.viewport(), ev)
        pump(self.app)
        self.assertAlmostEqual(self.w._cursor_t, 51.0, places=2)

    def test_lane_toggle_and_menu(self):
        got = []
        self.w.laneVisibilityChanged.connect(lambda lbl, on: got.append((lbl, on)))
        self.w._toggle_lane(1, False)
        pump(self.app)
        self.assertEqual(got, [("RCOU[0].C1", False)])
        self.assertTrue(self.w._lanes[1].hidden)
        self.w._toggle_lane(1, True)
        pump(self.app)
        self.w._refresh()
        pump(self.app)
        # 固定量程：RCOU 泳道 Y 轴应停在 1000–2000
        yr = self.w._lanes[1].plot.getViewBox().viewRange()[1]
        self.assertAlmostEqual(float(yr[0]), 1000.0, places=1)
        self.assertAlmostEqual(float(yr[1]), 2000.0, places=1)
        menu = self.w._build_lane_menu(0)
        texts = [a.text() for a in menu.actions() if a.text()]
        for need in ("显示该泳道", "固定量程…", "Y 轴对数", "复制通道名", "导出该泳道 CSV…"):
            self.assertIn(need, texts)
        self.w._set_log_y(0, True)
        pump(self.app)
        self.assertTrue(self.w._lanes[0].log_y)
        self.w._isolate(2)
        pump(self.app)
        self.assertEqual([l.hidden for l in self.w._lanes], [True, True, False, True])

    def test_envelope_switch_and_reset(self):
        # 全览：点数 40000 > raw_limit -> 包络
        self.w.zoom_to(0.0, 200.0)
        self.w._refresh()
        self.assertEqual(self.w._lanes[0].mode, "env")
        # 放大到 2 秒窗口：400 点 <= 阈值 -> 真实折线
        self.w.zoom_to(50.0, 52.0)
        pump(self.app, 4)
        self.w._refresh()
        pump(self.app, 2)
        self.assertEqual(self.w._lanes[0].mode, "raw", "放大后应切回真实折线")
        self.w.reset_view()
        pump(self.app, 3)
        self.assertAlmostEqual(self.w._view[0], 0.0, places=1)
        self.assertAlmostEqual(self.w._view[1], 200.0, places=1)

    def test_export_png_and_csv(self):
        out = OUT
        png = OUT / "unittest_main.png"
        self.assertTrue(self.w.export_png(str(png), 1400, 800))
        self.assertGreater(png.stat().st_size, 2000)
        csvp = OUT / "unittest_main.csv"
        rows = self.w.export_csv(str(csvp), 10.0, 20.0)
        self.assertGreater(rows, 1000)
        head = csvp.read_text(encoding="utf-8-sig").splitlines()[:3]
        self.assertEqual(head[0], "t,label,unit,value")
        self.assertIn("VIBE[0].VibeX", head[1])

    def test_events_regions_and_theme(self):
        self.w.set_events([{"t": 60.0, "severity": "critical", "display": "Crash: Disarming"},
                           {"t": 30.0, "severity": "info", "display": "MODE Loiter"},
                           {"t": float("nan"), "severity": "warn", "display": "坏事件应被忽略"},
                           {}])
        self.w.set_regions([{"t0": 55.0, "t1": 65.0, "severity": "error", "title": "振动超标"},
                            {"t0": 70.0, "t1": 60.0, "severity": "warn"},      # 非法区间应忽略
                            {"t1": 80.0}])
        pump(self.app)
        self.w._refresh()
        pump(self.app)
        ticks = [l.event_items for l in self.w._lanes if l.has_data]
        self.assertTrue(all(len(items) >= 2 for items in ticks))
        self.assertTrue(all(len(l.region_items) == 1 for l in self.w._lanes if l.has_data))
        self.w.set_theme("dark")
        pump(self.app, 3)
        self.assertEqual(self.w._theme, "dark")
        self.assertEqual(len(self.w.lanes()), 4)
        self.w.set_cursor_visible(False)
        self.assertFalse(self.w._lanes[0].cursor.isVisible())

    def test_precomputed_envelope_only(self):
        """内核可能只给预算包络（无原始点）；概览必须能画，且不能崩。"""
        t = np.linspace(0, 300, 400)
        env = LaneSpec(label="VIBE[0].VibeY", color=PALETTES["light"][2], unit=" m/s²",
                       env_t=t, env_min=np.sin(t) - 0.2, env_max=np.sin(t) + 0.2,
                       env_mean=np.sin(t), note="VIBE#0.VibeY")
        w = make_widget([env], t_span=(0.0, 300.0))
        try:
            self.assertTrue(w._lanes[0].has_data)
            w._refresh()
            pump(self.app)
            self.assertTrue(w.export_png(str(OUT / "unittest_preenv.png"), 1200, 500))
        finally:
            w.deleteLater()

    def test_empty_and_degenerate_inputs(self):
        for lanes in ([],
                      [LaneSpec("只时间", (1, 2, 3), t=np.linspace(0, 10, 50), y=None)],
                      [LaneSpec("常数道", (1, 2, 3), t=np.linspace(0, 10, 50), y=np.ones(50))],
                      [LaneSpec("乱序道", (1, 2, 3), t=np.array([5, 1, 4, 2, 3.0]),
                                y=np.array([5, 1, 4, 2, 3.0]))],
                      [LaneSpec("单点", (1, 2, 3), t=np.array([1.0]), y=np.array([2.0]))]):
            w = make_widget(lanes)
            pump(self.app)
            w._refresh()
            pump(self.app)
            self.assertTrue(w.export_png(str(OUT / f"unittest_degenerate_{len(w.lanes())}.png"), 800, 400))
            w.deleteLater()


class TestEventBadges(unittest.TestCase):
    """事件标识：图上只画感叹号徒标（警告黄/错误严red），文字改到悬停提示里按时间顺序列出。"""

    def setUp(self):
        self.app = qapp()
        t, y = synth()
        self.w = make_widget([LaneSpec(label="VIBE[0].VibeX", color=PALETTES["light"][0],
                                       unit=" m/s\u00b2", t=t, y=y, note="VIBE#0.VibeX")],
                             t_span=(0.0, 200.0))
        self.w.set_events([
            {"t": 100.0, "severity": "warn", "kind": "VIBE", "display": "VibeX 18.2 超参考阈值"},
            {"t": 100.4, "severity": "warn", "kind": "VIBE", "display": "VibeY 17.0 超参考阈值"},
            {"t": 150.0, "severity": "critical", "kind": "ERR", "display": "CRASH_CHECK 炸机检测"},
            {"t": 150.0, "severity": "critical", "kind": "MSG", "display": "Crash: Disarming"},
            {"t": 150.02, "severity": "error", "kind": "EV", "display": "DISARMED 上锁"},
            {"t": 30.0, "severity": "info", "kind": "MODE", "display": "MODE AUTO（不该出徒标）"},
        ])
        self.w._refresh()
        pump(self.app, 4)
        self.lane0 = self.w._lanes[0]
        self.vb = self.lane0.plot.getViewBox()
        self.x0, self.x1 = _rng_bounds(self.vb.viewRange()[0])

    def tearDown(self):
        self.w.deleteLater()
        self.w = None

    def _pos(self, t: float, fy: float = 0.97, dx: float = 0.0) -> QtCore.QPoint:
        fx = (t - self.x0) / (self.x1 - self.x0)
        pt = lane_viewport_point(self.w, 0, fx, fy)
        return QtCore.QPoint(pt.x() + int(dx), pt.y())

    def test_same_pixel_cluster_merged(self):
        # 6 条事件 -> 2 个徒标：info 不出徒标，同像元簇内 warn ×2 / critical ×3 各合并成一个
        self.assertEqual([b["sev"] for b in self.w._badges], ["warn", "critical"])
        self.assertEqual([b["n"] for b in self.w._badges], [2, 3])

    def test_badge_glyph_is_only_an_exclamation(self):
        for b in self.w._badges:
            self.assertEqual(b["item"].toPlainText(), "!")
        # 不再把事件原文画在图上
        texts = [i.toPlainText() for i in self.lane0.badge_items]
        self.assertNotIn("CRASH_CHECK 炸机检测", texts)

    def test_tooltip_lists_events_in_time_order(self):
        b = self.w._badges[1]
        tip = self.w._badge_tip(self.lane0, b["t"], self._pos(b["t"]))
        self.assertIsNotNone(tip)
        lines = tip.splitlines()
        self.assertEqual(lines[0], "严重 × 3 条（按时间顺序）")
        self.assertIn("CRASH_CHECK", lines[1])
        self.assertIn("Crash: Disarming", lines[2])
        self.assertIn("DISARMED", lines[3])
        ts = [float(ln.split("t=")[1].split("s")[0]) for ln in lines[1:4]]
        self.assertEqual(ts, sorted(ts))

    def test_miss_when_off_badge(self):
        b = self.w._badges[1]
        self.assertIsNone(self.w._badge_tip(self.lane0, b["t"], self._pos(b["t"], dx=40.0)))
        self.assertIsNone(self.w._badge_tip(self.lane0, b["t"], self._pos(b["t"], fy=0.5)))

    def test_hover_prefers_badge_then_readout(self):
        b = self.w._badges[1]
        self.w._update_hover(self.lane0, self._pos(b["t"]))
        self.assertIn("按时间顺序", QtWidgets.QToolTip.text())
        self.w._update_hover(self.lane0, self._pos(b["t"], dx=60.0))
        self.assertIn("t=", QtWidgets.QToolTip.text())      # 回到普通读数提示
        self.assertNotIn("按时间顺序", QtWidgets.QToolTip.text())


class TestLaneHeight(unittest.TestCase):
    """低分辨率屏可读性：每道最小高 + 滚动，而不是把曲线压扁；隐藏道整行收起。"""

    def _widget(self, n_lanes: int, hide: tuple[int, ...] = ()):
        t, y = synth()
        specs = [LaneSpec(label=f"L{i}", color=PALETTES["light"][i % 16], unit="", t=t, y=y,
                          note=f"ATT#0.F{i}", hidden=(i in hide)) for i in range(n_lanes)]
        return make_widget(specs, t_span=(0.0, 200.0), size=(900, 420))

    def test_scroll_area_present(self):
        w = self._widget(3)
        try:
            self.assertIsInstance(w._scroll, QtWidgets.QScrollArea)
            self.assertIs(w._scroll.widget(), w._graphics)
        finally:
            w.deleteLater()

    def test_min_height_counts_only_visible_lanes(self):
        w = self._widget(10, hide=(1, 3, 5, 7, 9))
        try:
            h = w.lane_min_height()
            self.assertEqual(w._graphics.minimumHeight(), 36 + 5 * (h + 40))
            self.assertFalse(w._lanes[1].plot.isVisible())
            self.assertTrue(w._lanes[0].plot.isVisible())
        finally:
            w.deleteLater()

    def test_set_lane_min_height_clamps_and_applies(self):
        w = self._widget(4)
        try:
            base = w._graphics.minimumHeight()
            w.set_lane_min_height(200)
            self.assertEqual(w.lane_min_height(), 200)
            self.assertGreater(w._graphics.minimumHeight(), base)
            self.assertEqual(w._lanes[0].plot.minimumHeight(), 240)
            w.set_lane_min_height(10_000)          # 越界应钳位，不能把图区撑爆
            self.assertEqual(w.lane_min_height(), 400)
            w.set_lane_min_height(1)               # 下限也不能低于 70
            self.assertEqual(w.lane_min_height(), 70)
        finally:
            w.deleteLater()

    def test_bottom_axis_follows_last_visible_lane(self):
        w = self._widget(4, hide=(3,))
        try:
            self.assertGreater(w._lanes[2].plot.getAxis("bottom").height(), 20)
            self.assertLess(w._lanes[3].plot.getAxis("bottom").height(), 10)
        finally:
            w.deleteLater()


class TestEnvelopeOnlyZoom(unittest.TestCase):
    """预设只给包络（没有原始点）时，滚轮放大不能把曲线画空。"""

    def setUp(self):
        self.app = qapp()
        et = np.linspace(0.0, 200.0, 2400)
        self.spec = LaneSpec(label="VIBE[0].VibeX", color=PALETTES["light"][0], unit=" m/s\u00b2",
                             env_t=et, env_min=np.full(2400, 1.0), env_max=np.full(2400, 5.0),
                             env_mean=np.full(2400, 3.0), key="VIBE#0.VibeX")
        self.w = make_widget([self.spec], t_span=(0.0, 200.0))

    def tearDown(self):
        self.w.deleteLater()
        self.w = None

    def _env_n(self) -> int:
        got = self.w._lanes[0].env_lo.getData()[0]
        return 0 if got is None else int(got.size)

    def test_zoom_in_keeps_curve_visible(self):
        self.w._set_view((100.0, 110.0), emit=False)
        self.w._refresh()
        pump(self.app, 3)
        n = self._env_n()
        self.assertGreater(n, 0, "放大后包络不能为空（旧版直接画空，曲线消失）")
        self.assertLess(n, 2400, "应只取与视图重叠的桶")
        self.assertEqual(self.w._lanes[0].mode, "env")

    def test_zoom_out_beyond_span_still_draws(self):
        self.w._set_view((-50.0, 400.0), emit=False)
        self.w._refresh()
        pump(self.app, 3)
        self.assertEqual(self._env_n(), 2400)      # 重叠太少 → 退回整条包络，不空白

    def test_lanes_needing_refine_reports_channel_key(self):
        need = self.w.lanes_needing_refine()
        self.assertEqual(len(need), 1)
        self.assertEqual(need[0]["key"], "VIBE#0.VibeX")
        self.assertEqual(need[0]["index"], 0)
        self.assertEqual(tuple(round(v, 1) for v in need[0]["cov"]), (0.0, 200.0))

    def test_refine_envelope_swaps_and_validates(self):
        t2 = np.linspace(100.0, 110.0, 500)
        self.assertTrue(self.w.refine_envelope(0, t2, np.full(500, 2.0), np.full(500, 4.0)))
        self.w._set_view((102.0, 104.0), emit=False)
        self.w._refresh()
        pump(self.app, 3)
        self.assertGreater(self._env_n(), 0)
        self.assertEqual(tuple(round(v, 1) for v in self.w._lanes[0].span()), (100.0, 110.0))
        self.assertFalse(self.w.refine_envelope(0, t2[:3], np.full(2, 1.0), np.full(3, 2.0)))
        self.assertFalse(self.w.refine_envelope(99, t2, t2, t2))

    def test_view_changed_is_debounced(self):
        got = []
        self.w.viewChanged.connect(lambda a, b: got.append((a, b)))
        self.w._set_view((60.0, 80.0), emit=False)
        self.w._on_view_changed(self.w._lanes[0].plot, (60.0, 80.0))
        pump(self.app, 2)
        self.assertEqual(got, [], "去抖未到不该发信号")
        self.assertTrue(self.w._refine_timer.isActive())
        self.assertEqual(self.w._refine_timer.interval(), 350)
        self.w._refine_timer.stop()
        self.w._emit_view_changed()
        self.assertEqual([tuple(round(v, 1) for v in g) for g in got], [(60.0, 80.0)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
