"""示例插件 —— 把七类挂载点各用一次，可以照抄。

安装：把本文件拷到 ``%LOCALAPPDATA%\\APMLogViewer\\plugins\\``（或「插件」页里
「添加目录…」指定的目录），然后在主窗点「插件 → 重新扫描并重载」。不用重编 exe。

约定：
* 文件名（或包目录名）就是插件 id；以 ``_`` 或 ``.`` 开头的会被跳过；
* 必须提供 ``register(ctx)``；初始化抛异常只会停用本插件；
* 想读日志数据用 ``ctx.store``（只读快照），想改界面用 ``ctx.control``；
* 想收宿主事件用 ``ctx.signals``。
"""
from __future__ import annotations

PLUGIN_NAME = "示例：动能与航段摘要"
__version__ = "1.0"

import numpy as np
from PySide6 import QtWidgets

from apmlog.plugins import LaneRequest, Preset


def register(ctx) -> None:
    log = ctx.log

    # ---- 1) 虚拟/派生通道：动能 ½·m·v²（m 用参数取，缺参数就退回 1.5 kg） ----
    def kinetic(store):
        try:
            s = store.series("GPS.Spd", max_points=20000)     # m/s
        except KeyError:
            s = store.series("VIBE.VibeX")                    # 拿不到速度就画个占位
        mass = store.param("PLUGIN_MASS_KG", 1.5) or 1.5
        return s.t, 0.5 * float(mass) * np.asarray(s.v) ** 2

    ctx.register_synthetic_channel("SYN.KE", kinetic, label="动能", unit="J")

    # ---- 2) 预设视图：把合成通道和真实通道放同一页 ----
    ctx.register_preset(Preset("demo_energy", "插件：动能与速度",
                               [LaneRequest("SYN", ["KE"]),
                                LaneRequest("GPS", ["Spd"])],
                               note="动能按 PLUGIN_MASS_KG 计算（缺省 1.5 kg）"))

    # ---- 3) 告警规则：动能峰值超过阈值 ----
    def energy_rule(store):
        try:
            k = ctx.store.series("SYN.KE", max_points=20000)
        except (KeyError, RuntimeError):
            return []
        v = np.asarray(k.v)
        if v.size == 0:
            return []
        i = int(np.nanargmax(v))
        if float(v[i]) < 5000.0:
            return []
        return [{"rule": "ke_high", "title": "动能偏高", "severity": "warn",
                 "t0": float(k.t[i]), "t1": float(k.t[i]), "value": float(v[i]),
                 "threshold": 5000.0, "unit": "J",
                 "message": "插件规则：½mv² 峰值超过 5000 J，检查是否超速或超重"}]

    ctx.register_alert_rule(energy_rule)

    # ---- 4) 曲线右键工具：算选中区的均值/峰值 ----
    def stats_tool(inner_ctx, sel):
        lines = [f"区间 {sel.t0:.1f}–{sel.t1:.1f}s（{'整段' if sel.whole else '选区'}）"]
        for key in sel.keys:
            try:
                s = inner_ctx.store.series(key, sel.t0, sel.t1)
            except (KeyError, ValueError):
                lines.append(f"  {key}: 取不到")
                continue
            st = s.stats()
            lines.append(f"  {key}: 均值 {st['mean']:.3f}{s.unit} "
                         f"峰值 {st['max']:.3f}{s.unit} 点 {s.n}")
        return "\n".join(lines)

    ctx.register_plot_tool("统计选中区间", stats_tool)

    # ---- 5) 地图图层：画出 home 点与 30 m 半径圈 ----
    class HomeLayer:
        name = "示例：home 圈"
        color = "#ffb400"

        def render(self, store):
            tracks = store.tracks()
            if not tracks:
                return []
            tr = tracks[0]
            home = tr.get("home")
            if home and home[0] is not None and home[1] is not None:
                lat, lon = float(home[0]), float(home[1])
            else:
                # 没记 home 就用轨迹第一个点（不自造位置，只拿已有数据）
                lats, lons = tr.get("lat"), tr.get("lon")
                if lats is None or len(lats) == 0:
                    return []
                lat, lon = float(lats[0]), float(lons[0])
            if not (abs(lat) <= 90 and abs(lon) <= 180):
                return []
            return [{"kind": "point", "lat": lat, "lon": lon, "label": "home"},
                    {"kind": "circle", "lat": lat, "lon": lon, "radius_m": 30.0}]

    ctx.register_map_layer(HomeLayer())

    # ---- 6) 导出器：一份纯文本摘要 ----
    def summary_export(store, path):
        s = store.stats()
        modes = store.modes()
        lines = [f"日志 {store.name()}",
                 f"固件 {s.get('firmware')}｜机型 {s.get('frame_type')}",
                 f"时长 {float(s.get('duration') or 0):.1f} s｜报文 {s.get('n_messages')}"]
        lines += [f"  模式 {m['mode']} {m['t0']:.1f}–{m['t1']:.1f}s" for m in modes]
        return "\n".join(lines)

    ctx.register_exporter("航段摘要", summary_export)

    # ---- 7) 独立标签页：一个只读的插件页 ----
    page = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(page)
    lay.addWidget(QtWidgets.QLabel(f"这是插件页（宿主 {ctx.host_version}，"
                                   f"接口版本 {ctx.api_version}）"))
    ctx.register_tab("示例插件", page)

    # ---- 事件订阅：日志加载完在插件页上写一行 ----
    def on_loaded(info):
        try:
            lay.addWidget(QtWidgets.QLabel(f"已加载 {info.get('name')}"))
        except RuntimeError:
            log.debug("插件页已销毁，忽略")

    ctx.signals.log_loaded.connect(on_loaded)
    log.info("示例插件已注册：%s", PLUGIN_NAME)
