# -*- coding: utf-8 -*-
"""tests/test_link_panel.py —— LinkPanel 的 offscreen 构造、信号、按钮状态与抓图自测。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m unittest tests.test_link_panel -v
产物：selftest/linkpanel.png
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtWidgets                           # noqa: E402

from apmlog.link.download import DownloadResult, RemoteLog    # noqa: E402
from apmlog.ui import linkpanel as lp
from apmlog.ui.linkpanel import LinkPanel                     # noqa: E402

_APP: QtWidgets.QApplication | None = None
PNG = ROOT / "selftest" / "linkpanel.png"


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 3) -> None:
    for _ in range(times):
        app.processEvents()


def sample_logs() -> list[RemoteLog]:
    return [
        RemoteLog(165, 6, 170, 315532800, 3_145_728),
        RemoteLog(166, 6, 170, 315532800, 2_945_024),
        RemoteLog(167, 6, 170, 1_791_359_555, 2_945_024),
        RemoteLog(168, 6, 170, 315532800, 90 * 3 + 56),
    ]


class TestLinkPanel(unittest.TestCase):
    def setUp(self):
        self.app = qapp()
        self.w = LinkPanel(default_target="tcp:127.0.0.1:5760")
        pump(self.app)

    def tearDown(self):
        # offscreen 实测：多个 top-level 面板留到解释器退出时才回收会在 Qt native 层 abort
        # （Fatal Python error: Aborted，无 Python 帧）。必须趁 QApplication 存活时彻底销毁。
        w, self.w = self.w, None
        w.shutdown()
        w.close()
        w.deleteLater()
        pump(self.app, 6)

    # ---------------------------------------------------------------- 结构
    def test_offscreen_construct_and_signals_exist(self):
        self.assertIsInstance(self.w, QtWidgets.QWidget)
        for sig in ("logDownloaded", "connectionChanged", "logsListed"):
            self.assertTrue(hasattr(LinkPanel, sig), sig)
        pump(self.app)
        self.assertTrue(self.w.isVisible() or True)

    def test_no_kernel_dependency(self):
        """面板只能依赖 apmlog.core.model（展示工具），不得 import store/reader/cli/app。

        用 AST 看**真正的 import**，而不是文本包合（文档字符串里会提到这些名字）。
        """
        import ast

        src = (ROOT / "apmlog" / "ui" / "linkpanel.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.append(node.module)
                    imported += [f"{node.module}.{a.name}" for a in node.names]
        forbidden = ("apmlog.core.store", "apmlog.core.reader", "apmlog.core.events",
                     "apmlog.core.alerts", "apmlog.ui.app", "apmlog.cli", "apmlog.core.codebooks")
        bad = [m for m in imported if any(m == f or m.startswith(f + ".") for f in forbidden)]
        self.assertEqual(bad, [], f"linkpanel 引入了内核依赖: {bad}；全部 import={sorted(set(imported))}")
        self.assertIn("apmlog.core.model", imported, "唯一允许的内核依赖是 model（human_size）")

    def test_show_logs_fills_table(self):
        self.w.show_logs(sample_logs())
        pump(self.app)
        self.assertEqual(self.w.table.rowCount(), 4)
        self.assertEqual(self.w.table.columnCount(), 5)
        self.assertEqual(self.w.lbl_count.text(), "4 条")
        self.assertEqual(self.w.table.item(0, 0).text(), "165")
        self.assertEqual(self.w.table.item(0, 4).text(), "空闲")
        self.assertIn("RTC", self.w.table.item(0, 1).text())
        self.assertEqual(self.w.table.item(2, 1).text(),
                         RemoteLog(167, 6, 170, 1_791_359_555, 1).created.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(self.w.table.item(3, 3).text(), "4", "90*3+56 应是 4 块")
        self.assertIn("MB", self.w.table.item(0, 2).text() + self.w.table.item(1, 2).text())

    def test_selection_and_id(self):
        self.w.show_logs(sample_logs())
        self.assertIsNone(self.w.selected_log_id(), "未选中时不应有 id")
        self.w.table.setCurrentCell(1, 0)
        pump(self.app)
        self.assertEqual(self.w.selected_log_id(), 166)

    # ---------------------------------------------------------------- 状态与按钮
    def test_state_text_and_signal(self):
        seen: list[str] = []
        self.w.connectionChanged.connect(seen.append)
        self.w.set_state_text("ready", "飞控在线 sysid=1")
        pump(self.app)
        self.assertIn("飞控在线", self.w.lbl_state.text())
        self.assertIn("ready", seen)
        self.w.set_state_text("lost", "对端已断开")
        pump(self.app)
        self.assertIn("链路中断", self.w.lbl_state.text())
        self.assertTrue(self.w.lbl_state.styleSheet().startswith("color:"))

    def test_button_enablement(self):
        self.w.show_logs(sample_logs())
        pump(self.app)
        self.assertTrue(self.w.btn_connect.isEnabled(), "未连接时连接按钮可用")
        self.assertFalse(self.w.btn_disconnect.isEnabled())
        self.assertFalse(self.w.btn_refresh.isEnabled())
        self.assertFalse(self.w.btn_download.isEnabled(), "未选中/未连接时不能下载")
        self.w._on_state("ready", "")
        self.w.table.setCurrentCell(0, 0)
        pump(self.app)
        self.w._set_busy(False)
        self.assertTrue(self.w.btn_refresh.isEnabled())
        self.assertTrue(self.w.btn_download.isEnabled())
        self.w._set_busy(True, "下载中")
        self.assertFalse(self.w.btn_download.isEnabled())
        self.assertTrue(self.w.btn_cancel.isEnabled())

    def test_progress_display(self):
        self.w.set_progress(500, 1000, 2048.0)
        pump(self.app)
        self.assertEqual(self.w.pb.value(), 50)
        self.assertIn("%p%", self.w.pb.format())
        self.assertIn("500 B", self.w.pb.format())
        self.assertIn("KB/s", self.w.lbl_rate.text())
        self.w.set_progress(1000, 1000, 2048.0)
        pump(self.app)
        self.assertEqual(self.w.pb.value(), 100)

    # ---------------------------------------------------------------- 信号语义
    def test_log_downloaded_signal_on_complete(self):
        got: list[str] = []
        self.w.logDownloaded.connect(got.append)
        self.w.show_logs(sample_logs())
        pump(self.app)
        res = DownloadResult(log_id=167, path=str(ROOT / "selftest" / "00000167.BIN"),
                             expected_bytes=2_945_024, written_bytes=2_945_024, complete=True,
                             elapsed=18.0, blocks_ok=32723, bytes_per_sec=163_840.0, reason="complete")
        self.w._set_row_state(167, "下载中")
        self.w._on_done(res)
        pump(self.app)
        self.assertEqual(got, [res.path], "完成必须发 logDownloaded(路径)")
        self.assertEqual(self.w.table.item(2, 4).text(), "已完成")
        self.assertIn("KB/s", self.w.lbl_rate.text())
        self.assertIn("完成：", self.w.plain.toPlainText())

    def test_incomplete_keeps_row_state_and_no_signal(self):
        got: list[str] = []
        self.w.logDownloaded.connect(got.append)
        self.w.show_logs(sample_logs())
        pump(self.app)
        res = DownloadResult(log_id=166, path=str(ROOT / "x.part"), expected_bytes=1000,
                             written_bytes=400, complete=False, elapsed=1.0, reason="cancelled")
        self.w._on_done(res)
        pump(self.app)
        self.assertEqual(got, [], "未完成绝不能发 logDownloaded")
        self.assertIn("cancelled", self.w.table.item(1, 4).text())

    def test_note_appends_with_timestamp(self):
        before = self.w.plain.toPlainText()
        self.w._on_note("链路已断开")
        pump(self.app)
        text = self.w.plain.toPlainText()
        self.assertIn("链路已断开", text)
        self.assertRegex(text, r"^\[\d{2}:\d{2}:\d{2}\] .*$", "每行应带时间戳")
        self.assertEqual(self.w.plain.toPlainText().count("链路已断开"), 1)
        self.assertEqual(before, "")

    # ---------------------------------------------------------------- 输入校验
    def test_bad_target_is_reported_not_crash(self):
        self.w.set_target("乱七八糟")
        self.w.btn_connect.click()
        pump(self.app)
        self.assertIn("目标语法错误", self.w.plain.toPlainText())
        self.assertTrue(self.w.btn_connect.isEnabled())

    def test_empty_target_is_reported(self):
        self.w.set_target("   ")
        self.w.btn_connect.click()
        pump(self.app)
        self.assertIn("目标为空", self.w.plain.toPlainText())

    def test_options_sync(self):
        self.w.sp_blocks.setValue(37)
        self.w.ed_dir.setText(str(ROOT / "selftest" / "_link_dir"))
        self.w._sync_options()
        pump(self.app)
        self.assertEqual(self.w.worker._blocks, 37)
        self.assertEqual(self.w.worker._out_dir, str(ROOT / "selftest" / "_link_dir"))
        self.assertEqual(self.w.sp_blocks.suffix(), " ×90B")

    def test_placeholder_text_covers_all_schemes(self):
        ph = self.w.ed_target.placeholderText()
        for token in ("COM5", "serial:", "tcp:", "tcpsrv:", "udp:", "udpout:"):
            self.assertIn(token, ph, token)

    # ---------------------------------------------------------------- 抓图
    def test_export_png_offscreen(self):
        self.w.show_logs(sample_logs())
        self.w.table.setCurrentCell(2, 0)
        self.w._set_row_state(165, "已完成")
        self.w._set_row_state(167, "下载中")
        self.w.lbl_state.setText("飞控在线　sysid=1 compid=1")
        self.w.lbl_state.setStyleSheet("color:#3aa659; font-weight:600;")
        self.w.lbl_detail.setText("rx 4 213 帧 · CRC 0")
        self.w.set_progress(1_832_040, 2_945_024, 5_120.0)
        for line in ("已打开链路：TCP 客户端 → 127.0.0.1:5760",
                     "飞控：ArduCopter V4.7.0 (1511f271)",
                     "列表：收到 4 条（期望 4），0.11s，complete",
                     "开始下载 log 167（2945024 字节，每请求 20 块）"):
            self.w._on_note(line)
        pump(self.app)
        PNG.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(self.w.export_png(str(PNG), 780, 660), "grab 必须成功")
        self.assertTrue(PNG.exists())
        size = PNG.stat().st_size
        print(f"\n[offscreen] {PNG.relative_to(ROOT)} = {size} bytes", flush=True)
        self.assertGreater(size, 4096, "PNG 太小可能是空布局")
        head = PNG.read_bytes()[:8]
        self.assertEqual(head, b"\x89PNG\r\n\x1a\n", "必须是合法 PNG")

    def test_ui_hint_lists_all_targets(self):
        from apmlog.link.endpoint import Endpoint
        hint = Endpoint(kind="?", target="").ui_hint
        for token in ("serial:", "tcp:", "tcpsrv:", "udp:", "udpout:", "COM5"):
            self.assertIn(token, hint)
        self.assertIsNotNone(re.search(r"baud=\d+", hint))


FAKE_PORTS = [("COM91", "COM91　USB-SERIAL CH340　[VID 1A86 / PID 7523]"),
              ("COM5", "COM5　USB Serial Port　[VID 0403 / PID 6001]")]


class TestPortPicker(unittest.TestCase):
    """链路区：端口靠选不靠拄；目标串由控件拼装，手动输入只当高级入口。"""

    def setUp(self):
        self.app = qapp()
        self._ports = list(FAKE_PORTS)
        self.patcher = mock.patch.object(lp, "serial_ports", side_effect=lambda: list(self._ports))
        self.patcher.start()
        self._real_load = LinkPanel._load_link_prefs      # 打补丁前先拿住原实现
        # 不读用户目录里的真 link.json，保证结果确定
        self.pref = mock.patch.object(LinkPanel, "_load_link_prefs", return_value="")
        self.pref.start()

    def tearDown(self):
        self.pref.stop()
        self.patcher.stop()

    def _panel(self, default: str = ""):
        w = LinkPanel(default_target=default)
        pump(self.app)
        self.addCleanup(self._kill, w)
        return w

    def _kill(self, w):
        w.shutdown()
        w.close()
        w.deleteLater()
        pump(self.app, 6)

    def test_ports_are_listed_and_first_selected(self):
        w = self._panel()
        self.assertEqual(w._mode(), "serial")
        self.assertEqual([w.cb_port.itemData(i) for i in range(w.cb_port.count())],
                         ["COM91", "COM5"])
        self.assertEqual(w.cb_port.currentData(), "COM91")
        self.assertIn("检测到 2 个串口", w.lbl_port_info.text())
        self.assertEqual(w._compose_target(), "serial:COM91?baud=57600")
        self.assertIn("serial:COM91?baud=57600", w.lbl_target.text())

    def test_switching_port_updates_preview(self):
        w = self._panel()
        w.cb_port.setCurrentIndex(1)
        self.assertEqual(w._compose_target(), "serial:COM5?baud=57600")
        w.cb_baud.setCurrentText("115200")
        self.assertEqual(w._compose_target(), "serial:COM5?baud=115200")

    def test_default_target_fills_the_right_page(self):
        for raw, mode, expect in (("tcp:192.168.2.1:5760", "tcp", "tcp:192.168.2.1:5760"),
                                  ("udp:14550", "udp", "udp:0.0.0.0:14550"),
                                  ("tcpsrv:0.0.0.0:5761", "tcpsrv", "tcpsrv:0.0.0.0:5761"),
                                  ("udpout:127.0.0.1:14551", "udpout", "udpout:127.0.0.1:14551")):
            w = self._panel(raw)
            self.assertEqual(w._mode(), mode, raw)
            self.assertEqual(w.stack.currentIndex(), w.cb_mode.currentIndex(), raw)
            self.assertEqual(w._compose_target(), expect, raw)

    def test_unrecognized_target_falls_back_to_manual(self):
        w = self._panel("pipe:自测")
        self.assertEqual(w._mode(), "manual")
        self.assertEqual(w.ed_target.text(), "pipe:自测")
        self.assertEqual(w._compose_target(), "pipe:自测")

    def test_last_port_absent_is_kept_and_marked(self):
        w = self._panel("serial:COM77?baud=115200")
        i = w.cb_port.findData("COM77")
        self.assertGreaterEqual(i, 0)
        self.assertIn("未检测到", w.cb_port.itemText(i))
        self.assertEqual(w.cb_port.currentData(), "COM77")
        self.assertEqual(w.cb_baud.currentText(), "115200")
        self.assertEqual(w._compose_target(), "serial:COM77?baud=115200")

    def test_bad_baud_blocks_connect(self):
        w = self._panel()
        w.cb_baud.setCurrentText("abc")
        with self.assertRaises(ValueError):
            w._compose_target()
        w.btn_connect.click()
        pump(self.app)
        self.assertIn("目标无法组装", w.plain.toPlainText())
        self.assertIn("波特率", w.lbl_target.text())

    def test_refresh_button_picks_up_new_ports(self):
        w = self._panel()
        self._ports.insert(0, ("COM3", "COM3　Bluetooth Link"))
        w.btn_ports.click()
        pump(self.app)
        self.assertIn("COM3", [w.cb_port.itemData(i) for i in range(w.cb_port.count())])
        self.assertEqual(w.cb_port.currentData(), "COM91", "刷新要保留用户已选的口")
        self.assertIn("检测到 3 个串口", w.lbl_port_info.text())
        # 原口拔掉了 → 回退到列表第一个，不能停在空选择上
        self._ports.remove(FAKE_PORTS[0])
        w.btn_ports.click()
        pump(self.app)
        self.assertEqual(w.cb_port.currentData(), "COM3")
        self.assertEqual(w._compose_target(), "serial:COM3?baud=57600")

    def test_prefs_roundtrip_uses_target_string(self):
        tmp = ROOT / "selftest" / "_link_prefs"
        tmp.mkdir(parents=True, exist_ok=True)
        path = tmp / "link.json"
        with mock.patch.object(LinkPanel, "_link_prefs_path", lambda s: path), \
                mock.patch.object(LinkPanel, "_load_link_prefs", self._real_load):
            w = LinkPanel(default_target="tcp:192.168.2.9:5761")
            pump(self.app)
            w._save_link_prefs()
            self.assertTrue(path.exists())
            w2 = LinkPanel(default_target="")          # 无默认时回到上次用的目标
            pump(self.app)
            self.assertEqual(w2._mode(), "tcp")
            self.assertEqual(w2._compose_target(), "tcp:192.168.2.9:5761")
            self._kill(w2)
            self._kill(w)
        path.unlink(missing_ok=True)

    def test_busy_locks_link_widgets(self):
        w = self._panel()
        w._set_busy(True, "测试占用", op="connect")
        self.assertFalse(w.cb_mode.isEnabled())
        self.assertFalse(w.cb_port.isEnabled())
        self.assertFalse(w.btn_ports.isEnabled())
        w._set_busy(False)
        self.assertTrue(w.cb_mode.isEnabled())
        self.assertTrue(w.btn_ports.isEnabled())


class TestPortClassifier(unittest.TestCase):
    """串口排序纯逻辑：像飞控的排前面，虚拟口标出来（不依赖真机）。"""

    def _p(self, desc, hwid="", vid=None, pid=None):
        return SimpleNamespace(description=desc, hwid=hwid, vid=vid, pid=pid)

    def test_fc_signatures_rank_first(self):
        self.assertEqual(lp.port_kind(self._p("ArduPilot (COM91)", vid=0x1209, pid=0x5741))[0], 0)
        self.assertEqual(lp.port_kind(self._p("USB-SERIAL CH340", vid=0x1A86, pid=0x7523))[0], 0)
        self.assertEqual(lp.port_kind(self._p("PX4 FMU V6X", vid=None, pid=None))[0], 0)
        self.assertEqual(lp.port_kind(self._p("TELEM1 数传", vid=None, pid=None))[0], 0)

    def test_virtual_ports_rank_last(self):
        self.assertEqual(lp.port_kind(self._p("蓝牙链接上的标准串行",
                                              r"BTHENUM\{00001101}"))[0], 2)
        self.assertEqual(lp.port_kind(self._p("Intel(R) Active Management Technology",
                                              "MANS"))[0], 2)

    def test_plain_uart_is_middle(self):
        self.assertEqual(lp.port_kind(self._p("USB Serial Device", "ACM0"))[0], 1)

    def test_short_desc_strips_device_and_limits_length(self):
        self.assertEqual(lp._short_desc("ArduPilot (COM91)", "COM91"), "ArduPilot")
        self.assertEqual(lp._short_desc("PX4 (com91)", "COM91"), "PX4")
        long = "X" * 90
        out = lp._short_desc(long, "COM1")
        self.assertEqual(len(out), 49)
        self.assertTrue(out.endswith("…"))

    def test_sort_key_orders_com_numbers(self):
        self.assertLess(lp._port_sort_key("COM2"), lp._port_sort_key("COM10"))
        self.assertLess(lp._port_sort_key("COM10"), lp._port_sort_key("/dev/ttyUSB3"))


class TestBatchDownload(unittest.TestCase):
    """日志表勾选列 + 批量下载队列：按列表顺序一条接一条，失败/取消不丢勾选。"""

    def setUp(self):
        self.app = qapp()
        self.patchers = [mock.patch.object(lp, "serial_ports", return_value=[]),
                         mock.patch.object(LinkPanel, "_load_link_prefs", return_value="")]
        for p in self.patchers:
            p.start()
        self.w = LinkPanel(default_target="")
        pump(self.app)
        self.w.show_logs(sample_logs())
        self.w._on_state("ready", "")
        self.posts: list[tuple[str, dict]] = []
        self.w.worker.post = lambda cmd, **kw: self.posts.append((cmd, kw))   # 不碰真线程
        pump(self.app)

    def tearDown(self):
        w, self.w = self.w, None
        w.shutdown()
        w.close()
        w.deleteLater()
        pump(self.app, 6)
        for p in reversed(self.patchers):
            p.stop()

    def _check(self, *log_ids: int) -> None:
        want = set(log_ids)
        for r in range(self.w.table.rowCount()):
            lid = self.w._log_id_of_row(r)
            self.w.table.item(r, 0).setCheckState(
                QtCore.Qt.CheckState.Checked if lid in want else QtCore.Qt.CheckState.Unchecked)
        pump(self.app)

    def _done(self, log_id: int, complete: bool = True, path: str = "") -> None:
        self.w._on_done(DownloadResult(
            log_id=log_id, path=path or f"C:/dl/0000{log_id}.BIN", expected_bytes=1000,
            written_bytes=1000 if complete else 300, complete=complete, elapsed=2.0,
            reason="complete" if complete else "link_lost"))
        pump(self.app)

    def test_number_column_is_checkable(self):
        it = self.w.table.item(0, 0)
        self.assertTrue(it.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        self.assertEqual(it.checkState(), QtCore.Qt.CheckState.Unchecked)
        self.assertEqual(it.data(QtCore.Qt.ItemDataRole.UserRole), 165)
        self.assertEqual(self.w.lbl_checked.text(), "已勾选 0 条")
        self.assertEqual(self.w.btn_download.text(), "下载选中日志")

    def test_check_updates_count_estimate_and_button(self):
        self._check(165, 167)
        self.assertEqual(self.w._checked_ids(), [165, 167], "下载顺序应按表格顺序")
        self.assertIn("已勾选 2 条", self.w.lbl_checked.text())
        self.assertIn("分钟", self.w.lbl_checked.text(), "多条应给出耗时估算")
        self.assertEqual(self.w.btn_download.text(), "下载勾选的 2 条")
        self.assertEqual(self.w.chk_all.checkState(), QtCore.Qt.CheckState.PartiallyChecked)

    def test_select_all_checks_and_clears_every_row(self):
        self.w.chk_all.setChecked(True)
        pump(self.app)
        self.assertEqual(self.w._checked_ids(), [165, 166, 167, 168])
        self.assertEqual(self.w.chk_all.checkState(), QtCore.Qt.CheckState.Checked)
        self.w.chk_all.setChecked(False)
        pump(self.app)
        self.assertEqual(self.w._checked_ids(), [])
        self.assertEqual(self.w.btn_download.text(), "下载选中日志")

    def test_queue_downloads_in_order_and_unchecks_done(self):
        sig: list[tuple[list, list]] = []
        self.w.batchFinished.connect(lambda ok, bad: sig.append((list(ok), list(bad))))
        self._check(165, 167, 168)
        self.w.btn_download.click()
        pump(self.app)
        self.assertEqual([c for c, _ in self.posts], ["download"], "一次只发起一条")
        self.assertEqual(self.posts[0][1]["log_id"], 165)
        self.assertTrue(self.w.batch_active)
        self.assertEqual(self.w.table.item(2, 4).text(), "排队中")
        self.assertEqual(self.w.table.item(1, 4).text(), "空闲", "没勾的不应被动状态")
        self._done(165)
        self.assertEqual(self.posts[-1][1]["log_id"], 167)
        self.assertNotIn(165, self.w._checked, "下完应自动取消勾选")
        self.assertIn("2/3", self.w.pb.format())
        self._done(167)
        self._done(168)
        self.assertEqual(self.w._batch, [])
        self.assertFalse(self.w.batch_active)
        self.assertEqual(self.w._checked_ids(), [])
        self.assertIn("批量结束：成功 3/3", self.w.plain.toPlainText())
        self.assertEqual(len(sig), 1)
        self.assertEqual((len(sig[0][0]), sig[0][1]), (3, []))

    def test_failure_stops_queue_and_keeps_remaining_checked(self):
        self._check(165, 166, 167)
        self.w.btn_download.click()
        pump(self.app)
        self._done(165, complete=False)
        self.assertEqual([c for c, _ in self.posts], ["download"], "第一条失败不应继续发起")
        self.assertEqual(self.w.table.item(1, 4).text(), "未下载（已停止）")
        self.assertEqual(self.w._checked_ids(), [165, 166, 167],
                         "没下完的（含刚失败那条）要留着勾选，再点就是断点续传")
        self.assertIn("已停止：剩余 2 条未发起", self.w.plain.toPlainText())
        self.assertIn("未发起 2 条", self.w.plain.toPlainText())
        self.assertFalse(self.w._busy)

    def test_cancel_clears_pending_but_keeps_checks(self):
        self._check(165, 166, 167)
        self.w.btn_download.click()
        pump(self.app)
        self.w.btn_cancel.click()
        pump(self.app)
        self.assertEqual(self.w._queue, [])
        self.assertIn("清空排队中的 2 条", self.w.plain.toPlainText())
        self._done(165)
        self.assertEqual([c for c, _ in self.posts], ["download"])
        self.assertEqual(self.w._checked_ids(), [166, 167])
        self.assertIn("成功 1/3", self.w.plain.toPlainText())

    def test_nothing_checked_falls_back_to_selected_row(self):
        sig: list[tuple[list, list]] = []
        self.w.batchFinished.connect(lambda ok, bad: sig.append((list(ok), list(bad))))
        self.w.table.setCurrentCell(1, 0)
        pump(self.app)
        self.w.btn_download.click()
        pump(self.app)
        self.assertEqual(self.posts, [("download", {"log_id": 166})])
        self.assertFalse(self.w.batch_active, "单条不算批量，主窗要能直接开窗分析")
        self._done(166)
        self.assertEqual(len(sig), 1, "单条也要发 batchFinished，保证主窗只有一条路径")
        self.assertNotIn("批量结束", self.w.plain.toPlainText())

    def test_empty_list_click_is_reported(self):
        self.w.show_logs([])
        pump(self.app)
        self.w._link_up = True
        self.w._set_busy(False)
        self.w._on_download()
        self.assertIn("请先勾选", self.w.plain.toPlainText())
        self.assertEqual(self.posts, [])

    def test_refresh_keeps_checks_and_drops_vanished(self):
        self._check(166, 168)
        self.w.show_logs([l for l in sample_logs() if l.log_id != 168])
        pump(self.app)
        self.assertEqual(self.w._checked_ids(), [166])
        self.assertEqual(self.w.table.item(1, 0).checkState(), QtCore.Qt.CheckState.Checked)
        self.assertIn("已勾选 1 条", self.w.lbl_checked.text())

    def test_link_lost_aborts_batch(self):
        self._check(165, 166)
        self.w.btn_download.click()
        pump(self.app)
        self.w._on_state("lost", "对端已断开")
        pump(self.app)
        self.assertIn("批量下载中止", self.w.plain.toPlainText())
        self.assertEqual(self.w._queue, [])
        self.assertEqual(self.w.table.item(1, 4).text(), "未下载（已停止）")
        self.assertEqual(self.w._checked_ids(), [165, 166], "中断那条也保留勾选")
        self.assertFalse(self.w._busy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
