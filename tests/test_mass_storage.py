# -*- coding: utf-8 -*-
"""tests/test_mass_storage.py —— U 盘模式按钮（``MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN``）。

本工程对飞控严格只读，这条是**唯一**一条会改变飞控状态的命令，所以护栏要逐条测：

* 已解锁（armed）→ 拦截，**一条命令都不发**；
* 固件不支持 → ``MAV_RESULT_UNSUPPORTED`` → 明确提示“固件没编进这个功能”；
* 正常受理 → ``param1=5``、``MAV_RESULT_ACCEPTED``，并真的进到 U 盘模式；
* 界面上：二次确认取消后**不发**，确认后才投命令；结果提示不能是空字符串。

真机未验证（见 docs/CONTRACT.md §30）；这里跑 FakeFC 回环。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m pytest tests/test_mass_storage.py -q
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets                                   # noqa: E402

from apmlog.link import ftp as ftpmod                           # noqa: E402
from apmlog.link.fakes import FakeFC                            # noqa: E402
from apmlog.ui import linkpanel as lp                           # noqa: E402
from apmlog.ui.linkpanel import LinkPanel                       # noqa: E402
from apmlog.ui.settings import Settings                         # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 3) -> None:
    for _ in range(times):
        app.processEvents()


class _WorkerBase(unittest.TestCase):
    """只连 FakeFC、不启 QThread 的 worker（同步调用，结果确定）。"""

    fc_kwargs: dict = {}

    def setUp(self):
        self.app = qapp()
        self.dir = Path(tempfile.mkdtemp(prefix="apmlog-msd-"))
        self.fc = FakeFC([(0, b"\x00" * 1024)], heartbeat_period=0.1, **self.fc_kwargs)
        self.w = lp._LinkWorker()
        self.results: list[tuple[bool, str]] = []
        self.notes: list[str] = []
        self.w.msd_result.connect(lambda ok, msg: self.results.append((ok, msg)))
        self.w.note.connect(self.notes.append)
        host, port = self.fc.serve_tcp("127.0.0.1", 0)
        self.w._do_connect(target=f"tcp:{host}:{port}", baud=57600, wait=3.0)
        self.assertIsNotNone(self.w.sess, "worker 没连上 FakeFC")

    def tearDown(self):
        try:
            if self.w.sess is not None:
                self.w.sess.close()
        finally:
            self.fc.stop()
        self.w.deleteLater()
        shutil.rmtree(self.dir, ignore_errors=True)
        pump(self.app, 3)

    def _sent_msd(self) -> list[tuple[int, tuple]]:
        return [c for c in self.fc.commands
                if c[0] == ftpmod.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN]


class TestArmedInterlock(_WorkerBase):
    fc_kwargs = {"armed": True}

    def test_armed_is_blocked_and_sends_nothing(self):
        self.assertTrue(self.w.sess.is_armed(), "FakeFC 心跳里的 armed 位没被识别")
        self.w._do_mass_storage()
        self.assertEqual(self.fc.commands, [], "armed 时必须一条命令都不发")
        self.assertEqual(len(self.results), 1)
        ok, msg = self.results[0]
        self.assertFalse(ok)
        self.assertIn("解锁", msg)
        self.assertFalse(self.fc.msd_entered)
        self.assertTrue(any("拦截" in n for n in self.notes), self.notes)


class TestUnsupportedFirmware(_WorkerBase):
    fc_kwargs = {"msd_supported": False}

    def test_unsupported_message_is_explicit(self):
        self.w._do_mass_storage()
        self.assertEqual(len(self.results), 1)
        ok, msg = self.results[0]
        self.assertFalse(ok)
        self.assertIn("不支持", msg)
        self.assertIn("固件", msg)
        self.assertEqual(len(self._sent_msd()), 1, "不支持是飞控回的 ACK，命令本身要发出去")
        self.assertFalse(self.fc.msd_entered)


class TestAcceptedPath(_WorkerBase):
    def test_command_layout_and_ack(self):
        self.w._do_mass_storage()
        sent = self._sent_msd()
        self.assertEqual(len(sent), 1)
        cmd, params = sent[0]
        self.assertEqual(cmd, 246)
        self.assertEqual(params[0], 5.0, "param1 必须是 REBOOT_TO_MASS_STORAGE(5)")
        self.assertEqual(params[1:], (0.0,) * 6)
        self.assertTrue(self.fc.msd_entered, "飞控侧应该已经进入 U 盘模式")
        ok, msg = self.results[0]
        self.assertTrue(ok)
        self.assertIn("U 盘", msg)
        self.assertIn("APM/LOGS", msg)

    def test_no_link_reports_clearly(self):
        self.w.sess.close()
        self.w.sess = None
        self.w._do_mass_storage()
        self.assertEqual(self.results, [(False, "未连接飞控")])
        self.assertEqual(self.fc.commands, [])

    def test_missing_ack_is_reported_as_uncertain(self):
        """飞控直接重启（不回 ACK）时不能报“失败”，要说“可能已经重启”。"""
        with mock.patch.object(type(self.w.sess), "wait_for", return_value=None):
            self.w._do_mass_storage()
        ok, msg = self.results[0]
        self.assertTrue(ok)
        self.assertIn("没收到 COMMAND_ACK", msg)


class TestPanelFlow(unittest.TestCase):
    def setUp(self):
        self.app = qapp()
        self.prev = Settings.instance().get("dl_transport")
        self.w = LinkPanel(default_target="tcp:127.0.0.1:5760")
        pump(self.app)
        self.w._link_up = True

    def tearDown(self):
        Settings.instance().set("dl_transport", self.prev)
        w, self.w = self.w, None
        w.shutdown()
        w.close()
        w.deleteLater()
        pump(self.app, 6)

    def test_controls_exist(self):
        self.assertTrue(hasattr(self.w, "btn_msd"))
        self.assertIn("U 盘", self.w.btn_msd.text())
        self.assertEqual(self.w.current_transport(), "auto")

    def test_transport_combo_persists(self):
        codes = [self.w.cb_transport.itemData(i) for i in range(self.w.cb_transport.count())]
        self.assertEqual(codes, ["auto", "ftp", "log"])
        self.w.cb_transport.setCurrentIndex(codes.index("ftp"))
        self.assertEqual(self.w.current_transport(), "ftp")
        self.assertEqual(Settings.instance().get("dl_transport"), "ftp")
        self.assertEqual(self.w.worker._transport, "ftp")

    def test_cancel_confirm_sends_nothing(self):
        with mock.patch.object(LinkPanel, "_confirm_msd", return_value=False), \
                mock.patch.object(self.w.worker, "post") as post:
            self.w._on_mass_storage()
        post.assert_not_called()
        self.assertFalse(self.w._busy)
        self.assertIn("已取消", self.w.plain.toPlainText())

    def test_confirm_posts_command(self):
        with mock.patch.object(LinkPanel, "_confirm_msd", return_value=True), \
                mock.patch.object(self.w.worker, "post") as post:
            self.w._on_mass_storage()
        post.assert_called_once_with("mass_storage")
        self.assertTrue(self.w._busy)
        self.assertEqual(self.w._busy_op, "mass_storage")

    def test_not_connected_warns(self):
        self.w._link_up = False
        self.w.worker.sess = None
        with mock.patch.object(QtWidgets.QMessageBox, "warning") as warn, \
                mock.patch.object(self.w.worker, "post") as post:
            self.w._on_mass_storage()
        warn.assert_called_once()
        post.assert_not_called()

    def test_result_message_is_never_blank(self):
        shown: list[str] = []
        with mock.patch.object(QtWidgets.QMessageBox, "information",
                               side_effect=lambda *a: shown.append(a[2])), \
                mock.patch.object(QtWidgets.QMessageBox, "warning",
                                  side_effect=lambda *a: shown.append(a[2])):
            self.w._on_msd_result(True, "飞控已接受")
            self.w._on_msd_result(False, "飞控处于解锁（armed）状态，已拦截")
        self.assertTrue(all(isinstance(s, str) and s.strip() for s in shown), shown)
        self.assertFalse(self.w._busy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
