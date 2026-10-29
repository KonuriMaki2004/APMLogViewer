"""一次性核查：主题插件（注册 → 设置里可选 → 应用 → 停用后回退）并出截图。

用法（仓库根目录）：
    .venv/Scripts/python.exe tools/_check_theme_plugin.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6 import QtGui, QtWidgets                                    # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

tmp = Path(tempfile.mkdtemp(prefix="apmlog_themecheck_"))
plug = tmp / "plugins"
plug.mkdir()
(plug / "theme_plugin.py").write_text(
    (ROOT / "plugins-samples" / "theme_plugin.py").read_text(encoding="utf-8"), encoding="utf-8")
(plug / "broken_theme.py").write_text(
    'PLUGIN_NAME = "坏主题"\ndef register(ctx):\n'
    '    ctx.register_theme({"id": "bad", "name": "缺色", "colors": {"window": "#000000"}})\n',
    encoding="utf-8")
os.environ["APMLOG_PLUGIN_DIRS"] = str(plug)

from apmlog.ui import settings as st                                    # noqa: E402
st.Settings.set_instance(st.Settings(tmp / "settings.json"))
from apmlog.ui.settings import Settings                                 # noqa: E402
Settings.instance().set("plugin_dirs", "[]")

import apmlog.app as appmod                                             # noqa: E402
appmod.MainWindow._views_path = lambda self: tmp / "views.json"

win = appmod.MainWindow()
win.resize(1500, 900)
win.show()
for _ in range(8):
    app.processEvents()


def wcolor() -> str:
    return app.palette().color(QtGui.QPalette.ColorRole.Window).name()


print("=== 插件状态 ===")
for info in win.plugins.plugins():
    print(f"  {info.id:16s} {info.status:9s} 提供={info.provides} 错误={info.error[:60]}")

print("=== 注册到的主题 ===")
for t in win.plugins.themes_available():
    print(f"  {t['value']:28s} {t['name']:6s} curve={t['curve'] or '自动'} {t['colors']}")

from apmlog.ui.settings import SettingsDialog                            # noqa: E402
dlg = SettingsDialog(win, themes_provider=win.plugins.themes_available)
dlg.show()
for _ in range(6):
    app.processEvents()
values = [dlg.cb_ui_theme.itemData(i) for i in range(dlg.cb_ui_theme.count())]
texts = [dlg.cb_ui_theme.itemText(i) for i in range(dlg.cb_ui_theme.count())]
print("=== 设置下拉 ===")
print("  ", list(zip(texts, values)))
dlg.grab().save(str(ROOT / "selftest" / "theme_combo.png"))

print("=== 应用「午夜蓝」 ===")
before = wcolor()
dlg.cb_ui_theme.setCurrentIndex(dlg.cb_ui_theme.findData("plugin:theme_plugin:midnight"))
for _ in range(8):
    app.processEvents()
print(f"  窗口底色 {before} → {wcolor()}｜ui_theme={Settings.instance().get('ui_theme')}"
      f"｜曲线={Settings.instance().get('theme')}")
win.grab().save(str(ROOT / "selftest" / "theme_midnight.png"))
dlg.grab().save(str(ROOT / "selftest" / "theme_combo_applied.png"))

from apmlog.ui.plugindialog import PluginDialog                          # noqa: E402
pd = PluginDialog(win.plugins, win)
pd.show()
for _ in range(6):
    app.processEvents()
pd.grab().save(str(ROOT / "selftest" / "theme_plugin_dialog.png"))

print("=== 停用主题插件 → 自动回退 ===")
win.plugins.disable("theme_plugin")
for _ in range(8):
    app.processEvents()
print(f"  窗口底色 {wcolor()}｜ui_theme={Settings.instance().get('ui_theme')}")
print("  状态栏:", win.statusBar().currentMessage())

pd.close()
dlg.close()
win.close()
print("OK")
