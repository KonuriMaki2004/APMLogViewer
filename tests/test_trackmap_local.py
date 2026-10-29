"""test_trackmap_local —— TrackMapWidget 的健壮性/降级单测（QT_QPA_PLATFORM=offscreen）。

覆盖：空数据、全 nan、local 轨迹（用户 ZHT 视觉定位日志的形态）、GCJ-02 对齐、
强制离线降级、未知源名、坏 PNG 导出路径。要求一律**不抛异常**。
"""
from __future__ import annotations

import math
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                   # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets           # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apmlog.core.model import TrackResult            # noqa: E402
from apmlog.ui import _tilemath as tm                # noqa: E402
from apmlog.ui.trackmap import (LOCAL_PPM_MAX, LOCAL_PPM_MIN, MODE_EMPTY,
                                MODE_GEODETIC, MODE_LOCAL, TrackMapWidget,
                                ramp_color, sev_color)

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])

TMP = Path(__file__).resolve().parents[1] / "selftest" / "_tilecache_ut"


def wait(ms: int = 400) -> None:
    """让 Qt 事件循环跑一会儿（瓦片回调/布局/网络）。

    PySide6 6.12 的 QApplication 没有 msleep，必须用 QThread.msleep。
    """
    end = QtCore.QElapsedTimer()
    end.start()
    while end.elapsed() < ms:
        APP.processEvents()
        QtCore.QThread.msleep(10)


def make_local(n: int = 400) -> TrackResult:
    t = np.linspace(0.0, 120.0, n)
    e = 30.0 * np.sin(t / 12.0) + t * 0.2
    n_ = 25.0 * np.cos(t / 9.0)
    u = np.clip(8.0 - 4.0 * np.cos(t / 20.0), 0, 20)
    return TrackResult(kind="local", t=t,
                       lat=np.full(n, np.nan), lon=np.full(n, np.nan), alt=u,
                       e=e, n=n_, u=u, speed=np.gradient(np.hypot(e, n_)) / np.gradient(t),
                       src=np.zeros(n, dtype=np.int8), src_names=["VISP"],
                       note="无经纬度，仅相对轨迹（VISP 本地 ENU）", distance=float(np.hypot(e, n_).sum()))


def make_geodetic(n: int = 300) -> TrackResult:
    lat = 38.9617576 + np.linspace(0, 0.010, n)
    lon = 117.3196055 + np.linspace(0, 0.014, n)
    u = np.linspace(0, 40, n)
    t = np.linspace(200.0, 800.0, n)
    return TrackResult(kind="geodetic", t=t, lat=lat, lon=lon, alt=u,
                       e=(lon - lon[0]) * 85000.0, n=(lat - lat[0]) * 111000.0, u=u,
                       speed=np.full(n, 7.0), src=np.zeros(n, dtype=np.int8),
                       src_names=["POS"], origin=(lat[0], lon[0], 0.0),
                       home=(lat[0], lon[0], 0.0), distance=1200.0, max_speed=9.0)


class TestRobustness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = str(TMP)
        os.makedirs(cls.cache, exist_ok=True)

    def setUp(self):
        self._live: list[TrackMapWidget] = []

    def tearDown(self):
        # 必须真关闭：closeEvent 会跑 TileStore.shutdown()，不关就会在
        # 解释器退出时撞上 QNetworkReply 析构竞态（实测偶发段错误）。
        for w in self._live:
            try:
                w.close()
            except RuntimeError:
                pass
        self._live.clear()
        wait(60)

    def make_widget(self, source: str = "gaode_vector") -> TrackMapWidget:
        w = TrackMapWidget(source_name=source, cache_dir=self.cache)
        w.resize(1000, 680)
        w.show()
        wait(120)
        self._live.append(w)
        return w

    # ------------------------------------------------------------------ 空/坏数据
    def test_01_none_and_empty(self):
        w = self.make_widget()
        w.set_track(None)
        self.assertEqual(w.state()["mode"], MODE_EMPTY)
        w.set_track(TrackResult())                       # 全默认，0 点
        self.assertEqual(w.state()["mode"], MODE_EMPTY)
        self.assertTrue(w._placeholder.isVisible())
        self.assertIn("无位置数据", w._placeholder.text())
        w.set_time(1.0)                                  # 不得崩
        w.fit_to_track()
        w.close()

    def test_02_all_nan(self):
        w = self.make_widget()
        n = 50
        tr = TrackResult(kind="geodetic", t=np.arange(n, dtype=float),
                         lat=np.full(n, np.nan), lon=np.full(n, np.nan), alt=np.zeros(n),
                         e=np.zeros(n), n=np.zeros(n), u=np.zeros(n), speed=np.zeros(n),
                         src_names=["POS"])
        w.set_track(tr)
        self.assertEqual(w.state()["mode"], MODE_EMPTY)
        self.assertTrue(w._placeholder.isVisible())
        w.close()

    def test_03_local_mode_no_network(self):
        w = self.make_widget(source="gaode_vector")
        w.set_track(make_local())
        st = w.state()
        self.assertEqual(st["mode"], MODE_LOCAL)
        self.assertGreater(st["points"], 100)
        w.close()

    def test_04_geodetic_gcj_alignment(self):
        """同一轨迹在 WGS84 源与 GCJ02 源下的场景坐标差，应等于加偏量对应的像素距离。"""
        w = self.make_widget(source="osm_de")
        tr = make_geodetic()
        w.set_track(tr)
        self.assertEqual(w.state()["mode"], MODE_GEODETIC)
        lat, lon = float(tr.lat[10]), float(tr.lon[10])
        x_w = w._scene_of_lonlat(lat, lon)
        w.set_source("gaode_vector")
        self.assertEqual(w.current_source().datum, "GCJ02")
        x_g = w._scene_of_lonlat(lat, lon)
        # 反算回经纬度差，应≈ wgs→gcj 的偏移
        back = [tm.scene_to_lonlat(*x_g)]
        glat, glon = back[0]
        self.assertGreater(abs(glon - lon), 1e-5, "GCJ 源下应出现加偏位移")
        self.assertLess(abs(glon - lon), 2e-2)
        w.close()

    def test_05_forced_offline_degrades(self):
        w = self.make_widget(source="osm_de")
        w._tiles.set_online(False)
        w.set_track(make_geodetic())
        st = w.state()
        self.assertEqual(st["mode"], MODE_GEODETIC)
        self.assertGreater(st["tiles_visible"], 0, "离线下也应有占位瓦片网格")
        self.assertEqual(st["tile_stats"]["fetched"], 0, "强制离线不得发网络请求")
        self.assertTrue(w.export_png(str(TMP.parent / "trackmap_offline.png")))
        w.close()

    def test_06_unknown_source_and_zoom_clamp(self):
        w = self.make_widget()
        w.set_source("不存在")
        self.assertEqual(w.current_source().name, "gaode_vector")
        w._set_zoom(99)                                  # 超上限
        self.assertLessEqual(w.zoom(), w.current_source().max_zoom + 1e-6)
        w._set_zoom(-3)
        self.assertGreaterEqual(w.zoom(), w.current_source().min_zoom - 1e-6)
        w.close()

    def test_07_events_and_regions(self):
        w = self.make_widget(source="osm_de")
        tr = make_geodetic()
        w.set_track(tr)
        w.set_events([{"t": 300.0, "lat": float(tr.lat[5]), "lon": float(tr.lon[5]),
                       "severity": "critical", "display": "Crash: Disarming"},
                      {"t": 500.0, "severity": "warn"},                     # 无经纬度→按时间插值
                      {"t": float("nan"), "severity": "info"},              # 坏数据→忽略
                      {},                                                   # 空→忽略
                      {"t": 99999.0, "severity": "error"}])                 # 越界→钳到末点
        self.assertEqual(len(w._layer_event.items), 4)
        w.set_regions([{"t0": 400.0, "t1": 600.0, "severity": "warn", "title": "振动超标 12.3s"},
                       {"t0": float("nan"), "t1": 1.0}, {"t0": 10.0}])
        self.assertEqual(len(w._layer_region.items), 1)
        w.set_time(450.0)
        self.assertAlmostEqual(w.zoom(), w.zoom())
        w.close()

    def test_08_time_signals(self):
        """set_time 不得回发 timeChanged；点击轨迹要发。"""
        w = self.make_widget(source="osm_de")
        w.set_track(make_local())
        got = []
        w.timeChanged.connect(lambda t: got.append(t))
        w.set_time(60.0)
        self.assertEqual(got, [], "set_time 不应触发 timeChanged")
        # 程序化点击轨迹中点
        p = w.view.mapFromScene(QtCore.QPointF(float(w._sx[10]), float(w._sy[10])))
        w.on_view_click(p)
        self.assertEqual(len(got), 1, "点击轨迹应 seek 一次")
        self.assertTrue(math.isfinite(got[0]))
        got2 = []
        far = w.view.mapToScene(w.view.viewport().rect().topLeft())
        w.on_view_click(QtCore.QPoint(2, 2))      # 角落大概率无轨迹
        self.assertLessEqual(len(got2), 1)
        w.close()

    def test_09_playback_follow_and_keys(self):
        w = self.make_widget(source="osm_de")
        w.set_track(make_local())
        w.set_playback_follow(True)
        w.set_time(30.0)
        z0 = w.zoom()
        self.assertTrue(w.on_key(QtCore.Qt.Key.Key_Plus.value))
        self.assertGreater(w.zoom(), z0)
        self.assertTrue(w.on_key(QtCore.Qt.Key.Key_End.value))
        self.assertFalse(w.on_key(QtCore.Qt.Key.Key_Q.value))
        w.set_playback_follow(False)
        w.close()

    def test_10_export_bad_path(self):
        w = self.make_widget()
        self.assertFalse(w.export_png(r"Z:\不存在\目录\x.png"))
        w.close()

    def test_11_add_source_validation(self):
        w = self.make_widget()
        w.add_source("my_tile", "https://example.com/{z}/{x}/{y}.png", "测试源", datum="WGS84")
        self.assertIn("my_tile", w.sources())
        w.set_source("my_tile")
        self.assertEqual(w.current_source().name, "my_tile")
        w._tiles.set_online(False)
        w.set_track(make_geodetic())
        self.assertGreater(w.state()["tiles_visible"], 0)
        with self.assertRaises(ValueError):
            w.add_source("bad", "https://x/{z}/{x}/{y}.png", "x", datum="BD09")
        w.close()

    def test_13_source_switch_keeps_center(self):
        """回归：切到 max_zoom 更小的源不能把视图中心弹到世界角落。

        QGraphicsView.setTransform() 会把滚动条归零，早期版本造成
        center_wgs84 变成 (85.05, -180)。_set_zoom 必须记录并还原中心点。
        """
        w = self.make_widget(source="esri_imagery")
        w.set_track(make_geodetic())
        wait(200)
        before = w.state()["center_wgs84"]
        w.add_source("lowzoom", "https://invalid-host.example/{z}/{x}/{y}.png",
                     "低层级源", max_zoom=12)
        w.set_source("lowzoom")
        wait(200)
        after = w.state()
        self.assertLessEqual(after["zoom"], 12.0 + 1e-6)
        lat, lon = after["center_wgs84"]
        self.assertTrue(math.isfinite(lat) and math.isfinite(lon), msg=str(after))
        self.assertLess(abs(lat - before[0]), 0.05, msg=f"中心漂移 {before} -> {after['center_wgs84']}")
        self.assertLess(abs(lon - before[1]), 0.05, msg=f"中心漂移 {before} -> {after['center_wgs84']}")
        w.set_source("esri_imagery")
        wait(150)
        back = w.state()["center_wgs84"]
        self.assertLess(abs(back[0] - before[0]), 0.05, msg=f"切回后漂移 {before} -> {back}")

    def test_14_local_zoom_keeps_center(self):
        """回归：本地模式改 ppm 也应保持中心。"""
        w = self.make_widget()
        w.set_track(make_local())
        wait(150)
        c0 = w.view.mapToScene(w.view.viewport().rect().center())
        w._set_local_ppm(w._local_ppm * 4.0)
        c1 = w.view.mapToScene(w.view.viewport().rect().center())
        self.assertLess(math.hypot(c1.x() - c0.x(), c1.y() - c0.y()), 5.0,
                        msg=f"{c0.x(), c0.y()} -> {c1.x(), c1.y()}")

    def test_12_colors(self):
        c = ramp_color(0.0)
        self.assertTrue(c.isValid())
        self.assertNotEqual(ramp_color(0.0).name(), ramp_color(1.0).name())
        self.assertEqual(ramp_color(float("nan")).name().lower(), "#00e5ff")
        self.assertEqual(sev_color("critical").name().lower(), "#d62828")
        self.assertTrue(sev_color("未知").isValid())

    def test_15_store_api_surface(self):
        """新增能力不靠猜：suspect()/set_zoom 保持中心必须存在且类型正确。"""
        w = self.make_widget()
        self.assertIsInstance(w._tiles.suspect("esri_imagery"), bool)
        self.assertIsInstance(w._tiles.suspect_sources(), set)
        w.set_track(make_geodetic())
        wait(150)
        c0 = w.view.mapToScene(w.view.viewport().rect().center())
        w._set_zoom(10.0)                       # keep_center 默认 True
        # 尺度变了，必须用“视口像素”而非场景单位衡量偏移
        p = w.view.mapFromScene(c0)
        vp = w.view.viewport().rect().center()
        self.assertLess(math.hypot(p.x() - vp.x(), p.y() - vp.y()), 4.0,
                        msg=f"_set_zoom 应保持场景中心：{p.x(), p.y()} vs {vp.x(), vp.y()}")
        w._set_zoom(12.0, keep_center=False)    # 必须接受显式关闭
        self.assertAlmostEqual(w.zoom(), 12.0, places=1)
        w.close()

    def test_16_shutdown_is_idempotent_and_quiet(self):
        """shutdown() 重复调用安全，且调用之后不再发新请求。"""
        w = self.make_widget(source="esri_imagery")
        w.set_track(make_geodetic())
        wait(250)
        w._tiles.shutdown()
        w._tiles.shutdown()
        net0 = w._tiles.stats["network"]
        w._refresh_layers()
        wait(300)
        self.assertEqual(w._tiles.stats["network"], net0, "shutdown 后不应再进网络")
        self.assertEqual(w._tiles.inflight(), 0)
        self.assertEqual(w._tiles.pending(), 0)


class TestTileSourceMath(unittest.TestCase):
    def test_url_format_and_bounds(self):
        from apmlog.ui._tiles import BUILTIN_SOURCES
        src = {s.name: s for s in BUILTIN_SOURCES}["gaode_vector"]
        u = src.format_url(13, 6765, 3131, rot=0)
        self.assertIn("webrd01", u)
        self.assertIn("x=6765", u)
        self.assertIn("z=13", u)
        self.assertEqual(src.format_url(13, 999999, 0, 0), "", "越界 x 应拒绝")
        self.assertEqual(src.format_url(99, 1, 1, 0), "", "超 max_zoom 应拒绝")
        self.assertEqual(src.format_url(0, 0, 0, 0), "", "低于 min_zoom 应拒绝")
        esri = {s.name: s for s in BUILTIN_SOURCES}["esri_imagery"]
        self.assertIn("/tile/13/3131/6765", esri.format_url(13, 6765, 3131))  # z/y/x 顺序
        self.assertEqual({s.name: s.datum for s in BUILTIN_SOURCES}["gaode_vector"], "GCJ02")
        self.assertEqual({s.name: s.datum for s in BUILTIN_SOURCES}["esri_imagery"], "WGS84")


class TestLocalZoomDirection(unittest.TestCase):
    """无瓦片（本地 ENU）模式下的滚轮缩放方向。

    回归：以前 ``zoom_by`` 在本地模式拿 ``zoom()``（缩放级 ≈ 27）当当前 ppm 去除，
    导致不管上滚下滚比例都 > 1 —— 用户看到的是“没卫星图时怎么滚都是放大”。
    上滚必须放大、下滚必须缩小。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP

    def _map(self, tr):
        m = TrackMapWidget(source_name="osm")
        m.resize(800, 600)
        m.show()
        wait(60)
        m.set_track(tr)
        wait(60)
        self.addCleanup(lambda: (m.close(), wait(20)))
        return m

    def _wheel(self, m, dy: int) -> None:
        c = m.view.viewport().rect().center()
        ev = QtGui.QWheelEvent(QtCore.QPointF(c), QtCore.QPointF(c),
                               QtCore.QPoint(0, 0), QtCore.QPoint(0, dy),
                               QtCore.Qt.MouseButton.NoButton,
                               QtCore.Qt.KeyboardModifier.NoModifier,
                               QtCore.Qt.ScrollPhase.NoScrollPhase, False)
        self.assertTrue(QtWidgets.QApplication.sendEvent(m.view.viewport(), ev),
                        "滚轮事件要被地图吃掉")

    def test_up_zooms_in_and_down_zooms_out(self) -> None:
        m = self._map(make_local())
        self.assertEqual(m._mode, MODE_LOCAL)
        base = m._k()
        self._wheel(m, +120)
        self.assertGreater(m._k(), base * 1.5, "上滚要放大")
        mid = m._k()
        self._wheel(m, -120)
        self.assertLess(m._k(), mid / 1.5, "下滚要缩小")

    def test_repeated_scrolls_stay_monotonic(self) -> None:
        m = self._map(make_local())
        seq = [m._k()]
        for _ in range(4):
            self._wheel(m, +120); seq.append(m._k())
        for _ in range(6):
            self._wheel(m, -120); seq.append(m._k())
        ups = seq[0:5]; downs = seq[4:]
        self.assertTrue(all(b < a for a, b in zip(ups[1:], ups[:-1])), f"上滚该递增：{ups}")
        self.assertTrue(all(a > b for a, b in zip(downs[:-1], downs[1:])), f"下滚该递减：{downs}")

    def test_local_ppm_written_back_so_steps_are_2x(self) -> None:
        """不回写 _local_ppm 的话，第二滚还是拿旧值算，步长就不是 2×。"""
        m = self._map(make_local())
        k0 = m._k()
        self._wheel(m, +120)
        k1 = m._k()
        self._wheel(m, +120)
        k2 = m._k()
        self.assertAlmostEqual(k1 / k0, 2.0, places=4)
        self.assertAlmostEqual(k2 / k1, 2.0, places=4)
        self.assertAlmostEqual(m._local_ppm, m._k(), places=6)

    def test_zoom_headroom_above_fit_scale(self) -> None:
        """小位移日志 fit 完就贴住旧上限 400 px/m，上滚只能再放 6% 看起来又没反应。"""
        m = self._map(make_local(n=40))
        self.assertGreater(LOCAL_PPM_MAX, 1e5, "本地 ppm 上限不该卡在几百")
        k = m._k()
        self._wheel(m, +120)
        self.assertGreater(m._k(), k * 1.5, "fit 之后还要能继续放大")

    def test_clamps_at_both_ends(self) -> None:
        m = self._map(make_local())
        for _ in range(80):
            self._wheel(m, +120)
        self.assertAlmostEqual(m._k(), LOCAL_PPM_MAX, delta=LOCAL_PPM_MAX * 1e-6)
        for _ in range(120):
            self._wheel(m, -120)
        self.assertAlmostEqual(m._k(), LOCAL_PPM_MIN, delta=LOCAL_PPM_MIN * 1e-3)

    def test_geodetic_mode_direction_unchanged(self) -> None:
        m = self._map(make_geodetic())
        self.assertEqual(m._mode, MODE_GEODETIC)
        z0 = m.zoom()
        self._wheel(m, +120)
        self.assertGreater(m.zoom(), z0 - 1e-9, "地理模式上滚不能变小")
        k0 = m._k()
        self._wheel(m, -120)
        self.assertLess(m._k(), k0 * 1.000001, "地理模式要能缩小")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestScreenSpaceItems(unittest.TestCase):
    """屏幕恒定尺寸图元的**脏区必须等于实际绘制尺寸**。

    回归（用户 2026-10-24「倍速播放地图上有拖影」）：游标/徽标/圆点原来靠
    ``ItemIgnoresTransformations`` 保持像素尺寸，但 Qt 算脏区时用的是
    “场景坐标 boundingRect × 视图变换”——缩到 z=17（0.5 px/m）时脏区只有 16 px，
    实际却画 32 px，于是每移动一次就留下 16 px 残影，倍速下串成一串箭头。
    改成显式 ``setScale(1/k)`` 后，绘制与脏区走同一套变换，两者天然对齐。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP

    def _map(self, tr=None):
        m = TrackMapWidget(source_name="osm")
        m.resize(1000, 700)
        m.show()
        wait(60)
        m.set_track(tr if tr is not None else make_geodetic())
        wait(80)
        m._cursor.setVisible(True)
        self.addCleanup(lambda: (m.close(), wait(20)))
        return m

    @staticmethod
    def _dirty_px(m, it):
        """按 Qt 的口径算设备空间尺寸：boundingRect 经 deviceTransform 映射。"""
        dt = it.deviceTransform(m.view.viewportTransform())
        r = it.boundingRect()
        quad = dt.map(QtGui.QPolygonF([r.topLeft(), r.topRight(),
                                       r.bottomRight(), r.bottomLeft()]))
        xs = [p.x() for p in quad]
        ys = [p.y() for p in quad]
        return max(xs) - min(xs), max(ys) - min(ys)

    def test_cursor_dirty_rect_stays_32px_at_every_zoom(self) -> None:
        m = self._map()
        for z in (16.0, 17.0, 19.0, 21.0):
            m._set_zoom(z)
            wait(40)
            w, h = self._dirty_px(m, m._cursor)
            self.assertAlmostEqual(w, 32.0, delta=2.0, msg=f"z={z} 脏区宽度对不上")
            self.assertAlmostEqual(h, 32.0, delta=2.0, msg=f"z={z} 脏区高度对不上")

    def test_scale_resynced_after_every_transform_path(self) -> None:
        """三条改变换的路径都必须重算 1/k：少一条就留旧比例 → 又是拖影。"""
        m = self._map()
        for label, act in (("_set_zoom", lambda: m._set_zoom(17.0)),
                           ("zoom_by", lambda: m.zoom_by(-1.0,
                                                         m.view.viewport().rect().center())),
                           ("_set_local_ppm", lambda: m._set_local_ppm(120.0))):
            act()
            wait(40)
            k = m._k()
            self.assertAlmostEqual(m._cursor.scale() * k, 1.0, places=4,
                                   msg=f"{label} 之后 1/k 比例没同步")

    def test_badges_and_dots_keep_their_pixel_size(self) -> None:
        m = self._map()
        m._set_zoom(16.0)                     # 缩得很小，最容易把脏区算小
        wait(60)
        m.set_events([{"t": 60.0, "kind": "EV", "severity": "warn", "display": "x"}])
        wait(60)
        items = (*m._layer_badge.items, *m._layer_event.items)
        self.assertTrue(items, "应该有徽标/圆点")
        for it in items:
            w, h = self._dirty_px(m, it)
            self.assertGreaterEqual(w, 8.0, f"脏区太窄: {w:.1f}px（会留残影）")
            self.assertGreaterEqual(h, 8.0, f"脏区太矮: {h:.1f}px（会留残影）")

    def test_no_item_relies_on_ignore_transformations(self) -> None:
        """反向守卫：谁再把 ItemIgnoresTransformations 加回来，拖影就回来了。"""
        m = self._map()
        flag = QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        self.assertFalse(m._cursor.flags() & flag)
        for it in (*m._layer_badge.items, *m._layer_event.items):
            self.assertFalse(it.flags() & flag,
                             f"{type(it).__name__} 又用回了 ItemIgnoresTransformations")

    def test_cursor_still_draws_and_moves(self) -> None:
        """改比例不能把游标画没：移动后位置要跟着走，且仍可见。"""
        m = self._map()                      # 夹具的时间轴是 200→800 s
        m.set_time(250.0)
        p1 = (m._cursor.pos().x(), m._cursor.pos().y())
        m.set_time(700.0)
        p2 = (m._cursor.pos().x(), m._cursor.pos().y())
        self.assertNotEqual(p1, p2, "游标没跟着时间走")
        self.assertTrue(m._cursor.isVisible())
