"""apmlog.core.alerts —— 震动与电量告警（v1 只做这两类）。

诚实性原则
----------
* 阈值优先取自**日志自身的参数**（``BATT_VOLT_MIN`` / ``BATT_ARM_VOLT`` 等），
  ``AlertHit.source='param'``；参数不可用时退回内置参考值并标 ``source='default-ref'``，
  UI 必须显示出处——震动量级与电池压降都存在明显机型差异。
* ``BAT.Volt`` 是整包还是单体电压由 :func:`apmlog.core.reader.bat_semantics` 从同一份日志
  反推；推不出来就**跳过欠压规则**并在结果里写"无法判定"，不猜。
* 通道缺失（如无 BAT / 无 VIBE）→ 产出 ``hint`` 说明无法评估，而不是静默 0 条。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from .model import AlertHit, ChannelKey

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "alerts.yaml"

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "vibration": {
        "enabled": True, "rms_warn": 30.0, "rms_crit": 60.0, "window_s": 1.0,
        "min_duration_s": 0.5, "merge_gap_s": 1.0, "clip_warn_total": 1,
        "clip_crit_total": 300, "report_axis_dominance": True,
    },
    "battery": {
        "enabled": True, "use_params": True,
        # 单芯阈值（用户 2026-10-24 定）：默认低于 3.3V 警告、低于 3.1V 严重。
        # cell_count 是电芯数（"14S"），0 = 自动识别；手动填的优先于自动。
        "cell_count": 0, "cell_warn": 3.30, "cell_crit": 3.10,
        "pack_ratio_of_arm": 0.90, "pack_ratio_crit": 0.82, "sag_warn_pct": 8.0,
        "sag_crit_pct": 15.0, "throttle_high": 0.30,
        "temp_warn": 50.0, "temp_crit": 65.0, "end_drop_pct": 15.0,
        "end_drop_window_s": 3.0, "min_duration_s": 0.3, "merge_gap_s": 1.5,
        # 整包绝对阈值（界面可填），0 = 不启用
        "pack_warn_v": 0.0, "pack_crit_v": 0.0,
    },
}

_VIBE_AXES = ("VibeX", "VibeY", "VibeZ")


def load_config(path: Optional[str | Path] = None) -> dict[str, Any]:
    """读 YAML 并深合并到内置默认值；文件缺失/损坏时退回默认并记日志。"""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if p.exists():
        try:
            import yaml
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                for section, vals in data.items():
                    if isinstance(vals, dict):
                        cfg.setdefault(section, {}).update(vals)
                    else:
                        cfg[section] = vals
                cfg["version"] = data.get("version", 1)
        except Exception as exc:  # pragma: no cover
            log.warning("告警配置读取失败(%s)，使用内置默认值", exc)
    else:
        log.info("未找到告警配置 %s，使用内置默认值", p)
    return cfg


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #

def _intervals(t: np.ndarray, mask: np.ndarray, min_dur: float,
               merge_gap: float) -> list[tuple[int, int]]:
    """布尔掩码 → 连续区间的 (起始索引, 结束索引含)。"""
    if t.size == 0 or not mask.any():
        return []
    idx = np.nonzero(mask)[0]
    runs: list[tuple[int, int]] = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > 1 and t[i] - t[prev] > merge_gap:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    out = []
    for a, b in runs:
        if t[b] - t[a] >= min_dur or a == b:
            out.append((a, b))
    return out


def _pick_inst(store, type_: str, field: str) -> Optional[int]:
    """选一个有数据的子实例（最小实例号），并告知调用方。"""
    tm = store.meta.get("types", {}).get(type_)
    if not tm:
        return None
    insts = [i for i in tm.get("insts", []) if i >= 0]
    if not insts:
        return None
    return min(insts)


def _hint(store: "Any", rule: str, title: str, msg: str) -> AlertHit:
    return AlertHit(rule=rule, title=title, severity="hint", message=msg, source="n/a")


def _fmt(v: float, d: int = 2) -> str:
    if v is None or not math.isfinite(v):
        return "-"
    return f"{v:.{d}f}"


# --------------------------------------------------------------------------- #
# 震动
# --------------------------------------------------------------------------- #

def vibe_alerts(store, cfg: dict[str, Any]) -> list[AlertHit]:
    out: list[AlertHit] = []
    tm = store.meta.get("types", {}).get("VIBE")
    if not tm:
        out.append(_hint(store, "vibe", "振动无法评估", "日志中没有 VIBE 报文"))
        return out
    inst = _pick_inst(store, "VIBE", "VibeX")
    window = float(cfg.get("window_s", 1.0)) or 1.0
    warn = float(cfg.get("rms_warn", 30.0))
    # 严重档必须不低于警告档：调反了会让 mask_w 恒空（全被 mask_c 吃掉）
    crit = max(float(cfg.get("rms_crit", 60.0)), warn)
    min_dur = float(cfg.get("min_duration_s", 0.5))
    gap = float(cfg.get("merge_gap_s", 1.0))
    dur = float(store.meta.get("t_end", 0.0))
    buckets = max(16, int(dur / window))

    per_axis: dict[str, float] = {}
    for axis in _VIBE_AXES:
        if axis not in tm.get("cols", {}):
            continue
        key = ChannelKey("VIBE", axis, inst)
        env = store.envelope(key, buckets=buckets)
        if env.t.size == 0:
            continue
        v = env.vmax
        per_axis[axis] = float(np.nanpercentile(v, 95)) if np.isfinite(v).any() else math.nan
        # 先算严重档，警告档只统计没达严重档的时间段，避免同一事件重复刷屏
        mask_c = np.isfinite(v) & (v >= crit)
        mask_w = np.isfinite(v) & (v >= warn) & ~mask_c
        for mask, thr, sev in ((mask_c, crit, "critical"), (mask_w, warn, "warn")):
            for a, b in _intervals(env.t, mask, min_dur, gap):
                peak = float(np.nanmax(v[a:b + 1]))
                out.append(AlertHit(
                    rule=f"vibe_{axis.lower()}",
                    title=f"振动超标（{axis}{f'[{inst}]' if inst is not None else ''}）",
                    severity=sev, t0=float(env.t[a]), t1=float(env.t[b]), value=peak,
                    threshold=float(thr), unit=env.unit or "m/s²", channel=key,
                    message=(f"{axis} 在 {window:.0f}s 窗内峰值 {_fmt(peak)} m/s² ≥ {_fmt(thr)}"
                             "（参考阈值，机型相关，可在「设置 → 告警阈值」调整）"),
                    source="default-ref"))
    # 主轴方向提示
    if cfg.get("report_axis_dominance", True) and per_axis:
        dom = max(per_axis, key=lambda k: (per_axis[k] if math.isfinite(per_axis[k]) else -1))
        out.append(AlertHit(rule="vibe_axis", title="振动主轴", severity="info",
                            value=float(per_axis[dom]), threshold=math.nan, unit="m/s²",
                            channel=ChannelKey("VIBE", dom, inst),
                            message="各轴 95 分位（按 " + f"{window:.0f}s 窗峰值）："
                                    + "、".join(f"{k}={_fmt(per_axis[k])}" for k in _VIBE_AXES if k in per_axis)
                                    + f"；主轴 {dom}。若主轴为 Z 多为电机/螺旋桨激励，水平轴偏大先看结构动平衡。",
                            source="n/a"))

    # 削波计数：Clip 是累计计数器，用正增量求和
    if "Clip" in tm.get("cols", {}):
        key = ChannelKey("VIBE", "Clip", inst)
        try:
            s = store.series(key)
        except KeyError:
            s = None
        if s is not None and s.v.size > 1:
            d = np.diff(s.v)
            d = d[np.isfinite(d)]
            inc = d[d > 0]
            total = float(inc.sum()) if inc.size else 0.0
            warn_t = float(cfg.get("clip_warn_total", 1))
            crit_t = float(cfg.get("clip_crit_total", 300))
            if total >= crit_t:
                sev = "critical"
            elif total >= warn_t:
                sev = "warn"
            else:
                sev = ""
            if sev:
                first_i = int(np.nonzero(d > 0)[0][0]) if (d > 0).any() else 0
                last_i = int(np.nonzero(d > 0)[0][-1]) if (d > 0).any() else 0
                out.append(AlertHit(rule="vibe_clip", title="加速度计削波(clipping)", severity=sev,
                                    t0=float(s.t[first_i]), t1=float(s.t[last_i + 1]),
                                    value=total, threshold=float(crit_t if sev == "critical" else warn_t),
                                    unit="次", channel=key, evidence=[ChannelKey("VIBE", a, inst) for a in _VIBE_AXES],
                                    message=(f"滤波削波累计 {total:.0f} 次（{s.t[first_i]:.1f}s–{s.t[last_i + 1]:.1f}s）。"
                                             "削波意味着加速度超出滤波量程，INS 输出会失真，通常比 RMS 数值更值得警惕。"),
                                    source="default-ref"))
    return out


def mask_over(v: np.ndarray, thr: float) -> np.ndarray:
    return np.isfinite(v) & (v >= thr)


# --------------------------------------------------------------------------- #
# 电量
# --------------------------------------------------------------------------- #

def resolve_cells(cfg: dict[str, Any], sem: dict[str, Any]) -> tuple[int | None, str, str]:
    """定电芯数（几 S）。手动填的优先于自动识别。

    返回 ``(cells, src, note)``：``src`` 是 ``manual`` / ``auto`` / ``none``，
    ``note`` 给界面和告警正文用（必须能说清"这个串数是哪来的"）。

    自动识别的三级证据，越靠前越硬：
    1. ``bat_semantics.cells_est``（单芯量级日志里 ARM ÷ 观测值推出来的）；
    2. ``BATT_ARM_VOLT ÷ 4.2``（满电整包电压，ArduPilot 建议按满电填）；
    3. 观测到的最高整包电压 ÷ 4.25 V/芯（4.25 是锂电单芯上限），向上取整。
    第 3 级是估算：只作默认值，界面上必须标明"自动估算"且允许手动覆盖。
    """
    manual = 0
    try:
        manual = int(float(cfg.get("cell_count", 0) or 0))
    except (TypeError, ValueError):
        manual = 0
    if manual > 0:
        return manual, "manual", f"手动设置 {manual}S"
    est = sem.get("cells_est")
    try:
        if est and float(est) >= 1:
            n = int(round(float(est)))
            return n, "auto", f"自动识别 {n}S（按 ARM 电压与观测值反推）"
    except (TypeError, ValueError):
        pass
    try:
        arm = float(sem.get("batt_arm_voltage") or 0)
    except (TypeError, ValueError):
        arm = 0.0
    if arm > 9.0:
        n = int(round(arm / 4.2))
        if 1 <= n <= 24:
            return n, "auto", f"自动识别 {n}S（BATT_ARM_VOLT {arm:.1f}V ÷ 4.2V）"
    try:
        vmax = float(sem.get("volt_max") or 0)
    except (TypeError, ValueError):
        vmax = 0.0
    if 9.0 < vmax < 90.0:
        n = int(math.ceil(vmax / 4.25))
        if 1 <= n <= 24:
            return n, "auto", f"自动估算 {n}S（最高整包电压 {vmax:.1f}V ÷ 4.25V/芯 向上取整）"
    return None, "none", "无法自动识别串数（缺 BATT_ARM_VOLT / BATT_VOLT_MIN，电压也不足以推断）"


def batt_alerts(store, cfg: dict[str, Any],
                user_keys: frozenset[str] = frozenset()) -> list[AlertHit]:
    """电池规则。

    **RemPct（剩余电量百分比）不参与任何判定**（2026-10-24 用户指令）：它是飞控按
    电压反推的估计值，等于拿估计值二次推导，且整包/单芯量级一变就失真。判定的唯一依据是
    **估算出来的单芯电压（整包 ÷ 当前采用的电芯数）+ 单芯阈值**。
    yaml / 旧设置里的 ``remain_warn`` / ``remain_crit`` 已无消费者，留着也不生效。

    ``user_keys`` 指明哪些阈值是**用户在界面里显式设的**。这一步不能省：
    yaml 里本来就有 cell_warn=3.50 之类的参考值，光看"值 > 0"根本分不清
    "用户填了 3.5" 和 "yaml 默认就是 3.5"，会把按 BATT_VOLT_MIN 推导的
    证据链（source=param）静默顶掉。
    """
    out: list[AlertHit] = []
    tm = store.meta.get("types", {}).get("BAT")
    if not tm:
        out.append(_hint(store, "batt", "电量无法评估", "日志中没有 BAT 报文"))
        return out
    inst = _pick_inst(store, "BAT", "Volt")
    cols = tm.get("cols", {})
    sem = store.meta.get("bat_semantics", {}) or {}
    mode = sem.get("mode", "unknown")
    min_dur = float(cfg.get("min_duration_s", 0.3))
    gap = float(cfg.get("merge_gap_s", 1.5))

    def ser(field: str):
        if field not in cols:
            return None
        try:
            return store.series(ChannelKey("BAT", field, inst))
        except KeyError:
            return None

    volt = ser("Volt")
    voltr = ser("VoltR")
    arm_v = store.param("BATT_ARM_VOLT") or store.param("BATT2_ARM_VOLT")
    vmin_p = store.param("BATT_VOLT_MIN") or store.param("BATT2_VOLT_MIN")

    # --- 欠压：按"单芯电压"判定；串数可手动覆盖自动识别（手动优先） ---
    cells, cells_src, cells_note = resolve_cells(cfg, sem)
    w_cell = float(cfg.get("cell_warn", 3.3) or 3.3)
    c_cell = float(cfg.get("cell_crit", 3.1) or 3.1)
    if c_cell > w_cell:                      # 欠压"越低越严重"：严重档必须不高于警告档
        w_cell, c_cell = c_cell, w_cell

    field = "Volt"                          # 单芯判定都基于 BAT.Volt
    per_cell = None                          # 单芯电压序列
    basis = ""
    if mode == "cell" and volt is not None:
        per_cell = volt.v                    # 日志本身记的就是单芯电压
        basis = "日志记的是单体电压，直接比"
    elif mode == "pack" and cells and volt is not None:
        per_cell = volt.v / float(cells)     # 整包 ÷ 串数 = 单芯
        basis = f"整包电压 ÷ {int(cells)} 串"

    if per_cell is not None:
        ok = np.isfinite(per_cell) & (per_cell > 0.5) & (per_cell < 20.0)
        mask_c = ok & (per_cell < c_cell)
        mask_w = ok & (per_cell < w_cell) & ~mask_c
        src = "user" if (cells_src == "manual" or "cell_warn" in set(user_keys)) else "default-ref"
        tail = (f"（{cells_note}；整包 ÷ {int(cells)} 串）" if cells
                else "（日志记的就是单体电压）")
        for mask, thr, sev in ((mask_c, c_cell, "critical"), (mask_w, w_cell, "warn")):
            for a, b in _intervals(volt.t, mask, min_dur, gap):
                lo = float(np.nanmin(per_cell[a:b + 1]))
                pack_lo = float(np.nanmin(volt.v[a:b + 1])) if volt is not None else math.nan
                extra = "" if not (cells and mode == "pack") else f"，整包 {_fmt(pack_lo)} V"
                # 差得很近时多给一位小数，避免"3.900 < 3.90"这种自相矛盾的显示
                vdp = 4 if abs(lo - thr) < 5e-3 else 3
                out.append(AlertHit(
                    rule=f"batt_undervolt_{'crit' if sev == 'critical' else 'warn'}",
                    title=f"电压偏低（{field}{f'[{inst}]' if inst is not None else ''}）",
                    severity=sev, t0=float(volt.t[a]), t1=float(volt.t[b]),
                    value=lo, threshold=thr, unit="V/芯", channel=volt.key,
                    message=(f"单芯最低 {_fmt(lo, vdp)} V < 阈值 {_fmt(thr, 2)} V{extra}"
                             f"{tail}"),
                    source=src))
    else:
        # 没有串数又只有整包电压：按参数/比例兜底，并明确提示去填电芯数
        if mode == "pack":
            out.append(_hint(
                store, "batt_cells_unknown", "电芯数未知",
                "这份日志没能自动识别电池串数（缺 BATT_ARM_VOLT / BATT_VOLT_MIN，"
                "最高电压也不足以推断），无法按单芯电压判定。"
                "请在「设置 → 告警阈值（电池）」里手动填写电芯数（如 14S）。"))
        field = "VoltR" if (mode == "pack" and "VoltR" in cols) else "Volt"
        crit = warn = math.nan
        src = "default-ref"
        note = ""
        if mode == "cell":
            if vmin_p and cells and float(cells) >= 1:
                crit = float(vmin_p) / float(cells)
                warn = crit * 1.06
                src = "param"
                note = f"阈值来自参数 BATT_VOLT_MIN={_fmt(float(vmin_p))}V ÷ 估算 {cells} 串"
            else:
                crit = float(cfg.get("cell_crit", 3.1) or 3.1)
                warn = float(cfg.get("cell_warn", 3.3) or 3.3)
                note = "使用单体参考阈值"
        elif mode == "pack":
            if vmin_p:
                crit = float(vmin_p)
                warn = crit * 1.06
                src = "param"
                note = f"阈值来自参数 BATT_VOLT_MIN={_fmt(crit)}V"
            elif arm_v:
                crit = float(arm_v) * float(cfg.get("pack_ratio_crit", 0.82))
                warn = float(arm_v) * float(cfg.get("pack_ratio_of_arm", 0.90))
                note = f"阈值来自参数 BATT_ARM_VOLT={_fmt(float(arm_v))}V × 比例（参考值）"
            else:
                note = "整包量级但缺 BATT_VOLT_MIN / BATT_ARM_VOLT，跳过欠压判定"
        else:
            note = f"无法判定 BAT.Volt 量级（{sem.get('note', '')}），跳过欠压判定"
        s = ser(field) if field else None
        if s is not None and s.v.size and math.isfinite(crit) and math.isfinite(warn):
            mask_c = np.isfinite(s.v) & (s.v > 0.5) & (s.v < crit)
            mask_w = np.isfinite(s.v) & (s.v > 0.5) & (s.v < warn) & ~mask_c
            for mask, thr, sev in ((mask_c, crit, "critical"), (mask_w, warn, "warn")):
                for a, b in _intervals(s.t, mask, min_dur, gap):
                    lo = float(np.nanmin(s.v[a:b + 1]))
                    out.append(AlertHit(
                        rule=f"batt_undervolt_{'crit' if sev == 'critical' else 'warn'}",
                        title=f"电压偏低（{field}{f'[{inst}]' if inst is not None else ''}）",
                        severity=sev, t0=float(s.t[a]), t1=float(s.t[b]), value=lo,
                        threshold=thr, unit=s.unit or "V", channel=s.key,
                        message=(f"{field} 最低 {_fmt(lo)} {s.unit or 'V'} < 阈值 {_fmt(thr)}；"
                                 f"量级判定≈{mode}。{note}。"),
                        source=src))

    # --- 整包绝对阈值：设置里填的整包电压，低于就报（0 = 不启用）---
    p_crit = float(cfg.get("pack_crit_v", 0) or 0)
    p_warn = float(cfg.get("pack_warn_v", 0) or 0)
    if p_crit > 0 and p_warn > 0:
        p_crit, p_warn = min(p_crit, p_warn), max(p_crit, p_warn)   # 同上，越低越严重
    if (p_crit > 0 or p_warn > 0) and mode != "unknown":
        pv = ser("VoltR")
        if pv is None:
            pv = volt
        if pv is not None and pv.v.size:
            mul = float(cells) if (mode == "cell" and cells and float(cells) >= 1) else 1.0
            pack = pv.v * mul          # cell 量级的日志要乘回整包
            ok = np.isfinite(pack) & (pack > 0.5)
            mask_c = ok & (pack < p_crit) if p_crit > 0 else np.zeros(pack.size, dtype=bool)
            mask_w = ok & (pack < p_warn) & ~mask_c if p_warn > 0 else np.zeros(pack.size, dtype=bool)
            for mask, thr, sev, rule in ((mask_c, p_crit, "critical", "batt_pack_low_crit"),
                                         (mask_w, p_warn, "warn", "batt_pack_low_warn")):
                if not mask.any():
                    continue
                for a, b in _intervals(pv.t, mask, min_dur, gap):
                    lo = float(np.nanmin(pack[a:b + 1]))
                    out.append(AlertHit(
                        rule=rule, title="整包电压偏低", severity=sev,
                        t0=float(pv.t[a]), t1=float(pv.t[b]), value=lo, threshold=thr,
                        unit="V", channel=pv.key,
                        message=(f"整包电压最低 {_fmt(lo)} V < 界面设的阈值 {_fmt(thr)} V"
                                 f"（{'cell 量级 × ' + _fmt(mul, 0) + ' 串' if mul != 1.0 else 'pack 量级'}）"),
                        source="user"))

    if mode == "unknown" and volt is not None:
        out.append(AlertHit(rule="batt_semantics", title="电压量级无法判定", severity="hint",
                            value=float(np.nanmax(volt.v)) if volt.v.size else math.nan,
                            threshold=math.nan, unit=volt.unit, channel=volt.key,
                            message=(f"BAT.Volt 范围 {_fmt(float(np.nanmin(volt.v)))}–"
                                     f"{_fmt(float(np.nanmax(volt.v)))}，参数里也没有可用的 BATT_ARM_VOLT/"
                                     "BATT_VOLT_MIN，无法确定是整包还是单体电压 → 已跳过欠压判定。"),
                            source="n/a"))

    # --- 高油门压降 sag ---
    if volt is not None and mode != "unknown":
        th, thr_ch = None, None
        for tn, cn in (("CTUN", "ThO"), ("MOTB", "ThrOut")):
            if tn in store.meta.get("types", {}):
                try:
                    th = store.series(ChannelKey(tn, cn, _pick_inst(store, tn, cn)))
                    thr_ch = th.key
                    break
                except KeyError:
                    continue
        base_t0 = store.stats.takeoff_t if store.stats.takeoff_t else float(volt.t[0])
        base_mask = (volt.t >= base_t0) & (volt.t <= base_t0 + 10.0) & np.isfinite(volt.v)
        baseline = float(np.nanmedian(volt.v[base_mask])) if base_mask.any() else math.nan
        if th is not None and math.isfinite(baseline) and baseline > 0:
            hi = np.zeros(volt.t.size, dtype=bool)
            j = np.searchsorted(th.t, volt.t, side="right") - 1
            okj = (j >= 0) & (j < th.v.size)
            hi[okj] = np.isfinite(th.v[j[okj]]) & (th.v[j[okj]] > float(cfg.get("throttle_high", 0.30)))
            for pct, sev in ((float(cfg.get("sag_crit_pct", 15.0)), "critical"),
                             (float(cfg.get("sag_warn_pct", 8.0)), "warn")):
                limit = baseline * (1.0 - pct / 100.0)
                mask = hi & np.isfinite(volt.v) & (volt.v < limit)
                if mode == "unknown":
                    break
                for a, b in _intervals(volt.t, mask, min_dur, gap):
                    lo = float(np.nanmin(volt.v[a:b + 1]))
                    out.append(AlertHit(rule=f"batt_sag_{'crit' if sev == 'critical' else 'warn'}",
                                        title="高油门电压压降", severity=sev, t0=float(volt.t[a]),
                                        t1=float(volt.t[b]), value=lo, threshold=limit,
                                        unit=volt.unit, channel=volt.key,
                                        evidence=[c for c in (thr_ch,) if c],
                                        message=(f"基线 {_fmt(baseline)}（解锁后 10s 中位数），"
                                                 f"高油门段最低 {_fmt(lo)}，压降 "
                                                 f"{_fmt((baseline - lo) / baseline * 100.0, 1)}% ≥ "
                                                 f"{_fmt(pct, 1)}%。内阻偏大/线束接触不良/电池老化都会是这个形态。"),
                                        source="default-ref"))
                break

    # --- 温度（哨兵值 -1/0 自动跳过）---
    temp = ser("Temp")
    if temp is not None and temp.v.size:
        valid = np.isfinite(temp.v) & (temp.v > 5.0)
        if valid.any():
            for thr, sev in ((float(cfg.get("temp_crit", 65.0)), "critical"),
                             (float(cfg.get("temp_warn", 50.0)), "warn")):
                mask = valid & (temp.v >= thr)
                for a, b in _intervals(temp.t, mask, min_dur, gap):
                    out.append(AlertHit(rule=f"batt_temp_{'crit' if sev == 'critical' else 'warn'}",
                                        title="电池温度偏高", severity=sev, t0=float(temp.t[a]),
                                        t1=float(temp.t[b]), value=float(np.nanmax(temp.v[a:b + 1])),
                                        threshold=thr, unit=temp.unit or "°C", channel=temp.key,
                                        message=f"Temp 峰值 {_fmt(float(np.nanmax(temp.v[a:b + 1])), 1)}°C ≥ {_fmt(thr, 0)}°C（参考值，未含电芯类型差异）",
                                        source="default-ref"))
                if mask.any():
                    break

    # --- 末段突降（提示供电中断，不下结论）---
    if volt is not None and volt.v.size > 20 and mode != "unknown":
        win = float(cfg.get("end_drop_window_s", 3.0))
        tail = volt.t >= float(volt.t[-1]) - win
        if tail.any():
            head = float(np.nanmax(volt.v[~tail][-20:])) if (~tail).any() else math.nan
            lo = float(np.nanmin(volt.v[tail]))
            if math.isfinite(head) and head > 0:
                drop = (head - lo) / head * 100.0
                if drop >= float(cfg.get("end_drop_pct", 15.0)):
                    out.append(AlertHit(rule="batt_end_drop", title="末段电压突降", severity="error",
                                        t0=float(volt.t[tail][0]), t1=float(volt.t[-1]), value=lo,
                                        threshold=head * (1 - cfg.get("end_drop_pct", 15.0) / 100.0),
                                        unit=volt.unit, channel=volt.key,
                                        message=(f"最后 {win:.0f}s 内 {_fmt(head)} → {_fmt(lo)}"
                                                 f"（降 {_fmt(drop, 1)}%）。日志在"
                                                 f"{'仍有电量读数' if math.isfinite(lo) else '读数中断'}"
                                                 "时结束，需与事件时间线/姿态末段一起看，不能直接判定为掉电。"),
                                        source="default-ref"))
    return out


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def evaluate(store, config_path: Optional[str | Path] = None,
             overrides: Optional[dict[str, dict[str, Any]]] = None) -> list[AlertHit]:
    """跑全部启用的规则族，按严重度→时间排序返回。

    ``overrides`` 是界面设置的覆盖层（如 ``{"vibration": {"rms_warn": 35.0}}``），
    在 yaml 之后深合并：yaml 仍是配置文件真源，界面只覆盖用户明确改过的项。
    """
    cfg = load_config(config_path)
    user_keys: dict[str, set[str]] = {}
    if overrides:
        for section, vals in overrides.items():
            if isinstance(vals, dict):
                keep = {k: v for k, v in vals.items() if v is not None}
                if keep:
                    cfg.setdefault(str(section), {}).update(keep)
                    user_keys.setdefault(str(section), set()).update(keep)
    hits: list[AlertHit] = []
    vcfg = cfg.get("vibration", {})
    if vcfg.get("enabled", True):
        hits += vibe_alerts(store, vcfg)
    bcfg = cfg.get("battery", {})
    if bcfg.get("enabled", True):
        hits += batt_alerts(store, bcfg, frozenset(user_keys.get("battery", ())))
    hits.sort(key=lambda h: (-_rank(h.severity), h.t0 if math.isfinite(h.t0) else 1e18))
    return hits


def _rank(sev: str) -> int:
    from .model import severity_rank
    return severity_rank(sev)


__all__ = ["evaluate", "load_config", "vibe_alerts", "batt_alerts", "DEFAULT_CONFIG_PATH"]
