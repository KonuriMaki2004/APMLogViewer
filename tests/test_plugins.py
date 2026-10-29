# -*- coding: utf-8 -*-
"""tests/test_plugins.py —— 插件机制（外部目录 / 沙箱隔离 / 只读接口 / 七类挂载点）。

用户 2026-10-28：在「设置」与「关于」之间加「插件」入口，对外开放宿主能力：
只读 ``ctx.store``、控制口 + 事件订阅、以及告警规则/合成通道/预设/曲线工具/
地图图层/标签页/导出器七类挂载点；免安装版要能扫外部目录、坏插件只停自己、
打包要预留常用科学计算库。
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                              # noqa: E402
from PySide6 import QtWidgets                                   # noqa: E402

from apmlog.core.model import (ChannelInfo, ChannelKey, Envelope, EventRec, LogStats,  # noqa: E402
                               Series)
from apmlog.plugins import api as papi                          # noqa: E402
from apmlog.plugins import manager as mgr_mod                   # noqa: E402
from apmlog.plugins.manager import FAILED, PluginManager        # noqa: E402
from apmlog.ui import settings as st                            # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(times: int = 4) -> None:
    app = qapp()
    for _ in range(times):
        app.processEvents()


# --------------------------------------------------------------------------- #
# 插件源码（写进临时目录，模拟"外部插件"）
# --------------------------------------------------------------------------- #
GOOD_PLUGIN = r'''
PLUGIN_NAME = "全挂载点插件"
__version__ = "1.2.3"
import numpy as np

CALLS = []


def register(ctx):
    CALLS.append(ctx.api_version)

    def rule(store):
        return [{"rule": "demo", "title": "演示告警", "severity": "warn",
                 "t0": 1.0, "t1": 2.0, "value": 9.0, "threshold": 5.0, "unit": "m/s"}]

    ctx.register_alert_rule(rule)

    def energy(store):
        s = store.series("VIBE.VibeX")
        return s.t, np.asarray(s.v) * np.asarray(s.v)

    ctx.register_synthetic_channel("SYN.KE", energy, label="动能", unit="J")

    from apmlog.plugins import Preset, LaneRequest
    ctx.register_preset(Preset("plugin_demo", "插件演示", [LaneRequest("SYN", ["KE"])]))

    def tool(inner_ctx, sel):
        return "lane=%s keys=%d t0=%.2f whole=%s" % (
            sel.lane_label, len(sel.keys), sel.t0, sel.whole)

    ctx.register_plot_tool("演示工具", tool)

    class Layer:
        name = "演示图层"
        color = "#ff8800"

        def render(self, store):
            return [{"kind": "line", "points": [(30.0, 120.0), (30.001, 120.001)]},
                    {"kind": "point", "lat": 30.0, "lon": 120.0, "label": "家"},
                    {"kind": "circle", "lat": 30.0, "lon": 120.0, "radius_m": 25.0}]

    ctx.register_map_layer(Layer())

    from PySide6 import QtWidgets
    ctx.register_tab("插件页", QtWidgets.QLabel("hello from plugin"))

    def export(store, path):
        return "t0=%s" % store.stats().get("t_start")

    ctx.register_exporter("演示导出", export)
'''

BAD_IMPORT = r'''
PLUGIN_NAME = "导入就炸"
raise RuntimeError("boom at import")
'''

BAD_REGISTER = r'''
PLUGIN_NAME = "注册就炸"


def register(ctx):
    raise ValueError("boom in register")
'''

BOOM_RULE = r'''
PLUGIN_NAME = "规则总炸"


def register(ctx):
    def rule(store):
        raise ZeroDivisionError("rule boom")

    ctx.register_alert_rule(rule)
'''

NOT_A_PLUGIN = r'''
PLUGIN_NAME = "没有 register"
VALUE = 1
'''


# --------------------------------------------------------------------------- #
# 假宿主
# --------------------------------------------------------------------------- #
class FakeStore:
    """最小 LogStore 替身：只实现插件接口会碰到的读方法。"""

    def __init__(self, root: Path) -> None:
        self.name = "fake.bin"
        self.path = str(root / "fake.bin")
        self.meta = {"stats": {"t_start": 0.0, "t_end": 100.0}, "types": {}}
        self.stats = LogStats(path=self.path, duration=100.0, t_start=0.0, t_end=100.0,
                              firmware="Copter 4.7.0", frame_type="QUAD", n_messages=101,
                              n_params=3, pos_source="POS", mode_seq=["LOITER"])
        self.t = np.linspace(0.0, 100.0, 101)
        self.v = np.sin(self.t)
        self.alerts_calls = 0

    # --- 读接口 ---
    def types(self) -> list[str]:
        return ["VIBE"]

    def channel_infos(self, type_prefix=None) -> list[ChannelInfo]:
        return [ChannelInfo(ChannelKey("VIBE", "VibeX"), "f8", 101, 0.0, 100.0, -1.0, 1.0,
                            "m/s²", "")]

    def channel_range(self, key) -> dict:
        return {"min": -1.0, "max": 1.0, "unit": "m/s²"}

    def series(self, key, t0=None, t1=None, max_points=None, drop_nan=False) -> Series:
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(str(key))
        t, v = self.t, self.v
        return Series(k, t, v, "m/s²")

    def envelope(self, key, t0=None, t1=None, buckets=2000) -> Envelope:
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(str(key))
        return Envelope(k, self.t[:5], self.v[:5], self.v[:5], self.v[:5],
                        np.ones(5), "m/s²")

    def events(self, kinds=None, severity_min=None, t0=None, t1=None) -> list[EventRec]:
        return [EventRec(t=3.0, kind="MSG", text="armed", severity="info")]

    def params(self) -> dict:
        return {"BATT_CAPACITY": (5200.0, 3300.0)}

    def param(self, name, default=None):
        return self.params().get(name, (default, None))[0]

    def modes(self) -> list[tuple[float, float, str]]:
        return [(0.0, 100.0, "LOITER")]

    def tracks(self) -> list:
        return []

    def track(self, key=None):
        return None

    def alerts(self, config_path=None, overrides=None):
        self.alerts_calls += 1
        return []


class FakeHost:
    """宿主替身：主窗里插件真正会碰到的那些方法。"""

    def __init__(self, root: Path) -> None:
        self.store = FakeStore(root)
        self.status: list[str] = []
        self.opened: list[str] = []
        self.seek_t = 0.0
        self.snapshot: dict = {}

    def set_status(self, text: str, temporary: bool = False) -> None:
        self.status.append(str(text))

    def plugin_open_log(self, path: str) -> bool:
        self.opened.append(str(path))
        return True

    def plugin_seek(self, t: float) -> bool:
        self.seek_t = float(t)
        return True

    def plugin_zoom(self, a: float, b: float) -> bool:
        self.snapshot["zoom"] = (a, b)
        return True

    def plugin_play(self, on: bool) -> bool:
        self.snapshot["play"] = bool(on)
        return True

    def plugin_set_preset(self, pid: str) -> bool:
        self.snapshot["preset"] = str(pid)
        return True

    def plugin_add_channel(self, key: str) -> bool:
        self.snapshot["channel"] = str(key)
        return True

    def plugin_select_tab(self, name: str) -> bool:
        self.snapshot["tab"] = str(name)
        return True

    def plugin_refresh(self) -> bool:
        self.snapshot["refresh"] = True
        return True

    def plugin_export_png(self, path: str) -> bool:
        self.snapshot["png"] = str(path)
        return True


# --------------------------------------------------------------------------- #
# 基类：临时插件目录 + 隔离设置
# --------------------------------------------------------------------------- #
class PluginCase(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmp = Path(tempfile.mkdtemp(prefix="apmlog_plugin_"))
        self.dir = self.tmp / "plugins"
        self.dir.mkdir(parents=True, exist_ok=True)
        st.Settings.set_instance(st.Settings(self.tmp / "settings.json"))
        self._p1 = mock.patch.object(mgr_mod, "default_plugin_dir", lambda: self.dir)
        self._p1.start()
        self.addCleanup(self._p1.stop)
        self.host = FakeHost(self.tmp)

    def tearDown(self) -> None:
        st.Settings.set_instance(None)

    def write(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def manager(self) -> PluginManager:
        m = PluginManager(self.host)
        m.load_all()
        return m

    def status_of(self, m: PluginManager, pid: str) -> str:
        for i in m.plugins():
            if i.id == pid:
                return i.status
        return "missing"


# --------------------------------------------------------------------------- #
# 1. 外部目录发现
# --------------------------------------------------------------------------- #
class TestDiscovery(PluginCase):
    def test_loads_py_and_package_and_skips_noise(self) -> None:
        self.write("good.py", GOOD_PLUGIN)
        self.write("pkg/__init__.py", 'PLUGIN_NAME = "包插件"\n__version__ = "2.0"\n'
                                      "def register(ctx):\n    ctx.status = 1\n")
        self.write("_private.py", "raise RuntimeError('不该被加载')")
        self.write(".hidden.py", "raise RuntimeError('不该被加载')")
        self.write("notes.txt", "不是插件")
        m = self.manager()
        ids = {i.id for i in m.plugins()}
        self.assertEqual(ids, {"good", "pkg"})
        good = [i for i in m.plugins() if i.id == "good"][0]
        self.assertEqual(good.name, "全挂载点插件")
        self.assertEqual(good.version, "1.2.3")
        self.assertEqual(good.status, "loaded")
        self.assertEqual(self.status_of(m, "pkg"), "loaded")

    def test_empty_dir_means_no_plugins_and_no_noise(self) -> None:
        m = self.manager()
        self.assertEqual(m.plugins(), [])
        self.assertEqual(m.alert_rules(), [])
        self.assertEqual(m.map_layer_primitives(), [])

    def test_missing_dir_is_tolerated(self) -> None:
        """目录不存在（用户还没建）不该抛异常，程序照常启动。"""
        self._p1.stop()
        self._p1 = mock.patch.object(mgr_mod, "default_plugin_dir",
                                     lambda: self.tmp / "nope" / "deeper")
        self._p1.start()
        m = self.manager()
        self.assertEqual(m.plugins(), [])
        self.assertTrue((self.tmp / "nope" / "deeper").is_dir(), "默认目录应被建出来")

    def test_dirs_include_settings_and_env(self) -> None:
        extra = self.tmp / "extra"
        extra.mkdir()
        st.Settings.instance().set("plugin_dirs", json.dumps([str(extra)]))
        with mock.patch.dict(os.environ, {mgr_mod.ENV_PLUGIN_DIRS: str(self.tmp / "envd")}):
            dirs = [str(p) for p in mgr_mod.discover_dirs()]
        self.assertIn(str(self.dir), dirs)
        self.assertIn(str(extra), dirs)
        self.assertIn(str(self.tmp / "envd"), dirs)

    def test_duplicate_dirs_are_collapsed(self) -> None:
        st.Settings.instance().set("plugin_dirs", json.dumps([str(self.dir)]))
        dirs = [str(p) for p in mgr_mod.discover_dirs()]
        self.assertEqual(sum(1 for d in dirs if d == str(self.dir)), 1)


# --------------------------------------------------------------------------- #
# 2. 异常隔离沙箱
# --------------------------------------------------------------------------- #
class TestIsolation(PluginCase):
    def test_import_error_only_kills_that_plugin(self) -> None:
        self.write("good.py", GOOD_PLUGIN)
        self.write("bad.py", BAD_IMPORT)
        m = self.manager()
        self.assertEqual(self.status_of(m, "good"), "loaded")
        self.assertEqual(self.status_of(m, "bad"), "failed")
        bad = [i for i in m.plugins() if i.id == "bad"][0]
        self.assertIn("boom at import", bad.error)
        self.assertIn("Traceback", bad.traceback_text)
        # 好插件的贡献仍然在
        self.assertEqual(len(m.alert_rules()), 1)

    def test_register_error_is_contained(self) -> None:
        self.write("good.py", GOOD_PLUGIN)
        self.write("badreg.py", BAD_REGISTER)
        m = self.manager()
        self.assertEqual(self.status_of(m, "badreg"), "failed")
        bad = [i for i in m.plugins() if i.id == "badreg"][0]
        self.assertIn("boom in register", bad.error)
        self.assertEqual(len(m.presets()), 1, "坏插件不该影响好插件的预设")

    def test_missing_register_is_reported(self) -> None:
        self.write("noreg.py", NOT_A_PLUGIN)
        m = self.manager()
        info = m.plugins()[0]
        self.assertEqual(info.status, "failed")
        self.assertIn("register", info.error)

    def test_runtime_error_does_not_propagate(self) -> None:
        self.write("boom.py", BOOM_RULE)
        m = self.manager()
        hits = m.run_alert_rules()          # 规则里除零 → 空结果，不抛
        self.assertEqual(hits, [])
        info = m.plugins()[0]
        self.assertEqual(info.errors, 1)
        self.assertIn("ZeroDivisionError", info.error)

    def test_repeated_errors_disable_the_plugin(self) -> None:
        self.write("boom.py", BOOM_RULE)
        m = self.manager()
        self.assertEqual(m.plugins()[0].status, "loaded")
        for _ in range(papi.MAX_PLUGIN_ERRORS):
            m.run_alert_rules()
        info = m.plugins()[0]
        self.assertEqual(info.status, "disabled")
        self.assertIn("自动停用", info.error)
        self.assertEqual(m.alert_rules(), [], "停用后规则要摘干净")

    def test_bad_extension_arguments_are_rejected(self) -> None:
        m = self.manager()
        self.assertFalse(m.add_alert_rule("x", "不是函数"))
        self.assertFalse(m.add_synthetic("x", "没有点号", lambda s: ([], [])))
        self.assertFalse(m.add_preset("x", object()))
        self.assertFalse(m.add_tab("x", "标题", "不是 QWidget"))
        self.assertFalse(m.add_exporter("x", "", lambda s, p: ""))

    def test_disable_then_enable_restores_contributions(self) -> None:
        self.write("good.py", GOOD_PLUGIN)
        m = self.manager()
        self.assertEqual(len(m.presets()), 1)
        self.assertTrue(m.disable("good"))
        self.assertEqual(m.presets(), [])
        self.assertEqual(m.synthetics(), [])
        self.assertEqual(m.tabs(), [])
        self.assertTrue(m.enable("good"))
        self.assertEqual(len(m.presets()), 1)
        self.assertEqual(self.status_of(m, "good"), "loaded")


# --------------------------------------------------------------------------- #
# 3. 只读数据接口
# --------------------------------------------------------------------------- #
class TestReadOnlyStore(PluginCase):
    def view(self):
        return PluginManager(self.host).current_store_view()

    def test_available_and_metadata(self) -> None:
        v = self.view()
        self.assertTrue(v.available)
        self.assertEqual(v.name(), "fake.bin")
        self.assertEqual(v.stats()["firmware"], "Copter 4.7.0")
        self.assertEqual(v.types(), ["VIBE"])
        self.assertTrue(v.has_type("VIBE"))
        self.assertFalse(v.has_type("GPS"))
        self.assertEqual(v.modes()[0]["mode"], "LOITER")

    def test_channels_and_ranges(self) -> None:
        v = self.view()
        chans = v.channels()
        self.assertEqual(chans[0]["key"], "VIBE.VibeX")
        self.assertEqual(chans[0]["unit"], "m/s²")
        self.assertEqual(v.keys(), ["VIBE.VibeX"])
        self.assertEqual(v.channel_range("VIBE.VibeX")["max"], 1.0)

    def test_series_is_a_copy_and_read_only(self) -> None:
        v = self.view()
        s = v.series("VIBE.VibeX")
        self.assertEqual(s.n, 101)
        self.assertFalse(s.t.flags.writeable)
        self.assertFalse(s.v.flags.writeable)
        with self.assertRaises(ValueError):
            s.v[0] = 123.0                      # numpy 自己拒绝写入
        self.assertGreater(s.stats()["max"], 0.9)
        self.assertLessEqual(s.stats()["max"], 1.0)

    def test_events_params_envelope(self) -> None:
        v = self.view()
        self.assertEqual(v.events()[0]["text"], "armed")
        self.assertEqual(v.params()["BATT_CAPACITY"]["value"], 5200.0)
        self.assertEqual(v.param("BATT_CAPACITY"), 5200.0)
        self.assertEqual(v.envelope("VIBE.VibeX").n, 5)

    def test_view_rejects_writes_and_has_no_store_reference(self) -> None:
        v = self.view()
        with self.assertRaises(AttributeError):
            v.something = 1
        with self.assertRaises(AttributeError):
            del v.name
        self.assertFalse(hasattr(v, "_store"))
        self.assertFalse(hasattr(v, "close"))

    def test_params_dict_mutation_does_not_leak_back(self) -> None:
        v = self.view()
        d = v.params()
        d["BATT_CAPACITY"]["value"] = -1.0
        self.assertEqual(v.params()["BATT_CAPACITY"]["value"], 5200.0)

    def test_empty_view_reports_instead_of_faking(self) -> None:
        self.host.store = None
        v = self.view()
        self.assertFalse(v.available)
        self.assertEqual(v.types(), [])
        with self.assertRaises(RuntimeError):
            v.series("VIBE.VibeX")

    def test_frozen_array_helper(self) -> None:
        a = papi.frozen_array([1, 2, 3])
        self.assertFalse(a.flags.writeable)
        self.assertIsInstance(a, np.ndarray)


# --------------------------------------------------------------------------- #
# 4. 控制口与信号
# --------------------------------------------------------------------------- #
class TestControlAndSignals(PluginCase):
    def test_control_port_routes_to_host(self) -> None:
        m = PluginManager(self.host)
        c = m.control
        self.assertTrue(c.open_log("C:/x.bin"))
        self.assertTrue(c.seek(12.5))
        self.assertTrue(c.zoom(1, 2))
        self.assertTrue(c.play(True))
        self.assertTrue(c.set_preset("vibe"))
        self.assertTrue(c.add_channel("VIBE.VibeX"))
        self.assertTrue(c.select_tab("轨迹地图"))
        self.assertTrue(c.refresh())
        self.assertTrue(c.export_png("C:/x.png"))
        c.status("hi")
        self.assertEqual(self.host.opened, ["C:/x.bin"])
        self.assertAlmostEqual(self.host.seek_t, 12.5)
        self.assertEqual(self.host.snapshot["tab"], "轨迹地图")
        self.assertTrue(any("hi" in s for s in self.host.status))

    def test_control_failure_does_not_raise(self) -> None:
        def boom(*_a):
            raise RuntimeError("host broke")
        self.host.plugin_seek = boom
        m = PluginManager(self.host)
        errs: list[tuple[str, str]] = []
        m.pluginError.connect(lambda pid, msg: errs.append((pid, msg)))
        self.assertFalse(m.control.seek(1.0))
        pump()
        self.assertTrue(errs, "控制口失败必须报给界面，不能默默吞掉")
        self.assertIn("host broke", errs[0][1])

    def test_signals_carry_plain_data(self) -> None:
        m = PluginManager(self.host)
        got: list[object] = []
        m.signals.log_loaded.connect(got.append)
        m.signals.time_changed.connect(lambda t: got.append(t))
        m.signals.log_loaded.emit({"name": "a.bin"})
        m.signals.time_changed.emit(3.5)
        pump()
        self.assertEqual(got[0], {"name": "a.bin"})
        self.assertAlmostEqual(got[1], 3.5)


# --------------------------------------------------------------------------- #
# 5. 七类挂载点
# --------------------------------------------------------------------------- #
class TestExtensionPoints(PluginCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("good.py", GOOD_PLUGIN)
        self.m = self.manager()

    def test_alert_rule_merges_as_alert_hit(self) -> None:
        hits = self.m.run_alert_rules()
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h.title, "演示告警")
        self.assertEqual(h.severity, "warn")
        self.assertEqual(h.source, "plugin:good")
        self.assertIn("plugin:good", h.rule)
        d = h.as_dict()
        self.assertEqual(d["unit"], "m/s")
        self.assertAlmostEqual(d["t0"], 1.0)

    def test_synthetic_channel_computes(self) -> None:
        syn = self.m.synthetic_for("SYN.KE")
        self.assertIsNotNone(syn)
        data = self.m.synthetic_series(syn)
        self.assertEqual(data.n, 101)
        self.assertAlmostEqual(float(np.max(np.abs(np.asarray(data.v)))), 1.0, delta=0.02)
        self.assertEqual(data.unit, "J")
        self.assertEqual(self.m.synthetic_count(syn), 101)

    def test_synthetic_channel_usable_as_series(self) -> None:
        src = self.m.channel_source(self.host.store)
        s = src.series("SYN.KE")
        self.assertEqual(s.key.key, "SYN.KE")
        self.assertEqual(s.n, 101)
        keys = [c.key.key for c in src.channel_infos()]
        self.assertIn("SYN.KE", keys)
        self.assertIn("VIBE.VibeX", keys)

    def test_synthetic_channel_with_bad_return_is_rejected(self) -> None:
        bad = papi.SyntheticChannel(key="SYN.BAD", fn=lambda store: (np.arange(3), np.arange(2)),
                                    plugin="good")
        self.assertIsNone(self.m.synthetic_series(bad))
        self.assertEqual(self.m.plugins()[0].errors, 1)

    def test_preset_registered_and_resolvable(self) -> None:
        presets = self.m.presets()
        self.assertEqual(len(presets), 1)
        pid, pr = presets[0]
        self.assertEqual(pid, "good")
        self.assertEqual(pr.id, "plugin_demo")
        self.assertIs(self.m.preset_by_id("plugin_demo"), pr)
        self.assertEqual(pr.lanes[0].type, "SYN")
        self.assertEqual(pr.lanes[0].fields, ["KE"])

    def test_plot_tool_runs_with_selection(self) -> None:
        tools = self.m.plot_tools()
        self.assertEqual(len(tools), 1)
        pid, name, fn = tools[0]
        sel = papi.PlotSelection(lane_index=0, lane_label="VIBE.VibeX", keys=("VIBE.VibeX",),
                                 t0=1.0, t1=2.0, whole=False)
        out = self.m.run_plot_tool(pid, fn, sel)
        self.assertIn("lane=VIBE.VibeX", out)
        self.assertIn("keys=1", out)
        self.assertFalse(sel.whole)

    def test_plot_tool_exception_is_isolated(self) -> None:
        def boom(ctx, sel):
            raise RuntimeError("tool boom")
        self.m.add_plot_tool("good", "炸掉的工具", boom)
        sel = papi.PlotSelection(0, "x", (), 0.0, 1.0, True)
        self.assertIs(self.m.run_plot_tool("good", boom, sel), FAILED)

    def test_map_layer_primitives(self) -> None:
        layers = self.m.map_layer_primitives()
        self.assertEqual(len(layers), 1)
        layer = layers[0]
        self.assertEqual(layer["name"], "演示图层")
        self.assertEqual(layer["color"], "#ff8800")
        kinds = [p["kind"] for p in layer["prims"]]
        self.assertEqual(kinds, ["line", "point", "circle"])
        self.assertEqual(len(layer["prims"][0]["points"]), 2)
        self.assertAlmostEqual(layer["prims"][2]["radius_m"], 25.0)

    def test_map_layer_bad_data_is_dropped_not_guessed(self) -> None:
        def render(store):
            return [{"kind": "line", "points": [(999.0, 999.0), (1.0, 2.0)]},
                    {"kind": "point", "lat": float("nan"), "lon": 0.0},
                    {"kind": "point", "lat": 30.0, "lon": 120.0, "color": "不是颜色"},
                    {"kind": "circle", "lat": 30.0, "lon": 120.0, "radius_m": -5},
                    "垃圾"]
        layer = SimpleNamespace(name="脏图层", color="#123456", render=render)
        self.assertTrue(self.m.add_map_layer("zz", layer))
        prims = [lay for lay in self.m.map_layer_primitives() if lay["name"] == "脏图层"][0]
        self.assertEqual(len(prims["prims"]), 1)
        self.assertEqual(prims["prims"][0]["color"], "#123456", "坏颜色退回图层默认色")

    def test_tab_and_exporter(self) -> None:
        tabs = self.m.tabs()
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0][1], "插件页")
        self.assertIsInstance(tabs[0][2], QtWidgets.QWidget)
        exps = self.m.exporters()
        self.assertEqual(len(exps), 1)
        pid, name, fn = exps[0]
        out = Path(tempfile.mkdtemp(prefix="apmlog_export_")) / "x.txt"
        ok, msg = self.m.run_exporter(pid, name, fn, str(out))
        self.assertTrue(ok, msg)
        self.assertIn("t0=0.0", out.read_text(encoding="utf-8"))

    def test_channel_source_covers_what_presets_need(self) -> None:
        """真事故回归：ChannelSource 曾缺 envelope → build_lanes 把整道当异常跳过。"""
        from apmlog.ui.presets import build_lanes
        src = self.m.channel_source(self.host.store)
        for name in ("channel_infos", "series", "envelope", "track", "modes"):
            self.assertTrue(callable(getattr(src, name, None)), f"ChannelSource 缺 {name}()")
        self.assertIsInstance(src.meta, dict, "ChannelSource.meta 应当是属性")
        lanes, skipped = build_lanes(src, "vibe", 0.0, 100.0)
        self.assertTrue(lanes, f"预设应当能出泳道，实际 skipped={skipped}")
        self.assertFalse([x for x in skipped if "异常" in x], skipped)

    def test_synthetic_envelope_shares_the_bucketing(self) -> None:
        src = self.m.channel_source(self.host.store)
        env = src.envelope("SYN.KE", 0.0, 100.0, buckets=20)
        self.assertEqual(len(np.asarray(env.t)), 20)
        self.assertTrue(np.isfinite(np.asarray(env.vmax)).any())

    def test_provides_lists_all_kinds(self) -> None:
        info = [i for i in self.m.plugins() if i.id == "good"][0]
        for kind in ("告警规则", "合成通道", "预设视图", "曲线工具", "地图图层", "标签页", "导出器"):
            self.assertIn(kind, info.provides)


# --------------------------------------------------------------------------- #
# 6. 主窗集成
# --------------------------------------------------------------------------- #
class TestMainWindowIntegration(PluginCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("good.py", GOOD_PLUGIN)
        self.write("bad.py", BAD_IMPORT)
        views = self.tmp / "views.json"
        self._p2 = mock.patch("apmlog.app.MainWindow._views_path", lambda self_: views)
        self._p2.start()
        self.addCleanup(self._p2.stop)
        from apmlog.app import MainWindow
        self.win = MainWindow()
        pump()
        self.addCleanup(self.win.close)

    def _actions(self) -> list[str]:
        tb = [w for w in self.win.findChildren(QtWidgets.QToolBar)][0]
        return [a.text() for a in tb.actions()]

    def test_toolbar_has_plugins_between_settings_and_about(self) -> None:
        acts = self._actions()
        self.assertIn("插件", acts)
        self.assertEqual(acts.index("插件"), acts.index("设置") + 1)
        self.assertEqual(acts.index("关于"), acts.index("插件") + 1)

    def test_bad_plugin_does_not_stop_the_window(self) -> None:
        statuses = {i.id: i.status for i in self.win.plugins.plugins()}
        self.assertEqual(statuses.get("good"), "loaded")
        self.assertEqual(statuses.get("bad"), "failed")
        self.assertIsNotNone(self.win.centralWidget())

    def test_plugin_preset_appears_in_combo(self) -> None:
        box = self.win.preset_box
        ids = [box.itemData(i) for i in range(box.count())]
        self.assertIn("plugin_demo", ids)
        self.assertIn("vibe", ids, "内置预设不能在加插件时丢掉")

    def test_plugin_tab_is_added(self) -> None:
        titles = [self.win.tabs.tabText(i) for i in range(self.win.tabs.count())]
        self.assertIn("插件页", titles)

    def test_synthetic_channel_in_tree_and_spec(self) -> None:
        self.win.store = self.host.store
        self.win._refresh_channel_tree_plugins()
        tree = self.win.channel_tree.tree
        tops = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
        self.assertIn("插件通道", tops)
        self.win.channel_tree.set_store(self.host.store)      # 真报文树 + 插件组共存
        self.win._refresh_channel_tree_plugins()
        tops = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
        self.assertIn("VIBE", tops)
        self.assertIn("插件通道", tops)

    def test_alert_rules_merge_into_compute_alerts(self) -> None:
        self.win.store = self.host.store
        hits = self.win._compute_alerts()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "plugin:good")
        self.assertEqual(self.host.store.alerts_calls, 1, "宿主规则仍然只算一次")

    def test_map_layers_reach_the_widget(self) -> None:
        self.win.store = self.host.store
        self.win._refresh_map_layers()
        self.assertEqual(len(self.win.map.plugin_layers()), 1)

    def test_disable_removes_everything_it_added(self) -> None:
        self.win.store = self.host.store
        self.win.plugins.disable("good")
        pump()
        box = self.win.preset_box
        self.assertNotIn("plugin_demo", [box.itemData(i) for i in range(box.count())])
        self.assertNotIn("插件页", [self.win.tabs.tabText(i) for i in range(self.win.tabs.count())])
        self.assertEqual(self.win._compute_alerts(), [])
        self.assertEqual(self.win.map.plugin_layers(), [])

    def test_plot_tools_show_up_in_lane_menu(self) -> None:
        self.assertIn("演示工具", self.win.plot.plot_tools())

    def test_plugin_error_goes_to_status_bar(self) -> None:
        self.win.plugins.pluginError.emit("good", "测试错误")
        pump()
        self.assertIn("测试错误", self.win.statusBar().currentMessage())

    # ---- 2026-10-29：插件页带大 minimumWidth 时不能把左侧栏挤扁 ----
    def _host_of_plugin_tab(self) -> QtWidgets.QWidget:
        for i in range(self.win.tabs.count()):
            w = self.win.tabs.widget(i)
            if isinstance(w, QtWidgets.QScrollArea) and w.widget() is not None:
                return w
        self.fail("插件页没有包滑滚动宿主")

    def test_plugin_tab_is_wrapped_in_scroll_host(self) -> None:
        host = self._host_of_plugin_tab()
        self.assertTrue(host.widgetResizable())
        self.assertIsInstance(host.widget(), QtWidgets.QLabel, "插件控件本体没变，只是多了层壳")
        self.assertEqual(host.widget().text(), "hello from plugin")

    def test_wide_plugin_page_does_not_squeeze_left_pane(self) -> None:
        """插件要求最小宽 1400 时，标签区的最小宽不能跟着涨（否则左侧栏拉不回来）。"""
        self.win.show()                              # 不 show 时子控件几何还是脏的
        self.win.resize(1728, 940)
        pump()
        host = self._host_of_plugin_tab()
        base = self.win.tabs.minimumSizeHint().width()
        host.widget().setMinimumWidth(1400)          # 第三方插件的典型毛病
        self.win.tabs.setCurrentWidget(host)
        pump()
        self.assertLessEqual(self.win.tabs.minimumSizeHint().width(), base + 40,
                             "插件的最小宽顶到了标签区，左侧栏会被挤扁")
        split = self.win.centralWidget().widget(0)          # 横向分隔条
        split.setSizes([620, 900])
        pump()
        self.assertGreaterEqual(split.sizes()[0], 500,
                                "左侧栏应该能被拉回来，而不是被插件页的尺寸锁住")

    def test_plugin_tab_still_selectable_by_name(self) -> None:
        self.assertTrue(self.win.plugin_select_tab("插件页"))
        pump()
        self.assertEqual(self.win.tabs.tabText(self.win.tabs.currentIndex()), "插件页")

    def test_plugin_dialog_opens_and_lists(self) -> None:
        from apmlog.ui.plugindialog import PluginDialog
        dlg = PluginDialog(self.win.plugins, self.win)
        try:
            dlg.show()
            pump()
            self.assertEqual(dlg.table.rowCount(), 2)
            names = [dlg.table.item(r, 0).text() for r in range(dlg.table.rowCount())]
            self.assertIn("全挂载点插件", names)
            self.assertIsNotNone(dlg.findChild(QtWidgets.QListWidget))
        finally:
            dlg.close()


# --------------------------------------------------------------------------- #
# 7. 打包预留
# --------------------------------------------------------------------------- #
class TestPackaging(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = (Path(__file__).resolve().parent.parent / "packaging"
                     / "apmlogviewer.spec").read_text(encoding="utf-8")

    def test_plugin_modules_are_hidden_imports(self) -> None:
        for mod in ("apmlog.plugins", "apmlog.plugins.api", "apmlog.plugins.manager",
                    "apmlog.ui.plugindialog"):
            self.assertIn(f'"{mod}"', self.spec, f"{mod} 必须进 hiddenimports")

    def test_scientific_libraries_are_reserved(self) -> None:
        self.assertIn("OPTIONAL_PLUGIN_MODULES", self.spec)
        for lib in ("scipy", "pandas", "matplotlib.backends.backend_agg", "sklearn",
                    "h5py", "PIL", "numba"):
            self.assertIn(f'"{lib}"', self.spec, f"{lib} 应出现在插件可选依赖里")

    def test_optional_libs_are_not_excluded_when_installed(self) -> None:
        self.assertIn("excludes = [e for e in excludes", self.spec)
        self.assertIn("in _OPT_ROOTS and _has(", self.spec)

    def test_spec_is_valid_python(self) -> None:
        compile(self.spec, "apmlogviewer.spec", "exec")


# --------------------------------------------------------------------------- #
# 8. 插件目录与设置
# --------------------------------------------------------------------------- #
class TestSettingsKey(PluginCase):
    def test_plugin_dirs_default_is_empty_json(self) -> None:
        self.assertEqual(st.DEFAULTS["plugin_dirs"], "[]")
        self.assertEqual(st.Settings.instance().get("plugin_dirs"), "[]")

    def test_plugin_dirs_roundtrip(self) -> None:
        s = st.Settings.instance()
        s.set("plugin_dirs", json.dumps(["C:/a", "C:/b"]))
        self.assertEqual(json.loads(s.get("plugin_dirs")), ["C:/a", "C:/b"])


if __name__ == "__main__":
    unittest.main()
