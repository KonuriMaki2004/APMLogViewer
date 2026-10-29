"""电池阈值：单体电压与整包电压（设置页可填，0 = 自动/不启用）。

用户 2026-10-24：「在设置页面里加电池单体电压设置和总电压设置，比如低于多少 V 显示报警」。

要验清三件事：
1. 填了整包阈值就按整包电压比，0 = 彻底不启用（不能误报）；
2. 填了单体阈值时优先于 BATT_VOLT_MIN/比例推导；日志是整包量级就按串数换算；
   推不出串数**必须明确告知**，不许静默忽略；
3. 欠压"越低越严重"：严重档不许高于警告档。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                                    # noqa: E402
from PySide6 import QtWidgets                                         # noqa: E402

from apmlog.core import alerts as A                                   # noqa: E402
from apmlog.ui import settings as st                                  # noqa: E402


def qapp() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(app, ms: int = 60) -> None:
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        app.processEvents()


class _Ser:
    def __init__(self, t, v, unit="V"):
        self.t = np.asarray(t, dtype=float)
        self.v = np.asarray(v, dtype=float)
        self.unit = unit
        self.key = SimpleNamespace(key="BAT.Volt")


class _Store:
    """batt_alerts 的最小替身。sem 控制"整包/单体"量级判定。"""

    def __init__(self, voltage, sem, params=None, t=None):
        v = np.asarray(voltage, dtype=float)
        tt = np.arange(v.size, dtype=float) if t is None else np.asarray(t, dtype=float)
        self._ser = _Ser(tt, v)
        self._sem = sem
        self._params = dict(params or {})
        self.meta = {"types": {"BAT": {"cols": {"Volt": 0, "VoltR": 0}}},
                     "bat_semantics": sem, "t_end": float(tt[-1]) if tt.size else 0.0}
        self.stats = SimpleNamespace(takeoff_t=0.0)

    def series(self, key, *a, **k):
        return self._ser

    def param(self, name, default=None):
        return self._params.get(name, default)


CELL_SEM = {"mode": "cell", "cells_est": None, "note": "test"}
PACK_SEM = {"mode": "pack", "cells_est": None, "note": "test"}
PACK13 = {"mode": "pack", "cells_est": 13, "note": "test"}


def run(store, cfg, user_keys=None):
    """跑一次 batt_alerts。user_keys 模拟"用户在界面里显式设了这些阈值"。"""
    base = {"min_duration_s": 0.3, "merge_gap_s": 1.0}
    base.update(cfg)
    if user_keys is None:
        user_keys = frozenset(k for k in ("cell_count", "cell_warn", "cell_crit")
                              if base.get(k))
    with mock.patch.object(A, "_pick_inst", lambda *a, **k: 0):
        return A.batt_alerts(store, base, frozenset(user_keys))


def rules(hits, prefix="batt_"):
    return [h.rule for h in hits if h.rule.startswith(prefix)]


class TestPackAbsolute(unittest.TestCase):
    def test_zero_means_disabled(self) -> None:
        store = _Store([50.0, 49.0, 48.0, 47.0], PACK_SEM)
        hits = run(store, {"pack_warn_v": 0.0, "pack_crit_v": 0.0})
        self.assertEqual(rules(hits, "batt_pack_low"), [], "0 = 不启用，绝不能误报")

    def test_below_threshold_fires(self) -> None:
        store = _Store([52.0] * 5 + [45.0] * 5 + [52.0] * 5, PACK_SEM)
        hits = run(store, {"pack_warn_v": 50.0, "pack_crit_v": 44.0})
        got = rules(hits, "batt_pack_low")
        self.assertIn("batt_pack_low_warn", got)
        warn = next(h for h in hits if h.rule == "batt_pack_low_warn")
        self.assertAlmostEqual(warn.threshold, 50.0)
        self.assertLess(warn.value, 50.0)
        self.assertEqual(warn.source, "user")

    def test_values_above_threshold_stay_quiet(self) -> None:
        store = _Store([52.0] * 10, PACK_SEM)
        hits = run(store, {"pack_warn_v": 50.0, "pack_crit_v": 44.0})
        self.assertEqual(rules(hits, "batt_pack_low"), [])

    def test_crit_above_warn_is_ordered(self) -> None:
        """调反时按 min/max 排好：警告取较高的那个（先响），严重取较低的。"""
        store = _Store([46.0] * 10, PACK_SEM)
        hits = run(store, {"pack_warn_v": 45.0, "pack_crit_v": 50.0})
        got = rules(hits, "batt_pack_low")
        self.assertEqual(got, ["batt_pack_low_warn"],
                         "排序后警告=50（先响）、严重=45；46 只够警告")
        warn = next(h for h in hits if h.rule == "batt_pack_low_warn")
        self.assertAlmostEqual(warn.threshold, 50.0)

    def test_unknown_mode_is_skipped(self) -> None:
        store = _Store([10.0] * 10, {"mode": "unknown", "note": "未知"})
        self.assertEqual(rules(run(store, {"pack_warn_v": 20.0}), "batt_pack_low"), [])

    def test_cell_mode_scales_to_pack(self) -> None:
        """cell 量级的日志（记的是单体）→ 乘串数才是整包。"""
        store = _Store([3.4] * 10, {"mode": "cell", "cells_est": 13, "note": "t"})
        hits = run(store, {"pack_warn_v": 45.0})
        self.assertIn("batt_pack_low_warn", rules(hits, "batt_pack_low"),
                      "3.4×13=44.2 < 45 应报警")


class TestUserCellOverrides(unittest.TestCase):
    def test_user_cell_beats_param_derived(self) -> None:
        store = _Store([3.45] * 10, CELL_SEM, {"BATT_VOLT_MIN": 3000.0})
        hits = run(store, {"cell_warn": 3.50, "cell_crit": 3.20})
        uv = [h for h in hits if h.rule.startswith("batt_undervolt")]
        self.assertTrue(uv, "3.45 低于用户设的 3.50，应报警")
        self.assertEqual({h.source for h in uv}, {"user"})
        self.assertIn(3.50, {h.threshold for h in uv})

    def test_zero_uses_product_default_3_3(self) -> None:
        """阈值填 0 时退回产品默认 3.3/3.1（不是 yaml 覆盖或"永不报警"）。"""
        store = _Store([3.25] * 10, CELL_SEM)
        hits = run(store, {"cell_warn": 0.0, "cell_crit": 0.0})
        uv = [h for h in hits if h.rule.startswith("batt_undervolt")]
        self.assertTrue(uv, "3.25 < 3.3 应报警")
        self.assertEqual({round(h.threshold, 6) for h in uv}, {3.3})
        self.assertNotEqual({h.source for h in uv}, {"user"})

    def test_pack_mode_with_cells_uses_per_cell(self) -> None:
        """整包 45.5V / 13 串 = 3.5 V/芯。"""
        store = _Store([45.5] * 10, PACK13)
        hits = run(store, {"cell_warn": 3.60, "cell_crit": 3.30})
        uv = [h for h in hits if h.rule.startswith("batt_undervolt")]
        self.assertTrue(uv, "3.5 < 3.6 应报警")
        self.assertAlmostEqual(min(h.value for h in uv), 3.5, places=2,
                               msg="回报的应该是单体电压，不是整包 45.5")

    def test_pack_mode_without_cells_tells_the_user(self) -> None:
        """推不出串数时必须明确告知，不能静默忽略用户设的单体阈值。"""
        store = _Store([50.0] * 10, PACK_SEM)
        hits = run(store, {"cell_warn": 3.5, "cell_crit": 3.2})
        self.assertIn("batt_cells_unknown", rules(hits),
                      "要学会说“识别不出来，请手填”，而不是当没设过")
        hint = next(h for h in hits if h.rule == "batt_cells_unknown")
        self.assertEqual(hint.severity, "hint")
        self.assertIn("电芯数", hint.message, "提示必须告诉用户去填电芯数")

    def test_user_crit_above_warn_is_swapped(self) -> None:
        store = _Store([3.4] * 10, CELL_SEM)
        hits = run(store, {"cell_warn": 3.2, "cell_crit": 3.6})
        uv = [h for h in hits if h.rule.startswith("batt_undervolt")]
        self.assertTrue(uv)
        for h in uv:
            self.assertGreaterEqual(h.threshold, 3.4, "warn 必须 >= crit")


class TestSettingsKeys(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="batk_")
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self.dir) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))

    def test_defaults_are_per_cell_3_3_and_3_1(self) -> None:
        s = st.Settings.instance()
        self.assertAlmostEqual(float(s.get("cell_warn")), 3.3)
        self.assertAlmostEqual(float(s.get("cell_crit")), 3.1)
        self.assertEqual(int(s.get("cell_count")), 0, "电芯数默认自动识别")
        self.assertEqual(float(s.get("pack_warn_v")), 0.0, "整包阈值默认不启用")
        self.assertEqual(float(s.get("pack_crit_v")), 0.0)

    def test_yaml_keeps_useful_reference_values(self) -> None:
        cfg = A.load_config()["battery"]
        self.assertGreater(cfg["cell_warn"], 0.0, "0 覆盖层不参与时要有参考值兜底")
        self.assertEqual(cfg["pack_warn_v"], 0.0)

    def test_ranges(self) -> None:
        s = st.Settings.instance()
        s.set("pack_warn_v", 500.0)
        self.assertLessEqual(s.get("pack_warn_v"), 200.0)
        s.set("cell_warn", -3.0)
        self.assertGreaterEqual(s.get("cell_warn"), 0.0)


class TestDialogRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="batdlg_")
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self.dir) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        from apmlog.ui.settings import SettingsDialog
        self.dlg = SettingsDialog()
        self.dlg.show()
        pump(self.app)

    def tearDown(self) -> None:
        self.dlg.close()
        pump(self.app)

    def test_battery_rows_exist_with_units(self) -> None:
        for k, suffix in (("cell_warn", "V/芯"), ("cell_crit", "V/芯"),
                          ("pack_warn_v", "V"), ("pack_crit_v", "V")):
            self.assertIn(k, self.dlg._widgets, k)
            w = self.dlg._widgets[k]
            self.assertIn(suffix, w.suffix())
            self.assertTrue(w.toolTip())

    def test_zero_shows_a_meaningful_label(self) -> None:
        self.assertEqual(self.dlg._widgets["cell_warn"].specialValueText(), "自动")
        self.assertEqual(self.dlg._widgets["pack_warn_v"].specialValueText(), "不启用")

    def test_two_column_layout(self) -> None:
        cols = self.dlg._body.layout()
        self.assertEqual(cols.count(), 2, "设置页应为两列")
        self.assertGreaterEqual(self.dlg.width(), 1000, "默认窗口要更宽一些（不放滚动区）")

    def test_cell_crit_never_above_warn(self) -> None:
        w = self.dlg._widgets["cell_warn"]
        c = self.dlg._widgets["cell_crit"]
        c.setValue(3.2)
        w.setValue(3.6)          # 警告抬高，严重不许高于它
        self.assertLessEqual(c.value(), 3.6)


class TestOverrides(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="batov_")
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self.dir) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.show()
        pump(self.app)

    def tearDown(self) -> None:
        self.w.store = None
        self.w.close()
        pump(self.app)

    def test_pack_zeros_omitted_cell_defaults_sent(self) -> None:
        """整包 0 = 不启用（不塞覆盖层）；单芯 3.3/3.1 是产品默认，必须下发。"""
        ov = self.w._alert_overrides()
        batt = ov.get("battery", {})
        self.assertNotIn("pack_warn_v", batt)
        self.assertNotIn("pack_crit_v", batt)
        self.assertNotIn("cell_count", batt, "0 = 自动识别，不要写进覆盖层")
        self.assertAlmostEqual(batt.get("cell_warn"), 3.3)
        self.assertAlmostEqual(batt.get("cell_crit"), 3.1)

    def test_manual_cell_count_reaches_the_engine(self) -> None:
        st.Settings.instance().set("cell_count", 14)
        self.assertEqual(self.w._alert_overrides()["battery"].get("cell_count"), 14.0)

    def test_values_passed_and_clamped(self) -> None:
        s = st.Settings.instance()
        s.set("cell_warn", 3.5)
        s.set("cell_crit", 3.8)          # 调反
        s.set("pack_warn_v", 45.0)
        s.set("pack_crit_v", 48.0)       # 调反
        ov = self.w._alert_overrides()
        self.assertLessEqual(ov["battery"]["cell_crit"], ov["battery"]["cell_warn"])
        self.assertLessEqual(ov["battery"]["pack_crit_v"], ov["battery"]["pack_warn_v"])

    def test_setting_change_is_debounced(self) -> None:
        self.w._apply_setting("pack_warn_v", 44.0)
        self.assertTrue(self.w._alert_timer.isActive())

    def test_cell_count_change_triggers_recalc(self) -> None:
        """改电芯数必须立刻重算（不能只在下次打开日志时才生效）。"""
        self.w._alert_timer.stop()
        self.w._apply_setting("cell_count", 14)
        self.assertTrue(self.w._alert_timer.isActive(), "改串数应触发重算")


if __name__ == "__main__":
    unittest.main()


class TestCellCount(unittest.TestCase):
    """电芯数（几 S）：自动识别、手动优先、改 S 即时改变判定。"""

    def test_manual_beats_auto(self) -> None:
        n, src, note = A.resolve_cells({"cell_count": 6}, PACK13)
        self.assertEqual((n, src), (6, "manual"))
        self.assertIn("6S", note)

    def test_auto_from_arm_voltage(self) -> None:
        n, src, note = A.resolve_cells({}, {"mode": "pack", "batt_arm_voltage": 58.8})
        self.assertEqual((n, src), (14, "auto"))
        self.assertIn("14S", note)
        self.assertIn("BATT_ARM_VOLT", note, "要说清自动识别的依据")

    def test_auto_from_peak_voltage_when_params_missing(self) -> None:
        n, src, note = A.resolve_cells({}, {"mode": "pack", "volt_max": 57.0,
                                           "batt_arm_voltage": 0.0})
        self.assertEqual((n, src), (14, "auto"))
        self.assertIn("估算", note, "按电压推的要标明是估算")

    def test_auto_fails_without_evidence(self) -> None:
        n, src, note = A.resolve_cells({}, {"mode": "pack"})
        self.assertIsNone(n, "没证据就不许编一个串数")
        self.assertEqual(src, "none")
        self.assertIn("无法自动识别", note)

    def test_auto_ignores_absurd_voltage(self) -> None:
        self.assertIsNone(A.resolve_cells({}, {"mode": "pack", "volt_max": 300.0})[0])

    def test_changing_s_changes_the_alerts_immediately(self) -> None:
        v = [45.5] * 10
        at13 = [h for h in run(_Store(v, PACK13), {"cell_warn": 3.6, "cell_crit": 3.3})
                if h.rule.startswith("batt_undervolt")]
        at6 = [h for h in run(_Store(v, PACK13),
                              {"cell_count": 6, "cell_warn": 3.6, "cell_crit": 3.3})
               if h.rule.startswith("batt_undervolt")]
        self.assertTrue(at13, "45.5 ÷ 13 = 3.5 V/芯，低于 3.6 应报警")
        self.assertFalse(at6, "45.5 ÷ 6 = 7.58 V/芯，改了串数就不该报警")

    def test_message_has_no_rempct_or_percent(self) -> None:
        """正文只许出现电压与串数，不许再引用 RemPct / 百分比。"""
        hits = run(_Store([45.5] * 10, PACK13), {"cell_warn": 3.6, "cell_crit": 3.3})
        uv = [h for h in hits if h.rule.startswith("batt_undervolt")]
        self.assertTrue(uv)
        for h in uv:
            msg = h.message or ""
            self.assertNotIn("RemPct", msg)
            self.assertNotIn("剩余电量", msg)
            self.assertNotIn("%", msg)
            self.assertIn("13S", msg)
            self.assertEqual(h.unit, "V/芯")

    def test_message_states_the_cells_used(self) -> None:
        hits = run(_Store([45.5] * 10, PACK13), {"cell_warn": 3.6, "cell_crit": 3.3})
        uv = next(h for h in hits if h.rule.startswith("batt_undervolt"))
        self.assertIn("13S", uv.message, "正文必须写明按几 S 换算")
        self.assertIn("单芯", uv.message)
        self.assertEqual(uv.unit, "V/芯")


class TestCellsLabel(unittest.TestCase):
    """设置页要明确显示"当前采用几 S"；自动失败时引导手填。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="cellui_")
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self.dir) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))

    def _dlg(self, info):
        from apmlog.ui.settings import SettingsDialog
        d = SettingsDialog(cells_provider=lambda: info)
        d.show()
        pump(self.app)
        self.addCleanup(d.close)
        return d

    def test_shows_auto_cells(self) -> None:
        d = self._dlg({"cells": 14, "src": "auto",
                       "detail": "自动估算 14S（最高整包电压 57.0V ÷ 4.25V/芯 向上取整）"})
        t = d.lbl_cells.text()
        self.assertIn("14S", t)
        self.assertIn("自动", t)

    def test_manual_wins_and_says_so(self) -> None:
        d = self._dlg({"cells": 14, "src": "auto", "detail": ""})
        d._widgets["cell_count"].setValue(6)
        pump(self.app)
        t = d.lbl_cells.text()
        self.assertIn("6S", t)
        self.assertIn("手动", t)
        self.assertIn("14S", t, "要说明自动识别是几 S、已被手填覆盖")

    def test_failure_asks_user_to_fill(self) -> None:
        d = self._dlg({"cells": None, "src": "none", "detail": "无法自动识别串数"})
        t = d.lbl_cells.text()
        self.assertIn("失败", t)
        self.assertIn("电芯数", t, "失败时必须告诉用户去填电芯数")

    def test_spinbox_zero_means_auto(self) -> None:
        d = self._dlg({"cells": None, "src": "none", "detail": ""})
        w = d._widgets["cell_count"]
        self.assertEqual(w.value(), 0)
        self.assertEqual(w.specialValueText(), "自动")
        self.assertEqual(w.suffix(), "S")
        self.assertEqual(w.maximum(), 24)
        self.assertEqual(w.minimum(), 0)
