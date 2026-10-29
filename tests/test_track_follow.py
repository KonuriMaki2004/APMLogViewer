# -*- coding: utf-8 -*-
"""tests/test_track_follow.py —— 地图轨迹播放（跟随时间线逐点绘制）。

用户 2026-10-24：「地图页加轨迹播放：轨迹跟随时间线播放、实时逐点绘制，支持多条轨迹
同时绘制以便对比…默认打开日志显示全部轨迹；点到/拖到某个位置，轨迹显示到对应时刻。
时间线上方、'地图跟随'前加'全部轨迹显示'按钮，点击恢复完整轨迹，再点切回跟随。」

验收点：
1. 默认（按钮按下）= 完整轨迹；切到跟随 = 只画到游标处（主轨迹与副轨迹都裁）；
2. 多条轨迹各自颜色 + 名称标签；
3. 高频 time 更新要合并重画（不跟着 20fps 全量重绘）；
4. 图例不许说谎：取消勾选主轨迹后，任何重画都不许把它画回来。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                             # noqa: E402
from PySide6 import QtWidgets                                  # noqa: E402

from apmlog.ui.trackmap import TrackMapWidget                  # noqa: E402
from tests.test_multitrack_ui import FakeTrack, pump, qapp     # noqa: E402

NL = chr(10)


def _mk(n=41, label="EKF 融合位置 (POS)", key="POS") -> TrackMapWidget:
    m = TrackMapWidget()
    m.set_tracks([FakeTrack(key=key, label=label, n=n)])
    pump(qapp())
    return m


def _spy_emit(m, sink):
    """把 _emit 包一层，记录每次真正画出去的 x 范围。"""
    orig = m._emit

    def spy(layer, xs, ys, *a, **k):
        xs = np.asarray(xs, dtype=float)
        if xs.size:
            sink.append((float(np.nanmin(xs)), float(np.nanmax(xs))))
        return orig(layer, xs, ys, *a, **k)

    m._emit = spy
    return orig


class TestFollowClipping(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_default_is_full_track(self) -> None:
        m = _mk()
        self.assertFalse(m.track_follow(), "默认必须画完整轨迹")
        self.assertEqual(m._playhead, float("inf"))

    def test_primary_clipped_at_playhead(self) -> None:
        m = _mk(n=41)
        tr = m._tr
        t_mid = float(tr.t[20])
        m.set_track_follow(True)
        m.set_playhead(t_mid)
        seen: list = []
        _spy_emit(m, seen)
        m._redraw_follow()
        self.assertTrue(seen, "跟随模式下应该画了东西")
        self.assertAlmostEqual(max(hi for _lo, hi in seen), float(m._sx[20]), places=3,
                               msg="画出的最后一个点必须正好是游标时刻的位置")

    def test_switching_back_restores_full(self) -> None:
        m = _mk(n=41)
        m.set_track_follow(True)
        m.set_playhead(float(m._tr.t[10]))
        seen: list = []
        _spy_emit(m, seen)
        m._redraw_follow()
        short = max(hi for _lo, hi in seen)
        seen2: list = []
        _spy_emit(m, seen2)
        m.set_track_follow(False)
        full = max(hi for _lo, hi in seen2)
        self.assertGreater(full, short, "切回完整轨迹后要画到终点")
        self.assertAlmostEqual(full, float(m._sx[-1]), places=3)

    def test_secondary_is_clipped_too(self) -> None:
        m = TrackMapWidget()
        a = FakeTrack(key="POS", label="POS", n=41)
        b = FakeTrack(key="GPS#0", label="GPS1", n=41, dlat=0.0002)
        m.set_tracks([a, b])
        pump(self.app)
        self.assertTrue(m.legend.is_checked("GPS#0"))
        m.set_track_follow(True)
        cut = float(b.t[20])
        xs, ys = m._xy_for(b, until=cut)
        self.assertTrue(xs.size)
        self.assertLessEqual(xs.size, 21, "副轨迹也必须只画到游标处")
        xf, _yf = m._xy_for(b)
        self.assertGreater(xf.size, xs.size, "不裁剪时应更长")

    def test_time_update_moves_playhead_only_in_follow_mode(self) -> None:
        m = _mk()
        m.set_time(5.0)
        self.assertEqual(m._playhead, float("inf"), "完整轨迹模式下不需要裁剪")
        m.set_track_follow(True)
        m.set_time(5.0)
        self.assertEqual(m._playhead, 5.0)

    def test_redraw_is_coalesced(self) -> None:
        m = _mk()
        draws = []
        m._draw_track = lambda: draws.append(1)
        m._draw_others = lambda: None
        m._sync_screen_items = lambda: None
        m.schedule_layer_refresh = lambda: None
        m.set_track_follow(True)                 # 模式切换本身重画一次
        base = len(draws)
        for t in np.linspace(1.0, 40.0, 20):     # 模拟 20fps 的播放
            m.set_playhead(float(t))
        self.assertLessEqual(len(draws) - base, 1,
                             "一串播放时间更新只该排一次重画（靠定时器合并）")
        self.assertTrue(m._ph_timer.isActive())


class TestTrackNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _badges(self, layer):
        return [getattr(it, "_text", "") for it in layer.items
                if it.__class__.__name__ == "_Badge"]

    def test_names_shown_when_multiple_tracks(self) -> None:
        m = TrackMapWidget()
        a = FakeTrack(key="POS", label="EKF 融合位置", n=30)
        b = FakeTrack(key="GPS#0", label="原始 GNSS", n=30, dlat=0.0002)
        m.set_tracks([a, b])
        pump(self.app)
        m._rebuild_tracks()
        names = self._badges(m._layer_track2)
        self.assertIn("原始 GNSS", names, "副轨迹要有名称标签")
        self.assertIn("EKF 融合位置", self._badges(m._layer_badge), "主轨迹也要标名称")

    def test_no_name_badge_for_single_track(self) -> None:
        m = _mk()
        m._rebuild_tracks()
        names = self._badges(m._layer_badge)
        self.assertNotIn("EKF 融合位置 (POS)", names, "单轨迹有标题，不必再占一块地图")

    def test_track_colors_differ(self) -> None:
        m = TrackMapWidget()
        m.set_tracks([FakeTrack(key="POS", label="POS", n=20),
                      FakeTrack(key="GPS#0", label="GPS", n=20, dlat=0.0002)])
        pump(self.app)
        colors = [e.color for e in m._entries]
        self.assertEqual(len(set(colors)), len(colors), "每条轨迹颜色必须不同")


class TestLegendDoesNotLie(unittest.TestCase):
    """反向守卫：取消勾选主轨迹后，任何重画都不许把它画回来（原先会）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_unchecking_primary_hides_it_even_after_redraw(self) -> None:
        m = TrackMapWidget()
        m.set_tracks([FakeTrack(key="POS", label="POS", n=20),
                      FakeTrack(key="GPS#0", label="GPS", n=20, dlat=0.0002)])
        pump(self.app)
        m._on_legend(["GPS#0"])
        self.assertTrue(m._primary_hidden())
        m._rebuild_tracks()                          # 重画（喂姿态等路径也会走到）
        vis = [it.isVisible() for it in m._layer_track.items]
        self.assertTrue(vis and not any(vis), "主轨迹已取消勾选，重画后不许再显示")

    def test_primary_name_badge_follows_the_checkbox(self) -> None:
        m = TrackMapWidget()
        m.set_tracks([FakeTrack(key="POS", label="POS", n=20),
                      FakeTrack(key="GPS#0", label="GPS", n=20, dlat=0.0002)])
        pump(self.app)
        m._on_legend(["GPS#0"])
        before = len(m._layer_badge.items)
        m._rebuild_tracks()
        self.assertLessEqual(len(m._layer_badge.items), before, "主轨迹不画就不该有它的标签")


class TestPlayBarButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_button_sits_before_map_follow(self) -> None:
        from apmlog.app import PlayBar
        bar = PlayBar()
        lay = bar.layout()
        order = []
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None:
                order.append(w)
        self.assertIn(bar.btn_all_tracks, order)
        self.assertLess(order.index(bar.btn_all_tracks), order.index(bar.chk_follow),
                        "「全部轨迹显示」必须在「地图跟随」前面")
        self.assertEqual(bar.btn_all_tracks.text(), "全部轨迹显示")
        self.assertTrue(bar.btn_all_tracks.isChecked(), "默认按下 = 显示全部轨迹")
        self.assertTrue(bar.btn_all_tracks.isCheckable())

    def test_toggle_emits_mode(self) -> None:
        from apmlog.app import PlayBar
        bar = PlayBar()
        got: list[bool] = []
        bar.trackModeToggled.connect(got.append)
        bar.btn_all_tracks.setChecked(False)
        bar.btn_all_tracks.setChecked(True)
        self.assertEqual(got, [False, True])

    def test_app_handler_flips_map_follow(self) -> None:
        from apmlog.app import MainWindow
        from apmlog.ui import settings as st
        d = tempfile.mkdtemp(prefix="tf_")
        prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(d) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(prev))
        w = MainWindow()
        self.addCleanup(w.close)
        w.map.set_tracks([FakeTrack(key="POS", label="POS", n=20)])
        pump(self.app)
        w._track_mode_changed(False)
        self.assertTrue(w.map.track_follow(), "按钮弹起 = 跟随时间线")
        w._track_mode_changed(True)
        self.assertFalse(w.map.track_follow(), "按钮按下 = 完整轨迹")


if __name__ == "__main__":
    unittest.main()
