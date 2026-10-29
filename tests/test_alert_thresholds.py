"""告警阈值可配置：设置页改振动阈值 → 覆盖 yaml → 立即重算。

用户 2026-10-24：「设置页面加一些功能，比如振动值超标（设定超过多少为警告，默认为30）」。

三层要一起验：
1. 引擎（`alerts.evaluate`）能接受覆盖层，且 yaml 仍是文件真源；
2. 设置项（`Settings` + 设置对话框）能存住、能夹住越界、严重不允许低于警告；
3. 主窗接线：改完只刷新与告警有关的四处，而且合并连续改动（防抖）。
"""

from __future__ import annotations

import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets                                          # noqa: E402

from apmlog.core import alerts as A                                    # noqa: E402
from apmlog.core.model import AlertHit                                 # noqa: E402
from apmlog.ui import settings as st                                   # noqa: E402


def qapp() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(app, ms: int = 60) -> None:
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        app.processEvents()


class TestEngineDefaults(unittest.TestCase):
    def test_default_warn_is_30(self) -> None:
        """用户指定的默认值：超过 30 m/s² 报警告。"""
        v = A.DEFAULTS["vibration"]
        self.assertEqual(v["rms_warn"], 30.0)
        self.assertEqual(v["rms_crit"], 60.0, "严重必须高于警告")

    def test_yaml_agrees_with_code_defaults(self) -> None:
        """yaml 里的值和代码默认一致，否则命令行与界面会给出不同结论。"""
        cfg = A.load_config().get("vibration", {})
        self.assertEqual(cfg["rms_warn"], A.DEFAULTS["vibration"]["rms_warn"])
        self.assertEqual(cfg["rms_crit"], A.DEFAULTS["vibration"]["rms_crit"])


class TestOverrides(unittest.TestCase):
    def test_overrides_reach_the_engine(self) -> None:
        seen = {}

        def fake_vibe(store, cfg):
            seen.update(cfg)
            return []

        with mock.patch.object(A, "vibe_alerts", fake_vibe), \
             mock.patch.object(A, "batt_alerts", lambda *a, **k: []):
            A.evaluate(object(), overrides={"vibration": {"rms_warn": 12.5, "rms_crit": 25.0}})
        self.assertEqual(seen["rms_warn"], 12.5)
        self.assertEqual(seen["rms_crit"], 25.0)

    def test_overrides_do_not_leak_into_the_yaml(self) -> None:
        before = A.load_config()["vibration"]["rms_warn"]
        with mock.patch.object(A, "vibe_alerts", lambda *a, **k: []), \
             mock.patch.object(A, "batt_alerts", lambda *a, **k: []):
            A.evaluate(object(), overrides={"vibration": {"rms_warn": 99.0}})
        self.assertEqual(A.load_config()["vibration"]["rms_warn"], before)

    def test_none_values_are_ignored(self) -> None:
        seen = {}

        def fake_vibe(store, cfg):
            seen.update(cfg)
            return []

        with mock.patch.object(A, "vibe_alerts", fake_vibe), \
             mock.patch.object(A, "batt_alerts", lambda *a, **k: []):
            A.evaluate(object(), overrides={"vibration": {"rms_warn": None}})
        self.assertEqual(seen["rms_warn"], A.DEFAULTS["vibration"]["rms_warn"])

class _Env:
    def __init__(self, t, v):
        self.t = t
        self.vmax = v
        self.unit = "m/s²"


class _VibeStore:
    """只给 vibe_alerts 用的最小 store：固定返回一条 VIBE 包络。"""

    meta = {"types": {"VIBE": {"cols": {"VibeX": 0}}}, "t_end": 10.0}

    def __init__(self, peaks):
        self._peaks = peaks

    def envelope(self, key, buckets: int = 0):
        import numpy as np
        t = np.arange(len(self._peaks), dtype=float)
        return _Env(t, np.asarray(self._peaks, dtype=float))


class TestCritClamp(unittest.TestCase):
    def test_crit_below_warn_does_not_create_alerts_below_warn(self) -> None:
        """把严重阈值调得比警告还低时，低于警告值的振动不该被报成严重。

        引擎里 `crit = max(crit, warn)` 就是干这个的：否则 30 m/s² 会被报成
        “严重”，而用户明明说的“超过 50 才算”。
        """
        import numpy as np
        store = _VibeStore([0.0, 30.0, 0.0, 30.0, 0.0, 0.0])
        with mock.patch.object(A, "_pick_inst", lambda *a, **k: None):
            hits = A.vibe_alerts(store, {"rms_warn": 50.0, "rms_crit": 10.0,
                                         "window_s": 1.0, "min_duration_s": 0.5,
                                         "merge_gap_s": 1.0,
                                         "report_axis_dominance": False})
        vib = [h for h in hits if h.rule.startswith("vibe_") and h.rule != "vibe_axis"]
        self.assertEqual(vib, [], f"30 m/s² 低于警告 50，不该报：{[(h.severity, h.value) for h in vib]}")

    def test_above_warn_is_reported_with_the_user_threshold(self) -> None:
        store = _VibeStore([0.0, 55.0, 0.0, 55.0, 0.0, 0.0])
        with mock.patch.object(A, "_pick_inst", lambda *a, **k: None):
            hits = A.vibe_alerts(store, {"rms_warn": 50.0, "rms_crit": 60.0,
                                         "window_s": 1.0, "min_duration_s": 0.5,
                                         "merge_gap_s": 1.0,
                                         "report_axis_dominance": False})
        vib = [h for h in hits if h.rule.startswith("vibe_") and h.rule != "vibe_axis"]
        self.assertTrue(vib, "超过用户设的警告阈值就该报")
        self.assertEqual({h.threshold for h in vib}, {50.0}, "阈值要回显用户设的值")
        self.assertEqual({h.severity for h in vib}, {"warn"})


class TestSettingsKeys(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="thr_")
        self._prev = st.Settings._instance
        self.s = st.Settings(Path(self.dir) / "settings.json")
        st.Settings.set_instance(self.s)
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))

    def test_defaults(self) -> None:
        self.assertEqual(self.s.get("vibe_warn"), 30.0)
        self.assertEqual(self.s.get("vibe_crit"), 60.0)

    def test_persisted(self) -> None:
        self.s.set("vibe_warn", 18.0)
        again = st.Settings(Path(self.dir) / "settings.json")
        self.assertEqual(again.get("vibe_warn"), 18.0)

    def test_out_of_range_is_clamped(self) -> None:
        self.s.set("vibe_warn", -5.0)
        self.assertGreaterEqual(self.s.get("vibe_warn"), 1.0)
        self.s.set("vibe_warn", 9999.0)
        self.assertLessEqual(self.s.get("vibe_warn"), 200.0)

    def test_change_signal_fires(self) -> None:
        got = []
        self.s.changed.connect(lambda k, v: got.append((k, v)))
        self.s.set("vibe_warn", 22.0)
        self.assertIn(("vibe_warn", 22.0), got)


class TestSettingsDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="thrdlg_")
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

    def test_vibration_rows_exist(self) -> None:
        self.assertIn("vibe_warn", self.dlg._widgets)
        self.assertIn("vibe_crit", self.dlg._widgets)
        w = self.dlg._widgets["vibe_warn"]
        self.assertIsInstance(w, QtWidgets.QDoubleSpinBox)
        self.assertEqual(w.value(), 30.0)
        self.assertIn("m/s", w.suffix())
        self.assertTrue(w.toolTip(), "要给用户解释单位与参考值")

    def test_no_scroll_area_and_everything_fits(self) -> None:
        """用户 2026-10-24：设置页不许出现滑动条，内容必须整页放得下。

        反向守卫：谁把滚动区加回来（哪怕藏起来），或者窗口最小尺寸小于内容，
        这里就转红——那就意味着有内容被裁掉了。
        """
        self.assertIsNone(self.dlg.findChild(QtWidgets.QScrollArea),
                          "设置页不该再有滚动区")
        for sb in self.dlg.findChildren(QtWidgets.QScrollBar):
            self.assertFalse(sb.isVisible(), "不该出现可见的滑动条")
        hint = self.dlg.sizeHint()
        # 屏幕比内容小时才允许让步（否则窗口超出屏幕就拖不动了），规则与
        # SettingsDialog._size_to_content 一致
        scr = self.dlg.screen()
        avail = scr.availableGeometry() if scr is not None else hint
        self.assertGreaterEqual(self.dlg.minimumWidth(), min(hint.width(), avail.width()),
                                "最小宽度必须容得下内容（屏幕够大时）")
        self.assertGreaterEqual(self.dlg.minimumHeight(), min(hint.height(), avail.height()),
                                "最小高度必须容得下内容（屏幕够大时）")
        # 内容不会溢出：按内容尺寸放开后，最后一个部件的底边仍在 body 内
        self.dlg.resize(*self.dlg._content_size)
        pump(self.app)
        body = self.dlg._body
        bottom = max((w.geometry().bottom() for w in body.findChildren(QtWidgets.QWidget)
                      if w.isVisible() and w.parent() is body), default=0)
        self.assertLessEqual(bottom, body.height() + 1, "内容不许超出页面底边")

    def test_editing_updates_settings_immediately(self) -> None:
        self.dlg._widgets["vibe_warn"].setValue(18.0)
        self.assertEqual(st.Settings.instance().get("vibe_warn"), 18.0)

    def test_raising_warn_above_crit_bumps_crit(self) -> None:
        w = self.dlg._widgets["vibe_warn"]
        c = self.dlg._widgets["vibe_crit"]
        c.setValue(40.0)
        w.setValue(55.0)
        self.assertGreaterEqual(c.value(), 55.0, "严重阈值不许低于警告阈值")
        self.assertGreaterEqual(float(st.Settings.instance().get("vibe_crit")), 55.0)

    def test_external_change_refreshes_the_widget(self) -> None:
        st.Settings.instance().set("vibe_warn", 27.0)
        pump(self.app)
        self.assertEqual(self.dlg._widgets["vibe_warn"].value(), 27.0)


class _FakeStore:
    """只提供 _reload_alerts 需要的最小面。"""

    meta = {"t_end": 100.0, "stats": {"t_start": 0.0}}

    def __init__(self, hits):
        self._hits = hits
        self.calls = []

    def alerts(self, config_path=None, overrides=None):
        self.calls.append(dict(overrides or {}))
        return list(self._hits)


class TestMainWindowWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="thrwin_")
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

    def _load_fake(self, hits):
        fake = _FakeStore(hits)
        self.w.store = fake
        return fake

    def test_overrides_carry_both_thresholds_and_are_clamped(self) -> None:
        st.Settings.instance().set("vibe_warn", 80.0)
        st.Settings.instance().set("vibe_crit", 10.0)
        ov = self.w._alert_overrides()
        self.assertEqual(ov["vibration"]["rms_warn"], 80.0)
        self.assertGreaterEqual(ov["vibration"]["rms_crit"], 80.0,
                                "严重档不能低于警告档")

    def test_overrides_passed_into_compute(self) -> None:
        fake = self._load_fake([])
        st.Settings.instance().set("vibe_warn", 25.0)
        self.w._alerts = self.w._compute_alerts()
        self.assertTrue(fake.calls)
        self.assertEqual(fake.calls[-1]["vibration"]["rms_warn"], 25.0)

    def test_reload_refreshes_table_and_status(self) -> None:
        hits = [AlertHit(rule="vibe_vibez", title="振动超标（VibeZ）", severity="critical",
                         t0=10.0, t1=12.0, value=71.0, threshold=60.0, unit="m/s²")]
        self._load_fake(hits)
        self.w._reload_alerts()
        self.assertEqual(self.w.alert_table.rowCount(), 1)
        msg = self.w.statusBar().currentMessage()
        self.assertIn("30", msg)                # 阈值回显
        self.assertIn("C1", msg)                # 计数

    def test_reload_is_fast_and_does_not_redo_lanes(self) -> None:
        """不能为了改阈值把整窗泳道重画一遍（那要 1 秒）。"""
        fake = self._load_fake([])
        with mock.patch.object(self.w, "_apply", wraps=self.w._apply) as spy:
            self.w._reload_alerts()
        spy.assert_not_called()
        self.assertTrue(fake.calls)

    def test_setting_change_is_debounced(self) -> None:
        self._load_fake([])
        self.w._apply_setting("vibe_warn", 22.0)
        self.assertTrue(self.w._alert_timer.isActive(), "改阈值应走防抖，而不是立刻重算")
        t0 = time.time()
        while self.w._alert_timer.isActive() and time.time() - t0 < 2:
            pump(self.app, 40)
        self.assertFalse(self.w._alert_timer.isActive())

    def test_old_store_without_overrides_falls_back(self) -> None:
        class OldStore:
            meta = {"t_end": 10.0}
            def alerts(self, config_path=None):        # 不接受 overrides
                return [AlertHit(rule="vibe_vibex", title="t", severity="warn",
                                 t0=1.0, t1=2.0)]
        self.w.store = OldStore()
        got = self.w._compute_alerts()
        self.assertEqual(len(got), 1, "旧 store 也要能算出告警，别因阈值功能丢失")

    def test_no_store_is_safe(self) -> None:
        self.w.store = None
        self.assertEqual(self.w._compute_alerts(), [])
        self.w._reload_alerts()                 # 不炸


if __name__ == "__main__":
    unittest.main()
