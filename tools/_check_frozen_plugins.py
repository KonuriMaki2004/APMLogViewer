"""核查冻结包：插件模块是否进包、常用科学库预留是否生效、exe 能否自检。

用法：
    .venv/Scripts/python.exe tools/_check_frozen_plugins.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "APMLogViewer" / "APMLogViewer.exe"
INTERNAL = EXE.parent / "_internal"
if not EXE.exists():
    raise SystemExit(f"未找到 {EXE}，先 build")

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader   # noqa: E402
import tempfile                                                             # noqa: E402

car = CArchiveReader(str(EXE))
pyz_name = [n for n in car.toc if n.endswith(".pyz")][0]
tmp = Path(tempfile.mkdtemp()) / "a.pyz"
tmp.write_bytes(car.extract(pyz_name))
z = ZlibArchiveReader(str(tmp))


def names_of(code, acc):
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            names_of(c, acc)
    acc.update(code.co_names)
    acc.update(code.co_varnames)
    for c in code.co_consts:
        if isinstance(c, str):
            acc.add(c)
    return acc


def syms(mod: str) -> set[str]:
    code = z.extract(mod)
    return names_of(code, set()) if isinstance(code, types.CodeType) else set()


print("=== 插件模块是否进包 ===")
for mod in ("apmlog.plugins", "apmlog.plugins.api", "apmlog.plugins.manager",
            "apmlog.ui.plugindialog"):
    print(f"  {mod:28s} {'在' if mod in z.toc else '缺'}")

print("=== 关键符号 ===")
checks = {
    "apmlog.plugins.manager": ["PluginManager", "ChannelSource", "guard", "_load_candidate",
                               "map_layer_primitives", "synthetic_series", "FAILED"],
    "apmlog.plugins.api": ["StoreView", "ControlPort", "PluginSignals", "PlotSelection",
                           "MapLayerPlugin", "SyntheticChannel", "frozen_array"],
    "apmlog.ui.plugindialog": ["PluginDialog"],
    "apmlog.app": ["_open_plugins", "act_plugins", "_sync_plugin_ui", "_refresh_preset_box",
                   "plugin_seek", "_on_plot_tool", "_refresh_channel_tree_plugins"],
    "apmlog.ui.seriesplot": ["plotToolRequested", "set_plot_tools"],
    "apmlog.ui.trackmap": ["set_plugin_layers", "_draw_plugin_layers", "_PluginDot"],
}
for mod, want in checks.items():
    acc = syms(mod)
    missing = [s for s in want if s not in acc]
    # 方法名不一定出现在名字表里（类属性），放宽：只要模块能取到 code 且大部分命中
    print(f"  {mod:28s} {'齐全' if not missing else '缺 ' + str(missing)}")

print("=== 科学库预留 ===")
for lib in ("scipy", "pandas", "matplotlib", "PIL", "sklearn", "numba", "h5py", "openpyxl"):
    top = lib.split(".")[0]
    print(f"  {lib:12s} {'在包内' if top in z.toc else '未装→未打包（安装后重打包即可）'}")
print("  duckdb      " + ("在包内（不该）" if "duckdb" in z.toc else "按设计不打包"))

print(f"=== 包内模块总数: {len(z.toc)} ===")
print(f"=== _internal 大小: {sum(f.stat().st_size for f in INTERNAL.rglob('*') if f.is_file())/1048576:.1f} MB ===")
