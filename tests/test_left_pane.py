# -*- coding: utf-8 -*-
"""tests/test_left_pane.py —— 左侧栏不能被右侧内容"钉住"。

用户 2026-10-29：装了第三方插件之后左侧栏被挤扁，而且怎么往回拉都拉不大。

根因是 QSplitter 按两侧的 `minimumSizeHint` 分配宽度，而右侧"标签区"的最小宽会被
页里的内容顶起来：

1. 插件页（第三方控件常带很大的 minimumWidth）→ 页面直接 addTab 就把它算进标签区；
2. 地图页底部的状态行（QLabel 的文字宽度就是它的最小宽）——日志一长、瓦片失败的
   提示一多，这一行能顶到 1200 像素。

这里把两个机制各自钉上加锁，再验一遍"分隔条能把左侧拉大"。
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

from apmlog.ui.trackmap import TrackMapWidget                     # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(times: int = 10) -> None:
    app = qapp()
    for _ in range(times):
        app.processEvents()


LONG_STATUS = ("esri imagery (WGS84) ｜ z=18.2 · 0.41 m/px ｜ 光标 38.962713, 117.319340 ｜ "
               "游标 t=415.5s 高度 0.0 m 速度 - ｜ 瓦片 内存44 磁盘44 下载0 失败12 "
               "｜ ⚠ 12 张失败，已用占位显示 ｜ 数据源回退：离线缓存")


class TestMapStatusLabel(unittest.TestCase):
    """地图底部的状态行不该把地图页的最小宽顶起来。"""

    def setUp(self) -> None:
        qapp()
        self.map = TrackMapWidget(cache_dir=Path(tempfile.mkdtemp()) / "tiles")

    def tearDown(self) -> None:
        self.map.close()
        self.map.deleteLater()
        pump(3)

    def test_status_line_has_no_width_demand(self) -> None:
        pol = self.map.status.sizePolicy()
        self.assertEqual(pol.horizontalPolicy(), QtWidgets.QSizePolicy.Policy.Ignored,
                         "状态行必须放弃横向尺寸诉求，否则长文字会顶住左侧栏")

    def test_long_status_text_does_not_grow_minimum_width(self) -> None:
        self.map.status.setText("短")
        pump(4)
        base = self.map.minimumSizeHint().width()
        self.map.status.setText(LONG_STATUS)
        pump(4)
        after = self.map.minimumSizeHint().width()
        self.assertLessEqual(after, base + 20,
                             f"状态行文字变长后地图页最小宽涨到 {after}（原 {base}）")

    def test_status_still_shows_text(self) -> None:
        self.map.status.setText(LONG_STATUS)
        self.assertIn("失败12", self.map.status.text())


class TestMainWindowSplitter(unittest.TestCase):
    """整窗：插件页 + 超长状态行都在时，左侧栏仍能拉大。"""

    def setUp(self) -> None:
        qapp()
        self.tmp = Path(tempfile.mkdtemp(prefix="apmlog_leftpane_"))
        from apmlog.ui import settings as st
        self.settings = st.Settings(self.tmp / "settings.json")
        st.Settings.set_instance(self.settings)
        self._p = mock.patch("apmlog.app.MainWindow._views_path",
                             lambda self_: self.tmp / "views.json")
        self._p.start()
        self.addCleanup(self._p.stop)
        from apmlog.app import MainWindow
        self.win = MainWindow()
        self.addCleanup(self.win.close)
        self.win.show()
        self.win.resize(1728, 940)
        pump(10)

    def _split(self) -> QtWidgets.QSplitter:
        return self.win.centralWidget().widget(0)

    def test_plugin_page_and_long_status_do_not_pin_the_splitter(self) -> None:
        # 一个"很像第三方插件"的页：要求 1400 像素最小宽
        wide = QtWidgets.QLabel("插件页")
        wide.setMinimumWidth(1400)
        self.win.plugins.add_tab("leftpane_probe", "宽插件页", wide)   # type: ignore[attr-defined]
        self.win._sync_plugin_ui()
        self.win.map.status.setText(LONG_STATUS)
        pump(10)
        self.assertLess(self.win.tabs.minimumSizeHint().width(), 1100,
                        "标签区最小宽被撑爆了，左侧栏会被压扁")
        split = self._split()
        split.setSizes([700, 800])
        pump(8)
        self.assertGreaterEqual(split.sizes()[0], 650,
                                f"左侧栏拉不回来：{split.sizes()}")

    def test_left_pane_keeps_usable_minimum(self) -> None:
        split = self._split()
        split.setSizes([10, 1500])
        pump(8)
        self.assertGreaterEqual(split.sizes()[0], 80, "左侧栏不该被压成一条缝")


if __name__ == "__main__":
    unittest.main()
