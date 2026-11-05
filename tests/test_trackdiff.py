# -*- coding: utf-8 -*-
"""tests/test_trackdiff.py —— 两条轨迹之间差异距离的口径（几何/时间/百分比）。

口径必须钉死，否则"单航线 0.02%"这种数字没人能复核：
* 距离 = 同一时刻的水平 ENU 距离；
* 基准 = 重叠段两条轨迹里程的平均值；
* 总体 = Σ均值偏差 ÷ Σ平均里程（按里程加权）。
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                                        # noqa: E402

from apmlog.core.model import TrackResult                                 # noqa: E402
from apmlog.core.trackdiff import (MAX_GAP_S, PairDiff, compare, compare_many,   # noqa: E402
                                   fmt_m, fmt_pct, overall)

LAT0, LON0 = 39.0, 117.0
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(LAT0))


def _col(v, t: np.ndarray) -> np.ndarray:
    """标量 → 与 t 等长的列；数组原样（加 0.0 复制一份）。"""
    if v is None:
        return np.zeros_like(t)
    a = np.asarray(v, dtype=float)
    return np.full_like(t, float(a)) if a.ndim == 0 else a + 0.0


def geodetic(key: str, t, *, e=None, n=None, label: str = "") -> TrackResult:
    """沿正东直线飞：e 为东向米、n 为北向米（默认 0）。"""
    t = np.asarray(t, dtype=float)
    e = _col(e, t)
    n = _col(n, t)
    lat = LAT0 + n / M_PER_DEG_LAT
    lon = LON0 + e / M_PER_DEG_LON
    return TrackResult(kind="geodetic", t=t, lat=lat, lon=lon, alt=np.zeros_like(t),
                       e=e, n=n, u=np.zeros_like(t), speed=np.zeros_like(t),
                       key=key, label=label or key)


def local(key: str, t, *, e=None, n=None) -> TrackResult:
    t = np.asarray(t, dtype=float)
    e = _col(e, t)
    n = _col(n, t)
    nan = np.full_like(t, np.nan)
    return TrackResult(kind="local", t=t, lat=nan, lon=nan, alt=nan, e=e, n=n, u=nan,
                       speed=np.zeros_like(t), key=key, label=key)


class TestGeometry(unittest.TestCase):
    def test_constant_offset_is_the_distance(self):
        t = np.arange(0.0, 61.0, 0.1)                    # 10 Hz，60 s
        a = geodetic("POS", t, e=5.0 * t)                # 东向 5 m/s → 300 m
        b = geodetic("GPS#0", t, e=5.0 * t, n=3.0)       # 北偏 3 m
        d = compare(a, b)
        self.assertTrue(d.ok, d.reason)
        self.assertAlmostEqual(d.mean, 3.0, places=3)
        self.assertAlmostEqual(d.p95, 3.0, places=3)
        self.assertAlmostEqual(d.max, 3.0, places=3)
        self.assertAlmostEqual(d.length_a, 5.0 * t[-1], delta=0.5)
        self.assertAlmostEqual(d.pct, 3.0 / (5.0 * t[-1]) * 100.0, delta=0.02)
        self.assertEqual(d.frame, "geodetic")
        self.assertEqual(d.n, t.size)
        self.assertEqual(d.dropped, 0)

    def test_origin_is_shared_so_first_point_offset_pollutes(self):
        """两条轨迹各自的首点不同：直接用各自的 e/n 相减会把原点平移算成差异。"""
        t = np.arange(0.0, 31.0, 0.1)
        a = geodetic("POS", t, e=2.0 * t)
        # b 从东 50 m 处起算自己的 ENU（模拟 GPS 后补锁），但经纬度上与 a 重合
        b = geodetic("GPS#0", t, e=2.0 * t)
        b.e = b.e - 50.0                                  # 故意把 e 挪 50 m（错误基准）
        d = compare(a, b)
        self.assertAlmostEqual(d.mean, 0.0, places=3, msg="必须按经纬度重投影，不能减 e/n")

    def test_percent_uses_average_length(self):
        t = np.arange(0.0, 41.0, 0.1)
        a = geodetic("A", t, e=10.0 * t)                 # ≈400 m
        b = geodetic("B", t, e=10.0 * t, n=4.0)          # 同样里程，北偏 4 m
        d = compare(a, b)
        self.assertAlmostEqual(d.length, 10.0 * t[-1], delta=0.5)      # 409 m
        self.assertAlmostEqual(d.pct, d.mean / d.length * 100.0, places=6)
        self.assertAlmostEqual(d.pct, 4.0 / (10.0 * t[-1]) * 100.0, delta=0.02)

    def test_local_pair_uses_enu_directly(self):
        t = np.arange(0.0, 61.0, 0.1)                    # 60 s，180 m > 100 m 门槛
        a = local("XKF1", t, e=3.0 * t, n=0.0)
        b = local("VISP", t, e=3.0 * t, n=2.0)
        d = compare(a, b)
        self.assertTrue(d.ok, d.reason)
        self.assertEqual(d.frame, "local")
        self.assertAlmostEqual(d.mean, 2.0, places=3)
        self.assertAlmostEqual(d.pct, 2.0 / d.length * 100.0, delta=0.01)


class TestAlignAndGuards(unittest.TestCase):
    def test_rates_differ_and_sparser_is_the_base(self):
        t_fast = np.arange(0.0, 31.0, 0.05)              # 20 Hz
        t_slow = np.arange(0.0, 31.0, 0.2)               # 5 Hz
        a = geodetic("POS", t_fast, e=4.0 * t_fast)
        b = geodetic("GPS#0", t_slow, e=4.0 * t_slow, n=1.5)
        d = compare(a, b)
        self.assertEqual(d.n, t_slow.size, "样本数应等于较稀一条的点数")
        self.assertAlmostEqual(d.mean, 1.5, places=3)

    def test_no_overlap(self):
        a = geodetic("A", np.arange(0.0, 10.0, 0.1), e=np.arange(0.0, 10.0, 0.1))
        b = geodetic("B", np.arange(20.0, 30.0, 0.1), e=np.arange(20.0, 30.0, 0.1))
        d = compare(a, b)
        self.assertFalse(d.ok)
        self.assertIn("没有时间重叠", d.reason)

    def test_mixed_frames_rejected(self):
        t = np.arange(0.0, 10.0, 0.1)
        d = compare(geodetic("POS", t, e=t), local("XKF1", t, e=t))
        self.assertFalse(d.ok)
        self.assertIn("坐标系不同", d.reason)

    def test_gap_in_dense_track_drops_samples(self):
        t_base = np.arange(0.0, 61.0, 1.0)               # 1 Hz 基准
        t_dense = np.concatenate([np.arange(0.0, 30.0, 0.1),
                                  np.arange(40.0, 61.0, 0.1)])   # 中间 10 s 空洞
        a = geodetic("A", t_base, e=t_base)
        b = geodetic("B", t_dense, e=t_dense, n=1.0)
        d = compare(a, b)
        self.assertTrue(d.ok, d.reason)
        self.assertGreater(d.dropped, 5, "空洞里的基准点必须被丢掉，不能硬插")
        self.assertLess(d.n, t_base.size)

    def test_jump_in_dense_track_dropped(self):
        t = np.arange(0.0, 21.0, 0.1)
        b = geodetic("B", t, e=np.where(t > 10.0, t + 5000.0, t))
        d = compare(geodetic("A", t, e=t), b)
        self.assertTrue(d.ok)
        self.assertGreater(d.dropped, 0)

    def test_short_flight_has_no_percentage(self):
        t = np.arange(0.0, 11.0, 0.1)
        a = geodetic("A", t, e=2.0 * t)                  # 20 m
        b = geodetic("B", t, e=2.0 * t, n=1.0)
        d = compare(a, b)
        self.assertTrue(d.ok)
        self.assertFalse(math.isfinite(d.pct), "里程 <100 m 时百分比没有意义")

    def test_too_few_samples(self):
        t = np.arange(0.0, 0.5, 0.1)                     # 只有 5 个点
        d = compare(geodetic("A", t, e=t), geodetic("B", t, e=t, n=1.0))
        self.assertFalse(d.ok)
        self.assertIn("样本", d.reason)

    def test_empty_track(self):
        a = geodetic("A", np.arange(0.0, 10.0, 0.1), e=np.arange(0.0, 10.0, 0.1))
        d = compare(a, TrackResult(kind="none"))
        self.assertFalse(d.ok)


class TestOverallAndFormat(unittest.TestCase):
    def _d(self, mean, length):
        return PairDiff(a_key="A", b_key="B", ok=True, mean=mean, length=length,
                        pct=mean / length * 100.0)

    def test_overall_is_weighted_by_length(self):
        d1 = self._d(1.0, 1000.0)
        d2 = self._d(4.0, 3000.0)
        o = overall([d1, d2])
        self.assertTrue(o.ok)
        self.assertAlmostEqual(o.length_sum, 4000.0)
        self.assertAlmostEqual(o.mean_sum, 5.0)
        self.assertAlmostEqual(o.pct, 5.0 / 4000.0 * 100.0)
        # 简单平均会是 (0.1% + 0.133%)/2 = 0.117%，加权后是 0.125%
        self.assertNotAlmostEqual(o.pct, (0.1 + 0.1333) / 2, places=3)

    def test_overall_skips_failed_pairs(self):
        ok = self._d(2.0, 2000.0)
        bad = PairDiff(a_key="X", b_key="Y", reason="坐标系不同")
        o = overall([ok, bad])
        self.assertEqual(o.n_pairs, 2)
        self.assertEqual(o.n_used, 1)
        self.assertAlmostEqual(o.pct, 0.1)

    def test_overall_none(self):
        o = overall([PairDiff(reason="没有时间重叠")])
        self.assertFalse(o.ok)
        self.assertFalse(math.isfinite(o.pct))

    def test_fmt(self):
        self.assertEqual(fmt_pct(float("nan")), "—")
        self.assertEqual(fmt_pct(0.0), "0%")
        self.assertEqual(fmt_pct(0.004), "0.0040%")
        self.assertEqual(fmt_pct(0.00331), "0.0033%")
        self.assertEqual(fmt_pct(0.0000001), "<0.0001%")
        self.assertEqual(fmt_pct(0.042), "0.042%")
        self.assertEqual(fmt_pct(0.02), "0.020%")
        self.assertEqual(fmt_pct(0.42), "0.42%")
        self.assertEqual(fmt_pct(2.5), "2.50%")
        self.assertEqual(fmt_pct(1.234), "1.23%")
        self.assertEqual(fmt_pct(42.0), "42.0%")
        self.assertEqual(fmt_m(float("nan")), "—")
        self.assertEqual(fmt_m(0.789), "0.79 m")
        self.assertEqual(fmt_m(3940.0), "3.9 km")

    def test_gap_constant_is_documented_value(self):
        self.assertEqual(MAX_GAP_S, 2.0)

    def test_compare_many_pairs(self):
        t = np.arange(0.0, 21.0, 0.2)
        a = geodetic("POS", t, e=3.0 * t)
        b = geodetic("GPS#0", t, e=3.0 * t, n=2.0)
        c = geodetic("XKF1", t, e=3.0 * t, n=4.0)
        got = compare_many([a, b, c], ["POS", "GPS#0", "XKF1"])
        self.assertEqual([(d.a_key, d.b_key) for d in got],
                         [("POS", "GPS#0"), ("POS", "XKF1"), ("GPS#0", "XKF1")])
        self.assertAlmostEqual(got[1].mean, 4.0, places=3)
        # 缺的 key 直接忽略，不抛异常
        self.assertEqual(len(compare_many([a, b], ["POS", "ZZZ"])), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
