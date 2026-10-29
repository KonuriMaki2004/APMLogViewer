"""apmlog.plugins.api —— 插件能看到的**稳定**接口面。

四条硬约束（契约 §25）：

1. **只读**：``ctx.store`` 只给快照式纯数据（dict / frozen dataclass + 不可写 numpy），
   插件拿不到 ``LogStore`` 之类的宿主内部对象，也就没有改宿主状态的入口。
2. **稳定**：本模块里的类名、函数名、字段名是插件作者依赖的契约；要改先在
   ``docs/CONTRACT.md`` 记一笔并升 ``API_VERSION``。
3. **隔离**：插件代码一律经 ``PluginManager.guard`` 调用，异常只停用出错的那个插件。
4. **不假装**：读不到就是读不到（抛异常或返回空），绝不返回编造的值。

插件典型写法（外部目录里的一个 ``.py``）::

    from apmlog.plugins import PluginContext

    def register(ctx: PluginContext) -> None:
        ctx.register_alert_rule(my_rule)
        ctx.register_synthetic_channel("SYN.KE", kinetic_energy, label="动能", unit="J")
        ctx.signals.log_loaded.connect(on_loaded)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np
from PySide6 import QtCore

#: 插件接口版本。宿主启动时把当前值交给插件，插件可以据此拒绝运行。
API_VERSION = 1

#: 每个插件允许的最大连续异常次数；到点自动停用（防止一个坏插件刷屏拖慢主程序）
MAX_PLUGIN_ERRORS = 5


# --------------------------------------------------------------------------- #
# 只读数据
# --------------------------------------------------------------------------- #
def frozen_array(values: Any) -> np.ndarray:
    """拷一份 float64 并设为不可写：插件改不了宿主的数据，也改不了别人的。"""
    arr = np.array(values, dtype=np.float64, copy=True)
    arr.flags.writeable = False
    return arr


@dataclass(frozen=True)
class SeriesData:
    """一条时间序列的只读快照。``t`` 单位秒（自 boot），单调不减。"""

    key: str
    label: str
    unit: str
    t: np.ndarray
    v: np.ndarray

    def __post_init__(self) -> None:
        object.__setattr__(self, "t", frozen_array(self.t))
        object.__setattr__(self, "v", frozen_array(self.v))

    @property
    def n(self) -> int:
        return int(self.t.size)

    def finite(self) -> np.ndarray:
        return np.isfinite(self.v)

    def stats(self) -> dict[str, float]:
        """与宿主 ``Series.stats()`` 同口径的统计量（空序列给 nan，不编 0）。"""
        v = self.v[np.isfinite(self.v)]
        if v.size == 0:
            return {k: float("nan") for k in
                    ("min", "max", "mean", "std", "rms", "p05", "p50", "p95", "n")}
        return {
            "min": float(v.min()), "max": float(v.max()), "mean": float(v.mean()),
            "std": float(v.std()), "rms": float(np.sqrt(np.mean(v * v))),
            "p05": float(np.percentile(v, 5)), "p50": float(np.percentile(v, 50)),
            "p95": float(np.percentile(v, 95)), "n": float(v.size),
        }


@dataclass(frozen=True)
class EnvelopeData:
    """分桶 min/max 包络的只读快照（大数据量通道用它省内存）。"""

    key: str
    label: str
    unit: str
    t: np.ndarray
    vmin: np.ndarray
    vmax: np.ndarray
    vmean: np.ndarray
    count: np.ndarray

    def __post_init__(self) -> None:
        for name in ("t", "vmin", "vmax", "vmean", "count"):
            object.__setattr__(self, name, frozen_array(getattr(self, name)))

    @property
    def n(self) -> int:
        return int(self.t.size)


class StoreView:
    """只读日志视图。

    内部只留一组**闭包**（``_op``），连 ``LogStore`` 的引用都不在实例里；
    ``__setattr__`` 一律拒绝，插件改不到任何东西。
    """

    __slots__ = ("_op",)

    def __init__(self, ops: Mapping[str, Callable[..., Any]]) -> None:
        object.__setattr__(self, "_op", dict(ops))

    # 只读保护 ------------------------------------------------------------- #
    def __setattr__(self, name: str, value: Any) -> None:      # noqa: D105
        raise AttributeError("StoreView 是只读视图，不支持赋值")

    def __delattr__(self, name: str) -> None:                  # noqa: D105
        raise AttributeError("StoreView 是只读视图，不支持删除属性")

    def __repr__(self) -> str:                                 # pragma: no cover
        try:
            return f"<StoreView {self.name()!r}>"
        except Exception:                                      # noqa: BLE001
            return "<StoreView (未打开日志)>"

    # 元信息 --------------------------------------------------------------- #
    @property
    def available(self) -> bool:
        return bool(self._op.get("available", lambda: False)())

    def name(self) -> str:
        """日志文件名（不含目录）。"""
        return str(self._op["name"]())

    def path(self) -> str:
        """日志绝对路径（只读字符串，插件别拿它去写日志文件）。"""
        return str(self._op["path"]())

    def meta(self) -> dict[str, Any]:
        """缓存索引的深拷贝（固件、机型、统计、电池语义等）。"""
        return dict(self._op["meta"]())

    def stats(self) -> dict[str, Any]:
        """``LogStats`` 的字段字典。"""
        return dict(self._op["stats"]())

    def types(self) -> list[str]:
        """报文类型名列表（已排序）。"""
        return list(self._op["types"]())

    def has_type(self, type_: str) -> bool:
        return bool(self._op["has_type"](str(type_)))

    # 通道发现 ------------------------------------------------------------- #
    def channels(self, prefix: Optional[str] = None) -> list[dict[str, Any]]:
        """通道清单：``key/type/field/inst/label/unit/count/t0/t1/vmin/vmax``。"""
        return [dict(c) for c in self._op["channels"](prefix)]

    def keys(self, prefix: Optional[str] = None) -> list[str]:
        """只要通道键（``VIBE#1.VibeX`` 这种写法）。"""
        return [str(c["key"]) for c in self.channels(prefix)]

    def channel_range(self, key: str) -> dict[str, float]:
        """按实例精确计算的量程/统计（比 ``channels()`` 里的 min/max 准）。"""
        return dict(self._op["channel_range"](str(key)))

    # 时序数据 ------------------------------------------------------------- #
    def series(self, key: str, t0: Optional[float] = None, t1: Optional[float] = None,
               max_points: Optional[int] = None, drop_nan: bool = False) -> SeriesData:
        """取一条时序。``max_points`` 触发抽稀（宿主内部同一套算法）。"""
        return self._op["series"](str(key), t0, t1, max_points, drop_nan)

    def series_dict(self, keys: Sequence[str], t0: Optional[float] = None,
                    t1: Optional[float] = None,
                    max_points: Optional[int] = None) -> dict[str, SeriesData]:
        """批量取数；取不到的键直接不出现在结果里（与宿主行为一致）。"""
        out: dict[str, SeriesData] = {}
        for k in keys:
            try:
                out[str(k)] = self.series(k, t0, t1, max_points)
            except KeyError:
                continue
        return out

    def envelope(self, key: str, t0: Optional[float] = None, t1: Optional[float] = None,
                 buckets: int = 2000) -> EnvelopeData:
        """分桶包络（min/max/mean/count），用于整段速览。"""
        return self._op["envelope"](str(key), t0, t1, int(buckets))

    # 非时序结构化数据 ----------------------------------------------------- #
    def events(self, kinds: Optional[Iterable[str]] = None,
               severity_min: Optional[str] = None,
               t0: Optional[float] = None, t1: Optional[float] = None) -> list[dict[str, Any]]:
        """解码后的事件（含原始码与解码文本，见 EventRec.as_dict）。"""
        ks = None if kinds is None else [str(k) for k in kinds]
        return [dict(e) for e in self._op["events"](ks, severity_min, t0, t1)]

    def params(self) -> dict[str, dict[str, Any]]:
        """参数表：``{名称: {"value": 值, "default": 出厂默认或 None}}``。"""
        return {str(k): dict(v) for k, v in self._op["params"]().items()}

    def param(self, name: str, default: Optional[float] = None) -> Optional[float]:
        return self._op["param"](str(name), default)

    def modes(self) -> list[dict[str, Any]]:
        """飞行模式分段：``[{"t0":..,"t1":..,"mode":"LOITER"}, ...]``。"""
        return [dict(m) for m in self._op["modes"]()]

    def tracks(self) -> list[dict[str, Any]]:
        """全部位置轨迹（主轨迹在第一位），字段见 TrackResult.as_dict。"""
        return [dict(t) for t in self._op["tracks"]()]

    def track(self, key: Optional[str] = None) -> dict[str, Any]:
        return dict(self._op["track"](key))


def empty_store_view() -> StoreView:
    """没有打开日志时的空视图：读什么都报"没有日志"，而不是编造空数据。"""

    def _nope(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("还没有打开日志：请先打开一份 .BIN")

    return StoreView({
        "available": lambda: False,
        "name": lambda: "",
        "path": lambda: "",
        "meta": lambda: {},
        "stats": lambda: {},
        "types": lambda: [],
        "has_type": lambda _t: False,
        "channels": lambda _p=None: [],
        "channel_range": _nope,
        "series": _nope,
        "envelope": _nope,
        "events": lambda *_a, **_k: [],
        "params": lambda: {},
        "param": lambda _n, d=None: d,
        "modes": lambda: [],
        "tracks": lambda: [],
        "track": _nope,
    })


# --------------------------------------------------------------------------- #
# 信号（宿主 → 插件）
# --------------------------------------------------------------------------- #
class PluginSignals(QtCore.QObject):
    """宿主事件全部走 Qt Signal；插件用 ``connect`` 接收。

    参数一律是**纯数据**（dict/list/float/str），不带宿主内部对象。
    """

    #: 日志加载完成：``{"name","path","stats","types","n_alerts"}``
    log_loaded = QtCore.Signal(object)
    #: 日志关闭/切换前的旧日志不再可用
    log_closed = QtCore.Signal()
    #: 游标时刻变化（秒）
    time_changed = QtCore.Signal(float)
    #: 播放开始/停止
    play_state = QtCore.Signal(bool)
    #: 当前飞行模式变化
    mode_changed = QtCore.Signal(str)
    #: 告警重算完成：``[AlertHit.as_dict(), ...]``
    alerts_updated = QtCore.Signal(object)
    #: 设置项变化：``(key, value)``
    settings_changed = QtCore.Signal(str, object)
    #: 曲线选区变化：``{"t0","t1","whole_log"}``
    selection_changed = QtCore.Signal(object)
    #: 插件状态变化（加载/失败/停用）：``[PluginInfo.as_dict(), ...]``
    plugins_changed = QtCore.Signal(object)
    #: 当前界面主题变化：``"system"/"light"/"dark"/"custom"/"plugin:<pid>:<主题>"``
    theme_changed = QtCore.Signal(str)
    #: 主题不可用已回退：``(原主题值, 原因)``——主题插件据此知道自己被换下了
    theme_error = QtCore.Signal(str, str)


# --------------------------------------------------------------------------- #
# 控制口（插件 → 宿主）
# --------------------------------------------------------------------------- #
class ControlPort:
    """主动控制宿主的能力。每个动作都是**显式白名单方法**，没有通用后门。"""

    __slots__ = ("_op",)

    def __init__(self, ops: Mapping[str, Callable[..., Any]]) -> None:
        object.__setattr__(self, "_op", dict(ops))

    def __setattr__(self, name: str, value: Any) -> None:      # noqa: D105
        raise AttributeError("ControlPort 不支持赋值")

    def open_log(self, path: str) -> bool:
        """打开一份日志（异步，返回是否已受理）。"""
        return bool(self._op["open_log"](str(path)))

    def seek(self, t: float) -> bool:
        """把游标/地图/时间线都定位到 t 秒。"""
        return bool(self._op["seek"](float(t)))

    def zoom(self, t0: float, t1: float) -> bool:
        """曲线放大到 [t0, t1]。"""
        return bool(self._op["zoom"](float(t0), float(t1)))

    def play(self, on: bool) -> bool:
        """开始/停止播放。"""
        return bool(self._op["play"](bool(on)))

    def set_preset(self, preset_id: str) -> bool:
        """切换预设视图（内置或插件注册的 id）。"""
        return bool(self._op["set_preset"](str(preset_id)))

    def add_channel(self, key: str) -> bool:
        """把手动通道加进曲线页（``VIBE#0.VibeZ`` 或插件合成通道）。"""
        return bool(self._op["add_channel"](str(key)))

    def select_tab(self, name: str) -> bool:
        """切到某个标签页（按标题，如 "轨迹地图"）。"""
        return bool(self._op["select_tab"](str(name)))

    def status(self, text: str) -> None:
        """在状态栏留一句话（给用户看的）。"""
        self._op["status"](str(text))

    def refresh(self) -> bool:
        """重算当前视图（预设重新取数 + 重画）。"""
        return bool(self._op["refresh"]())

    def export_png(self, path: str) -> bool:
        """把当前曲线页导出成 PNG。"""
        return bool(self._op["export_png"](str(path)))


# --------------------------------------------------------------------------- #
# 挂载点用的数据类型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlotSelection:
    """曲线选区（右键工具回调的入参）。``whole`` 为真表示"没有选区，整段"。"""

    lane_index: int
    lane_label: str
    keys: tuple[str, ...]
    t0: float
    t1: float
    whole: bool = False

    def duration(self) -> float:
        return max(0.0, float(self.t1) - float(self.t0))

    def as_dict(self) -> dict[str, Any]:
        return {"lane_index": self.lane_index, "lane_label": self.lane_label,
                "keys": list(self.keys), "t0": self.t0, "t1": self.t1,
                "whole": self.whole}


@dataclass
class SyntheticChannel:
    """虚拟/派生通道：``fn(store) -> (t, v)``（也可以返回 SeriesData）。"""

    key: str
    fn: Callable[[StoreView], Any]
    label: str = ""
    unit: str = ""
    plugin: str = ""

    def display(self) -> str:
        return self.label or self.key


class MapLayerPlugin:
    """自定义地图图层的协议基类。

    ``render(store)`` 返回若干图元（可以混合）::

        {"kind": "line",  "points": [(lat, lon), ...], "color": "#2f6fed",
         "width": 2, "dash": False}
        {"kind": "point", "lat": .., "lon": .., "label": "起飞点", "color": "#e74c3c"}
        {"kind": "circle","lat": .., "lon": .., "radius_m": 30.0, "color": "#f0a500"}

    宿主负责投影（含 GCJ-02 纠偏）、限流（单层 ≤ 400 图元、单线 ≤ 5000 点）与重画。
    """

    #: 图层名（显示在图例/插件页）
    name: str = "插件图层"
    #: 默认颜色
    color: str = "#2f6fed"
    #: 是否显示
    visible: bool = True

    def render(self, store: StoreView) -> Sequence[dict[str, Any]]:
        raise NotImplementedError("插件图层必须实现 render(store)")


class SettingsView:
    """只读设置视图。要改设置请在宿主界面里改，插件不许静默改用户偏好。"""

    __slots__ = ("_get", "_items")

    def __init__(self, getter: Callable[[str, Any], Any],
                 items: Callable[[], dict[str, Any]]) -> None:
        object.__setattr__(self, "_get", getter)
        object.__setattr__(self, "_items", items)

    def __setattr__(self, name: str, value: Any) -> None:      # noqa: D105
        raise AttributeError("SettingsView 只读")

    def get(self, key: str, default: Any = None) -> Any:
        return self._get(str(key), default)

    def __getitem__(self, key: str) -> Any:
        return self._get(str(key), None)

    def keys(self) -> list[str]:
        return sorted(self._items().keys())

    def as_dict(self) -> dict[str, Any]:
        return dict(self._items())


@dataclass(frozen=True)
class ThemeSpec:
    """主题插件向宿主声明的**一套四色主题**。

    ``colors`` 必须同时给全四个角色：``window``（窗口/工具条底）、``base``（列表/输入区底）、
    ``text``（正文）、``accent``（选中/游标高亮）；键也收 ``c_window`` 这种设置项写法。
    颜色只收 ``#rgb`` / ``#rrggbb``——不合法就当**主题不受支持**拒掉，不猜也不自动修正。

    ``curve`` 留空 = 按 ``base`` 亮度自动选亮线/暗线；要强制就写 ``"light"`` / ``"dark"``。

    宿主拿到后只能读：主题不可变，切换主题=换一份 ThemeSpec。
    """

    id: str
    name: str
    colors: Mapping[str, str]
    curve: str = ""

    def color(self, role: str) -> str:
        try:
            return str(self.colors.get(role) or "")
        except AttributeError:                            # pragma: no cover
            return ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "curve": self.curve,
                "colors": {str(k): str(v) for k, v in dict(self.colors).items()}}


class PluginContext:
    """传给 ``register(ctx)`` 的上下文：只读数据 + 信号 + 控制口 + 挂载点。"""

    def __init__(self, manager: Any, plugin_id: str) -> None:
        object.__setattr__(self, "_mgr", manager)
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "log", logging.getLogger(f"apmlog.plugin.{plugin_id}"))

    # 只读 ---------------------------------------------------------------- #
    @property
    def api_version(self) -> int:
        return API_VERSION

    @property
    def store(self) -> StoreView:
        """当前日志的只读视图（日志切换后自动跟着换；没日志时 available=False）。"""
        return self._mgr.current_store_view()

    @property
    def signals(self) -> PluginSignals:
        return self._mgr.signals

    @property
    def control(self) -> ControlPort:
        return self._mgr.control

    @property
    def settings(self) -> SettingsView:
        return self._mgr.settings_view()

    @property
    def host_version(self) -> str:
        return str(self._mgr.host_version())

    # 挂载点 -------------------------------------------------------------- #
    def register_alert_rule(self, fn: Callable[[StoreView], Sequence[dict[str, Any]]]) -> bool:
        """自定义告警规则：``fn(store) -> [{"rule","title","severity","t0","t1",...}]``。"""
        return bool(self._mgr.add_alert_rule(self.plugin_id, fn))

    def register_synthetic_channel(self, key: str, fn: Callable[[StoreView], Any],
                                   *, label: str = "", unit: str = "") -> bool:
        """注册虚拟通道 ``TYPE.field``，可在通道树/预设里像真通道一样用。"""
        return bool(self._mgr.add_synthetic(self.plugin_id, key, fn,
                                            label=label, unit=unit))

    def register_preset(self, preset: Any) -> bool:
        """注册一个预设视图（``apmlog.plugins.Preset`` 实例）。"""
        return bool(self._mgr.add_preset(self.plugin_id, preset))

    def register_plot_tool(self, name: str,
                           callback: Callable[[PluginContext, PlotSelection], Any]) -> bool:
        """曲线右键分析工具：``callback(ctx, selection)``。"""
        return bool(self._mgr.add_plot_tool(self.plugin_id, name, callback))

    def register_map_layer(self, layer: Any) -> bool:
        """地图自定义图层（``MapLayerPlugin`` 子类实例，鸭子类型也认）。"""
        return bool(self._mgr.add_map_layer(self.plugin_id, layer))

    def register_tab(self, title: str, widget: Any) -> bool:
        """独立标签页（必须是一个 ``QWidget`` 实例）。"""
        return bool(self._mgr.add_tab(self.plugin_id, title, widget))

    def register_exporter(self, name: str,
                          fn: Callable[[StoreView, str], Any]) -> bool:
        """导出器：``fn(store, path)``；返回字符串时宿主负责写入该路径。"""
        return bool(self._mgr.add_exporter(self.plugin_id, name, fn))

    # ---- 主题 ----
    def register_theme(self, theme: Any) -> bool:
        """注册一套界面主题（``ThemeSpec`` 或同字段的字典/对象）。

        颜色不全、非十六进制、id 重复都会当场拒掉（不猜不修正）；拒掉会记错误。
        """
        return bool(self._mgr.add_theme(self.plugin_id, theme))

    def themes(self) -> list[dict[str, Any]]:
        """全部已注册主题（含别的插件和自己的）。"""
        return [t.as_dict() for t in self._mgr.themes()]

    def apply_theme(self, theme_id: str) -> bool:
        """应用主题： ``theme_id`` 可以是自己的 id、内置名（light/dark/system），
        或完整值 ``plugin:<pid>:<id>``。失败返回 False（宿主会回退默认主题）。"""
        return bool(self._mgr.apply_theme_from_plugin(self.plugin_id, str(theme_id)))


#: 给插件用的便捷别名（不要从内部模块 import）
__all__ = [
    "API_VERSION", "MAX_PLUGIN_ERRORS", "ControlPort", "EnvelopeData", "MapLayerPlugin",
    "PlotSelection", "PluginContext", "PluginSignals", "SeriesData", "SettingsView",
    "StoreView", "SyntheticChannel", "ThemeSpec", "empty_store_view", "frozen_array",
]
