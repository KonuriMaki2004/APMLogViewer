"""读取遮罩（正在读取日志 + 百分比进度条）的行为测试。

全部在 offscreen 平台跑，不依赖真机日志。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtCore, QtGui, QtWidgets                       # noqa: E402

from apmlog.ui import theme                                        # noqa: E402
from apmlog.ui.loading import (BAR_MIN_WIDTH, BAR_WIDTH_RATIO, TITLE,
                                LoadingOverlay)


def qapp() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(app, ms: int) -> None:
    """真实推进时间，让 QTimer 到期。"""
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def _tabs(app) -> QtWidgets.QTabWidget:
    t = QtWidgets.QTabWidget()
    t.addTab(QtWidgets.QLabel("页1"), "轨迹地图")
    t.addTab(QtWidgets.QLabel("页2"), "曲线")
    t.resize(900, 600)
    t.show()
    pump(app, 30)
    return t


class TestOverlayBasics(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self.tabs = _tabs(self.app)
        self.ov = LoadingOverlay(self.tabs, show_delay_ms=60)

    def tearDown(self) -> None:
        self.ov.end()
        self.tabs.close()
        self.tabs.deleteLater()
        pump(self.app, 20)

    def test_hidden_until_delay_elapses(self) -> None:
        self.assertFalse(self.ov.isVisible(), "begin 之前不许显示")
        self.ov.begin("a.BIN")
        self.assertFalse(self.ov.isVisible(), "延迟显示：快加载不该白闪一下")
        pump(self.app, 120)
        self.assertTrue(self.ov.isVisible(), "超过显示延迟才出现")

    def test_title_is_the_requested_wording(self) -> None:
        self.ov.begin("x.BIN")
        pump(self.app, 120)
        self.assertEqual(self.ov.lbl_title.text(), TITLE)
        self.assertEqual(TITLE, "正在读取日志", "用户指定的文案")

    def test_progress_follows_percentage(self) -> None:
        self.ov.begin("x.BIN")
        for fr, want in ((0.0, 0), (0.251, 25), (0.5, 50), (0.97, 97), (1.0, 100)):
            self.ov.set_progress(fr, f"解析中 {int(fr*100)}%")
            self.assertEqual(self.ov.bar.value(), want)
        self.assertEqual(self.ov.bar.minimum(), 0)
        self.assertEqual(self.ov.bar.maximum(), 100)

    def test_weird_fractions_are_clamped_not_crashing(self) -> None:
        self.ov.begin("x.BIN")
        for fr in (-5.0, 3.0, float("nan"), "0.5", None, float("inf")):
            self.ov.set_progress(fr, "")              # type: ignore[arg-type]
        self.assertIn(self.ov.bar.value(), (0, 50, 100))

    def test_end_hides_and_resets(self) -> None:
        self.ov.begin("x.BIN")
        pump(self.app, 120)
        self.ov.set_progress(0.6, "解析 MSG")
        self.ov.end()
        self.assertFalse(self.ov.isVisible(), "解析完毕必须恢复正常显示")
        self.assertEqual(self.ov.bar.value(), 0)
        self.assertFalse(self.ov.active)

    def test_end_is_idempotent_without_begin(self) -> None:
        self.ov.end()                                  # 没 begin 也不能炸
        self.ov.set_progress(0.5, "无")                # 未激活时忽略
        self.assertEqual(self.ov.bar.value(), 0)

    def test_message_is_selectable_text(self) -> None:
        """解析失败时用户要靠这行字看清路径，必须能选中复制。"""
        flags = self.ov.lbl_phase.textInteractionFlags()
        self.assertTrue(flags & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)


class TestOverlayGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_covers_page_area_but_leaves_tab_bar_visible(self) -> None:
        tabs = _tabs(self.app)
        ov = LoadingOverlay(tabs, show_delay_ms=0)
        ov.begin("x.BIN")
        pump(self.app, 60)
        bar_bottom = tabs.tabBar().geometry().bottom()
        self.assertEqual(ov.geometry().top(), bar_bottom + 1, "标签条要露在外面")
        self.assertEqual(ov.width(), tabs.width())
        self.assertEqual(ov.geometry().bottom(), tabs.height() - 1)
        ov.end(); tabs.close()

    def test_follows_host_resize(self) -> None:
        tabs = _tabs(self.app)
        ov = LoadingOverlay(tabs, show_delay_ms=0)
        ov.begin("x.BIN")
        pump(self.app, 60)
        tabs.resize(640, 420)
        pump(self.app, 60)
        self.assertEqual(ov.width(), 640)
        self.assertEqual(ov.height(), tabs.height() - tabs.tabBar().geometry().bottom() - 1)
        ov.end(); tabs.close()

    def test_bar_is_wide_and_tall_not_a_small_strip(self) -> None:
        """用户要求“做长做大一点”：条要拿内容区大部分宽度，并且够高。"""
        tabs = _tabs(self.app)          # 900 x 600
        ov = LoadingOverlay(tabs, show_delay_ms=0)
        ov.begin("x.BIN")
        pump(self.app, 60)
        area = ov.width()
        self.assertGreaterEqual(ov.bar.height(), 30, "条要粗")
        self.assertGreaterEqual(ov.bar.width(), 320, "条不能是个小窄块")
        self.assertGreater(ov.bar.width() / area, 0.5, "要拿内容区一半以上宽")
        self.assertLess(ov.bar.width(), area - 40, "但也不许贴到边")
        ov.end(); tabs.close()

    def test_bar_width_follows_resize(self) -> None:
        tabs = _tabs(self.app)
        ov = LoadingOverlay(tabs, show_delay_ms=0)
        ov.begin("x.BIN")
        pump(self.app, 60)
        w0 = ov.bar.width()
        tabs.resize(1600, 900)
        pump(self.app, 80)
        self.assertGreater(ov.bar.width(), w0 * 1.2, "内容区变宽，条要跟着变长")
        self.assertAlmostEqual(ov.bar.width(), ov.width() * BAR_WIDTH_RATIO, delta=40)
        ov.end(); tabs.close()

    def test_big_centred_percentage_font(self) -> None:
        """百分比写在条内居中，字号太小就白瞎了这根粗条。"""
        tabs = _tabs(self.app)
        ov = LoadingOverlay(tabs, show_delay_ms=0)
        ov.begin("x.BIN")
        pump(self.app, 60)
        self.assertGreaterEqual(ov.bar.font().pointSizeF(), 12.0)
        self.assertGreaterEqual(ov.lbl_title.font().pointSizeF(), 13.0)
        self.assertTrue(ov.bar.font().bold())
        self.assertEqual(ov.bar.format(), "%p%")     # 百分比居中写在条内
        # PySide6 的 QProgressBar 没有 textVisible() getter，只有 setter；走 Qt 属性读。
        self.assertTrue(bool(ov.bar.property("textVisible")))
        ov.end(); tabs.close()

    def test_theme_background_is_opaque(self) -> None:
        """底色必须是主题色且不透明：半透会透出上一份日志的残影。"""
        tabs = _tabs(self.app)
        ov = LoadingOverlay(tabs, show_delay_ms=0)
        ov.begin("x.BIN")
        pump(self.app, 60)
        self.assertTrue(ov.autoFillBackground())
        img = ov.grab().toImage()
        got = img.pixelColor(2, 2)
        want = ov.palette().color(QtGui.QPalette.ColorRole.Window)
        self.assertGreaterEqual(img.pixelColor(2, 2).alpha(), 250, "不能半透")
        self.assertEqual((got.red(), got.green(), got.blue()),
                         (want.red(), want.green(), want.blue()),
                         "遮罩底色就是主题底色，不能自己硬编码一个颜色")
        ov.end(); tabs.close()

    def test_dark_theme_dark_overlay(self) -> None:
        tabs = _tabs(self.app)
        win = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(win)
        lay.addWidget(tabs)
        theme.apply(win, "dark")
        win.show()
        pump(self.app, 40)
        ov = LoadingOverlay(tabs, show_delay_ms=0)
        ov.begin("x.BIN")
        pump(self.app, 60)
        got = ov.grab().toImage().pixelColor(2, 2)
        self.assertLess(got.lightness(), 128, "深色主题下遮罩不该还是白的")
        ov.end(); win.close()


class TestMainWindowWiring(unittest.TestCase):
    """只验接线，不真的解析日志。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.show()
        pump(self.app, 30)

    def tearDown(self) -> None:
        self.w.close()
        pump(self.app, 30)

    def test_overlay_exists_over_the_tabs(self) -> None:
        self.assertIsInstance(self.w.loading, LoadingOverlay)
        self.assertIs(self.w.loading.parent(), self.w.tabs)
        self.assertFalse(self.w.loading.isVisible())

    def test_progress_signal_feeds_bar_and_status(self) -> None:
        self.w.loading.begin("00000165.BIN")
        pump(self.app, 260)
        self.w._on_load_progress("C:/x/00000165.BIN", 0.42, "解析 MSG（42%）")
        self.assertEqual(self.w.loading.bar.value(), 42)
        self.assertIn("42%", self.w.statusBar().currentMessage())

    def test_failed_load_hides_overlay(self) -> None:
        self.w.loading.begin("bad.BIN")
        pump(self.app, 260)
        self.assertTrue(self.w.loading.isVisible())
        with mock.patch.object(QtWidgets.QMessageBox, "exec", lambda self, *a: 0):
            self.w._on_failed("FileNotFoundError: 找不到", "C:/nope/BIN")
        self.assertFalse(self.w.loading.isVisible(), "失败也要收遮罩")

    def test_fast_load_never_flickers(self) -> None:
        """begin 后立刻 end（缓存命中那种）：遮罩全程不得出现。"""
        self.w.loading.begin("hot.BIN")
        self.w._on_load_progress("C:/x/hot.BIN", 1.0, "完成")
        self.w.loading.end()
        pump(self.app, 260)
        self.assertFalse(self.w.loading.isVisible())

    def test_real_slow_load_shows_then_hides(self) -> None:
        """用假 worker 走完整条链路：显示 → 百分比 → 收掉。"""
        from apmlog import app as appmod

        class FakeWorker(QtCore.QThread):
            done = QtCore.Signal(object)
            failed = QtCore.Signal(str)
            progress = QtCore.Signal(float, str)

            def __init__(self, path, force, cache, parent=None):
                super().__init__(parent)
                self.path = path

            def run(self) -> None:
                for fr in (0.1, 0.5, 0.9):
                    self.progress.emit(fr, f"解析（{int(fr*100)}%）")
                    time.sleep(0.12)
                self.failed.emit("故意失败（本条只验遮罩收掉）")

        with mock.patch.object(appmod, "_LoadWorker", FakeWorker):
            self.w.load("C:/fake/x.BIN")
            with mock.patch.object(QtWidgets.QMessageBox, "exec", lambda self, *a: 0):
                t0 = time.time()
                while not self.w.loading.isVisible() and time.time() - t0 < 3:
                    self.app.processEvents()
                    time.sleep(0.005)
                self.assertTrue(self.w.loading.isVisible(), "慢加载要显示遮罩")
                seen = []
                t1 = time.time()
                while self.w.loading.isVisible() and time.time() - t1 < 5:
                    self.app.processEvents()
                    time.sleep(0.005)
                    seen.append(self.w.loading.bar.value())
        self.assertTrue(seen, "过程中要看到百分比在动")
        self.assertFalse(self.w.loading.isVisible(), "结束必须恢复正常显示")


if __name__ == "__main__":
    unittest.main()
