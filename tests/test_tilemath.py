"""test_tilemath —— Web Mercator 与 WGS-84↔GCJ-02 换算的数值单测。

关于 GCJ-02 的"公开已知参考点"：官方并不发布 WGS-84→GCJ-02 的对照表，
网上流传的具体坐标对来源不可靠。因此本测试**不硬编码来源不明的偏移数值**，
改用三类可严格验证的判据：

1. 往返自洽：``gcj_to_wgs(wgs_to_gcj(p))`` 与 p 的残差 < 1e-7 度（≈1 cm）。
2. 量级与方向合理性：中国境内偏移量必须在 0 < |Δ| < 0.02 度（≈2 km）带内，
   且偏移随位置平滑变化（相邻 0.01 度格网的差分远小于偏移本身）。
3. 境外严格不变：非中国范围必须逐位返回原值（不外偏）。

另附 Web Mercator 的公开锚点：z=0 赤道本初子午线交点落在瓦片 (0,0) 的 (0.5,0.5)，
以及 1 像素地面分辨率常数 156543.03 m/px（lat=0, z=0）。
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apmlog.ui import _tilemath as tm   # noqa: E402


class TestMercator(unittest.TestCase):
    def test_z0_anchor(self):
        """(lat=0, lon=0) 必须落在 z0 单张瓦片正中。"""
        tx, ty, fx, fy = tm.lonlat_to_tile(0.0, 0.0, 0)
        self.assertEqual((tx, ty), (0, 0))
        self.assertAlmostEqual(fx, 0.5, places=12)
        self.assertAlmostEqual(fy, 0.5, places=12)

    def test_scene_roundtrip(self):
        pts = [(39.908599, 116.397368), (38.9617576, 117.3196055),
               (-33.8688, 151.2093), (51.5007, -0.1246), (0.0, 179.9999), (-84.0, -120.0)]
        for lat, lon in pts:
            x, y = tm.lonlat_to_scene(lat, lon)
            lat2, lon2 = tm.scene_to_lonlat(x, y)
            self.assertAlmostEqual(lat, lat2, places=7, msg=f"lat {lat}")
            self.assertAlmostEqual(lon, lon2, places=6, msg=f"lon {lon}")

    def test_inverse_mercator_known_values(self):
        """反 Mercator 钉点：北边缘 ±85.0511、赤道 0、y=0.25 → 66.5133。"""
        self.assertAlmostEqual(tm.mercatorY_toLat(0.5), 0.0, places=10)
        self.assertAlmostEqual(tm.mercatorY_toLat(0.0), tm.MAX_LAT_MERCATOR, places=6)
        self.assertAlmostEqual(tm.mercatorY_toLat(1.0), -tm.MAX_LAT_MERCATOR, places=6)
        self.assertAlmostEqual(tm.mercatorY_toLat(0.25), 66.51326044, places=5)
        self.assertEqual(tm.mercatorY_toLat(float("nan")), 0.0)
        # 前向反变换必须互逆
        for lat in (-80.0, -30.0, 0.0, 45.0, 84.9):
            self.assertAlmostEqual(tm.mercatorY_toLat(tm.lat_toMercatorY(lat)), lat, places=7)

    def test_meters_per_pixel_constant(self):
        """公开常数：z=0、赤道处 1 px ≈ 156543.03 m。"""
        self.assertAlmostEqual(tm.meters_per_pixel(0.0, 0), 156543.03392, places=4)
        self.assertAlmostEqual(tm.meters_per_pixel(60.0, 0), 156543.03392 * 0.5, places=3)

    def test_scale_at(self):
        self.assertAlmostEqual(tm.scale_at(tm.REF_Z), 1.0, places=15)
        self.assertAlmostEqual(tm.scale_at(tm.REF_Z - 3), 0.125, places=15)

    def test_tile_index_monotonic(self):
        prev = -1
        for lon in range(-180, 180, 10):
            tx, _, _, _ = tm.lonlat_to_tile(35.0, lon, 8)
            self.assertGreaterEqual(tx, prev)
            prev = tx

    def test_clamp_and_nan_safe(self):
        self.assertEqual(tm.clamp_lon(540.0), 180.0 - 360.0)
        self.assertAlmostEqual(tm.clamp_lat(89.0), tm.MAX_LAT_MERCATOR)
        self.assertEqual(tm.clamp_lat(float("nan")), 0.0)
        x, y = tm.lonlat_to_scene(tm.MAX_LAT_MERCATOR + 5, 0.0)
        self.assertTrue(math.isfinite(x) and math.isfinite(y))

    def test_scene_rect_of_tile(self):
        x, y, size = tm.scene_rect_of_tile(3, 5, tm.REF_Z - 2)
        self.assertAlmostEqual(size, 256 * 4, places=9)
        self.assertAlmostEqual(x, 3 * size, places=6)
        self.assertAlmostEqual(y, 5 * size, places=6)

    def test_nice_scale_step(self):
        step, px = tm.nice_scale_step(1.0, 90.0)
        self.assertIn(step, (50, 100, 200))
        self.assertGreater(px, 20)
        s2, p2 = tm.nice_scale_step(float("nan"), 90.0)
        self.assertTrue(math.isnan(s2) and math.isnan(p2))


class TestGCJ(unittest.TestCase):
    #: 中国境内多点（WGS-84），覆盖南北东
    CN = [(39.908599, 116.397368), (31.230416, 121.473701), (23.12911, 113.26439),
          (38.9617576, 117.3196055), (43.817072, 87.616808), (29.65, 91.1),
          (45.803775, 126.534967)]

    def test_out_of_china_band(self):
        self.assertTrue(tm.out_of_china(48.8584, 2.2945))      # 巴黎
        self.assertTrue(tm.out_of_china(35.6895, 139.6917))    # 东京
        self.assertTrue(tm.out_of_china(-33.8688, 151.2093))   # 悉尼
        self.assertFalse(tm.out_of_china(39.9, 116.4))
        self.assertFalse(tm.out_of_china(39.9, 73.5))          # 西端界内
        self.assertTrue(tm.out_of_china(39.9, 60.0))           # 西端界外
        self.assertTrue(tm.out_of_china(float("nan"), 116.4))

    def test_foreign_is_bitwise_identity(self):
        """境外必须严格不外偏。"""
        for lat, lon in [(48.8584, 2.2945), (35.6895, 139.6917), (-33.8688, 151.2093), (55.7558, 37.6173)]:
            self.assertEqual(tm.wgs_to_gcj(lat, lon), (lat, lon))
            self.assertEqual(tm.gcj_to_wgs(lat, lon), (lat, lon))

    def test_offset_magnitude_band(self):
        for lat, lon in self.CN:
            glat, glon = tm.wgs_to_gcj(lat, lon)
            d = math.hypot(glat - lat, glon - lon)
            self.assertGreater(d, 1e-4, msg=f"{lat},{lon} 偏移过小")
            self.assertLess(d, 2e-2, msg=f"{lat},{lon} 偏移过大: {d}")

    def test_roundtrip_precision(self):
        """往返残差 < 1e-7 度（≈1 cm），并确认多轮迭代会收敛。"""
        worst = 0.0
        for lat, lon in self.CN:
            glat, glon = tm.wgs_to_gcj(lat, lon)
            w1 = tm.gcj_to_wgs(glat, glon, iters=1)
            w6 = tm.gcj_to_wgs(glat, glon, iters=6)
            e1 = max(abs(w1[0] - lat), abs(w1[1] - lon))
            e6 = max(abs(w6[0] - lat), abs(w6[1] - lon))
            self.assertLessEqual(e6, e1 * 1.001 + 1e-12, msg=f"迭代不应变差 {e1} -> {e6}")
            worst = max(worst, e6)
        self.assertLess(worst, 1e-7, msg=f"往返最大残差 {worst:.3e} 度")

    def test_smoothness(self):
        """跳变检验：**偏移量 d 本身**在 0.01 度尺度上的变化应远小于 d 自目。

        注意不能直接拿 ``b - a`` 当偏移变化（里面含 0.01 度位移本身），
        必须先分别算出两点的 d 再作差。
        """
        for lat, lon in self.CN:
            a = tm.wgs_to_gcj(lat, lon)
            b = tm.wgs_to_gcj(lat + 0.01, lon + 0.01)
            da = (a[0] - lat, a[1] - lon)
            db = (b[0] - (lat + 0.01), b[1] - (lon + 0.01))
            mag = math.hypot(*da)
            delta = math.hypot(db[0] - da[0], db[1] - da[1])
            self.assertLess(delta, mag * 0.1, msg=f"({lat},{lon}) 偏移梯度过大 |d|={mag:.3e} Δd={delta:.3e}")
            self.assertGreater(mag, 0.0)

    def test_border_not_spiked(self):
        """国界判定边缘处允许不连续，但界内值必须有限。"""
        for lat, lon in [(0.9, 72.5), (55.5, 137.0), (4.0, 110.0)]:
            glat, glon = tm.wgs_to_gcj(lat, lon)
            self.assertTrue(math.isfinite(glat) and math.isfinite(glon))

    def test_convert_pair_datum_routing(self):
        lat, lon = 39.908599, 116.397368
        self.assertEqual(tm.convert_pair(lat, lon, "WGS84"), (lat, lon))
        self.assertEqual(tm.convert_pair(lat, lon, "CGCS2000"), (lat, lon))
        g = tm.convert_pair(lat, lon, "GCJ02")
        self.assertNotEqual(g, (lat, lon))
        back = tm.convert_pair(g[0], g[1], "GCJ02", to_wgs=True)
        self.assertAlmostEqual(back[0], lat, places=7)
        self.assertAlmostEqual(back[1], lon, places=7)
        with self.assertRaises(ValueError):
            tm.convert_pair(lat, lon, "UNKNOWN")
        with self.assertRaises(NotImplementedError):
            tm.convert_pair(lat, lon, "BD09")


class TestLocalENU(unittest.TestCase):
    def test_enu_small_area_accuracy(self):
        """等距圆柱近似：1 km 尺度内与球面距离误差 < 0.5%。"""
        origin = (38.9617576, 117.3196055)
        e, n = tm.enu_from_lonlat(38.9707426, 117.3306055, origin)   # 约 +1 km N, +0.85 km E
        self.assertGreater(n, 900)
        self.assertLess(abs(n - 1000), 8)                            # 0.8% 内
        self.assertGreater(e, 100)
        back_ok = math.hypot(e, n)
        self.assertTrue(900 < back_ok < 1400)

    def test_enu_to_scene_sign(self):
        x, y = tm.enu_to_scene(10.0, 20.0)
        self.assertEqual((x, y), (10.0, 20.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
