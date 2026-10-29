# -*- coding: utf-8 -*-
"""核查：装了带大 minimumWidth 的第三方插件后，左侧栏还能自由拖动。

两段：
1. 启动就带插件（真实目录 `%LOCALAPPDATA%\\APMLogViewer\\plugins`）；
2. 启动时没有插件，运行中才把插件放进去（用户的实际操作路径）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets                                          # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

tmp = Path(tempfile.mkdtemp(prefix="apmlog_squeeze_check_"))
from apmlog.ui import settings as st                                    # noqa: E402
st.Settings.set_instance(st.Settings(tmp / "settings.json"))
import apmlog.app as appmod                                             # noqa: E402
appmod.MainWindow._views_path = lambda self: tmp / "views.json"

REAL_PLUGINS = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) \
    / "APMLogViewer" / "plugins"
LOG = r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000165.BIN"


def pump(n: int = 8) -> None:
    for _ in range(n):
        app.processEvents()


def detail(win, tag: str) -> None:
    split = win.centralWidget().widget(0)
    print(f"[{tag}] 左侧宽 {split.sizes()[0]} ｜标签区最小宽 {win.tabs.minimumSizeHint().width()}"
          f" ｜分隔条最小宽 {split.minimumSizeHint().width()}")
    for i in range(win.tabs.count()):
        w = win.tabs.widget(i)
        if isinstance(w, QtWidgets.QScrollArea) and w.widget() is not None:
            print(f"    插件页 {win.tabs.tabText(i)!r} 宿主最小宽 {w.minimumSizeHint().width()}"
                  f"｜插件控件最小宽 {w.widget().minimumSizeHint().width()}")


# ---------------- 1) 启动即带插件 ----------------
win = appmod.MainWindow()
win.show()
win.resize(1728, 960)
pump(10)
if Path(LOG).exists():
    win.load_sync(LOG)
    pump(10)
detail(win, "启动带插件")

split = win.centralWidget().widget(0)
split.setSizes([620, 900])
pump(6)
print(f"拖动到 620 后: {split.sizes()}（修之前只能停在 ~471）")
# 切到插件页，看窄了以后是插件内部出滚动条，而不是把左侧栏吃掉
for i in range(win.tabs.count()):
    if win.tabs.tabText(i) == "AI识别":
        win.tabs.setCurrentIndex(i)
        break
pump(8)
host = win.tabs.currentWidget()
if isinstance(host, QtWidgets.QScrollArea):
    print(f"插件页当前宽 {host.width()} ｜视口 {host.viewport().width()}"
          f" ｜横向滚动条 {'有' if host.horizontalScrollBar().isVisible() else '无'}"
          f" ｜内容宽 {host.widget().width()}")
win.grab().save(str(ROOT / "selftest" / "fix_left_pane_wide.png"))
split.setSizes([300, 1200])
pump(6)
win.grab().save(str(ROOT / "selftest" / "fix_left_pane_narrow.png"))
print(f"再拉回 300 后: {split.sizes()}")
win.close()

# ---------------- 2) 运行中才装入插件 ----------------
if REAL_PLUGINS.is_dir():
    plug_dir = tmp / "plugins"
    plug_dir.mkdir()
    os.environ["APMLOG_PLUGIN_DIRS"] = str(plug_dir)      # 管理器从环境变量发现目录
    appmod.MainWindow._views_path = lambda self: tmp / "views2.json"
    win2 = appmod.MainWindow()
    win2.show()
    win2.resize(1728, 960)
    pump(10)
    print(f"  管理器目录: {[str(p) for p in win2.plugins.dirs()]}")
    detail(win2, "运行前（无插件）")
    for f in REAL_PLUGINS.glob("*.py"):
        shutil.copy2(f, plug_dir / f.name)
    win2.plugins.reload()
    pump(12)
    detail(win2, "运行中装入插件后")
    sp2 = win2.centralWidget().widget(0)
    sp2.setSizes([600, 900])
    pump(6)
    print(f"运行中装插件后拖动到 600: {sp2.sizes()}")
    win2.grab().save(str(ROOT / "selftest" / "fix_left_pane_runtime.png"))
    win2.close()

print("OK")
