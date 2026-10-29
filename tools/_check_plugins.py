"""一次性核查：把示例插件放到外部目录，用真日志跑一遍七类挂载点并出截图。

用法（仓库根目录）：
    QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe tools/_check_plugins.py <日志.bin>
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets                                    # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

# 外部插件目录：拷一份示例插件，再加一个必炸的坏插件（验证隔离）
tmp = Path(tempfile.mkdtemp(prefix="apmlog_pcheck_"))
plug = tmp / "plugins"
plug.mkdir()
(plug / "example_plugin.py").write_text(
    (ROOT / "plugins-samples" / "example_plugin.py").read_text(encoding="utf-8"), encoding="utf-8")
(plug / "broken.py").write_text("raise RuntimeError('故意炸的坏插件')\n", encoding="utf-8")
os.environ["APMLOG_PLUGIN_DIRS"] = str(plug)

from apmlog.ui import settings as st                             # noqa: E402
st.Settings.set_instance(st.Settings(tmp / "settings.json"))
from apmlog.ui.settings import Settings                          # noqa: E402
Settings.instance().set("plugin_dirs", "[]")

import apmlog.app as appmod                                      # noqa: E402
appmod.MainWindow._views_path = lambda self: tmp / "views.json"   # 别污染全局 views.json

win = appmod.MainWindow()
win.resize(1500, 900)
win.show()
for _ in range(8):
    app.processEvents()

print("=== 插件加载 ===")
for info in win.plugins.plugins():
    print(f"  {info.id:16s} {info.status:9s} {info.name} 提供={info.provides}")

log_path = sys.argv[1] if len(sys.argv) > 1 else ""
if log_path:
    win.load_sync(log_path)
    for _ in range(10):
        app.processEvents()

    print("=== 合成通道 ===")
    for _pid, syn in win.plugins.synthetics():
        data = win.plugins.synthetic_series(syn)
        n = 0 if data is None else data.n
        print(f"  {syn.key} label={syn.display()} unit={syn.unit} 点数={n}")

    print("=== 告警（含插件） ===")
    for a in win._alerts:
        print(f"  [{a.severity:5s}] {a.source:14s} {a.title} t={a.t0:.1f}")
    rule_hits = win.plugins.run_alert_rules()
    print(f"  插件规则直接跑：{len(rule_hits)} 条" +
          (f"（{rule_hits[0].title} {rule_hits[0].value:.0f}{rule_hits[0].unit}）"
           if rule_hits else "（峰值未到示例阈值 5000 J）"))
    try:
        data = win.plugins.synthetic_series(win.plugins.synthetic_for("SYN.KE"))
        if data is not None and data.n:
            print(f"  动能峰值 = {float(__import__('numpy').nanmax(data.v)):.1f} J")
    except Exception as exc:                                     # noqa: BLE001
        print("  动能峰值取不到:", exc)

    print("=== 地图图层 ===")
    win._refresh_map_layers()
    for lay in win.map.plugin_layers():
        print(f"  {lay['name']} 图元={[p['kind'] for p in lay['prims']]}")

    print("=== 预设 / 标签页 / 曲线工具 ===")
    print("  预设:", [win.preset_box.itemData(i) for i in range(win.preset_box.count())])
    print("  标签页:", [win.tabs.tabText(i) for i in range(win.tabs.count())])
    print("  工具:", win.plot.plot_tools())

    print("=== 通道树（插件组） ===")
    tree = win.channel_tree.tree
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        if top.text(0) == "插件通道":
            print("  插件通道:", [top.child(j).text(0) for j in range(top.childCount())])

    print("=== 导出器 ===")
    for pid, name, fn in win.plugins.exporters():
        out = tmp / "summary.txt"
        ok, msg = win.plugins.run_exporter(pid, name, fn, str(out))
        print(f"  {name}: {ok} {msg}")
        if out.exists():
            print("   内容首行:", out.read_text(encoding="utf-8").splitlines()[0])

# 切到插件标签页 + 插件对话框，各出一张图
out_dir = ROOT / "selftest"
out_dir.mkdir(exist_ok=True)
for i in range(win.tabs.count()):
    if win.tabs.tabText(i) == "示例插件":
        win.tabs.setCurrentIndex(i)
for _ in range(6):
    app.processEvents()
if log_path:
    win.grab().save(str(out_dir / "plugins_tab.png"))

from apmlog.ui.plugindialog import PluginDialog                  # noqa: E402
dlg = PluginDialog(win.plugins, win)
dlg.show()
for _ in range(8):
    app.processEvents()
dlg.grab().save(str(out_dir / "plugins_dialog.png"))
print("截图: selftest/plugins_tab.png, selftest/plugins_dialog.png")

dlg.close()
win.close()
print("OK")
