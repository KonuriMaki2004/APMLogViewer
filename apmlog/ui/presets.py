"""apmlog.ui.presets —— 预设视图的通道组合与 LaneSpec 构造。

规则：
* 通道不存在 / 全 nan → 直接跳过该道（不画假线），并在 ``notes`` 里记录原因。
* 多实例类型（VIBE/IMU/BAT…）默认只取**最小实例号**，避免 9 条线糊在一起；
  需要看其它实例时用通道树手动添加。
* 点数超过阈值的通道走包络（min/max + mean），放大后由 SeriesPlot 内部再抽稀。
* ``TRACK.*`` 是从 TrackResult 派生的合成通道（高度/速度/水平距离），不是 DataFlash 报文。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

from ..core.model import ChannelKey
from .seriesplot import OVERLAY_COLORS, LaneSpec

#: 调色板（浅色主题，顺序稳定，同一通道在不同日志里颜色一致）
PALETTE: tuple[tuple[int, int, int], ...] = (
    (31, 119, 180), (214, 39, 40), (44, 160, 44), (227, 119, 194), (188, 189, 34),
    (23, 190, 207), (140, 86, 75), (255, 127, 14), (127, 127, 127), (174, 199, 232),
    (255, 152, 150), (152, 223, 138), (255, 187, 120), (187, 187, 187),
)


@dataclass
class LaneRequest:
    """一条泳道的取数请求。"""
    type: str
    fields: Sequence[str]
    inst: Optional[int] = None              # None=自动选最小实例
    fixed_range: Optional[tuple[float, float]] = None
    threshold: Optional[float] = None
    band: Optional[tuple[float, float]] = None
    style: str = "line"
    label_prefix: str = ""
    hide: Sequence[str] = ()                      # 这些字段初始隐藏（图例牌上一点就开）


@dataclass
class LanePair:
    """一条泳道 + 同轴叠加的对比曲线（目标值/指令值）。

    用户 2026-10-24：「单泳道多曲线同轴叠加」——ATT.Roll 与 ATT.DesRoll 共用一个
    Y 轴坐标系，但**各有独立胶囊**，点谁切谁。叠加曲线的初始显隐由 ``overlays_hidden``
    决定（默认隐藏：先看实际值，要看指令值再点开）。
    """
    type: str
    primary: str
    overlays: Sequence[str] = ()
    inst: Optional[int] = None
    fixed_range: Optional[tuple[float, float]] = None
    threshold: Optional[float] = None
    band: Optional[tuple[float, float]] = None
    style: str = "line"
    label_prefix: str = ""
    overlays_hidden: bool = True
    overlay_labels: Sequence[str] = ()      # 可选：给叠加曲线换显示名（如 "DesRoll"→"目标 Roll"）


@dataclass
class Preset:
    id: str
    name: str
    lanes: list[Any] = field(default_factory=list)   # LaneRequest 或 LanePair
    max_points: int = 20000
    envelope_above: int = 4000               # 超过该点数改用包络
    note: str = ""


def _req(t: str, *fields_: str, **kw: Any) -> LaneRequest:
    return LaneRequest(type=t, fields=list(fields_), **kw)


def _pair(t: str, primary: str, *overlays: str, **kw: Any) -> LanePair:
    return LanePair(type=t, primary=primary, overlays=list(overlays), **kw)


PRESETS: list[Preset] = [
    Preset("position", "位置与高度", [
        _req("TRACK", "alt", "speed"),
        _req("BARO", "Alt", label_prefix="气压"),
        _req("XKF1", "PD", label_prefix="EKF 高度(下正)"),
    ], note="TRACK.alt=相对home高度，来源随位置回退链变化，见地图页的源标注"),
    Preset("motor", "电机与执行器", [
        _req("RCOU", *[f"C{i}" for i in range(1, 9)], fixed_range=(1000, 2000)),
        _req("MOTB", "ThrOut", "ThrAvMx", "LiftMax", fixed_range=(0.0, 1.0)),
        _req("CTUN", "ThO", fixed_range=(0.0, 1.0)),
        _req("MOTB", "FailFlags"),
    ], note="RCOU 为 PWM µs（RCO2 里才是 15-18 路），MOTB 是归一量"),
    Preset("battery", "电量", [
        _req("BAT", "Volt", "VoltR", band=(3.0, 4.5)),
        _req("BAT", "Curr"),
        # RemPct 已退出判定（用户 2026-10-24）：曲线还能看，但不再画 20% 阈值线——
        # 那条线等于暗示"低于它就报警"，而引擎已改成只看单芯电压。
        _req("BAT", "RemPct", fixed_range=(0.0, 100.0)),
        _req("BAT", "Res"),
    ], note="判定只看单芯电压（整包÷电芯数），RemPct 仅作参考曲线，不参与告警"),
    Preset("vibe", "震动", [
        _req("VIBE", "VibeX", "VibeY", "VibeZ"),
        _req("VIBE", "Clip"),
        _req("IMU", "GHz", label_prefix="陀螺高频"),
    ], note="只画最小实例号的轴；换 IMU 实例请在通道树里加 VIBE[n].VibeX"),
    Preset("attitude", "姿态与控制", [
        # 单泳道多曲线：实际值与 Des* 目标值同一根 Y 轴，各有胶囊、单独显隐。
        # 道数从 10 收到 4，每个 Des* 一个开关（默认关，要看指令跟随再点开）。
        # 不再钉死 ±180：个别 -180° 跳变点会把整道压平，改用 1%~99% 稳健量程
        # （双击 Y 轴可切回全量，右键「固定量程…」也能手动钉住）
        _pair("ATT", "Roll", "DesRoll"),
        _pair("ATT", "Pitch", "DesPitch"),
        _pair("ATT", "Yaw", "DesYaw"),
        _pair("RATE", "R", "RDes"),
        _pair("RATE", "P", "PDes"),
    ], note="实际值与 Des* 目标值同轴叠加（虚线＝目标）；Y 轴按 1%~99% 稳衡量程，双击 Y 轴看全量"),
    Preset("gps", "GNSS 与 EKF", [
        _req("GPS", "NSats", "HDop"),
        _req("XKF3", "IPN", "IPE", "IPD", "IVN", "IVE", "IVD"),
        _req("XKF4", "TSF", "SP", "SV"),
    ], note="仅含 GNSS 的日志有 GPS；innovation 单位按 DataFlash 原值"),
]

PRESET_BY_ID = {p.id: p for p in PRESETS}


class PresetError(RuntimeError):
    pass


def _synthetic(store, name: str) -> Optional[tuple[np.ndarray, np.ndarray, str]]:
    """把 TrackResult 的派生量转成 (t, values, unit)。"""
    tr = store.track()
    if tr.n_points == 0:
        return None
    if name == "alt":
        v = tr.alt if tr.has_geodetic and np.isfinite(tr.alt).any() else tr.u
        return tr.t, v, "m"
    if name == "speed":
        return tr.t, tr.speed, "m/s"
    if name == "distance":
        if tr.e.size < 2:
            return None
        step = np.hypot(np.diff(tr.e), np.diff(tr.n))
        return tr.t[1:], np.concatenate([[0.0], np.nancumsum(step)]), "m"
    if name == "heading":
        de, dn = np.diff(tr.e), np.diff(tr.n)
        hd = np.degrees(np.arctan2(de, dn))
        return tr.t[1:], hd, "°"
    return None


#: "空通道"容差：全时段 |v| ≤ 此值即视为恒 0
EMPTY_ATOL = 1e-9


def _blank_channel(c: Any) -> Optional[bool]:
    """按通道元数据判断是否"空通道"（全时段恒 0 / 无有效点）。

    用整份日志的 vmin/vmax（不是当前视图窗口）——否则窗口刚好是静止段就会误判。
    元数据缺失时返回 None，交给调用方按数据再判一次。
    """
    try:
        cnt = int(getattr(c, "count", 0) or 0)
        lo = float(getattr(c, "vmin", float("nan")))
        hi = float(getattr(c, "vmax", float("nan")))
    except (TypeError, ValueError):
        return None
    if cnt <= 0:
        return True
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    return abs(lo) <= EMPTY_ATOL and abs(hi) <= EMPTY_ATOL


def _blank_values(v: Optional[np.ndarray]) -> bool:
    """按实际数值判断空通道（元数据不可用时的兜底）。"""
    if v is None:
        return False
    a = np.asarray(v, np.float64)
    f = a[np.isfinite(a)]
    if f.size == 0:
        return True
    return bool(np.all(np.abs(f) <= EMPTY_ATOL))


def build_lanes(store, preset: "Preset | str", t0: Optional[float] = None,
                t1: Optional[float] = None,
                max_points: Optional[int] = None) -> tuple[list[LaneSpec], list[str]]:
    """按预设取数，返回 (LaneSpec 列表, 跳过原因列表)。

    支持两类泳道描述：
    * ``LaneRequest``：一条泳道一根主曲线（老行为，完全不变）；
    * ``LanePair``：一条泳道 + 若干**同轴叠加**的对比曲线（ATT.Roll 与 ATT.DesRoll）。
    """
    p = PRESET_BY_ID[preset] if isinstance(preset, str) else preset
    if p is None:
        raise PresetError(f"未知预设: {preset}")
    lanes: list[LaneSpec] = []
    skipped: list[str] = []
    mp = int(max_points or p.max_points)
    infos = store.channel_infos()
    have = {(c.key.type, c.key.field): c for c in infos}
    per_type_inst: dict[str, Optional[int]] = {}
    color_i = 0

    def next_color() -> tuple[int, int, int]:
        nonlocal color_i
        c = PALETTE[color_i % len(PALETTE)]
        color_i += 1
        return c

    def pick_inst(t: str, forced: Optional[int]) -> Optional[int]:
        if forced is not None:
            return forced
        if t not in per_type_inst:
            insts = sorted({c.key.inst for c in infos
                            if c.key.type == t and c.key.inst is not None})
            per_type_inst[t] = insts[0] if insts else None
        return per_type_inst[t]

    def make_spec(t: str, f: str, *, forced_inst: Optional[int],
                  fixed_range: Optional[tuple[float, float]] = None,
                  threshold: Optional[float] = None,
                  band: Optional[tuple[float, float]] = None,
                  style: str = "line", prefix: str = "",
                  overlay: bool = False) -> Optional[LaneSpec]:
        """取一条通道 → LaneSpec。取不到就记 skipped 并返回 None。"""
        c = have.get((t, f))
        if c is None:
            skipped.append(f"{t}.{f}（日志无该报文/字段）")
            return None
        inst = forced_inst if c.key.inst is not None else None
        key = ChannelKey(t, f, inst)
        n_rows = int(store.meta.get("types", {}).get(t, {}).get("rows", 0))
        try:
            if n_rows > p.envelope_above:
                env = store.envelope(key, t0, t1, buckets=min(mp, 2400))
                if env.t.size == 0:
                    skipped.append(f"{key.label}（区间内无数据）")
                    return None
                spec = LaneSpec(label=(prefix + key.label), color=next_color(),
                                unit=env.unit, env_t=env.t, env_min=env.vmin, env_max=env.vmax,
                                env_mean=env.vmean)
                blank = _blank_channel(c)
                if blank is None:
                    blank = bool(np.all(np.abs(np.asarray(env.vmin, float)) <= EMPTY_ATOL)
                                 and np.all(np.abs(np.asarray(env.vmax, float)) <= EMPTY_ATOL))
            else:
                sd = store.series(key, t0, t1, max_points=mp, drop_nan=False)
                if sd.n == 0:
                    skipped.append(f"{key.label}（0 点）")
                    return None
                spec = LaneSpec.from_series(sd, next_color())
                spec.label = prefix + key.label
                blank = _blank_channel(c)
                if blank is None:
                    blank = _blank_values(sd.v)
            spec.key = key.key
            spec.y_min = fixed_range[0] if fixed_range else None
            spec.y_max = fixed_range[1] if fixed_range else None
            spec.threshold = threshold
            spec.band = band
            spec.style = style
            if not overlay and blank:
                # 空通道默认不展开：四旋翼的 RCOU.C5~C8 全时段恒 0，展开就是四条平线
                spec.empty = True
                spec.hidden = True
            return spec
        except KeyError as exc:
            skipped.append(f"{key.label}（取数失败: {exc}）")
        except Exception as exc:                        # 单条通道失败不能拖垮整个视图
            skipped.append(f"{key.label}（异常: {type(exc).__name__}: {exc}）")
        return None

    for req in p.lanes:
        # ---- 同轴叠加：一条泳道 + 若干对比曲线 ----
        if isinstance(req, LanePair):
            inst = pick_inst(req.type, req.inst)
            spec = make_spec(req.type, req.primary, forced_inst=inst,
                             fixed_range=req.fixed_range, threshold=req.threshold,
                             band=req.band, style=req.style, prefix=req.label_prefix)
            if spec is None:
                continue
            names = list(req.overlay_labels or ())
            for j, f in enumerate(req.overlays):
                ov = make_spec(req.type, f, forced_inst=inst, fixed_range=req.fixed_range,
                               threshold=None, band=None, style="line",
                               prefix=req.label_prefix, overlay=True)
                if ov is None:
                    continue
                if not ov.unit and spec.unit:
                    # 同一根 Y 轴，单位必然与主曲线一致（日志的 FMTU 常不给 Des* 列单位）
                    ov.unit = spec.unit
                ov.color = OVERLAY_COLORS[j % len(OVERLAY_COLORS)]
                ov.hidden = bool(req.overlays_hidden)
                if j < len(names) and names[j]:
                    ov.label = names[j]
                ov.note = ov.note or f
                spec.overlays.append(ov)
            lanes.append(spec)
            continue

        if req.type == "TRACK":
            for f in req.fields:
                got = _synthetic(store, f)
                if got is None:
                    skipped.append(f"TRACK.{f}（无轨迹数据）")
                    continue
                t, v, unit = got
                m = np.isfinite(t) & np.isfinite(v)
                if t0 is not None:
                    m &= t >= t0
                if t1 is not None:
                    m &= t <= t1
                if not m.any():
                    skipped.append(f"TRACK.{f}（区间内无有效点）")
                    continue
                lanes.append(LaneSpec(label=f"轨迹 {f}", color=next_color(), unit=unit,
                                      t=t[m], y=v[m], style=req.style,
                                      y_min=req.fixed_range[0] if req.fixed_range else None,
                                      y_max=req.fixed_range[1] if req.fixed_range else None,
                                      threshold=req.threshold, band=req.band,
                                      note=f"TRACK.{f}"))
            continue

        inst = pick_inst(req.type, req.inst)
        for f in req.fields:
            spec = make_spec(req.type, f, forced_inst=inst,
                             fixed_range=req.fixed_range, threshold=req.threshold,
                             band=req.band, style=req.style, prefix=req.label_prefix)
            if spec is None:
                continue
            if f in req.hide:
                spec.hidden = True
                if getattr(spec, "empty", False):
                    spec.empty = True      # 已空的道，隐藏原因以"空通道"为准（胶囊置灰）
            lanes.append(spec)
    return lanes, skipped


__all__ = ["PRESETS", "PRESET_BY_ID", "Preset", "LaneRequest", "LanePair", "LaneSpec",
           "build_lanes", "PresetError", "PALETTE", "OVERLAY_COLORS", "EMPTY_ATOL"]
