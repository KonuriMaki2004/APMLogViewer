# -*- coding: utf-8 -*-
"""tests/test_playback.py —— 时间线播放：从点击处起播、倍速、地图跟随、到点自动停。

播放引擎用假 store + 直接推进 _play_step，不靠真实日志也不靠睡够时间，
这样“倍速对不对、末尾停不停、手动定位会不会打断播放”都是确定性断言。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_playback.py -q
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
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtWidgets                              # noqa: E402

from apmlog.ui import settings as st                               # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 20) -> None:
    for _ in range(times):
        app.processEvents()


def wait(app: QtWidgets.QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.003)


class _Track:
    n_points = 0
    t = []


class FakeStore:
    """只满足播放路径需要的最小接口：track() 不给可查的点，close() 给 closeEvent 用。"""

    def track(self) -> _Track:
        return _Track()

    def close(self) -> None:
        pass


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.app = qapp()
        st.install_tip_style(self.app)
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_play_")
        self.addCleanup(self._dir.cleanup)
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self._dir.name) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        self.s = st.Settings.instance()
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.resize(1300, 860)
        self.w.show()
        pump(self.app)
        # 时间线/地图/曲线里只有 _on_time 会调后两者，这里换成记录器专注测播放引擎
        self.seen: list[float] = []

        def rec(t: float) -> None:
            self.seen.append(float(t))

        self.w.plot.set_time = rec                                              # type: ignore[assignment]
        self.w.map.set_time = rec                                               # type: ignore[assignment]
        self.w.map.set_playback_follow = lambda on: None                        # type: ignore[assignment]
        # 加载失败弹窗是模态的，offscreen 下会永久阻塞：换成记录器
        self.errors: list[str] = []
        self.w._on_failed = lambda msg, path=None: self.errors.append(f"{msg}|{path}")

    def tearDown(self) -> None:
        self.w._stop_play("")
        self.w.close()
        pump(self.app, 5)
        self.w.deleteLater()
        pump(self.app, 5)

    def arm(self, t0: float = 100.0, t1: float = 300.0) -> None:
        """假装已经加载了一份日志，并把时间线区间设成 [t0, t1]。"""
        self.w.store = FakeStore()
        self.w.timeline.set_span(t0, t1)
        self.w.play_bar.set_available(True)


class TestPlayBarWidget(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()          # 没有 QApplication 就 new QWidget 会直接崩解释器

    def test_initial_state(self) -> None:
        from apmlog.app import PlayBar
        b = PlayBar()
        self.addCleanup(b.deleteLater)
        self.assertEqual(b.btn_play.text(), "▶ 播放")
        self.assertFalse(b.btn_play.isChecked())
        self.assertEqual(b.rate(), 4.0, "默认 4×：十分钟飞行两分半播完")
        self.assertFalse(b.chk_follow.isChecked())

    def test_set_playing_does_not_re_emit_toggle(self) -> None:
        from apmlog.app import PlayBar
        b = PlayBar()
        self.addCleanup(b.deleteLater)
        fired: list[bool] = []
        b.playToggled.connect(fired.append)
        b.set_playing(True)
        self.assertEqual(b.btn_play.text(), "⏸ 暂停")
        self.assertTrue(b.btn_play.isChecked())
        self.assertEqual(fired, [], "程序设置状态不能反过来再发一次信号，否则和主窗互相触发")
        b.set_playing(False)
        self.assertEqual(fired, [])

    def test_readout_percent_is_relative_to_visible_span(self) -> None:
        from apmlog.app import PlayBar
        b = PlayBar()
        self.addCleanup(b.deleteLater)
        b.set_readout(200.0, 100.0, 300.0, False)
        self.assertIn("50%", b.lbl_time.text(), "日志常从非 0s 开始，百分比要按区间算")
        b.set_readout(100.0, 100.0, 300.0, False)
        self.assertIn("0%", b.lbl_time.text())
        b.set_readout(300.0, 100.0, 300.0, True)
        self.assertIn("100%", b.lbl_time.text())
        self.assertTrue(b.lbl_time.text().startswith("▶"))

    def test_rate_selection_and_set_rate(self) -> None:
        from apmlog.app import PlayBar
        b = PlayBar(rate=8.0)
        self.addCleanup(b.deleteLater)
        self.assertEqual(b.rate(), 8.0)
        b.set_rate(32.0)
        self.assertEqual(b.rate(), 32.0)
        b.set_rate(7.0)                                # 不存在的档位：保持原样而不是崩
        self.assertEqual(b.rate(), 32.0)


class TestPlaybackEngine(_Case):
    def test_play_disabled_before_any_log(self) -> None:
        self.assertFalse(self.w.play_bar.btn_play.isEnabled())
        self.w.play_bar.btn_play.click()
        pump(self.app)
        self.assertFalse(self.w._playing)

    def test_start_without_store_is_refused(self) -> None:
        self.w._start_play()
        self.assertFalse(self.w._playing)
        self.assertIn("先加载", self.w.statusBar().currentMessage())

    def test_start_begins_at_current_cursor(self) -> None:
        self.arm(100.0, 300.0)
        self.w._cursor_t = 250.0
        self.w._start_play()
        try:
            self.assertTrue(self.w._playing)
            self.assertAlmostEqual(self.w._play_t, 250.0, places=6)
            self.assertEqual(self.w.play_bar.btn_play.text(), "⏸ 暂停")
        finally:
            self.w._stop_play("")

    def test_cursor_outside_span_restarts_from_window_start(self) -> None:
        self.arm(100.0, 300.0)
        self.w._cursor_t = 5.0                     # 区间外的游标（比如换了缩放窗口）
        self.w._start_play()
        try:
            self.assertAlmostEqual(self.w._play_t, 100.0, places=6)
        finally:
            self.w._stop_play("")

    def test_step_advances_by_rate_times_real_time(self) -> None:
        self.arm(100.0, 300.0)
        self.w._play_t = 100.0
        self.w._play_rate = 100.0
        self.w._playing = True
        self.w._play_clock.start()
        self.w._play_wall = 0.0
        wait(self.app, 0.12)
        self.w._play_step()
        self.assertGreater(self.w._play_t, 100.0)
        self.assertLess(self.w._play_t, 100.0 + 100.0 * 1.5, "倍速换算不该离谱（100× 一秒才 100s）")
        self.assertTrue(self.seen, "每一步都要把时间推给曲线和地图")
        self.w._stop_play("")

    def test_reaches_end_and_auto_pauses(self) -> None:
        self.arm(100.0, 300.0)
        self.w._play_t = 299.99
        self.w._play_rate = 32.0
        self.w._playing = True
        self.w._play_clock.start()
        self.w._play_wall = 0.0
        wait(self.app, 0.08)
        self.w._play_step()
        self.assertFalse(self.w._playing)
        self.assertEqual(self.w.play_bar.btn_play.text(), "▶ 播放")
        self.assertIn("末尾", self.w.statusBar().currentMessage())
        self.assertAlmostEqual(self.w._cursor_t, 300.0, places=6, msg="停在末尾而不是越过")

    def test_manual_click_on_timeline_pauses_playback(self) -> None:
        self.arm(100.0, 300.0)
        self.w._start_play()
        self.assertTrue(self.w._playing)
        self.w._on_timeline_seek(180.0)          # 等价于用户在 180s 那个位置点了一下
        self.assertFalse(self.w._playing)
        self.assertAlmostEqual(self.w._cursor_t, 180.0, places=6)
        self.assertIn("▶ 播放", self.w.play_bar.btn_play.text())
        # 状态栏随后会被 _on_time 的游标提示覆盖，那是正常行为，不断它

    def test_rate_change_mid_play_does_not_jump(self) -> None:
        self.arm(100.0, 300.0)
        self.w._start_play()
        self.w._play_t = 160.0
        before = self.w._play_t
        self.w.play_bar.cb_rate.setCurrentIndex(self.w.play_bar.cb_rate.findData(16.0))
        pump(self.app)
        self.assertAlmostEqual(self.w._play_t, before, places=6, msg="变速只改快慢，不能瞬移")
        self.assertAlmostEqual(self.w._play_rate, 16.0)
        self.assertEqual(float(self.s.get("play_rate")), 16.0)
        self.w._stop_play("")

    def test_follow_checkbox_drives_map_and_persists(self) -> None:
        self.arm()
        calls: list[bool] = []
        self.w.map.set_playback_follow = calls.append                  # type: ignore[assignment]
        self.w.play_bar.chk_follow.setChecked(True)
        pump(self.app)
        self.assertEqual(calls, [True])
        self.assertTrue(bool(self.s.get("play_follow")))
        self.w.play_bar.chk_follow.setChecked(False)
        pump(self.app)
        self.assertEqual(calls, [True, False])
        self.assertFalse(bool(self.s.get("play_follow")))

    def test_reload_stops_playback(self) -> None:
        self.arm()
        self.w._playing = True
        bogus = str(Path(self._dir.name) / "nope.BIN")
        self.w.load(bogus)
        self.assertFalse(self.w._playing)
        self.assertFalse(self.w._play_timer.isActive())
        if self.w.worker is not None:
            self.w.worker.wait(5000)
        pump(self.app)
        self.assertTrue(any("nope.BIN" in e for e in self.errors), "加载失败仍要报出来")

    def test_settings_push_rate_into_the_bar(self) -> None:
        self.arm()
        self.s.set("play_rate", 2.0)
        pump(self.app)
        self.assertAlmostEqual(self.w.play_bar.rate(), 2.0)
        self.s.set("play_follow", True)
        pump(self.app)
        self.assertTrue(self.w.play_bar.chk_follow.isChecked())

    def test_rate_is_clamped_by_settings(self) -> None:
        self.s.set("play_rate", 1e9)
        self.assertEqual(float(self.s.get("play_rate")), 64.0)
        self.s.set("play_rate", "abc")
        self.assertEqual(float(self.s.get("play_rate")), 4.0)


class TestTimelineStillWorks(_Case):
    def test_playhead_and_span_accessor(self) -> None:
        self.arm(120.0, 260.0)
        self.assertEqual(self.w.timeline.span(), (120.0, 260.0))
        self.w._on_time(200.0)
        pump(self.app)
        self.assertAlmostEqual(self.w.timeline._t, 200.0, places=6)

    def test_empty_timeline_readout_stays_sane(self) -> None:
        self.w.play_bar.set_readout(0.0, 0.0, 0.0, False)
        self.assertIn("0%", self.w.play_bar.lbl_time.text())
        self.assertFalse(math.isnan(self.w.timeline.span()[1]))


if __name__ == "__main__":
    unittest.main()
