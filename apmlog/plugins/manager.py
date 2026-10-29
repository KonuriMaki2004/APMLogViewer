"""apmlog.plugins.manager —— 外部插件目录发现、加载、沙箱与注册表。

设计要点（契约 §25）：

* **免安装**：插件从 ``%LOCALAPPDATA%\\APMLogViewer\\plugins``（以及设置/环境变量里
  追加的目录）按目录扫描，不需要重编 exe；每个 ``.py`` 或包目录（含 ``__init__.py``）
  就是一个插件，入口固定是 ``register(ctx)``。
* **异常隔离**：插件初始化失败 → 记下 traceback 并停用该插件；插件回调抛异常 →
  记一次错误、本次调用当作"没有结果"，同一个插件累计 ``MAX_PLUGIN_ERRORS`` 次自动停用。
  主程序永远不因插件失败而退出或黑屏。
* **只读**：发给插件的是 ``StoreView``（纯数据快照），不是 ``LogStore``。
* **可回滚**：``disable()`` 会把该插件注册过的挂载点全部摘掉，界面立刻回到没有它的样子。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np
from PySide6 import QtCore, QtWidgets

from apmlog import __version__
from apmlog.core import reader as _rd
from apmlog.core.model import AlertHit, ChannelInfo, ChannelKey, Series, severity_rank
from apmlog.core.store import envelope_from
from apmlog.ui import theme as ui_theme
from apmlog.ui.settings import Settings

from .api import (API_VERSION, MAX_PLUGIN_ERRORS, ControlPort, PluginContext, PluginSignals,
                  SeriesData, SettingsView, StoreView, SyntheticChannel, ThemeSpec,
                  empty_store_view, frozen_array)

log = logging.getLogger(__name__)

#: 追加插件目录的环境变量（多个目录用 ``os.pathsep`` 分隔）
ENV_PLUGIN_DIRS = "APMLOG_PLUGIN_DIRS"

#: guard 失败时返回的哨兵（调用方据此判断"插件这条路没有结果"）
FAILED: Any = object()

#: 单个图层的图元上限 / 单条折线的点上限（防止插件把地图拖死）
MAX_LAYER_PRIMS = 400
MAX_LINE_POINTS = 5000
MAX_LAYERS = 50


def default_plugin_dir() -> Path:
    """默认外部插件目录（与 settings.json / cache 同根）。"""
    return _rd.cache_root().parent / "plugins"


def _parse_dirs(raw: Any) -> list[str]:
    """设置值 → 目录列表。接受 JSON 数组，也接受 ``;`` / ``os.pathsep`` 分隔的字符串。"""
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    parts: list[str] = []
    for sep in (os.pathsep, ";"):
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            break
    return parts or [text]


def discover_dirs() -> list[Path]:
    """按"默认目录 → 设置里的追加目录 → 环境变量"汇总，去重保序。"""
    out: list[Path] = [default_plugin_dir()]
    try:
        out += [Path(p) for p in _parse_dirs(Settings.instance().get("plugin_dirs"))]
    except Exception:                                    # noqa: BLE001 设置读失败不影响插件
        log.debug("读取 plugin_dirs 失败", exc_info=True)
    env = os.environ.get(ENV_PLUGIN_DIRS, "")
    if env:
        out += [Path(p) for p in env.split(os.pathsep) if p.strip()]
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        try:
            k = str(p.expanduser().resolve())
        except OSError:
            k = str(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(Path(k))
    return uniq


# --------------------------------------------------------------------------- #
# 插件信息
# --------------------------------------------------------------------------- #
@dataclass
class PluginInfo:
    """一个插件的加载状态。``status`` ∈ loaded / failed / disabled。"""

    id: str
    name: str = ""
    version: str = ""
    path: str = ""
    status: str = "loaded"
    error: str = ""
    traceback_text: str = ""
    errors: int = 0
    provides: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "loaded"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name or self.id, "version": self.version,
                "path": self.path, "status": self.status, "error": self.error,
                "errors": self.errors, "provides": list(self.provides)}


#: 状态中文名（界面直接显示，不在 UI 层另造一套词）
STATUS_CN = {"loaded": "已启用", "failed": "加载失败", "disabled": "已停用"}


# --------------------------------------------------------------------------- #
# 通道源：真通道 + 插件合成通道
# --------------------------------------------------------------------------- #
class ChannelSource:
    """给 ``build_lanes`` / ``_spec_for_key`` 用的通道源。

    外形与 ``LogStore`` 的读接口兼容（channel_infos / series / track / meta），
    区别是它认得插件注册的合成通道 —— 这样插件预设里也能引用自己算出来的通道。
    """

    def __init__(self, manager: "PluginManager", store: Any) -> None:
        self._mgr = manager
        self._store = store

    # 元信息 --------------------------------------------------------------- #
    @property
    def meta(self) -> dict[str, Any]:
        return dict(getattr(self._store, "meta", {}) or {})

    def types(self) -> list[str]:
        base = list(self._store.types()) if self._store is not None else []
        extra = sorted({ChannelKey.parse(s.key).type for _p, s in self._mgr.synthetics()})
        return sorted(set(base) | set(extra))

    def track(self, key: Optional[str] = None) -> Any:
        return self._store.track(key) if self._store is not None else None

    def modes(self) -> list[tuple[float, float, str]]:
        return self._store.modes() if self._store is not None else []

    def params(self) -> dict[str, tuple[float, Optional[float]]]:
        return self._store.params() if self._store is not None else {}

    def events(self, *a: Any, **kw: Any) -> list[Any]:
        return self._store.events(*a, **kw) if self._store is not None else []

    def envelope(self, key: Any, t0: Optional[float] = None, t1: Optional[float] = None,
                 buckets: int = 2000) -> Any:
        """包络。合成通道用同一套分桶算法（缺了它 build_lanes 会把整道当异常跳过）。"""
        text = key.key if isinstance(key, ChannelKey) else str(key)
        if self._mgr.synthetic_for(text) is not None:
            s = self.series(text, t0, t1)
            return envelope_from(s.t, s.v, s.key, s.unit, t0, t1, buckets)
        if self._store is None:
            raise KeyError(f"还没有打开日志，取不到 {text}")
        return self._store.envelope(key, t0, t1, buckets)

    # 通道 ---------------------------------------------------------------- #
    def channel_infos(self, type_prefix: Optional[str] = None) -> list[ChannelInfo]:
        out = list(self._store.channel_infos(type_prefix)) if self._store is not None else []
        for _pid, syn in self._mgr.synthetics():
            k = ChannelKey.parse(syn.key)
            if type_prefix and not k.type.upper().startswith(str(type_prefix).upper()):
                continue
            out.append(ChannelInfo(k, "f8", self._mgr.synthetic_count(syn), self._mgr.log_span()[0],
                                   self._mgr.log_span()[1], math.nan, math.nan, syn.unit, "插件通道"))
        return out

    def series(self, key: Any, t0: Optional[float] = None, t1: Optional[float] = None,
               max_points: Optional[int] = None, drop_nan: bool = False) -> Series:
        text = key.key if isinstance(key, ChannelKey) else str(key)
        syn = self._mgr.synthetic_for(text)
        if syn is not None:
            data = self._mgr.synthetic_series(syn, t0, t1, max_points, drop_nan)
            if data is None:
                raise KeyError(f"插件通道 {text} 取不到数据")
            k = ChannelKey.parse(syn.key)
            return Series(k, np.array(data.t, copy=True), np.array(data.v, copy=True),
                          data.unit or syn.unit, syn.display())
        if self._store is None:
            raise KeyError(f"还没有打开日志，取不到 {text}")
        return self._store.series(key, t0, t1, max_points, drop_nan)

    def series_many(self, keys: Sequence[Any], t0: Optional[float] = None,
                    t1: Optional[float] = None,
                    max_points: Optional[int] = None) -> dict[ChannelKey, Series]:
        out: dict[ChannelKey, Series] = {}
        for k in keys:
            try:
                kk = k if isinstance(k, ChannelKey) else ChannelKey.parse(str(k))
                out[kk] = self.series(kk, t0, t1, max_points)
            except KeyError:
                continue
        return out


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
class PluginManager(QtCore.QObject):
    """插件的加载、沙箱调用与注册表。主窗持有唯一实例。"""

    #: 注册表发生变化（加载/停用/重载）——界面据此同步预设框、标签页、地图图层
    changed = QtCore.Signal()
    #: 插件出错：``(plugin_id, 一句话说明)``，界面用它更新状态栏
    pluginError = QtCore.Signal(str, str)

    def __init__(self, host: Any, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._host = host
        self.signals = PluginSignals(self)
        self._infos: dict[str, PluginInfo] = {}
        self._modules: dict[str, Any] = {}
        # 注册表：每项都记着"谁注册的"，停用时按 owner 摘除
        self._alert_rules: list[tuple[str, Callable[..., Any]]] = []
        self._synthetics: list[tuple[str, SyntheticChannel]] = []
        self._presets: list[tuple[str, Any]] = []
        self._plot_tools: list[tuple[str, str, Callable[..., Any]]] = []
        self._map_layers: list[tuple[str, Any]] = []
        self._tabs: list[tuple[str, str, QtWidgets.QWidget]] = []
        self._exporters: list[tuple[str, str, Callable[..., Any]]] = []
        self._themes: list[tuple[str, ThemeSpec]] = []
        self._syn_count: dict[tuple[str, str], int] = {}
        self._theme_rejects: dict[str, int] = {}
        self._loaded_once = False
        ops = self._control_ops()
        self.control = ControlPort(ops)

    # ---------------------------------------------------------------- 宿主桥
    def host_version(self) -> str:
        return f"APMLogViewer {__version__}"

    def _control_ops(self) -> dict[str, Callable[..., Any]]:
        """把宿主的方法包一层：既做类型收敛，也做异常隔离。"""

        def call(name: str, *args: Any) -> Any:
            fn = getattr(self._host, name, None)
            if fn is None:
                return False
            try:
                return fn(*args)
            except Exception as exc:                      # noqa: BLE001 控制口失败不能炸插件宿主
                log.exception("插件控制口 %s 调用失败", name)
                self._emit_error(getattr(self, "_registering", "") or "host",
                                 f"控制口 {name} 失败：{type(exc).__name__}: {exc}")
                return False

        def status(text: str) -> None:
            fn = getattr(self._host, "set_status", None)
            if fn is not None:
                try:
                    fn(f"插件：{text}", temporary=True)
                except Exception:                        # noqa: BLE001
                    log.debug("状态栏写入失败", exc_info=True)

        return {
            "open_log": lambda p: call("plugin_open_log", p),
            "seek": lambda t: call("plugin_seek", t),
            "zoom": lambda a, b: call("plugin_zoom", a, b),
            "play": lambda on: call("plugin_play", on),
            "set_preset": lambda pid: call("plugin_set_preset", pid),
            "add_channel": lambda key: call("plugin_add_channel", key),
            "select_tab": lambda name: call("plugin_select_tab", name),
            "status": status,
            "refresh": lambda: call("plugin_refresh"),
            "export_png": lambda p: call("plugin_export_png", p),
        }

    # ---------------------------------------------------------------- 只读视图
    def current_store_view(self) -> StoreView:
        store = getattr(self._host, "store", None)
        if store is None:
            return empty_store_view()
        return StoreView({
            "available": lambda: True,
            "name": lambda: store.name,
            "path": lambda: str(store.path),
            "meta": lambda: json.loads(json.dumps(_jsonable(store.meta), default=str)),
            "stats": lambda: _stats_dict(store),
            "types": lambda: list(store.types()),
            "has_type": lambda t: t in set(store.types()),
            "channels": lambda prefix=None: [_chan_dict(c) for c in store.channel_infos(prefix)],
            "channel_range": lambda key: {k: float(v) for k, v in
                                          store.channel_range(key).items()
                                          if isinstance(v, (int, float))},
            "series": lambda key, t0, t1, mp, dn: _series_data(store.series(key, t0, t1, mp, dn)),
            "envelope": lambda key, t0, t1, nb: _envelope_data(store.envelope(key, t0, t1, nb)),
            "events": lambda kinds, sev, t0, t1: [e.as_dict() for e in
                                                  store.events(kinds, sev, t0, t1)],
            "params": lambda: {str(k): {"value": float(v[0]), "default": v[1]}
                               for k, v in store.params().items()},
            "param": lambda name, d=None: store.param(name, d),
            "modes": lambda: [{"t0": float(a), "t1": float(b), "mode": str(m)}
                              for a, b, m in store.modes()],
            "tracks": lambda: [_track_dict(t) for t in store.tracks()]
                              if hasattr(store, "tracks") else [_track_dict(store.track())],
            "track": lambda key=None: _track_dict(store.track(key)),
        })

    def settings_view(self) -> SettingsView:
        st = Settings.instance()

        def getter(key: str, default: Any) -> Any:
            try:
                v = st.get(key)
            except Exception:                            # noqa: BLE001
                return default
            return default if v is None else v

        def items() -> dict[str, Any]:
            try:
                fn = getattr(st, "as_dict", None)
                return dict(fn()) if callable(fn) else {}
            except Exception:                            # noqa: BLE001
                return {}

        return SettingsView(getter, items)

    def log_span(self) -> tuple[float, float]:
        store = getattr(self._host, "store", None)
        if store is None:
            return (math.nan, math.nan)
        try:
            return (float(store.meta.get("stats", {}).get("t_start", math.nan)),
                    float(store.meta.get("t_end", math.nan)))
        except Exception:                                # noqa: BLE001
            return (math.nan, math.nan)

    def channel_source(self, store: Any) -> ChannelSource:
        """给预设/通道页用的通道源（真通道 + 合成通道）。"""
        return ChannelSource(self, store)

    # ---------------------------------------------------------------- 加载
    def plugins(self) -> list[PluginInfo]:
        return [self._infos[k] for k in sorted(self._infos)]

    def dirs(self) -> list[Path]:
        return discover_dirs()

    def ensure_default_dir(self) -> Path:
        """默认插件目录不存在就建一个空目录（用户知道该往哪放）。"""
        d = default_plugin_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.debug("创建插件目录失败: %s", d, exc_info=True)
        return d

    def load_all(self) -> None:
        """清空后重新扫描全部目录。单个插件失败只影响它自己。"""
        self._infos.clear()
        self._modules.clear()
        self._clear_registry()
        self._syn_count.clear()
        self._theme_rejects.clear()
        self.ensure_default_dir()
        for d in self.dirs():
            for cand in self._candidates(d):
                self._load_candidate(cand)
        self._loaded_once = True
        self.changed.emit()
        self.signals.plugins_changed.emit([p.as_dict() for p in self.plugins()])

    def _candidates(self, d: Path) -> list[Path]:
        out: list[Path] = []
        try:
            if not d.is_dir():
                return out
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            log.debug("插件目录不可读: %s", d, exc_info=True)
            return out
        for e in entries:
            if e.name.startswith("_") or e.name.startswith("."):
                continue
            if e.is_file() and e.suffix.lower() == ".py":
                out.append(e)
            elif e.is_dir() and ((e / "__init__.py").exists() or (e / "plugin.py").exists()):
                out.append(e)
        return out

    def _load_candidate(self, path: Path) -> None:
        pid = path.stem if path.is_file() else path.name
        pid = re.sub(r"[^0-9A-Za-z_.-]", "_", pid) or "plugin"
        if pid in self._infos and self._infos[pid].status == "loaded":
            return
        info = PluginInfo(id=pid, name=pid, path=str(path))
        try:
            mod = self._import_module(pid, path)
            info.name = str(getattr(mod, "PLUGIN_NAME", "") or pid)
            info.version = str(getattr(mod, "__version__", "") or
                               getattr(mod, "VERSION", "") or "")
            entry = getattr(mod, "register", None)
            if not callable(entry):
                raise TypeError("插件缺少 register(ctx) 入口")
            self._modules[pid] = mod
            ctx = PluginContext(self, pid)
            self._registering = pid
            try:
                entry(ctx)
            finally:
                self._registering = ""
            info.provides = self._provides_of(pid)
            if self._theme_rejects.get(pid) and not info.provides:
                # 只提供主题、且一个都没通过校验 → 这个插件没有任何可用产出，直接判失败
                raise ValueError(f"提供的 {self._theme_rejects[pid]} 个主题全部不受支持")
        except KeyboardInterrupt:                        # pragma: no cover
            raise
        except BaseException as exc:                     # noqa: BLE001 插件初始化异常一律吞掉并记录
            self._modules.pop(pid, None)
            sys.modules.pop(self._mod_name(pid), None)
            self._drop(pid)
            info.status = "failed"
            info.error = f"{type(exc).__name__}: {exc}"
            info.traceback_text = traceback.format_exc()
            log.error("插件 %s 加载失败: %s", pid, info.error, exc_info=True)
            self._emit_error(pid, f"加载失败：{info.error}")
        self._infos[pid] = info

    def _mod_name(self, pid: str) -> str:
        return "_apmlog_plugin_" + re.sub(r"[^0-9A-Za-z_]", "_", pid)

    def _import_module(self, pid: str, path: Path) -> Any:
        name = self._mod_name(pid)
        sys.modules.pop(name, None)
        if path.is_file():
            spec = importlib.util.spec_from_file_location(name, path)
        else:
            init = path / "__init__.py"
            entry = init if init.exists() else (path / "plugin.py")
            spec = importlib.util.spec_from_file_location(
                name, entry, submodule_search_locations=[str(path)] if init.exists() else None)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 建立模块规格")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return mod

    # ---------------------------------------------------------------- 启停
    def disable(self, pid: str, reason: str = "手动停用") -> bool:
        info = self._infos.get(pid)
        if info is None:
            return False
        info.status = "disabled"
        info.error = reason
        self._drop(pid)
        mod = self._modules.pop(pid, None)
        if mod is not None:
            sys.modules.pop(self._mod_name(pid), None)
        log.info("插件 %s 已停用：%s", pid, reason)
        self.changed.emit()
        self.signals.plugins_changed.emit([p.as_dict() for p in self.plugins()])
        return True

    def enable(self, pid: str) -> bool:
        """重新加载单个插件（先摘掉旧贡献，再按原路径重来一次）。"""
        info = self._infos.get(pid)
        if info is None:
            return False
        path = Path(info.path)
        self._drop(pid)
        self._infos.pop(pid, None)
        olds = dict(self._infos)
        self._load_candidate(path)
        self._infos = {**olds, **self._infos}
        self.changed.emit()
        self.signals.plugins_changed.emit([p.as_dict() for p in self.plugins()])
        return self._infos.get(pid, info).ok

    def reload(self) -> None:
        self.load_all()

    # ---------------------------------------------------------------- 沙箱
    _registering: str = ""

    def _emit_error(self, pid: str, text: str) -> None:
        try:
            self.pluginError.emit(str(pid), str(text))
        except Exception:                                # noqa: BLE001
            log.debug("pluginError 信号发送失败", exc_info=True)

    def guard(self, pid: str, what: str, fn: Callable[..., Any], *args: Any,
              **kwargs: Any) -> Any:
        """在沙箱里调用插件代码：异常只记账，不往上抛。"""
        try:
            return fn(*args, **kwargs)
        except KeyboardInterrupt:                        # pragma: no cover
            raise
        except BaseException as exc:                     # noqa: BLE001 插件异常不许拖垮主程序
            self._note_error(pid, what, exc)
            return FAILED

    def _note_error(self, pid: str, what: str, exc: BaseException) -> None:
        info = self._infos.get(pid)
        text = f"{type(exc).__name__}: {exc}"
        log.error("插件 %s 在 %s 中出错: %s", pid or "?", what, text, exc_info=True)
        if info is None:
            self._emit_error(pid, f"{what} 出错：{text}")
            return
        info.errors += 1
        info.error = f"{what} 出错：{text}"
        info.traceback_text = traceback.format_exc()
        self._emit_error(pid, info.error)
        if info.errors >= MAX_PLUGIN_ERRORS and info.status == "loaded":
            self.disable(pid, f"连续 {info.errors} 次异常，已自动停用（{what}）")

    # ---------------------------------------------------------------- 注册表
    def _drop(self, pid: str) -> None:
        self._alert_rules = [x for x in self._alert_rules if x[0] != pid]
        self._synthetics = [x for x in self._synthetics if x[0] != pid]
        self._presets = [x for x in self._presets if x[0] != pid]
        self._plot_tools = [x for x in self._plot_tools if x[0] != pid]
        self._map_layers = [x for x in self._map_layers if x[0] != pid]
        self._tabs = [x for x in self._tabs if x[0] != pid]
        self._exporters = [x for x in self._exporters if x[0] != pid]
        self._themes = [x for x in self._themes if x[0] != pid]

    def _clear_registry(self) -> None:
        for pid in list(self._infos):
            self._drop(pid)
        self._alert_rules.clear()
        self._synthetics.clear()
        self._presets.clear()
        self._plot_tools.clear()
        self._map_layers.clear()
        self._tabs.clear()
        self._exporters.clear()
        self._themes.clear()

    def _provides_of(self, pid: str) -> list[str]:
        out: list[str] = []
        if any(p == pid for p, _ in self._alert_rules):
            out.append("告警规则")
        if any(p == pid for p, _ in self._synthetics):
            out.append("合成通道")
        if any(p == pid for p, _ in self._presets):
            out.append("预设视图")
        if any(p == pid for p, _n, _f in self._plot_tools):
            out.append("曲线工具")
        if any(p == pid for p, _ in self._map_layers):
            out.append("地图图层")
        if any(p == pid for p, _t, _w in self._tabs):
            out.append("标签页")
        if any(p == pid for p, _n, _f in self._exporters):
            out.append("导出器")
        if any(p == pid for p, _t in self._themes):
            out.append("主题")
        return out

    def _refresh_provides(self) -> None:
        for pid, info in self._infos.items():
            info.provides = self._provides_of(pid)

    # 告警规则 ------------------------------------------------------------- #
    def add_alert_rule(self, pid: str, fn: Callable[..., Any]) -> bool:
        if not callable(fn):
            self._emit_error(pid, "register_alert_rule 需要可调用对象")
            return False
        if any(p == pid and f is fn for p, f in self._alert_rules):
            return True
        self._alert_rules.append((pid, fn))
        self._refresh_provides()
        return True

    def alert_rules(self) -> list[tuple[str, Callable[..., Any]]]:
        return list(self._alert_rules)

    def run_alert_rules(self) -> list[AlertHit]:
        """跑全部插件规则并归一成 ``AlertHit``；单个规则失败只丢它自己的结果。"""
        view = self.current_store_view()
        if not view.available:
            return []
        out: list[AlertHit] = []
        for pid, fn in list(self._alert_rules):
            res = self.guard(pid, "告警规则", fn, view)
            if res is FAILED or not res:
                continue
            for item in _as_list(res):
                hit = _alert_from_dict(item, pid)
                if hit is not None:
                    out.append(hit)
        return out

    # 合成通道 ------------------------------------------------------------- #
    def add_synthetic(self, pid: str, key: str, fn: Callable[..., Any], *, label: str = "",
                      unit: str = "") -> bool:
        try:
            k = ChannelKey.parse(str(key))
        except ValueError as exc:
            self._emit_error(pid, f"合成通道键不合法：{key!r}（{exc}）")
            return False
        if not callable(fn):
            self._emit_error(pid, f"合成通道 {key} 缺少计算函数")
            return False
        if self.synthetic_for(k.key) is not None:
            self._emit_error(pid, f"合成通道 {k.key} 已被占用")
            return False
        self._synthetics.append((pid, SyntheticChannel(key=k.key, fn=fn,
                                                       label=label or k.label, unit=unit,
                                                       plugin=pid)))
        self._syn_count.clear()
        self._refresh_provides()
        return True

    def synthetics(self) -> list[tuple[str, SyntheticChannel]]:
        return list(self._synthetics)

    def synthetic_for(self, key: str) -> Optional[SyntheticChannel]:
        k = str(key).strip()
        for _pid, syn in self._synthetics:
            if syn.key == k:
                return syn
            try:
                if ChannelKey.parse(syn.key) == ChannelKey.parse(k):
                    return syn
            except ValueError:
                continue
        return None

    def synthetic_series(self, syn: SyntheticChannel, t0: Optional[float] = None,
                         t1: Optional[float] = None, max_points: Optional[int] = None,
                         drop_nan: bool = False) -> Optional[SeriesData]:
        """算一条合成通道（沙箱内）。失败/形状不对 → None，绝不返回编造数据。"""
        view = self.current_store_view()
        if not view.available:
            return None
        res = self.guard(syn.plugin or "?", f"合成通道 {syn.key}", syn.fn, view)
        if res is FAILED or res is None:
            return None
        t, v = _split_series(res)
        if t is None or v is None:
            self._note_error(syn.plugin or "?", f"合成通道 {syn.key}",
                             TypeError("返回值必须是 (t, v) 或 SeriesData"))
            return None
        t = np.asarray(t, dtype=np.float64)
        v = np.asarray(v, dtype=np.float64)
        if t.size != v.size:
            self._note_error(syn.plugin or "?", f"合成通道 {syn.key}",
                             ValueError(f"t/v 长度不一致 {t.size}!={v.size}"))
            return None
        lo = -math.inf if t0 is None else float(t0)
        hi = math.inf if t1 is None else float(t1)
        m = (t >= lo) & (t <= hi)
        t, v = t[m], v[m]
        if drop_nan and v.size:
            f = np.isfinite(v)
            t, v = t[f], v[f]
        if max_points and t.size > int(max_points):
            idx = np.unique(np.linspace(0, t.size - 1, int(max_points)).round().astype(int))
            t, v = t[idx], v[idx]
        return SeriesData(key=syn.key, label=syn.display(), unit=syn.unit, t=t, v=v)

    def synthetic_count(self, syn: SyntheticChannel) -> int:
        """通道树上显示的点数（算一次就缓存，别在树里反复算重活）。"""
        store = getattr(self._host, "store", None)
        ck = (syn.key, str(getattr(store, "path", "")))
        if ck in self._syn_count:
            return self._syn_count[ck]
        data = self.synthetic_series(syn, max_points=20000)
        n = 0 if data is None else int(data.n)
        self._syn_count[ck] = n
        return n

    # 预设 --------------------------------------------------------------- #
    def add_preset(self, pid: str, preset: Any) -> bool:
        for attr in ("id", "name", "lanes"):
            if not hasattr(preset, attr):
                self._emit_error(pid, f"预设缺少字段 {attr}")
                return False
        if not str(getattr(preset, "id", "")).strip():
            self._emit_error(pid, "预设 id 不能为空")
            return False
        if any(str(getattr(p, "id", "")) == str(preset.id) for _q, p in self._presets):
            self._emit_error(pid, f"预设 id 重复：{preset.id}")
            return False
        self._presets.append((pid, preset))
        self._refresh_provides()
        return True

    def presets(self) -> list[tuple[str, Any]]:
        return list(self._presets)

    def preset_by_id(self, preset_id: str) -> Any:
        for _pid, p in self._presets:
            if str(getattr(p, "id", "")) == str(preset_id):
                return p
        return None

    # 曲线工具 ----------------------------------------------------------- #
    def add_plot_tool(self, pid: str, name: str, fn: Callable[..., Any]) -> bool:
        if not callable(fn):
            self._emit_error(pid, f"曲线工具 {name} 需要可调用对象")
            return False
        text = str(name or "").strip()
        if not text:
            self._emit_error(pid, "曲线工具名不能为空")
            return False
        self._plot_tools.append((pid, text, fn))
        self._refresh_provides()
        return True

    def plot_tools(self) -> list[tuple[str, str, Callable[..., Any]]]:
        return list(self._plot_tools)

    def run_plot_tool(self, pid: str, fn: Callable[..., Any], sel: Any) -> Any:
        ctx = PluginContext(self, pid)
        return self.guard(pid, "曲线工具", fn, ctx, sel)

    # 地图图层 ----------------------------------------------------------- #
    def add_map_layer(self, pid: str, layer: Any) -> bool:
        if not callable(getattr(layer, "render", None)):
            self._emit_error(pid, "地图图层必须实现 render(store)")
            return False
        if len(self._map_layers) >= MAX_LAYERS:
            self._emit_error(pid, f"地图图层超过 {MAX_LAYERS} 个，已忽略")
            return False
        self._map_layers.append((pid, layer))
        self._refresh_provides()
        return True

    def map_layers(self) -> list[tuple[str, Any]]:
        return list(self._map_layers)

    def map_layer_primitives(self) -> list[dict[str, Any]]:
        """跑全部图层并把图元清洗成宿主敢画的样子（颜色/坐标/点数都校验）。"""
        view = self.current_store_view()
        if not view.available:
            return []
        out: list[dict[str, Any]] = []
        for pid, layer in list(self._map_layers):
            if not bool(getattr(layer, "visible", True)):
                continue
            name = str(getattr(layer, "name", "") or "插件图层")
            color = _clean_color(getattr(layer, "color", ""), "#2f6fed")
            res = self.guard(pid, f"地图图层 {name}", layer.render, view)
            if res is FAILED or not res:
                continue
            prims = _clean_prims(res, color)
            if prims:
                out.append({"name": name, "color": color, "plugin": pid, "prims": prims})
        return out

    # 标签页 ------------------------------------------------------------- #
    def add_tab(self, pid: str, title: str, widget: Any) -> bool:
        if not isinstance(widget, QtWidgets.QWidget):
            self._emit_error(pid, "register_tab 需要传入 QWidget 实例")
            return False
        text = str(title or "").strip()
        if not text:
            self._emit_error(pid, "标签页标题不能为空")
            return False
        self._tabs.append((pid, text, widget))
        self._refresh_provides()
        return True

    def tabs(self) -> list[tuple[str, str, QtWidgets.QWidget]]:
        return list(self._tabs)

    # 导出器 ------------------------------------------------------------- #
    def add_exporter(self, pid: str, name: str, fn: Callable[..., Any]) -> bool:
        if not callable(fn):
            self._emit_error(pid, f"导出器 {name} 需要可调用对象")
            return False
        text = str(name or "").strip()
        if not text:
            self._emit_error(pid, "导出器名不能为空")
            return False
        self._exporters.append((pid, text, fn))
        self._refresh_provides()
        return True

    def exporters(self) -> list[tuple[str, str, Callable[..., Any]]]:
        return list(self._exporters)

    def run_exporter(self, pid: str, name: str, fn: Callable[..., Any],
                     path: str) -> tuple[bool, str]:
        """跑一个导出器：返回 ``(是否成功, 给用户看的一句话)``。"""
        view = self.current_store_view()
        if not view.available:
            return False, "还没有打开日志"
        res = self.guard(pid, f"导出器 {name}", fn, view, str(path))
        if res is FAILED:
            return False, f"导出器「{name}」执行失败，详情见插件页"
        if isinstance(res, str):
            try:
                Path(path).write_text(res, encoding="utf-8")
            except OSError as exc:
                return False, f"写入失败：{exc}"
        if not Path(path).exists():
            return False, f"导出器「{name}」没有生成文件"
        return True, f"已导出 {Path(path).name}"

    # 主题 --------------------------------------------------------------- #
    def add_theme(self, pid: str, theme: Any) -> bool:
        """注册一套界面主题。

        校验口径：id 合法且本插件内不重复；``name`` 非空；四色（window/base/text/accent）
        齐全且均为十六进制；``curve`` 只能是空/light/dark。任一条不满足就**拒掉并记错误**
        （不猜、不自动补色）。拒掉不等于停用插件：它可能还有别的主题或别的扩展。
        """
        tid, name, curve = _theme_fields(theme)
        if not re.fullmatch(r"[0-9A-Za-z_.-]{1,40}", tid or ""):
            self._emit_error(pid, f"主题 id 不合法：{tid!r}（限字母数字 _ . -，≤40）")
            return False
        if not name:
            self._emit_error(pid, f"主题 {tid} 缺少显示名")
            return False
        if curve not in ("", "light", "dark"):
            self._emit_error(pid, f"主题 {tid} 的 curve 只能是空/light/dark，收到 {curve!r}")
            return False
        if any(p == pid and t.id == tid for p, t in self._themes):
            self._emit_error(pid, f"主题 id 重复：{tid}")
            return False
        try:
            colors = ui_theme.spec_from_theme(theme)
        except ValueError as exc:
            self._theme_rejects[pid] = self._theme_rejects.get(pid, 0) + 1
            self._emit_error(pid, f"主题 {tid} 不受支持：{exc}")
            return False
        spec = ThemeSpec(id=tid, name=name, colors=MappingProxyType(dict(colors)), curve=curve)
        self._themes.append((pid, spec))
        self._refresh_provides()
        return True

    def themes(self) -> list[ThemeSpec]:
        return [t for _pid, t in self._themes]

    def theme_owners(self) -> list[tuple[str, ThemeSpec]]:
        return list(self._themes)

    def themes_available(self) -> list[dict[str, Any]]:
        """给界面用：每个主题的显示信息 + 存在设置里的规范值。"""
        return [{"value": ui_theme.theme_value(pid, spec.id), "id": spec.id,
                 "name": spec.name, "plugin": pid, "curve": spec.curve,
                 "colors": dict(spec.colors)}
                for pid, spec in self._themes]

    def theme_by_value(self, value: str) -> Optional[tuple[str, ThemeSpec]]:
        """``plugin:<pid>:<id>`` → (pid, 主题)；找不到或不合法返回 None。"""
        parsed = ui_theme.parse_plugin_theme(value)
        if parsed is None:
            return None
        pid, tid = parsed
        for owner, spec in self._themes:
            if owner == pid and spec.id == tid:
                return owner, spec
        return None

    def resolve_theme_value(self, pid: str, theme_id: str) -> str:
        """插件里的 ``apply_theme("我的主题")`` 也可以直接写 id 或内置名。"""
        text = str(theme_id or "").strip()
        if text in ui_theme.UI_THEMES or ui_theme.is_plugin_theme(text):
            return text
        return ui_theme.theme_value(pid, text)

    def apply_theme(self, value: str) -> bool:
        """应用一个主题值（内置名或插件主题）。失败则停用它的插件并返回 False。

        回退内置默认主题与记日志由宿主负责（它才碰得到 palette）；
        这里只负责把失败归因到具体插件，并按“只停用该主题插件”的要求收尾。
        """
        text = str(value or "")
        owner = self.theme_by_value(text)
        try:
            ok = bool(self._host.plugin_apply_theme(text))
        except Exception as exc:                        # noqa: BLE001 宿主自己抛也必须收住
            log.exception("应用主题失败: %s", text)
            self._emit_error(owner[0] if owner else "host",
                             f"应用主题失败：{type(exc).__name__}: {exc}")
            ok = False
        if not ok and owner is not None:
            self.disable(owner[0], f"主题「{owner[1].name}」应用失败，已回退内置主题")
        return ok

    def apply_theme_from_plugin(self, pid: str, theme_id: str) -> bool:
        return bool(self.apply_theme(self.resolve_theme_value(pid, theme_id)))


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _theme_fields(theme: Any) -> tuple[str, str, str]:
    """从 ThemeSpec / 字典 / 任意对象里取 (id, name, curve)，缺省给空串。"""
    if isinstance(theme, Mapping):
        return (str(theme.get("id") or "").strip(),
                str(theme.get("name") or "").strip(),
                str(theme.get("curve") or "").strip().lower())
    return (str(getattr(theme, "id", "") or "").strip(),
            str(getattr(theme, "name", "") or "").strip(),
            str(getattr(theme, "curve", "") or "").strip().lower())


def _jsonable(obj: Any) -> Any:
    """把 meta 里的 ndarray / 元组之类收敛成可 json 化的结构。"""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return obj


def _stats_dict(store: Any) -> dict[str, Any]:
    from dataclasses import asdict
    try:
        return _jsonable(asdict(store.stats))
    except Exception:                                    # noqa: BLE001
        return {}


def _chan_dict(c: ChannelInfo) -> dict[str, Any]:
    return {"key": c.key.key, "type": c.key.type, "field": c.key.field,
            "inst": c.key.inst, "label": c.key.label, "unit": c.unit,
            "count": int(c.count), "t0": float(c.t0), "t1": float(c.t1),
            "vmin": float(c.vmin), "vmax": float(c.vmax),
            "coltype": str(getattr(c, "coltype", ""))}


def _series_data(s: Series) -> SeriesData:
    return SeriesData(key=s.key.key, label=s.label, unit=s.unit, t=s.t, v=s.v)


def _envelope_data(e: Any) -> Any:
    from .api import EnvelopeData
    return EnvelopeData(key=e.key.key, label=getattr(e, "label", e.key.label), unit=e.unit,
                        t=e.t, vmin=e.vmin, vmax=e.vmax, vmean=e.vmean, count=e.count)


def _track_dict(tr: Any) -> dict[str, Any]:
    if tr is None:
        return {}
    return {
        "key": str(getattr(tr, "key", "") or ""),
        "label": str(getattr(tr, "label", "") or ""),
        "kind": str(getattr(tr, "kind", "")),
        "n_points": int(getattr(tr, "n_points", 0)),
        "distance": float(getattr(tr, "distance", math.nan)),
        "max_speed": float(getattr(tr, "max_speed", math.nan)),
        "home": getattr(tr, "home", None),
        "origin": getattr(tr, "origin", None),
        "src_names": list(getattr(tr, "src_names", []) or []),
        "note": str(getattr(tr, "note", "") or ""),
        "t": frozen_array(getattr(tr, "t", [])),
        "lat": frozen_array(getattr(tr, "lat", [])),
        "lon": frozen_array(getattr(tr, "lon", [])),
        "alt": frozen_array(getattr(tr, "alt", [])),
        "e": frozen_array(getattr(tr, "e", [])),
        "n": frozen_array(getattr(tr, "n", [])),
        "u": frozen_array(getattr(tr, "u", [])),
        "speed": frozen_array(getattr(tr, "speed", [])),
    }


def _as_list(res: Any) -> list[Any]:
    if isinstance(res, dict):
        return [res]
    try:
        return list(res)
    except TypeError:
        return []


_SEVS = {"hint", "warn", "error", "critical", "info"}


def _alert_from_dict(item: Any, pid: str) -> Optional[AlertHit]:
    """把插件给的一条规则结果归一成 AlertHit；字段不合格就丢掉（不猜）。"""
    if not isinstance(item, Mapping):
        return None
    title = str(item.get("title") or item.get("rule") or "").strip()
    if not title:
        return None
    sev = str(item.get("severity") or "warn").lower()
    if sev not in _SEVS:
        sev = "warn"

    def num(key: str) -> float:
        try:
            return float(item.get(key, math.nan))
        except (TypeError, ValueError):
            return math.nan

    return AlertHit(rule=f"plugin:{pid}:{item.get('rule') or title}",
                    title=title, severity=sev, t0=num("t0"), t1=num("t1"),
                    value=num("value"), threshold=num("threshold"),
                    unit=str(item.get("unit") or ""),
                    message=str(item.get("message") or ""),
                    source=f"plugin:{pid}")


def _split_series(res: Any) -> tuple[Optional[Any], Optional[Any]]:
    if isinstance(res, SeriesData):
        return res.t, res.v
    if isinstance(res, tuple) and len(res) == 2:
        return res
    if isinstance(res, Mapping) and "t" in res and "v" in res:
        return res["t"], res["v"]
    return None, None


def _clean_color(value: Any, default: str) -> str:
    s = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        return s.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", s):
        return "#" + "".join(c * 2 for c in s[1:]).lower()
    return default


def _clean_prims(res: Any, default_color: str) -> list[dict[str, Any]]:
    """校验/裁剪插件图元。坐标不合法就丢掉那一个图元，不猜位置。"""
    out: list[dict[str, Any]] = []
    for item in _as_list(res)[:MAX_LAYER_PRIMS]:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "line").lower()
        color = _clean_color(item.get("color"), default_color)
        if kind in ("line", "polyline", "path"):
            pts = []
            for p in list(item.get("points") or [])[:MAX_LINE_POINTS]:
                ll = _ll(p)
                if ll is not None:
                    pts.append(ll)
            if len(pts) >= 2:
                width = item.get("width", 2)
                try:
                    width = max(1, min(12, int(width)))
                except (TypeError, ValueError):
                    width = 2
                out.append({"kind": "line", "points": pts, "color": color,
                            "width": width, "dash": bool(item.get("dash"))})
        elif kind in ("point", "marker"):
            ll = _ll((item.get("lat"), item.get("lon")))
            if ll is not None:
                out.append({"kind": "point", "lat": ll[0], "lon": ll[1], "color": color,
                            "label": str(item.get("label") or "")[:40]})
        elif kind == "circle":
            ll = _ll((item.get("lat"), item.get("lon")))
            try:
                r = float(item.get("radius_m", 0.0))
            except (TypeError, ValueError):
                r = 0.0
            if ll is not None and math.isfinite(r) and 0 < r <= 100000:
                out.append({"kind": "circle", "lat": ll[0], "lon": ll[1],
                            "radius_m": r, "color": color})
    return out


def _ll(obj: Any) -> Optional[tuple[float, float]]:
    try:
        if isinstance(obj, Mapping):
            lat, lon = float(obj.get("lat")), float(obj.get("lon"))
        else:
            lat, lon = float(obj[0]), float(obj[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (lat, lon)


__all__ = ["ChannelSource", "ENV_PLUGIN_DIRS", "FAILED", "PluginInfo", "PluginManager",
           "STATUS_CN", "default_plugin_dir", "discover_dirs"]
