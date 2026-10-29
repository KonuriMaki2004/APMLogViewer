# -*- coding: utf-8 -*-
"""tests/test_scan_dialog.py —— 目录扫描的后台线程、进度框与取消语义。

要守的原始 bug：``_LogListDock.scan()`` 在 UI 线程里 ``rglob("*")`` + 逐个 ``stat()``，
用户把整个磁盘当目录选进来就"未响应"。所以这里测三件事：

1. 扫描**不在主线程跑**（主线程在扫描期间仍能处理事件）；
2. 进度框有百分比、有进度条、取消按钮在进度条下方，取消后**已扫到的照常列出**；
3. 扫完能看到"日志数量 + 各类型数量"。

协议层的真实遍历行为在 ``tests/test_scan.py``（纯内核）里测，这里用假内核做
**确定性**的时序测试：真文件的 stat 太快，靠它制造"扫到一半取消"会飘。
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets                                  # noqa: E402

from apmlog.scan import LogFile, ScanResult                            # noqa: E402
from apmlog.ui import scandialog as SD                                 # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 20) -> None:
    for _ in range(times):
        app.processEvents()


def wait_for(app: QtWidgets.QApplication, pred, secs: float = 8.0) -> bool:
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    return False


def mkfile(i: int, ext: str = ".bin", kind: str = "DataFlash 日志") -> LogFile:
    return LogFile(path=f"/tmp/x{i}{ext}", name=f"x{i}{ext}", size=1024,
                   mtime=1000.0 - i, ext=ext, kind=kind)


# --------------------------------------------------------------- 假内核（确定性）
def fake_scan(root, *, progress=None, should_cancel=None, **limits):
    """两阶段假扫描：enumerate 50 个目录 → stat N 个候选，每步查取消。

    故意在 stat 阶段放慢，让测试能稳定地在"扫到一半"时取消。
    """
    total_files = int(limits.get("fake_files", 40))
    cancelled = False
    for i in range(1, 51):
        if progress:
            progress("enumerate", i, None, i)
        if should_cancel and should_cancel():
            return _res(root, (), cancelled=True, dirs=i, entries=i * 3)
    cands = [mkfile(i) for i in range(total_files)]
    if progress:
        progress("stat", 0, len(cands), len(cands))
    got = []
    for i, f in enumerate(cands, start=1):
        got.append(f)
        if progress:
            progress("stat", i, len(cands), len(cands))
        time.sleep(0.004)                       # 让取消有窗口
        if should_cancel and should_cancel():
            cancelled = True
            break
    return _res(root, tuple(got), cancelled=cancelled, dirs=50, entries=150)


def _res(root, files, *, cancelled=False, dirs=0, entries=0, truncated=False,
         reason="") -> ScanResult:
    bins = sum(1 for f in files if f.ext == ".bin")
    tl = sum(1 for f in files if f.ext == ".tlog")
    kinds = []
    if bins:
        kinds.append(("DataFlash 日志", bins))
    if tl:
        kinds.append(("遥测日志", tl))
    return ScanResult(root=str(root), files=tuple(files), by_kind=tuple(kinds),
                      total=len(files), supported=bins, dirs_scanned=dirs,
                      entries_scanned=entries, cancelled=cancelled,
                      truncated=truncated, truncated_reason=reason)


FAKE_TLOG = ScanResult(
    root="C:/logs", files=(mkfile(1), mkfile(2, ".tlog", "遥测日志")),
    by_kind=(("DataFlash 日志", 1), ("遥测日志", 1)), total=2, supported=1,
    dirs_scanned=7, entries_scanned=33)


# ------------------------------------------------------------------ 进度框本体
class TestProgressDialogWidgets(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _mk(self) -> SD.ScanProgressDialog:
        d = SD.ScanProgressDialog("C:/日志目录/子目录")
        d.show()
        pump(self.app)
        self.addCleanup(d.deleteLater)
        return d

    def test_title_says_scanning(self) -> None:
        d = self._mk()
        labels = [l.text() for l in d.findChildren(QtWidgets.QLabel)]
        self.assertIn("正在扫描日志", labels)

    def test_progress_bar_sits_above_the_cancel_button(self) -> None:
        """用户点名要"进度条下方一个取消按钮"，位置关系要能被几何验证。"""
        d = self._mk()
        self.assertTrue(d.btn_cancel.isVisible())
        self.assertGreater(d.btn_cancel.mapTo(d, QtCore.QPoint(0, d.btn_cancel.height())).y(),
                           d.bar.mapTo(d, QtCore.QPoint(0, d.bar.height())).y(),
                           "取消按钮必须在进度条下方")

    def test_starts_in_busy_mode_without_fake_percentage(self) -> None:
        d = self._mk()
        self.assertEqual(d.bar.minimum(), 0)
        self.assertEqual(d.bar.maximum(), 0, "总数未知时不能装作有百分比")

    def test_enumerate_phase_reports_dirs_and_found(self) -> None:
        d = self._mk()
        d.on_progress("enumerate", 12, None, 5)
        self.assertIn("12", d.phase.text())
        self.assertIn("5", d.phase.text())
        self.assertEqual(d.bar.maximum(), 0)

    def test_stat_phase_shows_a_real_percentage(self) -> None:
        d = self._mk()
        d.on_progress("stat", 0, 200, 200)
        self.assertEqual(d.bar.maximum(), 200)
        d.on_progress("stat", 50, 200, 200)
        self.assertEqual(d.bar.value(), 50)
        self.assertIn("25%", d.phase.text())
        d.on_progress("stat", 200, 200, 200)
        self.assertIn("100%", d.phase.text())

    def test_zero_candidates_does_not_divide_by_zero(self) -> None:
        d = self._mk()
        d.on_progress("stat", 0, 0, 0)
        self.assertEqual(d.bar.maximum(), 0)

    def test_cancel_emits_once_and_locks_the_button(self) -> None:
        d = self._mk()
        got: list[int] = []
        d.cancelRequested.connect(lambda: got.append(1))
        d.btn_cancel.click()
        d.btn_cancel.click()
        self.assertEqual(sum(got), 1, "重复点取消不该发第二遍")
        self.assertFalse(d.btn_cancel.isEnabled())
        self.assertIn("取消", d.btn_cancel.text())

    def test_finished_shows_counts_and_hides_cancel(self) -> None:
        d = self._mk()
        d.on_progress("stat", 0, 2, 2)
        d.on_finished(FAKE_TLOG)
        self.assertFalse(d.btn_cancel.isVisible())
        self.assertTrue(d.btn_done.isVisible())
        self.assertTrue(d.summary.isVisible())
        self.assertEqual(d.bar.value(), d.bar.maximum())
        txt = d.summary.text()
        self.assertIn("共 2 个日志", txt)
        self.assertIn("DataFlash 日志 1", txt)
        self.assertIn("遥测日志 1", txt)
        self.assertIn("可打开 1", txt)

    def test_cancelled_result_is_worded_as_cancelled(self) -> None:
        d = self._mk()
        d.on_finished(_res("C:/x", (mkfile(1),), cancelled=True))
        self.assertIn("已取消", d.summary.text())
        self.assertIn("已取消", d.phase.text())

    def test_truncated_result_says_the_list_is_incomplete(self) -> None:
        d = self._mk()
        d.on_finished(_res("C:/x", (mkfile(1),), truncated=True,
                           reason="候选日志已达 5000 个上限"))
        self.assertIn("不完整", d.summary.text())
        self.assertIn("5000", d.summary.text())

    def test_failure_still_leaves_a_way_out(self) -> None:
        d = self._mk()
        d.on_failed("扫描出错：OSError: 盘没了")
        self.assertTrue(d.btn_done.isVisible())
        self.assertIn("盘没了", d.summary.text())

    def test_esc_means_cancel_not_silent_abort(self) -> None:
        d = self._mk()
        got: list[int] = []
        d.cancelRequested.connect(lambda: got.append(1))
        d.keyPressEvent(esc_event())
        self.assertEqual(sum(got), 1)

    def test_esc_after_finish_just_closes(self) -> None:
        d = self._mk()
        got: list[int] = []
        d.cancelRequested.connect(lambda: got.append(1))
        d.on_finished(FAKE_TLOG)
        d.keyPressEvent(esc_event())
        self.assertEqual(got, [], "已经扫完了，Esc 不该再被当成取消")


def esc_event():
    return QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                           QtCore.Qt.Key.Key_Escape,
                           QtCore.Qt.KeyboardModifier.NoModifier)


# ------------------------------------------------------------------ 线程与接线
class TestControllerThreading(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        # 用假内核做确定性时序：真文件的 stat 太快，靠它制造“扫到一半”会飘
        self.patcher = mock.patch("apmlog.scan.scan_logs", fake_scan)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        # start() 会先校验目录存在，所以根路径得用真存在的临时目录
        import tempfile
        self.root = tempfile.mkdtemp(prefix="apmlog_scan_ctrl_")
        self.addCleanup(_rmtree, self.root)

    def _ctrl(self, **lim):
        c = SD.ScanController(limits=lim)
        self.addCleanup(c.shutdown)
        return c

    def test_main_thread_is_free_while_scanning(self) -> None:
        """扫描期间主线程必须还能转事件循环——这就是“未响应”的反面。"""
        c = self._ctrl(fake_files=40)
        results: list = []
        c.done.connect(results.append)
        c.start(self.root, modal=False)
        ticks = 0
        end = time.time() + 4.0
        while time.time() < end and not results:
            self.app.processEvents()
            ticks += 1
            time.sleep(0.002)
        self.assertTrue(results, "扫描没完成")
        self.assertGreater(ticks, 5, "主线程在扫描期间几乎没转，等于卡住")

    def test_progress_percentage_reaches_100(self) -> None:
        c = self._ctrl(fake_files=6)
        seen: list = []
        c.notice.connect(seen.append)
        out: list = []
        c.done.connect(out.append)
        c.start(self.root, modal=False)
        self.assertTrue(wait_for(self.app, lambda: out), "扫描没结束")
        self.assertTrue(any("%" in s for s in seen), f"没出现过百分比：{seen}")

    def test_cancel_keeps_what_was_already_found(self) -> None:
        c = self._ctrl(fake_files=400)
        out: list = []
        c.done.connect(out.append)
        c.start(self.root, modal=False)
        # 等到 stat 阶段真的开始（有百分比）再取消，保证是"扫到一半"
        self.assertTrue(wait_for(self.app, lambda: c.running()))
        time.sleep(0.08)
        c.cancel()
        self.assertTrue(wait_for(self.app, lambda: out), "取消后没有收尾")
        res = out[0]
        self.assertTrue(res.cancelled, "必须如实标记为已取消")
        self.assertGreater(res.total, 0, "取消时已扫到的日志不能丢")
        self.assertLess(res.total, 400, "根本没取消成功（全扫完了）")

    def test_only_one_scan_at_a_time(self) -> None:
        c = self._ctrl(fake_files=200)
        c.start(self.root, modal=False)
        self.assertTrue(c.running())
        self.assertFalse(c.start(self.root, modal=False), "第二次扫描必须被拒")
        out: list = []
        c.done.connect(out.append)
        c.cancel()
        wait_for(self.app, lambda: out)

    def test_invalid_directory_never_starts_a_thread(self) -> None:
        c = self._ctrl()
        notes: list = []
        c.notice.connect(notes.append)
        self.assertFalse(c.start("", modal=False))
        self.assertFalse(c.start("C:/肯定/不存在/的/目录/xyz", modal=False))
        self.assertFalse(c.running())
        self.assertEqual(len(notes), 2)
        self.assertTrue(any("不存在" in n for n in notes))

    def test_busy_signal_flips(self) -> None:
        c = self._ctrl(fake_files=3)
        busy: list = []
        c.busyChanged.connect(busy.append)
        out: list = []
        c.done.connect(out.append)
        c.start(self.root, modal=False)
        self.assertTrue(wait_for(self.app, lambda: out))
        self.assertEqual(busy, [True, False])

    def test_kernel_exception_becomes_a_message_not_a_hang(self) -> None:
        self.patcher.stop()                        # 换掉本类默认的假内核

        def boom(root, **kw):
            raise RuntimeError("内核炸了")
        with mock.patch("apmlog.scan.scan_logs", boom):
            c = self._ctrl()
            out: list = []
            c.done.connect(out.append)
            c.start(self.root, modal=False)
            self.assertTrue(wait_for(self.app, lambda: out), "异常后线程没收尾")
            self.assertIsNone(out[0])
            self.assertFalse(c.running())


def _rmtree(p: str) -> None:
    import shutil
    shutil.rmtree(p, ignore_errors=True)


# ------------------------------------------------------------------ 真内核集成
class TestRealKernelIntegration(unittest.TestCase):
    """不打桩：真线程 + 真 apmlog.scan，跑一个临时目录树。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        import tempfile
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_scan_ui_")
        self.root = Path(self._dir.name)
        self.addCleanup(self._dir.cleanup)
        sub = self.root / "2026" / "QUAD"
        sub.mkdir(parents=True)
        (self.root / "$Recycle.Bin").mkdir()
        now = time.time()
        for i, (name, ext) in enumerate([("a.BIN", ".BIN"), ("b.bin", ".bin"),
                                         ("c.tlog", ".tlog"), ("d.rlog", ".rlog"),
                                         ("e.txt", ".txt")]):
            f = (sub / name) if not name.startswith("e") else (self.root / name)
            f.write_bytes(b"x" * (100 + i))
            os.utime(f, (now - i * 100, now - i * 100))
        (self.root / "$Recycle.Bin" / "junk.bin").write_bytes(b"x")

    def test_finds_supported_and_other_kinds_and_skips_junk(self) -> None:
        from apmlog.scan import scan_logs
        r = scan_logs(str(self.root))
        self.assertEqual(r.supported, 2, "两个 .BIN 可打开")
        self.assertEqual(r.total, 4, "tlog/rlog 也要计入类型统计")
        names = {f.name for f in r.files}
        self.assertNotIn("junk.bin", names, "$Recycle.Bin 必须跳过")
        self.assertNotIn("e.txt", names, ".txt 不是日志")
        self.assertEqual([f.name for f in r.files][0], "a.BIN", "新的在前")

    def test_dock_lists_and_summarises(self) -> None:
        from apmlog.app import _LogListDock
        dock = _LogListDock()
        self.addCleanup(dock._scanner.shutdown)
        dock._scanner._limits["max_files"] = 100
        out: list = []
        dock.refresh_requested.connect(out.append)
        self.assertTrue(dock.scan(str(self.root), modal=False))
        self.assertTrue(wait_for(self.app, lambda: dock.list.count() > 0),
                        "扫描完成后列表还是空的")
        wait_for(self.app, lambda: not dock._scanner.running())
        texts = [dock.list.item(i).text() for i in range(dock.list.count())]
        self.assertTrue(any(t.startswith("a.BIN") for t in texts), texts)
        self.assertIn("共 4 个日志", dock.lbl_sum.text())
        self.assertIn("DataFlash 日志 2", dock.lbl_sum.text())
        self.assertIn("遥测日志 1", dock.lbl_sum.text())
        # 不支持的类型置灰且双击无效
        idx = next(i for i in range(dock.list.count())
                   if dock.list.item(i).text().startswith("c.tlog"))
        item = dock.list.item(idx)
        self.assertFalse(item.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled,
                         "不支持的类型要置灰")
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsEnabled)   # 强行解禁
        dock._activated(item)
        self.assertEqual(out, [], "不支持的日志不该触发加载")
        dock._activated(dock.list.item(0))
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
