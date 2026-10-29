"""apmlog.plugins —— 插件机制的公开入口。

插件作者只该 import 这个包（不要 import ``manager`` / ``api`` 的内部细节）::

    from apmlog.plugins import Preset, LaneRequest, PluginContext

    PLUGIN_NAME = "示例：起飞重量估算"
    __version__ = "1.0"

    def register(ctx: PluginContext) -> None:
        ...
"""
from __future__ import annotations

from .api import (API_VERSION, MAX_PLUGIN_ERRORS, ControlPort, EnvelopeData, MapLayerPlugin,
                  PlotSelection, PluginContext, PluginSignals, SeriesData, SettingsView,
                  StoreView, SyntheticChannel, ThemeSpec)
from .manager import (ENV_PLUGIN_DIRS, PluginInfo, PluginManager, STATUS_CN,
                      default_plugin_dir, discover_dirs)
from ..ui.presets import LanePair, LaneRequest, Preset

__all__ = [
    "API_VERSION", "MAX_PLUGIN_ERRORS", "ControlPort", "ENV_PLUGIN_DIRS", "EnvelopeData",
    "LanePair", "LaneRequest", "MapLayerPlugin", "PlotSelection", "PluginContext",
    "PluginInfo", "PluginManager", "PluginSignals", "Preset", "STATUS_CN", "SeriesData",
    "SettingsView", "StoreView", "SyntheticChannel", "ThemeSpec", "default_plugin_dir",
    "discover_dirs",
]
