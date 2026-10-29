# -*- coding: utf-8 -*-
"""tests/test_overlay_lanes.py —— 四条体验升级的验收。

用户 2026-10-24 的四条要求：
1. **单泳道多曲线同轴叠加**：ATT.Roll 与 ATT.DesRoll 同道、共用 Y 轴，但各有胶囊、单独显隐；
2. **空通道自动初始隐藏**：RCOU.C5~C8 全时段恒 0 → 泳道与胶囊初始关闭并置灰；
3. **Y 轴抗离群尖峰**：默认 1%~99% 分位 + 15% 外扩；双击 Y 轴恢复全量；
4. **精简读数条**：去掉「(桶N点)」，改成紧凑的「字段名 = 数值 单位」。

运行：QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe -m pytest tests/test_overlay_lanes.py -q
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                             # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets                   # noqa: E402

from apmlog.core.model import ChannelInfo, ChannelKey, Envelope, Series   # noqa: E402
from apmlog.ui import presets as P                              # noqa: E402
from apmlog.ui.seriesplot import (LaneSpec, SeriesPlot, robustness_range,   # noqa: E402
                                  Y_ROBUST_MARGIN, Y_ROBUST_PCT)

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


class _Store:
    """build_lanes 需要的最小 store 替身。"""

    def __init__(self, series: dict, meta_rows: dict | None = None,
                 blanks: set | None = None):
        self._s = series
        self._rows = meta_rows or {}
        self._blank = blanks or set()
        self.meta = {"types": {k: {"rows": v} for k, v in self._rows.items()}}
        st = next(iter(series.values()))
        self.meta["stats"] = {"t_start": float(st[0][0]), "t_end": float(st[0][-1])}
        self.stats = type("S", (), {"t_start": float(st[0][0]),
                                    "t_end": float(st[0][-1])})()

    def channel_infos(self):
        out = []
        for key, (t, v) in self._s.items():
            k = ChannelKey.parse(key)
            a = np.asarray(v, float)
            f = a[np.isfinite(a)]
            out.append(ChannelInfo(key=k, count=int(a.size),
                                   t0=float(t[0]), t1=float(t[-1]),
                                   vmin=float(f.min()) if f.size else float("nan"),
                                   vmax=float(f.max()) if f.size else float("nan")))
        return out

    def series(self, key, t0=None, t1=None, max_points=None, drop_nan=True):
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(key)
        t, v = self._s[k.key]
        t = np.asarray(t, float)
        v = np.asarray(v, float)
        m = np.ones(t.size, bool)
        if t0 is not None:
            m &= t >= t0
        if t1 is not None:
            m &= t <= t1
        return Series(k, t[m], v[m], unit=self._unit(k))

    def envelope(self, key, t0=None, t1=None, buckets: int = 2000):
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(key)
        t, v = self._s[k.key]
        t = np.asarray(t, float)
        v = np.asarray(v, float)
        n = min(int(buckets), max(1, t.size))
        edges = np.linspace(0, t.size, n + 1).astype(int)
        ct, lo, hi, me, cnt = [], [], [], [], []
        for i in range(n):
            a, b = edges[i], edges[i + 1]
            if b <= a:
                continue
            seg = v[a:b]
            f = seg[np.isfinite(seg)]
            ct.append(float(t[a:b].mean()))
            lo.append(float(f.min()) if f.size else np.nan)
            hi.append(float(f.max()) if f.size else np.nan)
            me.append(float(f.mean()) if f.size else np.nan)
            cnt.append(float(f.size))
        return Envelope(k, np.array(ct), np.array(lo), np.array(hi), np.array(me),
                        np.array(cnt), unit=self._unit(k))

    @staticmethod
    def _unit(k: ChannelKey) -> str:
        return "°" if k.type == "ATT" else ("µs" if k.type == "RCOU" else "")

    def track(self):                       # build_lanes 的 TRACK 分支用不到，给个空壳
        return type("T", (), {"n_points": 0})()


def _plot(lanes, size=(1100, 760)) -> SeriesPlot:
    w = SeriesPlot(theme="light")
    w.set_lanes(lanes)
    w.resize(*size)
    w.show()
    pump(qapp())
    w._refresh()
    return w


# --------------------------------------------------------------------------- #
# 1. 同轴叠加
# --------------------------------------------------------------------------- #


class TestOverlayLanes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _lanes(self):
        t = np.linspace(0.0, 60.0, 600)
        roll = 5.0 * np.sin(t / 4.0)
        des = roll + 1.5
        st = _Store({"ATT.Roll": (t, roll), "ATT.DesRoll": (t, des)},
                    {"ATT": 600})
        pair = P.LanePair(type="ATT", primary="Roll", overlays=["DesRoll"])
        preset = P.Preset(id="t", name="t", lanes=[pair], envelope_above=10 ** 9)
        lanes, skipped = P.build_lanes(st, preset)
        self.assertEqual(skipped, [])
        self.assertEqual(len(lanes), 1)
        return lanes

    def test_pair_becomes_one_lane_with_overlay(self) -> None:
        lanes = self._lanes()
        ln = lanes[0]
        self.assertEqual(ln.label, "ATT.Roll")
        self.assertEqual([o.label for o in ln.overlays], ["ATT.DesRoll"])
        self.assertTrue(ln.overlays[0].hidden, "叠加曲线默认隐藏（先看实测值）")
        self.assertEqual(ln.overlays[0].unit, ln.unit, "同一根 Y 轴必须同单位")

    def test_each_curve_has_its_own_chip(self) -> None:
        w = _plot(self._lanes())
        self.assertEqual(len(w._chips), 2, "一条主曲线 + 一条叠加曲线 = 两个胶囊")
        self.assertEqual([b.text() for b in w._chips], ["ATT.Roll", "ATT.DesRoll"])
        self.assertEqual([b.isChecked() for b in w._chips], [True, False])

    def test_chip_toggles_only_its_own_curve(self) -> None:
        w = _plot(self._lanes())
        ln = w._lanes[0]
        w._toggle_curve(0, 1, True)
        pump(self.app)
        w._refresh()
        self.assertFalse(ln.hidden, "开叠加曲线不该动主曲线")
        self.assertFalse(ln.ghosts[0].hidden)
        self.assertTrue(ln.ghosts[0].raw.isVisible())
        self.assertTrue(w._chips[1].isChecked(), "胶囊状态要跟着显隐走")
        w._toggle_curve(0, 1, False)
        pump(self.app)
        self.assertTrue(ln.ghosts[0].hidden)
        self.assertTrue(ln.raw.isVisible(), "关叠加曲线不该动主曲线")
        self.assertFalse(w._chips[1].isChecked())

    def test_overlay_shares_the_same_y_axis(self) -> None:
        w = _plot(self._lanes())
        ln = w._lanes[0]
        w._toggle_curve(0, 1, True)
        pump(self.app)
        w._refresh()
        self.assertIs(ln.ghosts[0].raw.getViewBox(), ln.plot.getViewBox(),
                      "叠加曲线必须挂在同一 ViewBox 上（同一 Y 轴坐标系）")
        # 同一坐标系：曲线数据范围互相包含（相差 1.5° 的偏置，量程应覆盖两条）
        lo, hi = [float(v) for v in ln.plot.getViewBox().viewRange()[1]]
        self.assertLessEqual(lo, float(np.nanmin(ln.ghosts[0].y)) - 0.5)
        self.assertGreaterEqual(hi, float(np.nanmax(ln.y)) + 0.5)

    def test_visibility_signal_carries_the_curve_label(self) -> None:
        w = _plot(self._lanes())
        got: list[tuple[str, bool]] = []
        w.laneVisibilityChanged.connect(lambda s_, on: got.append((s_, on)))
        w._toggle_curve(0, 1, True)
        pump(self.app)
        self.assertEqual(got, [("ATT.DesRoll", True)])
        w._toggle_curve(0, 0, False)
        pump(self.app)
        self.assertEqual(got[-1], ("ATT.Roll", False))

    def test_lane_without_overlays_keeps_old_behaviour(self) -> None:
        t = np.linspace(0.0, 10.0, 50)
        st = _Store({"ATT.Pitch": (t, np.zeros(t.size))}, {"ATT": 50})
        preset = P.Preset(id="t2", name="t2", lanes=[P._req("ATT", "Pitch")],
                          envelope_above=10 ** 9)
        lanes, _ = P.build_lanes(st, preset)
        self.assertEqual(lanes[0].overlays, [])
        w = _plot(lanes)
        self.assertEqual(len(w._chips), 1)


# --------------------------------------------------------------------------- #
# 2. 空通道自动初始隐藏
# --------------------------------------------------------------------------- #


class TestEmptyChannelAutoHide(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _motor_lanes(self):
        t = np.linspace(0.0, 20.0, 200)
        series = {}
        for i in range(1, 5):
            series[f"RCOU.C{i}"] = (t, 1500.0 + 10.0 * i + np.sin(t))
        for i in range(5, 9):
            series[f"RCOU.C{i}"] = (t, np.zeros(t.size))       # 四旋翼：C5~C8 恒 0
        st = _Store(series, {"RCOU": 200})
        preset = P.Preset(id="m", name="m", envelope_above=10 ** 9,
                          lanes=[P._req("RCOU", *[f"C{i}" for i in range(1, 9)])])
        lanes, skipped = P.build_lanes(st, preset)
        self.assertEqual(skipped, [])
        return lanes

    def test_zero_channels_are_hidden_and_greyed(self) -> None:
        lanes = self._motor_lanes()
        self.assertEqual(len(lanes), 8)
        for ln in lanes[:4]:
            self.assertFalse(ln.hidden, f"{ln.label} 有数据，不该隐藏")
            self.assertFalse(ln.empty)
        for ln in lanes[4:]:
            self.assertTrue(ln.hidden, f"{ln.label} 全时段恒 0，应默认隐藏")
            self.assertTrue(ln.empty, f"{ln.label} 应标为空通道（胶囊置灰）")

    def test_empty_chip_starts_unchecked_and_grey(self) -> None:
        w = _plot(self._motor_lanes())
        checked = [b.isChecked() for b in w._chips]
        self.assertEqual(checked, [True] * 4 + [False] * 4)
        self.assertIn("空通道", w._chips[4].toolTip())
        self.assertIn("#e9eaec", w._chips[4].styleSheet(), "空通道胶囊要置灰")
        self.assertNotIn("#e9eaec", w._chips[0].styleSheet())

    def test_empty_channel_can_still_be_shown(self) -> None:
        w = _plot(self._motor_lanes())
        w._toggle_lane(5, True)
        pump(self.app)
        self.assertFalse(w._lanes[5].hidden, "置灰≠禁用：点一下仍然能显示")
        self.assertTrue(w._chips[5].isChecked())

    def test_no_valid_points_counts_as_empty(self) -> None:
        t = np.linspace(0.0, 5.0, 20)
        st = _Store({"RCOU.C1": (t, np.full(t.size, np.nan))}, {"RCOU": 20})
        preset = P.Preset(id="n", name="n", envelope_above=10 ** 9,
                          lanes=[P._req("RCOU", "C1")])
        lanes, _ = P.build_lanes(st, preset)
        self.assertTrue(lanes[0].empty and lanes[0].hidden)

    def test_attitude_preset_uses_overlays_not_extra_lanes(self) -> None:
        ids = {p.id for p in P.PRESETS}
        self.assertIn("attitude", ids)
        att = P.PRESET_BY_ID["attitude"]
        self.assertTrue(all(isinstance(x, P.LanePair) for x in att.lanes))
        self.assertEqual(len(att.lanes), 5, "Roll/Pitch/Yaw/R/P 五条泳道，不是十条")


# --------------------------------------------------------------------------- #
# 3. Y 轴抗离群尖峰
# --------------------------------------------------------------------------- #


class TestRobustYRange(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_percentile_range_ignores_a_single_outlier(self) -> None:
        rng = np.random.default_rng(7)
        v = rng.normal(5.0, 2.0, 500)
        v[100] = -180.0                     # 个别跳变尖峰
        lo, hi = robustness_range(v)
        self.assertGreater(lo, -60.0, "1% 分位区间不该被单个 -180 拉爆")
        self.assertLess(lo, float(np.percentile(v, 1)), "要留 15% 外扩边距")
        self.assertGreater(hi, float(np.percentile(v, 99)))

    def test_margin_is_fifteen_percent_of_the_span(self) -> None:
        v = np.linspace(0.0, 100.0, 1000)
        lo, hi = robustness_range(v)
        p1, p99 = np.percentile(v, Y_ROBUST_PCT)
        self.assertAlmostEqual(lo, p1 - (p99 - p1) * Y_ROBUST_MARGIN, places=6)
        self.assertAlmostEqual(hi, p99 + (p99 - p1) * Y_ROBUST_MARGIN, places=6)

    def test_few_points_fall_back_to_min_max(self) -> None:
        v = np.array([1.0, 2.0, 3.0])
        lo, hi = robustness_range(v)
        self.assertLess(lo, 1.0)
        self.assertGreater(hi, 3.0)

    def test_all_nan_gives_none(self) -> None:
        self.assertIsNone(robustness_range(np.array([np.nan, np.nan])))

    def test_lane_view_uses_robust_range_and_full_toggle_restores(self) -> None:
        t = np.linspace(0.0, 60.0, 800)
        v = 5.0 + 2.0 * np.sin(t)
        v[400] = -180.0
        spec = LaneSpec(label="ATT.Roll", color=(31, 119, 180), unit="°", t=t, y=v)
        w = _plot([spec])
        lo, hi = [float(x) for x in w._lanes[0].plot.getViewBox().viewRange()[1]]
        self.assertGreater(lo, -100.0, f"默认视野不能含 -180 尖峰（现在 {lo:.1f}）")
        # 双击 Y 轴的等价动作：切到全量
        self.assertTrue(w._toggle_y_full(0))
        pump(self.app)
        w._refresh()
        lo2, hi2 = [float(x) for x in w._lanes[0].plot.getViewBox().viewRange()[1]]
        self.assertLess(lo2, -170.0, "恢复全量后必须能看到那个尖峰")
        # 再双击切回稳衡量程
        self.assertFalse(w._toggle_y_full(0))
        pump(self.app)
        w._refresh()
        lo3, _ = [float(x) for x in w._lanes[0].plot.getViewBox().viewRange()[1]]
        self.assertGreater(lo3, -100.0)

    def test_fixed_range_lane_ignores_robustness(self) -> None:
        t = np.linspace(0.0, 10.0, 100)
        v = np.zeros(t.size)
        v[50] = -180.0
        spec = LaneSpec(label="f", color=(1, 2, 3), t=t, y=v, y_min=-180.0, y_max=180.0)
        w = _plot([spec])
        lo, hi = [float(x) for x in w._lanes[0].plot.getViewBox().viewRange()[1]]
        self.assertAlmostEqual(lo, -180.0, places=1)
        self.assertAlmostEqual(hi, 180.0, places=1)

    def test_double_click_on_y_axis_hits_the_right_lane(self) -> None:
        t = np.linspace(0.0, 60.0, 300)
        specs = [LaneSpec(label=f"L{i}", color=(30 * i, 60, 90), unit="", t=t,
                          y=np.sin(t) + i) for i in range(3)]
        w = _plot(specs)
        lane = w._lanes[1]
        vb = lane.plot.getViewBox()
        r = vb.sceneBoundingRect()
        if r.width() <= 1 or r.height() <= 1:
            self.skipTest("offscreen 下布局还没算出来")
        mid_y = (r.top() + r.bottom()) / 2.0
        on_axis = w._graphics.mapFromScene(QtCore.QPointF(r.left() - 12.0, mid_y))
        in_plot = w._graphics.mapFromScene(QtCore.QPointF(r.center().x(), mid_y))
        self.assertIs(w._lane_row_at_pos(on_axis), lane)
        self.assertTrue(w._is_on_y_axis(lane, on_axis))
        self.assertFalse(w._is_on_y_axis(lane, in_plot), "绘图区里双击不算 Y 轴")
        # 真发一个双击事件，走完整分派
        gp = w._graphics.viewport().mapToGlobal(on_axis)
        ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonDblClick, QtCore.QPointF(on_axis),
                               QtCore.QPointF(gp), QtCore.Qt.LeftButton, QtCore.Qt.LeftButton,
                               QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(w._graphics.viewport(), ev)
        pump(self.app)
        self.assertIn(1, w._y_full, "双击 Y 轴应把该道切到全量范围")


# --------------------------------------------------------------------------- #
# 4. 精简读数条
# --------------------------------------------------------------------------- #


class TestCompactReadout(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _two_lane_plot(self):
        t = np.linspace(0.0, 60.0, 600)
        a = LaneSpec(label="ATT.Roll", color=(31, 119, 180), unit="°", t=t, y=np.sin(t))
        ov = LaneSpec(label="ATT.DesRoll", color=(232, 120, 40), unit="°", t=t,
                      y=np.sin(t) + 1.0, hidden=False)
        a.overlays.append(ov)
        b = LaneSpec(label="RATE.R", color=(214, 39, 40), unit="°/s", t=t, y=np.cos(t))
        return _plot([a, b])

    def test_no_bucket_suffix_in_the_bar(self) -> None:
        w = self._two_lane_plot()
        w.set_time(30.0)
        pump(self.app)
        txt = w._readout.text()
        self.assertNotIn("桶", txt, "底部读数条不该再出现 (桶N点)")
        self.assertNotIn("点)", txt)

    def test_format_is_name_equals_value_unit(self) -> None:
        w = self._two_lane_plot()
        w.set_time(30.0)
        pump(self.app)
        txt = w._readout.text()
        self.assertIn("ATT.Roll = ", txt)
        self.assertIn(" °", txt)
        self.assertIn("RATE.R = ", txt)
        self.assertIn(" °/s", txt)

    def test_overlay_value_sits_next_to_its_lane(self) -> None:
        w = self._two_lane_plot()
        w.set_time(30.0)
        pump(self.app)
        txt = w._readout.text()
        i_roll = txt.index("ATT.Roll")
        i_des = txt.index("ATT.DesRoll")
        i_rate = txt.index("RATE.R")
        self.assertLess(i_roll, i_des, "叠加曲线的读数紧跟主曲线")
        self.assertLess(i_des, i_rate, "同道的曲线在同一个分组里")

    def test_tooltip_still_carries_details(self) -> None:
        w = self._two_lane_plot()
        w.set_time(30.0)
        pump(self.app)
        tip = w._readout.toolTip()
        self.assertIn("ATT.Roll = ", tip)
        self.assertIn("ATT.DesRoll = ", tip)

    def test_hidden_overlay_is_not_in_the_bar(self) -> None:
        w = self._two_lane_plot()
        w._toggle_curve(0, 1, False)
        w.set_time(30.0)
        pump(self.app)
        self.assertNotIn("ATT.DesRoll", w._readout.text())


if __name__ == "__main__":
    unittest.main()
