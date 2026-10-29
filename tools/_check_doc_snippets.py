"""核查《插件开发指南.md》里的代码块：能编译、能当插件加载、回调能跑通。

做法：把文档里所有 ```python 块抽出来
1. 逐个 ``compile()``（语法护栏）；
2. 含 ``def register`` 的块当成真插件，单独放进临时目录加载一次；
3. 加载成功后把它的七类挂载点都**真跑一遍**（告警/地图/导出器/曲线工具/主题）。

用法（仓库根目录）：
    .venv/Scripts/python.exe tools/_check_doc_snippets.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                                                    # noqa: E402
from PySide6 import QtWidgets                                          # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from apmlog.core.model import (ChannelInfo, ChannelKey, Envelope, LogStats,  # noqa: E402
                               Series, TrackResult)
from apmlog.plugins import api as papi                                  # noqa: E402
from apmlog.plugins import manager as mgr_mod                           # noqa: E402
from apmlog.plugins.manager import PluginManager                        # noqa: E402
from apmlog.ui import settings as st                                    # noqa: E402


class Store:
    """够文档示例用的假日志：VIBE / GPS / BAT 三组通道 + 参数 + 轨迹。"""

    def __init__(self, root: Path) -> None:
        self.name = "demo.bin"
        self.path = str(root / "demo.bin")
        t = np.linspace(0.0, 300.0, 601)
        self.data = {
            ("VIBE", "VibeX"): (t, 12 + 6 * np.sin(t / 7.0), "m/s²"),
            ("GPS", "Spd"): (t, np.abs(8 * np.sin(t / 40.0)), "m/s"),
            ("BAT", "Volt"): (t, 25.2 - t * 0.004, "V"),
        }
        self.stats = LogStats(path=self.path, duration=300.0, t_start=0.0, t_end=300.0,
                              firmware="Copter 4.7.0", frame_type="QUAD", n_messages=1803,
                              n_params=1130, pos_source="POS", mode_seq=["LOITER"],
                              max_speed=8.0, distance=1200.0)
        self.meta = {"stats": {"t_start": 0.0, "t_end": 300.0}, "types": {}}

    def types(self) -> list[str]:
        return sorted({k[0] for k in self.data})

    def channel_infos(self, prefix=None) -> list[ChannelInfo]:
        out = []
        for (ty, fld), (t, v, unit) in sorted(self.data.items()):
            if prefix and not ty.upper().startswith(str(prefix).upper()):
                continue
            out.append(ChannelInfo(ChannelKey(ty, fld), "f8", int(t.size), float(t[0]),
                                   float(t[-1]), float(np.nanmin(v)), float(np.nanmax(v)),
                                   unit, ""))
        return out

    def channel_range(self, key) -> dict:
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(str(key))
        v = self.data[(k.type, k.field)][1]
        return {"min": float(np.nanmin(v)), "max": float(np.nanmax(v)),
                "mean": float(np.nanmean(v)), "unit": "?"}

    def series(self, key, t0=None, t1=None, max_points=None, drop_nan=False) -> Series:
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(str(key))
        if (k.type, k.field) not in self.data:
            raise KeyError(f"{k.type} 没有字段 {k.field}")
        t, v, unit = self.data[(k.type, k.field)]
        return Series(k, t, v, unit)

    def envelope(self, key, t0=None, t1=None, buckets=2000) -> Envelope:
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(str(key))
        s = self.series(k)
        return Envelope(k, s.t[:10], s.v[:10], s.v[:10], s.v[:10], np.ones(10), s.unit)

    def events(self, kinds=None, severity_min=None, t0=None, t1=None) -> list:
        return []

    def params(self) -> dict:
        return {"BATT_CELLS": (6.0, None), "PLUGIN_MASS_KG": (1.5, None)}

    def param(self, name, default=None):
        return self.params().get(name, (default, None))[0]

    def modes(self) -> list[tuple[float, float, str]]:
        return [(0.0, 300.0, "LOITER")]

    def track(self, key=None) -> TrackResult:
        t = np.linspace(0.0, 300.0, 301)
        return TrackResult(kind="geodetic", t=t, lat=30.0 + t * 1e-5, lon=120.0 + t * 1e-5,
                           alt=50 + 10 * np.sin(t / 20), e=t, n=t, u=t, speed=t / 30,
                           src_names=["POS"], home=(30.0, 120.0, 50.0), key="POS",
                           label="EKF 位置", distance=1200.0, max_speed=8.0)

    def tracks(self) -> list:
        return [self.track()]

    def alerts(self, config_path=None, overrides=None) -> list:
        return []


class Host:
    def __init__(self, root: Path) -> None:
        self.store = Store(root)
        self.themes: list[str] = []

    def set_status(self, text: str, temporary: bool = False) -> None:
        pass

    def plugin_apply_theme(self, value: str) -> bool:
        self.themes.append(value)
        return True


def blocks(md: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", md, flags=re.S)


def main() -> int:
    doc = (ROOT / "docs" / "插件开发指南.md").read_text(encoding="utf-8")
    code = blocks(doc)
    print(f"文档代码块: {len(code)} 个")

    # 1) 语法
    bad = 0
    for i, b in enumerate(code):
        try:
            compile(b, f"block{i}", "exec")
        except SyntaxError as exc:
            bad += 1
            print(f"  [语法错] 块 {i}: {exc}")
    print(f"语法 OK: {len(code) - bad}/{len(code)}")

    # 2) 能当插件的块，逐个真加载 + 真跑
    plugins = [b for b in code if "def register" in b]
    print(f"含 register() 的块: {len(plugins)}")
    problems = 0
    for i, src in enumerate(plugins):
        tmp = Path(tempfile.mkdtemp(prefix="apmlog_doc_"))
        pdir = tmp / "plugins"
        pdir.mkdir()
        (pdir / f"snip.py").write_text(src, encoding="utf-8")

        st.Settings.set_instance(st.Settings(tmp / "settings.json"))
        host = Host(tmp)
        real_default = mgr_mod.default_plugin_dir
        mgr_mod.default_plugin_dir = lambda: pdir           # 只当前进程，脚本跑完就退出
        try:
            m = PluginManager(host)
            m.load_all()
            info = m.plugins()[0] if m.plugins() else None
            if info is None or info.status != "loaded":
                problems += 1
                print(f"  [加载失败] 块 {i}: {getattr(info, 'error', '没有插件')}")
                continue
            # 七类挂载点挨个跑一遍（没有的就跳过）
            hits = m.run_alert_rules()
            prims = m.map_layer_primitives()
            tools = m.plot_tools()
            sel = papi.PlotSelection(0, "VIBE.VibeX", ("VIBE.VibeX",), 10.0, 20.0, False)
            for pid, name, fn in tools:
                m.run_plot_tool(pid, fn, sel)
            out = tmp / "out.txt"
            for pid, name, fn in m.exporters():
                m.run_exporter(pid, name, fn, str(out))
            for t in m.themes_available():
                m.apply_theme(t["value"])
            print(f"  块 {i:2d} 已启用｜提供={info.provides}｜告警 {len(hits)} 条"
                  f"｜图元层 {len(prims)}｜工具 {len(tools)}"
                  f"｜导出 {'有' if out.exists() else '无'}｜主题 {len(m.themes())}")
        finally:
            mgr_mod.default_plugin_dir = real_default

    print("全部通过" if problems == 0 and bad == 0 else f"有问题: 语法 {bad} / 加载 {problems}")
    return 0 if problems == 0 and bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
