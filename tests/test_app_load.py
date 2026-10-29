# -*- coding: utf-8 -*-
"""tests/test_app_load.py —— 打不开日志时的反馈（错误框要能复制原始路径）。

中文目录名一旦出问题，唯一能看清真实字符串的地方就是那个错误框；
以前用 QMessageBox.warning 一次性弹纯文本，选不中也复制不了。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_app_load.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtWidgets                            # noqa: E402

import numpy as np                                               # noqa: E402

from apmlog.ui import settings as st                              # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 25) -> None:
    for _ in range(times):
        app.processEvents()


class TestLoadFailure(unittest.TestCase):
    def setUp(self) -> None:
        self.app = qapp()
        st.install_tip_style(self.app)
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_load_")
        self.addCleanup(self._dir.cleanup)
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self._dir.name) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.show()
        pump(self.app)

    def tearDown(self) -> None:
        self.w.close()
        pump(self.app, 5)
        self.w.deleteLater()
        pump(self.app, 5)

    def _capture_box(self):
        seen: dict[str, str] = {}

        def fake_exec(box_self):
            seen["text"] = box_self.text()
            seen["info"] = box_self.informativeText()
            seen["detail"] = box_self.detailedText()
            seen["selectable"] = bool(box_self.textInteractionFlags()
                                      & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            return 0
        return fake_exec, seen

    def test_error_box_carries_the_exact_path_and_is_selectable(self) -> None:
        fake, seen = self._capture_box()
        path = "C:/Users/MAKI/Desktop/工作相关/ZHT/测试/测试3/日志/00000165.BIN"
        with mock.patch.object(QtWidgets.QMessageBox, "exec", fake):
            self.w._on_failed("FileNotFoundError: [WinError 3] 系统找不到指定的路径。", path)
        self.assertIn(path, seen["info"], "错误框里必须能看到原样路径")
        self.assertIn(path, seen["detail"])
        self.assertTrue(seen["selectable"], "路径要能用鼠标选中复制")
        self.assertIsNone(self.w.store, "打不开就别动当前视图")
        self.assertIn("解析失败", self.w.statusBar().currentMessage())

    def test_status_text_shows_in_exactly_one_place(self) -> None:
        """底部只保留左侧一份：状态栏不得再挂右侧 permanent 标签。"""
        self.w.set_status("只该出现一次的文本")
        sb = self.w.statusBar()
        self.assertEqual(sb.currentMessage(), "只该出现一次的文本")
        self.assertFalse(hasattr(self.w, "status_label"), "右侧常驻标签应已删除")
        perm = [w for w in sb.findChildren(QtWidgets.QLabel)
                if sb.isAncestorOf(w) or w.parent() is sb]
        self.assertEqual(perm, [], f"状态栏里不该再有常驻控件：{perm}")

    def test_temporary_message_does_not_erase_the_persistent_one(self) -> None:
        """临时提示超时后要把常驻状态恢复回来，不能留白。"""
        self.w.set_status("常驻状态")
        self.w.set_status("临时提示", temporary=True)
        self.assertEqual(self.w.statusBar().currentMessage(), "临时提示")
        self.w._on_status_message("")            # 模拟超时后 Qt 发来的空消息
        self.assertEqual(self.w.statusBar().currentMessage(), "常驻状态")

    def test_failed_load_does_not_touch_timeline_or_title(self) -> None:
        fake, _seen = self._capture_box()
        self.w.timeline.set_span(0.0, 300.0)
        self.w.timeline.set_events([{"t": 10.0, "kind": "EV", "severity": "info", "display": "x"}])
        with mock.patch.object(QtWidgets.QMessageBox, "exec", fake):
            self.w._on_failed("boom", "C:/nope/BIN")
        self.assertFalse(self.w.timeline.is_empty(), "失败后时间线不该被清空")
        self.w.close()
        pump(self.app, 5)

    def test_real_worker_failure_reports_path_through_the_same_channel(self) -> None:
        bogus = str(Path(self._dir.name) / "没有这个文件夹" / "00000001.BIN")
        fake, seen = self._capture_box()
        with mock.patch.object(QtWidgets.QMessageBox, "exec", fake):
            self.w.load(bogus)
            deadline = QtCore.QElapsedTimer()
            deadline.start()
            while deadline.elapsed() < 20000:
                pump(self.app, 3)
                if seen.get("info"):
                    break
        self.assertIn(bogus, seen.get("info", ""), "真失败路径也要把原路径显示出来")
        if self.w.worker is not None:
            self.w.worker.wait(5000)          # 等解析线程收尾，别在退出时拆掉运行中的 QThread
        self.assertIsNone(self.w.store)


if __name__ == "__main__":
    unittest.main()


class _Ser:
    """最小 Series 替身：_feed_battery 只读 .t / .v。"""

    def __init__(self, t, v):
        self.t = np.asarray(t, dtype=float)
        self.v = np.asarray(v, dtype=float)


class _BatStore:
    """只提供 _feed_battery 需要的东西。channels 决定哪个 key 存在。"""

    def __init__(self, channels: dict):
        self._c = channels

    def series(self, key, t0=None, t1=None, max_points=None):
        if key not in self._c:
            raise KeyError(f"没有通道 {key}")
        return self._c[key]


class TestBatteryFeed(unittest.TestCase):
    """`_feed_battery`：报文名是 **BAT**（BATT_ 只是参数前缀）。"""

    def setUp(self) -> None:
        self.app = qapp()
        self._prev = st.Settings._instance
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_bat_")
        self.addCleanup(self._dir.cleanup)
        st.Settings.set_instance(st.Settings(Path(self._dir.name) / "s.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.show()
        pump(self.app)

    def tearDown(self) -> None:
        self.w.store = None
        self.w.close()
        pump(self.app, 5)
        self.w.deleteLater()

    def test_uses_BAT_message_not_BATT(self) -> None:
        self.w.store = _BatStore({"BAT.Volt": _Ser([0, 1, 2], [55.0, 54.9, 54.8]),
                                  "BAT.Curr": _Ser([0, 1, 2], [10.0, 11.0, 12.0])})
        self.w._feed_battery()
        self.assertEqual(self.w.map._bat_t.size, 3, "BAT.Volt 没被读到（BATT 是参数前缀不是报文名）")
        self.assertEqual(self.w.map._bat_v.size, 3)
        self.assertEqual(self.w.map._bat_a.size, 3)

    def test_falls_back_to_BATT_if_that_is_all_there_is(self) -> None:
        self.w.store = _BatStore({"BATT.Volt": _Ser([0, 1], [30.0, 29.0])})
        self.w._feed_battery()
        self.assertEqual(self.w.map._bat_v.size, 2)
        # 只有电压没有电流：电流列应该全是 nan（仪表只显示电压那一项）
        self.assertTrue(np.all(np.isnan(self.w.map._bat_a)))

    def test_missing_voltage_means_no_readout(self) -> None:
        self.w.store = _BatStore({"BAT.Curr": _Ser([0, 1], [5.0, 6.0])})
        self.w._feed_battery()
        self.assertEqual(self.w.map._bat_t.size, 0, "只有电流也不该硬显示")

    def test_no_store_clears(self) -> None:
        self.w.map.set_battery([0.0, 1.0], [50.0, 50.0], [1.0, 2.0])
        self.w.store = None
        self.w._feed_battery()
        self.assertEqual(self.w.map._bat_t.size, 0)

    def test_all_nan_voltage_clears(self) -> None:
        self.w.store = _BatStore({"BAT.Volt": _Ser([0, 1], [float("nan")] * 2)})
        self.w._feed_battery()
        self.assertEqual(self.w.map._bat_t.size, 0)


class TestNoRemPctJudgement(unittest.TestCase):
    """RemPct 已退出判定（用户 2026-10-24）：它再低也不许产生任何告警。

    这是反向守卫——谁把 RemPct 判定路径加回来，这里立刻转红。
    """

    def test_low_rempct_produces_no_alert(self) -> None:
        import numpy as np
        from apmlog.core import alerts as AL
        from tests.test_battery_thresholds import _Store, PACK13
        n = 40
        store = _Store([45.5] * n, PACK13)
        t = np.arange(n, dtype=float)
        rem = _Ser(t, [100.0] + [3.0] * (n - 1))

        def series(key, *a, **k):
            return rem if "RemPct" in str(key) else store._ser

        store.series = series
        hits = AL.batt_alerts(store, dict(AL.DEFAULTS["battery"]))
        self.assertEqual([h.rule for h in hits if "remain" in h.rule], [],
                         "RemPct 掉到 3% 也不该报——判定只看单芯电压")

    def test_remain_keys_are_gone_from_defaults_and_yaml(self) -> None:
        from apmlog.core import alerts as AL
        self.assertNotIn("remain_warn", AL.DEFAULTS["battery"])
        self.assertNotIn("remain_crit", AL.DEFAULTS["battery"])
        cfg = AL.load_config()["battery"]
        self.assertNotIn("remain_warn", cfg)
        self.assertNotIn("remain_crit", cfg)

    def test_old_config_keys_are_ignored(self) -> None:
        """旧 yaml / 旧设置里残留的 remain_* 不许再起作用。"""
        from apmlog.core import alerts as AL
        from tests.test_battery_thresholds import _Store, PACK13
        store = _Store([45.5] * 10, PACK13)
        cfg = dict(AL.DEFAULTS["battery"])
        cfg.update({"remain_warn": 99.0, "remain_crit": 98.0})
        hits = AL.batt_alerts(store, cfg)
        self.assertEqual([h.rule for h in hits if "remain" in h.rule], [])


if __name__ == "__main__":
    unittest.main()
