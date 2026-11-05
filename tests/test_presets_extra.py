# -*- coding: utf-8 -*-
"""tests/test_presets_extra.py —— 0.1.2 新增曲线组（罗盘 / IMU / PID / 供电 / EKF / 遥控）。

要点：
1. 新组只往 ``PRESETS`` 里加条目，``build_lanes`` 的取数与 ``SeriesPlot`` 的绘制完全复用；
2. 老预设的通道组合必须**一个都没变**（这里用快照钉住，改动会被测试抓住）；
3. 新增组在“缺通道”的日志上要优雅跳过，而不是抛异常把整页弄黑；
4. 预设框（页面上的“预设”下拉）能选到全部预设，并且真的能画出泳道。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m pytest tests/test_presets_extra.py -q
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                                # noqa: E402

from PySide6 import QtWidgets                                     # noqa: E402

from apmlog.core.model import ChannelInfo, ChannelKey, Envelope, Series   # noqa: E402
from apmlog.ui.presets import PRESETS, PRESET_BY_ID, build_lanes          # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


class FakeTrack:
    """最小假轨迹：让 TRACK.alt / TRACK.speed 两条合成通道也能建出来。"""

    def __init__(self, n: int = 60):
        self.n_points = n
        self.t = np.linspace(0.0, 100.0, n)
        self.alt = np.linspace(0.0, 50.0, n)
        self.u = self.alt
        self.speed = np.linspace(0.0, 15.0, n)
        self.e = np.linspace(0.0, 100.0, n)
        self.n = np.linspace(0.0, 50.0, n)
        self.has_geodetic = True


class FakeStore:
    """只实现 build_lanes 用到的那几样：channel_infos / meta / series / envelope / track。"""

    def __init__(self, spec: dict[tuple[str, str, int | None], tuple[float, float, str, int]],
                 *, track_points: int = 60):
        # spec: (type, field, inst) -> (vmin, vmax, unit, rows)
        self._track_points = int(track_points)
        self._spec = dict(spec)
        self.meta = {"types": {}}
        for (t, _f, _i), (_lo, _hi, _u, rows) in spec.items():
            self.meta["types"].setdefault(t, {"rows": 0})
            self.meta["types"][t]["rows"] = max(self.meta["types"][t]["rows"], int(rows))

    def channel_infos(self) -> list[ChannelInfo]:
        out = []
        for (t, f, i), (lo, hi, unit, rows) in self._spec.items():
            out.append(ChannelInfo(key=ChannelKey(t, f, i), count=int(rows),
                                   t0=0.0, t1=float(max(1, rows)), vmin=lo, vmax=hi, unit=unit))
        return out

    def _key(self, key: ChannelKey) -> tuple[str, str, int | None]:
        return (key.type, key.field, key.inst)

    def series(self, key: ChannelKey, t0=None, t1=None, max_points=None, drop_nan=False) -> Series:
        lo, hi, unit, rows = self._spec[self._key(key)]
        n = int(min(rows, 400))
        t = np.linspace(0.0, 100.0, n)
        v = np.linspace(float(lo), float(hi), n)
        return Series(key=key, t=t, v=v, unit=unit)

    def envelope(self, key: ChannelKey, t0=None, t1=None, buckets=2400) -> Envelope:
        lo, hi, unit, rows = self._spec[self._key(key)]
        n = int(max(8, min(buckets, 200)))
        t = np.linspace(0.0, 100.0, n)
        vmin = np.full(n, float(lo))
        vmax = np.full(n, float(hi))
        vmean = (vmin + vmax) / 2.0
        return Envelope(key=key, t=t, vmin=vmin, vmax=vmax, vmean=vmean,
                        count=np.full(n, 10), unit=unit)

    def track(self):
        return FakeTrack(self._track_points)


def rich_store() -> FakeStore:
    """覆盖全部新增组需要的通道；rows 故意有大有小，两条取数分支（series/envelope）都走到。"""
    spec: dict[tuple[str, str, int | None], tuple[float, float, str, int]] = {
        # 老预设要用的（快照测试用）
        ("TRACK", "alt", None): (0.0, 50.0, "m", 0),
        ("TRACK", "speed", None): (0.0, 15.0, "m/s", 0),
        ("BARO", "Alt", None): (0.0, 50.0, "m", 9000),
        ("XKF1", "PD", 0): (-2.0, -40.0, "m", 9000),
        ("RCOU", "C1", None): (1000.0, 1800.0, "µs", 9000),
        ("RCOU", "C2", None): (1000.0, 1800.0, "µs", 9000),
        ("RCOU", "C3", None): (1000.0, 1800.0, "µs", 9000),
        ("RCOU", "C4", None): (1000.0, 1800.0, "µs", 9000),
        ("RCOU", "C5", None): (1500.0, 1500.0, "µs", 9000),
        ("RCOU", "C6", None): (1500.0, 1500.0, "µs", 9000),
        ("RCOU", "C7", None): (1500.0, 1500.0, "µs", 9000),
        ("RCOU", "C8", None): (1500.0, 1500.0, "µs", 9000),
        ("MOTB", "ThrAvMx", None): (0.0, 1.0, "", 5000),
        ("MOTB", "LiftMax", None): (0.0, 1.0, "", 5000),
        ("MOTB", "ThrOut", None): (0.0, 1.0, "", 5000),
        ("CTUN", "ThO", None): (0.0, 1.0, "", 5000),
        ("MOTB", "FailFlags", None): (0.0, 0.0, "", 5000),
        ("BAT", "Volt", 0): (22.0, 25.0, "V", 9000),
        ("BAT", "VoltR", 0): (3.6, 4.2, "V", 9000),
        ("BAT", "Curr", 0): (0.0, 40.0, "A", 9000),
        ("BAT", "RemPct", 0): (0.0, 100.0, "%", 9000),
        ("BAT", "Res", 0): (0.01, 0.05, "Ω", 9000),
        ("VIBE", "VibeX", 0): (0.0, 30.0, "", 9000),
        ("VIBE", "VibeY", 0): (0.0, 30.0, "", 9000),
        ("VIBE", "VibeZ", 0): (0.0, 30.0, "", 9000),
        ("VIBE", "Clip", 0): (0.0, 0.0, "", 9000),
        ("IMU", "GHz", 0): (0.0, 1.0, "", 9000),
        ("ATT", "Roll", None): (-30.0, 30.0, "°", 9000),
        ("ATT", "DesRoll", None): (-30.0, 30.0, "°", 9000),
        ("ATT", "Pitch", None): (-30.0, 30.0, "°", 9000),
        ("ATT", "DesPitch", None): (-30.0, 30.0, "°", 9000),
        ("ATT", "Yaw", None): (-180.0, 180.0, "°", 9000),
        ("ATT", "DesYaw", None): (-180.0, 180.0, "°", 9000),
        ("RATE", "R", None): (-100.0, 100.0, "", 9000),
        ("RATE", "RDes", None): (-100.0, 100.0, "", 9000),
        ("RATE", "P", None): (-100.0, 100.0, "", 9000),
        ("RATE", "PDes", None): (-100.0, 100.0, "", 9000),
        ("GPS", "NSats", 0): (5.0, 20.0, "", 9000),
        ("GPS", "HDop", 0): (0.5, 2.0, "", 9000),
        ("XKF3", "IPN", 0): (-1.0, 1.0, "", 9000),
        ("XKF3", "IPE", 0): (-1.0, 1.0, "", 9000),
        ("XKF3", "IPD", 0): (-1.0, 1.0, "", 9000),
        ("XKF3", "IVN", 0): (-1.0, 1.0, "", 9000),
        ("XKF3", "IVE", 0): (-1.0, 1.0, "", 9000),
        ("XKF3", "IVD", 0): (-1.0, 1.0, "", 9000),
        ("XKF4", "TSF", 0): (0.0, 1.0, "", 9000),
        ("XKF4", "SP", 0): (0.0, 1.0, "", 9000),
        ("XKF4", "SV", 0): (0.0, 1.0, "", 9000),
        # ---- 新增组 ----
        ("MAG", "MagX", 0): (-600.0, -380.0, "", 9000),
        ("MAG", "MagY", 0): (-350.0, 350.0, "", 9000),
        ("MAG", "MagZ", 0): (-580.0, 260.0, "", 9000),
        ("MAG", "MagX", 1): (-930.0, 440.0, "", 9000),
        ("IMU", "GyrX", 0): (-0.5, 0.6, "", 9000),
        ("IMU", "GyrY", 0): (-0.5, 0.5, "", 9000),
        ("IMU", "GyrZ", 0): (-0.9, 0.6, "", 9000),
        ("IMU", "AccX", 0): (-5.0, 5.0, "", 9000),
        ("IMU", "AccY", 0): (-5.0, 5.0, "", 9000),
        ("IMU", "AccZ", 0): (-19.0, -2.0, "", 9000),
        ("PIDR", "Act", 0): (-0.01, 0.01, "", 300),
        ("PIDR", "Tar", 0): (-0.05, -0.03, "", 300),
        ("PIDR", "P", 0): (-0.005, 0.0, "", 300),
        ("PIDR", "D", 0): (-0.001, 0.001, "", 300),
        ("PIDR", "FF", 0): (-0.002, 0.002, "", 300),
        ("POWR", "Vcc", None): (5.2, 5.4, "", 9000),
        ("MCU", "MVolt", None): (3.28, 3.30, "", 9000),
        ("MCU", "MTemp", None): (40.0, 46.0, "°C", 9000),
        ("BAT", "Temp", 0): (25.0, 35.0, "°C", 9000),
        ("XKF1", "PN", 0): (-1.0, 200.0, "m", 9000),
        ("XKF1", "PE", 0): (-100.0, 100.0, "m", 9000),
        ("XKF1", "VN", 0): (-8.0, 8.0, "m/s", 9000),
        ("XKF1", "VE", 0): (-8.0, 8.0, "m/s", 9000),
        ("XKF1", "VD", 0): (-3.0, 3.0, "m/s", 9000),
        ("RCIN", "C1", None): (1500.0, 1520.0, "µs", 9000),
        ("RCIN", "C2", None): (1480.0, 1730.0, "µs", 9000),
        ("RCIN", "C3", None): (1050.0, 1830.0, "µs", 9000),
        ("RCIN", "C4", None): (1060.0, 1950.0, "µs", 9000),
    }
    return FakeStore(spec)


NEW_IDS = ("mag", "imu", "pid", "power", "ekf", "rc")

#: 老预设的通道组合快照（新增曲线不许改动它们）
OLD_SNAPSHOT = {
    "position": ["轨迹 alt", "轨迹 speed", "气压BARO.Alt", "EKF 高度(下正)XKF1[0].PD"],
    "motor": ["RCOU.C1", "RCOU.C2", "RCOU.C3", "RCOU.C4", "RCOU.C5", "RCOU.C6", "RCOU.C7",
              "RCOU.C8", "MOTB.ThrOut", "MOTB.ThrAvMx", "MOTB.LiftMax", "CTUN.ThO",
              "MOTB.FailFlags"],
    "battery": ["BAT[0].Volt", "BAT[0].VoltR", "BAT[0].Curr", "BAT[0].RemPct", "BAT[0].Res"],
    "vibe": ["VIBE[0].VibeX", "VIBE[0].VibeY", "VIBE[0].VibeZ", "VIBE[0].Clip",
             "陀螺高频IMU[0].GHz"],
    "attitude": ["ATT.Roll", "ATT.Pitch", "ATT.Yaw", "RATE.R", "RATE.P"],
    "gps": ["GPS[0].NSats", "GPS[0].HDop", "XKF3[0].IPN", "XKF3[0].IPE", "XKF3[0].IPD",
            "XKF3[0].IVN", "XKF3[0].IVE", "XKF3[0].IVD", "XKF4[0].TSF", "XKF4[0].SP",
            "XKF4[0].SV"],
}


class TestRegistry(unittest.TestCase):
    def test_new_presets_registered(self):
        ids = [p.id for p in PRESETS]
        for pid in NEW_IDS:
            self.assertIn(pid, ids)
            self.assertIn(pid, PRESET_BY_ID)
        self.assertEqual(len(ids), len(set(ids)), "预设 id 不能重复")
        self.assertGreaterEqual(len(ids), 12)

    def test_names_are_human_readable(self):
        for pid in NEW_IDS:
            p = PRESET_BY_ID[pid]
            self.assertTrue(p.name.strip())
            self.assertTrue(p.note.strip(), f"{pid} 缺少数据来源说明")

    def test_existing_presets_kept(self):
        for pid in OLD_SNAPSHOT:
            self.assertIn(pid, PRESET_BY_ID)


class TestLaneBuilding(unittest.TestCase):
    def setUp(self):
        self.store = rich_store()

    def test_old_presets_unchanged(self):
        for pid, labels in OLD_SNAPSHOT.items():
            lanes, skipped = build_lanes(self.store, pid)
            self.assertEqual([ln.label for ln in lanes], labels, pid)
            self.assertEqual(skipped, [], pid)

    def test_mag_preset(self):
        lanes, skipped = build_lanes(self.store, "mag")
        self.assertEqual([ln.label for ln in lanes],
                         ["MAG[0].MagX", "MAG[0].MagY", "MAG[0].MagZ", "罗盘2 MAG[1].MagX"])
        self.assertEqual(lanes[3].key, "MAG#1.MagX", "罗盘2 必须取实例 1")
        self.assertEqual(skipped, [])

    def test_imu_preset_covers_gyro_and_accel(self):
        lanes, _ = build_lanes(self.store, "imu")
        self.assertEqual([ln.key for ln in lanes],
                         ["IMU#0.GyrX", "IMU#0.GyrY", "IMU#0.GyrZ",
                          "IMU#0.AccX", "IMU#0.AccY", "IMU#0.AccZ"])

    def test_pid_preset_has_target_overlay(self):
        lanes, _ = build_lanes(self.store, "pid")
        self.assertEqual(lanes[0].key, "PIDR#0.Act")
        self.assertEqual(len(lanes[0].overlays), 1)
        ov = lanes[0].overlays[0]
        self.assertEqual(ov.key, "PIDR#0.Tar")
        self.assertEqual(ov.label, "目标")
        self.assertTrue(ov.hidden, "目标曲线默认收起（先看实际值）")
        self.assertEqual([ln.key for ln in lanes[1:]], ["PIDR#0.P", "PIDR#0.D", "PIDR#0.FF"])

    def test_power_preset_bands(self):
        lanes, _ = build_lanes(self.store, "power")
        by_key = {ln.key: ln for ln in lanes}
        self.assertEqual(by_key["POWR.Vcc"].band, (4.5, 5.5))
        self.assertEqual(by_key["MCU.MVolt"].band, (3.0, 3.6))
        self.assertIn("MCU.MTemp", by_key)
        self.assertIn("BAT#0.Temp", by_key)

    def test_ekf_and_rc_presets(self):
        ekf, _ = build_lanes(self.store, "ekf")
        self.assertEqual([ln.key for ln in ekf],
                         ["XKF1#0.PN", "XKF1#0.PE", "XKF1#0.PD",
                          "XKF1#0.VN", "XKF1#0.VE", "XKF1#0.VD"])
        rc, _ = build_lanes(self.store, "rc")
        self.assertEqual([ln.key for ln in rc], ["RCIN.C1", "RCIN.C2", "RCIN.C3", "RCIN.C4"])
        self.assertEqual((rc[0].y_min, rc[0].y_max), (1000, 2000), "摇杆行程要钉住 µs 量程")

    def test_missing_channels_are_skipped_not_raised(self):
        bare = FakeStore({("MAG", "MagX", 0): (-600.0, -380.0, "", 9000)}, track_points=0)
        lanes, skipped = build_lanes(bare, "mag")
        self.assertEqual(len(lanes), 1)
        self.assertTrue(skipped, "缺的通道要写进跳过原因")
        self.assertTrue(all("MagY" in s or "MagZ" in s or "MagX" in s for s in skipped), skipped)

    def test_all_presets_build_on_empty_store(self):
        empty = FakeStore({}, track_points=0)
        for p in PRESETS:
            lanes, skipped = build_lanes(empty, p.id)
            self.assertEqual(lanes, [], p.id)
            self.assertTrue(skipped, f"{p.id} 在空日志上应记跳过原因")


class TestUiAndDrawing(unittest.TestCase):
    def setUp(self):
        self.app = qapp()
        # views.json 是全局文件：类级补丁，构造前就把读写指到临时目录
        import tempfile
        from apmlog.app import MainWindow
        self._views = Path(tempfile.mkdtemp(prefix="presets_views_")) / "views.json"
        self._orig_views_path = MainWindow._views_path
        _views = self._views
        MainWindow._views_path = lambda self: _views

    def tearDown(self):
        from apmlog.app import MainWindow
        MainWindow._views_path = self._orig_views_path

    def test_preset_box_lists_every_preset(self):
        from apmlog.app import MainWindow
        win = MainWindow()
        try:
            codes = [win.preset_box.itemData(i) for i in range(win.preset_box.count())]
            self.assertEqual(codes, [p.id for p in PRESETS])
        finally:
            win.close()
            win.deleteLater()
            for _ in range(4):
                self.app.processEvents()

    def test_preset_choice_persists(self):
        """在页面上选一次罗盘，重开窗口要还是罗盘（views.json 存的是预设 id）。"""
        import json
        from apmlog.app import MainWindow
        w = MainWindow()
        try:
            w.preset_box.setCurrentIndex(w.preset_box.findData("mag"))
            self.app.processEvents()
            self.assertEqual(w._preset, "mag")
            w._save_view()
            self.assertEqual(json.loads(self._views.read_text(encoding="utf-8"))["preset"], "mag")
        finally:
            w.close()
            w.deleteLater()
            for _ in range(3):
                self.app.processEvents()
        w2 = MainWindow()
        try:
            self.assertEqual(w2._preset, "mag")
            self.assertEqual(w2.preset_box.currentData(), "mag")
        finally:
            w2.close()
            w2.deleteLater()
            for _ in range(3):
                self.app.processEvents()

    def test_new_presets_draw_lanes(self):
        from apmlog.ui.seriesplot import SeriesPlot
        store = rich_store()
        plot = SeriesPlot()
        plot.resize(900, 700)
        try:
            for pid in NEW_IDS:
                lanes, _ = build_lanes(store, pid)
                plot.set_lanes(lanes, t_span=(0.0, 100.0))
                self.assertEqual(len(plot.lanes()), len(lanes), pid)
                self.assertEqual(plot.lane_labels(), [ln.label for ln in lanes], pid)
                widgets = plot.findChildren(QtWidgets.QPushButton)
                self.assertGreaterEqual(len(widgets), len(lanes), f"{pid} 胶囊没建出来")
        finally:
            plot.deleteLater()
            for _ in range(3):
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main(verbosity=2)
