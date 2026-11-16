# -*- coding: utf-8 -*-
"""tests/test_trackdiff_ui.py —— 地图右上角「轨迹差异」面板的接线。

用户要求：面板挂在轨迹勾选栏**下方**，切换勾选后同步更新，并把口径与基准写出来。
"""
from __future__ import annotations

import math
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                                        # noqa: E402

from PySide6 import QtWidgets                                             # noqa: E402

from apmlog.core.model import TrackResult                                 # noqa: E402
from apmlog.core.trackdiff import compare, fmt_pct                        # noqa: E402
from apmlog.ui.trackmap import TrackMapWidget                             # noqa: E402

LAT0, LON0 = 39.0, 117.0
KX = 111320.0 * math.cos(math.radians(LAT0))
KY = 110574.0

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(n: int = 6) -> None:
    app = qapp()
    for _ in range(n):
        app.processEvents()


def track(key: str, *, north: float = 0.0, seconds: float = 120.0,
          rate: float = 5.0, label: str = "") -> TrackResult:
    t = np.arange(0.0, seconds + 1e-9, 1.0 / rate)
    e = 4.0 * t                                        # 4 m/s 向东 ≈ 480 m
    lat = LAT0 + (north) / KY
    lon = LON0 + e / KX
    return TrackResult(kind="geodetic", t=t, lat=np.full_like(t, lat), lon=lon,
                       alt=np.zeros_like(t), e=e, n=np.full_like(t, north),
                       u=np.zeros_like(t), speed=np.full_like(t, 4.0),
                       key=key, label=label or key)


def three_tracks() -> list[TrackResult]:
    return [track("POS", north=0.0, label="EKF 融合位置 (POS)"),
            track("GPS#0", north=2.0, label="原始 GNSS (GPS1)"),
            track("XKF1", north=5.0, label="EKF 状态位置 (XKF1)")]


class TestDiffPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.maps: list[TrackMapWidget] = []

    def tearDown(self) -> None:
        for m in self.maps:
            m.close()
            m.deleteLater()
        pump(3)

    def _mk(self, tracks) -> TrackMapWidget:
        m = TrackMapWidget(source_name="OSM")
        m.resize(1280, 820)
        m.show()
        pump(4)
        m.set_tracks(tracks)
        pump(4)
        self.maps.append(m)
        return m

    def _boxes(self, m: TrackMapWidget) -> dict[str, QtWidgets.QCheckBox]:
        return {k: m.legend.checkbox(k) for k in ("POS", "GPS#0", "XKF1")
                if m.legend.checkbox(k) is not None}

    def _panel_text(self, m: TrackMapWidget) -> str:
        return " ".join(lab.text() for lab in m.diff.findChildren(QtWidgets.QLabel))

    def test_panel_visible_with_three_tracks(self):
        m = self._mk(three_tracks())
        self.assertTrue(m.diff.isVisible(), "多条轨迹时必须出现差异面板")
        text = self._panel_text(m)
        self.assertIn("轨迹差异", text)
        self.assertIn("总体", text)
        self.assertIn("POS ↔ GPS#0", text)
        self.assertIn("GPS#0 ↔ XKF1", text)
        self.assertIn("均值偏差", text, "口径必须写在面板上，不能只放 tooltip")

    def test_percentages_match_core(self):
        tracks = three_tracks()
        m = self._mk(tracks)
        d = compare(tracks[0], tracks[1])
        text = self._panel_text(m)
        self.assertIn(fmt_pct(d.pct), text)
        self.assertTrue(math.isfinite(d.pct))
        # 2 m 偏差 / 480 m ≈ 0.42%
        self.assertAlmostEqual(d.pct, 2.0 / 480.0 * 100.0, delta=0.02)
        self.assertIn("0.4", text)

    def test_updates_when_selection_changes(self):
        m = self._mk(three_tracks())
        boxes = self._boxes(m)
        self.assertEqual(len(boxes), 3)
        boxes["XKF1"].setChecked(False)
        pump(4)
        text = self._panel_text(m)
        self.assertNotIn("GPS#0 ↔ XKF1", text, "取消勾选后该对必须消失")
        self.assertIn("POS ↔ GPS#0", text)
        self.assertIn("总体", text)
        self.assertIn("1 对", text)

    def test_hidden_page_does_not_fall_back_to_all_tracks(self):
        """地图页不是当前 tab 时（每次切页会 _apply 重建轨迹），差异面板不能按全选算。"""
        m = self._mk(three_tracks())
        boxes = self._boxes(m)
        boxes["XKF1"].setChecked(False)
        pump(3)
        m.hide()                          # 切到曲线/告警等页面
        pump(2)
        m._update_legend()                # 模拟切页时 app._apply 重建一份
        pump(2)
        self.assertEqual(sorted(m.visible_tracks()), ["GPS#0", "POS"])
        text = self._panel_text(m)
        self.assertNotIn("XKF1", text, "未勾选的轨迹不能出现在差异面板里")
        self.assertIn("已选 2 条", text)

    def test_show_event_recomputes_after_hidden_change(self):
        """隐藏期间改了勾选，切回地图页时要按新选择重算，不能留旧结果。"""
        m = self._mk(three_tracks())
        m.hide()
        pump(2)
        m.legend.checkbox("XKF1").setChecked(False)
        pump(2)
        m.show()
        pump(3)
        text = self._panel_text(m)
        self.assertNotIn("POS ↔ XKF1", text)
        self.assertIn("已选 2 条", text)

    def test_hint_when_only_one_track(self):
        m = self._mk(three_tracks())
        boxes = self._boxes(m)
        boxes["GPS#0"].setChecked(False)
        boxes["XKF1"].setChecked(False)
        pump(4)
        self.assertTrue(m.diff.isVisible())
        self.assertIn("勾选 2 条及以上", self._panel_text(m))

    def test_single_track_hides_panel(self):
        m = self._mk([track("POS")])
        self.assertFalse(m.diff.isVisible())

    def test_panel_sits_below_the_legend(self):
        m = self._mk(three_tracks())
        legend, diff = m.legend, m.diff
        self.assertTrue(legend.isVisible())
        self.assertGreaterEqual(diff.y(), legend.y() + legend.height(),
                                "差异面板必须在轨迹勾选栏下方")
        self.assertAlmostEqual(diff.x() + diff.width(), legend.x() + legend.width(), delta=2.0,
                               msg="右边缘应与勾选栏对齐")

    def test_cache_is_reused_when_toggling_back(self):
        m = self._mk(three_tracks())
        before = len(m._diff_cache)
        boxes = self._boxes(m)
        boxes["XKF1"].setChecked(False)
        pump(3)
        boxes["XKF1"].setChecked(True)
        pump(3)
        self.assertEqual(len(m._diff_cache), before, "同一对数不该重复计算")

    def test_local_tracks_are_compared_in_enu(self):
        t = np.arange(0.0, 61.0, 0.2)
        a = TrackResult(kind="local", t=t, lat=np.full_like(t, np.nan),
                        lon=np.full_like(t, np.nan), alt=np.full_like(t, np.nan),
                        e=3.0 * t, n=np.zeros_like(t), u=np.full_like(t, np.nan),
                        speed=np.zeros_like(t), key="XKF1", label="EKF 状态位置")
        b = TrackResult(kind="local", t=t, lat=np.full_like(t, np.nan),
                        lon=np.full_like(t, np.nan), alt=np.full_like(t, np.nan),
                        e=3.0 * t, n=np.full_like(t, 3.0), u=np.full_like(t, np.nan),
                        speed=np.zeros_like(t), key="VISP", label="视觉位置")
        m = self._mk([a, b])
        text = self._panel_text(m)
        self.assertIn("XKF1 ↔ VISP", text)
        self.assertIn("本地 ENU", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
