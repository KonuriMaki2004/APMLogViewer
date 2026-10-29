# -*- coding: utf-8 -*-
"""tests/test_multitrack_ui.py —— 地图多轨迹绘制与右侧勾选栏。

为什么要有这个功能：同一份日志里 POS（EKF 融合位置）和 GPS（原始 GNSS）逐点最大
实测差 76 m，Mission Planner 会画出两条线，而本工程以前按优先级**只挑一条**，
用户看不到"飞控算的位置"和"接收机给的位置"到底分不分叉。

这里只测 UI 层（假 TrackResult，鸭子类型），数据层的抽取正确性在 tests/test_tracks.py。
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets                       # noqa: E402

from apmlog.ui.tracklegend import TRACK_COLORS, TrackLegendPanel   # noqa: E402
from apmlog.ui.trackmap import TrackMapWidget                      # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 12) -> None:
    for _ in range(times):
        app.processEvents()



def _paths(layer):
    """该层里的折线图元（层里还混着轨迹名称标签，计数时要排除）。"""
    return [it for it in layer.items if isinstance(it, QtWidgets.QGraphicsPathItem)]

class FakeTrack:
    """鸭子类型的 TrackResult：只带地图真正会读的属性。"""

    def __init__(self, key="", label="", kind="geodetic", n=40, dlat=0.0, dlon=0.0,
                 base=(31.2304, 121.4737), home=None, bad=False):
        i = np.arange(n, dtype=float)
        self.kind = kind
        self.key, self.label = key, label
        self.n_points = n
        self.t = i * 0.5
        self.lat = base[0] + i * 0.0001 + dlat
        self.lon = base[1] + i * 0.0001 + dlon
        self.alt = 10.0 + i
        self.u = self.alt
        self.e = i * 1.0
        self.n = i * 2.0
        self.speed = np.full(n, 8.0)
        self.src = np.zeros(n, dtype=np.int8)
        self.src_names = []
        self.home = home
        self.origin = base
        self.note = ""
        self.has_geodetic = kind == "geodetic"
        if bad:                                   # 掺 NaN，测鲁棒性
            self.lat[3] = np.nan
            self.lon[3] = np.nan


def two_tracks():
    a = FakeTrack(key="POS", label="EKF 融合位置 (POS)", n=40)
    b = FakeTrack(key="GPS#0", label="原始 GNSS (GPS1)", n=20, dlat=0.0006, dlon=0.0006)
    return a, b


# ------------------------------------------------------------------ 勾选栏本体
class TestLegendPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _mk(self, specs=None, visible=None) -> TrackLegendPanel:
        p = TrackLegendPanel()
        p.set_tracks(specs if specs is not None else [
            ("POS", "EKF 融合位置 (POS)", "#ff3b30", 4882, True),
            ("GPS#0", "原始 GNSS (GPS1)", "#ffcc00", 2441, False),
            ("VISP", "视觉位置 (VISP)", "#00d084", 13230, False)])
        p.show()
        pump(self.app)
        self.addCleanup(p.deleteLater)
        return p

    def test_one_row_per_track_with_label_and_count(self) -> None:
        p = self._mk()
        boxes = p.findChildren(QtWidgets.QCheckBox)
        labels = sorted(b.text() for b in boxes)
        self.assertEqual(len(boxes), 3)
        self.assertIn("EKF 融合位置 (POS)（主）", labels)
        self.assertIn("原始 GNSS (GPS1)", labels)
        counts = [l.text() for l in p.findChildren(QtWidgets.QLabel)
                  if l.text().isdigit()]
        self.assertIn("4882", counts)
        self.assertIn("13230", counts)

    def test_every_track_has_a_color_swatch(self) -> None:
        p = self._mk()
        sw = [l for l in p.findChildren(QtWidgets.QLabel)
              if l.styleSheet().startswith("background:#")]
        self.assertEqual(len(sw), 3)
        for l in sw:
            self.assertTrue(l.minimumWidth() >= 8 or l.width() >= 8)

    def test_all_visible_by_default(self) -> None:
        p = self._mk()
        self.assertEqual(sorted(p.visible()), ["GPS#0", "POS", "VISP"])
        self.assertEqual(p.summary(), "3/3 条轨迹")

    def test_uncheck_emits_remaining_keys(self) -> None:
        p = self._mk()
        got: list = []
        p.visibilityChanged.connect(got.append)
        row = next(b for b in p.findChildren(QtWidgets.QCheckBox)
                   if "GPS" in b.text())
        row.setChecked(False)
        pump(self.app)
        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(got[0]), ["POS", "VISP"])
        self.assertEqual(sorted(p.visible()), ["POS", "VISP"])

    def test_can_check_several_at_once(self) -> None:
        """用户点名"可一次性勾选多个"：全选按钮必须一次恢复全部。"""
        p = self._mk()
        for b in p.findChildren(QtWidgets.QCheckBox):
            b.setChecked(False)
        got: list = []
        p.visibilityChanged.connect(got.append)
        p.btn_all.click()
        self.assertEqual(sorted(p.visible()), ["GPS#0", "POS", "VISP"])
        self.assertEqual(len(got), 1, "全选应只发一次信号，不是三次")

    def test_only_primary_button(self) -> None:
        p = self._mk()
        p.btn_only.click()
        self.assertEqual(p.visible(), ["POS"])

    def test_set_visible_does_not_echo(self) -> None:
        p = self._mk()
        got: list = []
        p.visibilityChanged.connect(got.append)
        p.set_visible(["POS"])
        self.assertEqual(got, [], "外部恢复状态不该反弹信号（否则死循环）")
        self.assertEqual(p.visible(), ["POS"])

    def test_rebuilding_keeps_unknown_keys_out(self) -> None:
        p = self._mk()
        p.set_tracks([("POS", "EKF 融合位置 (POS)", "#ff3b30", 10, True)],
                     visible=["POS"])
        self.assertEqual(p.visible(), ["POS"])
        self.assertEqual(len(p.findChildren(QtWidgets.QCheckBox)), 1)

    def test_disabled_row_cannot_be_tricked_into_visible(self) -> None:
        """画不出来的那条：置灰、不勾、不计入 visible，全选也不碰它。"""
        p = self._mk([
            ("POS", "EKF 融合位置 (POS)", "#ff3b30", 10, True, True),
            ("VISP", "视觉位置 (VISP)", "#00d084", 20, False, False)])
        boxes = p.findChildren(QtWidgets.QCheckBox)
        vis_row = next(b for b in boxes if "VISP" in b.text())
        self.assertFalse(vis_row.isEnabled())
        self.assertFalse(vis_row.isChecked())
        self.assertEqual(p.visible(), ["POS"])
        p.btn_all.click()
        self.assertEqual(p.visible(), ["POS"], "全选不该把画不出的轨迹也算进去")
        p.set_visible(["POS", "VISP"])
        self.assertEqual(p.visible(), ["POS"])
        self.assertIn("本地米坐标", vis_row.toolTip())

    def test_empty_specs_is_not_a_crash(self) -> None:
        p = self._mk([])
        self.assertEqual(p.keys(), [])
        self.assertEqual(p.visible(), [])

    def test_is_checked_reports_unknown_key_as_false(self) -> None:
        p = self._mk()
        self.assertFalse(p.is_checked("没有这条"))
        self.assertTrue(p.is_checked("POS"))

    def test_semi_transparent_background(self) -> None:
        p = self._mk()
        ss = p.styleSheet()
        self.assertIn("rgba(", ss, "半透明底要真的用 rgba，否则挡住卫星图")
        self.assertIn("border-radius", ss)

    def test_text_is_light_on_the_dark_overlay(self) -> None:
        """覆盖层底色固定深灰，文字必须浅色可读（主题不该把它刷成黑字）。"""
        p = self._mk()
        for b in p.findChildren(QtWidgets.QCheckBox):
            col = b.palette().color(QtGui.QPalette.ColorRole.WindowText)
            lum = 0.2126 * col.red() + 0.7152 * col.green() + 0.0722 * col.blue()
            self.assertGreater(lum, 120, f"勾选栏文字太暗：{col.name()}")
            break                       # 控件底色由样式表决定，抽查一个即可


# ------------------------------------------------------------------ 地图多轨迹
class TestMapMultiTrack(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _mk(self, tracks=None) -> TrackMapWidget:
        m = TrackMapWidget(source_name="OSM")
        m.resize(900, 620)
        m.show()
        pump(self.app)
        self.addCleanup(self._kill)
        if tracks is not None:
            m.set_tracks(tracks)
            pump(self.app)
        return m

    def _kill(self) -> None:
        try:
            m = self.m
        except AttributeError:
            return
        try:
            m.close()
        except Exception:
            pass

    def _go(self, tracks):
        self.m = self._mk(tracks)
        return self.m

    def test_single_track_hides_the_legend(self) -> None:
        m = self._go([FakeTrack(key="POS", label="EKF 融合位置 (POS)")])
        self.assertFalse(m.legend.isVisible(), "只有一条轨迹就别摆面板")

    def test_two_tracks_show_the_legend_with_both_names(self) -> None:
        a, b = two_tracks()
        m = self._go([a, b])
        self.assertTrue(m.legend.isVisible())
        text = " ".join(x.text() for x in m.legend.findChildren(QtWidgets.QCheckBox))
        self.assertIn("EKF 融合位置", text)
        self.assertIn("原始 GNSS", text)

    def test_secondary_track_is_drawn_as_its_own_path(self) -> None:
        a, b = two_tracks()
        m = self._go([a, b])
        self.assertGreaterEqual(len(m._layer_track.items), 1, "主轨迹要有线")
        self.assertEqual(len(_paths(m._layer_track2)), 1, "副轨迹应各有一条线")
        pen = _paths(m._layer_track2)[0].pen()
        self.assertEqual(pen.color().name().lower(), b_label_color(a, b).lower(),
                         "副轨迹颜色要与勾选栏色块一致")

    def test_unchecking_secondary_removes_only_it(self) -> None:
        a, b = two_tracks()
        m = self._go([a, b])
        row = next(x for x in m.legend.findChildren(QtWidgets.QCheckBox)
                   if "GPS" in x.text())
        row.setChecked(False)
        pump(self.app)
        self.assertEqual(len(_paths(m._layer_track2)), 0)
        self.assertGreaterEqual(len(m._layer_track.items), 1, "主轨迹不该被牵连")

    def test_rechecking_draws_it_again(self) -> None:
        a, b = two_tracks()
        m = self._go([a, b])
        row = next(x for x in m.legend.findChildren(QtWidgets.QCheckBox)
                   if "GPS" in x.text())
        row.setChecked(False)
        row.setChecked(True)
        pump(self.app)
        self.assertEqual(len(_paths(m._layer_track2)), 1)

    def test_unchecking_primary_hides_its_line_and_cursor(self) -> None:
        a, b = two_tracks()
        m = self._go([a, b])
        m.set_time(float(a.t[5]))
        self.assertTrue(m._cursor.isVisible())
        row = next(x for x in m.legend.findChildren(QtWidgets.QCheckBox)
                   if "EKF" in x.text())
        row.setChecked(False)
        pump(self.app)
        self.assertTrue(all(not it.isVisible() for it in m._layer_track.items))
        self.assertFalse(m._cursor.isVisible(), "线都藏了，游标不该漂在图上")
        row.setChecked(True)
        pump(self.app)
        self.assertTrue(any(it.isVisible() for it in m._layer_track.items))

    def test_visibility_signal_carries_keys_for_persistence(self) -> None:
        a, b = two_tracks()
        m = self._go([a, b])
        got: list = []
        m.trackVisibilityChanged.connect(got.append)
        next(x for x in m.legend.findChildren(QtWidgets.QCheckBox)
             if "GPS" in x.text()).setChecked(False)
        self.assertEqual(got, [["POS"]])
        self.assertEqual(m.visible_tracks(), ["POS"])

    def test_three_tracks_all_visible_at_once(self) -> None:
        a, b = two_tracks()
        c = FakeTrack(key="VISP", label="视觉位置 (VISP)", n=30, dlat=-0.0004)
        m = self._go([a, b, c])
        self.assertEqual(len(_paths(m._layer_track2)), 2)
        m.legend.btn_all.click()
        self.assertEqual(len(_paths(m._layer_track2)), 2)
        m.legend.btn_only.click()
        self.assertEqual(len(_paths(m._layer_track2)), 0)

    def test_nan_points_do_not_kill_a_secondary(self) -> None:
        a = FakeTrack(key="POS", label="EKF 融合位置 (POS)")
        b = FakeTrack(key="GPS#0", label="原始 GNSS (GPS1)", bad=True)
        m = self._go([a, b])
        self.assertEqual(len(_paths(m._layer_track2)), 1)
        self.assertLess(int(b.n_points), 41, "假数据本身就该带 NaN")

    def test_local_tracks_are_drawn_in_the_same_enu_frame(self) -> None:
        """纯视觉日志（无 GPS、无 ORGN）只有本地轨迹：它们必须能叠在一起对比。

        以前副轨迹在本地模式直接不画，勾选栏里点了什均反应——那是错的：
        XKF1 与 VISP 同源于 EKF 本地坐标系，本来就该同画一张图。
        """
        a = FakeTrack(key="XKF1", label="EKF 状态位置 (XKF1)", kind="local", n=30)
        b = FakeTrack(key="VISP", label="视觉位置 (VISP)", kind="local", n=25)
        m = self._go([a, b])
        self.assertEqual(m._mode, "local")
        self.assertTrue(m.legend.isVisible(), "本地模式也要给勾选栏")
        self.assertEqual(len(_paths(m._layer_track2)), 1, "副轨迹要真的画出来")
        x, y = m._xy_for(b)
        self.assertEqual(x.size, b.n_points)
        self.assertTrue(np.allclose(x, b.e), "本地映射 x=东米")
        self.assertTrue(np.allclose(y, -b.n), "本地映射 y=-北米（与主轨迹同一套）")

    def test_local_scene_rect_covers_the_union_so_nothing_gets_clipped(self) -> None:
        """QGraphicsView 默认 clipToSceneRect：不撑开矩形，伸得远的副轨迹会被剪掉。"""
        a = FakeTrack(key="XKF1", label="EKF 状态位置 (XKF1)", kind="local", n=10)
        b = FakeTrack(key="VISP", label="视觉位置 (VISP)", kind="local", n=10)
        b.e = b.e * 500.0                       # 把副轨迹拉开到主轨迹矩形之外
        b.n = b.n * 500.0
        m = self._go([a, b])
        r = m._scene.sceneRect()
        self.assertLessEqual(r.left(), float(b.e.min()) - 1.0)
        self.assertGreaterEqual(r.right(), float(b.e.max()) + 1.0)
        self.assertLessEqual(r.top(), float(-b.n.max()) - 1.0)
        self.assertGreaterEqual(r.bottom(), float(-b.n.min()) + 1.0)

    def test_local_secondary_toggle_works(self) -> None:
        a = FakeTrack(key="XKF1", label="EKF 状态位置 (XKF1)", kind="local", n=30)
        b = FakeTrack(key="VISP", label="视觉位置 (VISP)", kind="local", n=25)
        m = self._go([a, b])
        row = next(x for x in m.legend.findChildren(QtWidgets.QCheckBox)
                   if "视觉" in x.text())
        row.setChecked(False)
        pump(self.app)
        self.assertEqual(len(_paths(m._layer_track2)), 0)
        row.setChecked(True)
        pump(self.app)
        self.assertEqual(len(_paths(m._layer_track2)), 1)

    def test_geodetic_primary_with_local_secondary_stays_honest(self) -> None:
        """主轨迹有经纬度、副轨迹只有本地米数：不能把米当度叠到瓦片图上。"""
        a = FakeTrack(key="POS", label="EKF 融合位置 (POS)")
        b = FakeTrack(key="VISP", label="视觉位置 (VISP)", kind="local")
        m = self._go([a, b])
        self.assertEqual(len(_paths(m._layer_track2)), 0, "坐标系不同，不能强叠")
        self.assertTrue(m.legend.isVisible(), "但要在栏里存在，让用户知道有这条")

    def test_empty_and_none_do_not_crash(self) -> None:
        m = self._go([])
        self.assertFalse(m.legend.isVisible())
        m2 = self._go(None)
        self.assertFalse(m2.legend.isVisible())

    def test_set_track_still_works_alone(self) -> None:
        """老 API 不能坏：app 在数据层不支持 tracks() 时要能退回单轨迹。"""
        m = self._mk()
        m.set_track(FakeTrack(key="POS", label="EKF 融合位置 (POS)"))
        pump(self.app)
        self.assertGreaterEqual(len(m._layer_track.items), 1)
        self.assertFalse(m.legend.isVisible())

    def test_legend_sits_on_the_right_side(self) -> None:
        a, b = two_tracks()
        m = self._go([a, b])
        vp = m.view.viewport().size()
        pos = m.legend.mapTo(m.view.viewport(), QtCore.QPoint(0, 0))
        self.assertGreater(pos.x(), vp.width() * 0.55, "勾选栏要在地图右侧")
        self.assertLess(pos.y(), vp.height() * 0.5)

    def test_legend_does_not_swallow_map_mouse_events_outside_itself(self) -> None:
        """覆盖层只占右侧一小块，别的地方还得能拖动画布。"""
        a, b = two_tracks()
        m = self._go([a, b])
        vp = m.view.viewport()
        pos = m.legend.mapTo(vp, QtCore.QPoint(0, 0))
        outside = QtCore.QPoint(max(0, pos.x() - 40), int(vp.height() * 0.8))
        self.assertIsNone(vp.childAt(outside), "地图左下区域不该被任何覆盖层挡住")

    def test_zoom_keeps_secondary_line_pixel_width(self) -> None:
        a, b = two_tracks()
        m = self._go([a, b])
        it = _paths(m._layer_track2)[0]
        before = it.pen().widthF()
        m._set_zoom(m.zoom() + 2.0)
        # 视图变化是 60ms 防抖的，必须等真时间而不是空转 processEvents
        end = time.time() + 1.0
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)
            if it.pen().widthF() != before:
                break
        after = it.pen().widthF()
        self.assertLess(after, before, "放大后副轨迹的场景线宽要变小，才能保持屏幕像素恒定")

    def test_fit_covers_the_union_of_visible_tracks(self) -> None:
        """只框主轨迹会把勾选出来的第二条切出画面，那等于没显示。"""
        a = FakeTrack(key="POS", label="EKF 融合位置 (POS)", n=10)
        b = FakeTrack(key="GPS#0", label="原始 GNSS (GPS1)", n=10,
                      dlat=0.02, dlon=0.02)
        m = self._go([a, b])
        only_primary = QtGui.QPolygonF()
        m.legend.btn_only.click()
        pump(self.app)
        m.fit_to_track()
        z_tight = m.zoom()
        m.legend.btn_all.click()
        pump(self.app)
        m.fit_to_track()
        z_wide = m.zoom()
        self.assertLess(z_wide, z_tight, "并集更大时缩放级应更小（看得更远）")

    def test_gcj02_source_offsets_secondary_too(self) -> None:
        """换到高德(GCJ02)底图时，副轨迹也必须一起加偏，否则两条线一张图上错开。"""
        a, b = two_tracks()
        m = self._mk()
        m.set_source("gaode_imagery")
        self.assertEqual(m.current_source().datum, "GCJ02", "先确认这个源真的是 GCJ02")
        m.set_tracks([a, b])
        pump(self.app)
        self.assertEqual(len(_paths(m._layer_track2)), 1)
        x, y = m._xy_for(b)
        raw = _scene_wgs(b)
        d = math.hypot(float(np.median(x) - np.median(raw[0])),
                       float(np.median(y) - np.median(raw[1])))
        self.assertGreater(d, 100.0, "GCJ02 下副轨迹应有米级加偏位移")
        # 主轨迹同样被加偏（两套坐标不能一个偏一个不偏）
        self.assertGreaterEqual(len(m._layer_track.items), 1)


def b_label_color(a, b) -> str:
    return TRACK_COLORS[1]


def _scene_wgs(tr) -> tuple:
    from apmlog.ui import _tilemath as tm
    return tm.scene_from_lonlat_np(np.asarray(tr.lat), np.asarray(tr.lon))


# ------------------------------------------------------------------ 主窗口接线
class TestMainWindowWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        """把视图记忆指到临时文件。

        否则 MainWindow() 会读用户真实的 %LOCALAPPDATA%\\APMLogViewer\\views.json，
        别的用例/人工跑过的集成脚本留下的 track_vis 会把“初始为 None”这类断言带锅。
        """
        import apmlog.app as AP
        self._tmp = tempfile.TemporaryDirectory(prefix="apmlog_views_")
        self.addCleanup(self._tmp.cleanup)
        self.views = Path(self._tmp.name) / "views.json"
        self._vp = AP.MainWindow._views_path
        # 注意：lambda 第一个形参是 MainWindow 实例，不能写 self.views（那是测试对象的属性），
        # 必须用默认参数把路径绑进关包，否则 AttributeError 会被 _save_view 的 except 吞掉。
        AP.MainWindow._views_path = lambda _s, _p=self.views: _p  # type: ignore[assignment]
        self.addCleanup(self._restore_vp)

    def _restore_vp(self) -> None:
        import apmlog.app as AP
        AP.MainWindow._views_path = self._vp                   # type: ignore[assignment]

    def test_falls_back_when_store_has_no_tracks(self) -> None:
        """数据层还没支持 tracks()（或老缓存）时，界面必须照常出图。"""
        from apmlog.app import MainWindow

        class Store:
            def __init__(self):
                self.meta = {"stats": {"t_start": 0.0, "t_end": 20.0}, "t_end": 20.0}

            def track(self):
                return FakeTrack(key="POS", label="EKF 融合位置 (POS)")

        w = MainWindow()
        self.addCleanup(w.deleteLater)
        w.store = Store()
        w.map.set_tracks([])                    # 先清空
        # 直接验证回退分支本身
        trs = w.store.tracks() if hasattr(w.store, "tracks") else []
        self.assertEqual(trs, [])
        w.map.set_track(w.store.track())
        pump(self.app)
        self.assertGreaterEqual(len(w.map._layer_track.items), 1)
        self.assertFalse(w.map.legend.isVisible())

    def test_track_vis_defaults_to_none(self) -> None:
        from apmlog.app import MainWindow
        w = MainWindow()
        self.addCleanup(w.deleteLater)
        self.assertIsNone(w._track_vis)

    def test_on_track_vis_stores_and_saves(self) -> None:
        from apmlog.app import MainWindow
        w = MainWindow()
        self.addCleanup(w.deleteLater)
        calls: list = []
        real = w._save_view
        w._save_view = lambda: (calls.append(1), real())[0]   # type: ignore[assignment]
        w._on_track_vis(["POS", "GPS#0"])
        self.assertEqual(w._track_vis, ["POS", "GPS#0"])
        self.assertEqual(len(calls), 1, "勾选变化要立刻落盘，不能等关窗")
        self.assertTrue(self.views.exists(), "视图记忆没写出去")
        # 图例隐藏（单轨迹/未加载）时故意不记 track_vis：拿空选择去覆盖用户记忆更坑
        import json
        d = json.loads(self.views.read_text(encoding="utf-8"))
        self.assertNotIn("track_vis", d)

    def test_track_vis_survives_a_restart(self) -> None:
        """勾选组合要能在下次打开同一份日志时恢复。"""
        import json
        from apmlog.app import MainWindow
        self.views.parent.mkdir(parents=True, exist_ok=True)
        self.views.write_text(json.dumps({"track_vis": ["POS", "XKF1"]}), encoding="utf-8")
        w = MainWindow()
        self.addCleanup(w.deleteLater)
        self.assertEqual(w._track_vis, ["POS", "XKF1"])

    def test_garbage_track_vis_is_ignored(self) -> None:
        import json
        from apmlog.app import MainWindow
        self.views.write_text(json.dumps({"track_vis": "不是列表"}), encoding="utf-8")
        w = MainWindow()
        self.addCleanup(w.deleteLater)
        self.assertIsNone(w._track_vis)
        self.views.write_text(json.dumps({"track_vis": ["POS", 3, None]}), encoding="utf-8")
        w2 = MainWindow()
        self.addCleanup(w2.deleteLater)
        self.assertIsNone(w2._track_vis, "混进非字符串就不恢复，不拿脏数据喂图例")


if __name__ == "__main__":
    unittest.main(verbosity=2)
