# -*- coding: utf-8 -*-
"""apmlog.core.trackdiff —— 两条位置轨迹之间的差异距离（估计值）。

口径是固定的，**界面上必须原样注明**（用户只关心不同定位设备之间的差异，例如
VIO/VISP 与 GPS/POS，所以这里只比"同一时刻、同一条飞行"的两条轨迹）：

* 距离：同一时刻两条轨迹的**水平**欧氏距离，单位 m，在共同原点的局部 ENU 里算；
* 对齐：以采样较稀的一条为时间基准，另一条线性插值；插值跨越的空档 > ``MAX_GAP_S``
  或落在 >200 m/s 的位置跳变上，就丢弃该样本（不外推、不硬插）；
* 归一化：``差异% = 均值偏差 ÷ 重叠段两条轨迹里程的平均值 × 100``；
* 总体：``Σ均值偏差 ÷ Σ平均里程 × 100``，即各对的差异%按里程加权；
* 不含高程：POS/GPS 的高度是 RelHomeAlt/AMSL 相对值，XKF1/VISP 的 ``u=-PD`` 相对
  EKF 原点，基准不同，相减会留下一个未知常量偏置。

为什么不直接减 ``TrackResult.e/n``：每条 geodetic 轨迹的 ENU 是**各自**以首个有效点
为原点投影的（实测同一日志两条轨迹首点就差 0.45 m，GPS 后补锁时能到几十米），
直接相减等于把两个坐标系的平移量算进了"差异"里。所以这里统一用经纬度在**同一个
原点**上重投影；本地轨迹（无经纬度）本来就共用 EKF 本地原点，才可以直接减。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import numpy as np

__all__ = [
    "MAX_GAP_S", "MIN_LENGTH_M", "PairDiff", "OverallDiff",
    "compare", "compare_many", "overall", "fmt_pct", "fmt_m",
]

#: 插值侧允许跨越的最大采样间隔。真机 103.6 MB 日志里有 4 处 >1 s 的空档、最大 4.8 s，
#: 取 2 s 既能容纳正常丢包，又不会在断点处凭空造点。
MAX_GAP_S = 2.0
#: 与 reader._MAX_STEP_MPS 一致：单步等效速度超此值视为位置跳变，不计里程。
MAX_STEP_MPS = 200.0
#: 少于这么多对齐样本就不给结论（一条 5 Hz 的日志里 10 点 = 2 s）。
MIN_SAMPLES = 10
#: 重叠段里程短于此值，百分比会被放大到没有意义（悬停日志）。
MIN_LENGTH_M = 100.0

_EARTH_KX = 111320.0        # 每度经度（赤道），乘 cos(lat)
_EARTH_KY = 110574.0        # 每度纬度


def _keys(tr: object) -> tuple[str, str]:
    return (str(getattr(tr, "key", "") or ""), str(getattr(tr, "label", "") or ""))


def _kind(tr: object) -> str:
    """geodetic（有经纬度）/ local（只有本地米）/ none。"""
    kind = str(getattr(tr, "kind", "local") or "local")
    if kind == "geodetic" and bool(getattr(tr, "has_geodetic", False)):
        return "geodetic"
    return "local" if kind != "none" else "none"


def _plane(tr: object, lat0: float, lon0: float) -> Optional[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """轨迹 → (t, x=东米, y=北米)，只保留有效点。"""
    t = np.atleast_1d(np.asarray(getattr(tr, "t", ()), dtype=np.float64))
    if t.size == 0:
        return None
    if _kind(tr) == "geodetic":
        lat = np.atleast_1d(np.asarray(getattr(tr, "lat", ()), dtype=np.float64))
        lon = np.atleast_1d(np.asarray(getattr(tr, "lon", ()), dtype=np.float64))
        m = min(t.size, lat.size, lon.size)
        t, lat, lon = t[:m], lat[:m], lon[:m]
        ok = (np.isfinite(lat) & np.isfinite(lon)
              & (np.abs(lat) <= 90.0) & (np.abs(lon) <= 180.0))
        if not ok.any():
            return None
        kx = _EARTH_KX * math.cos(math.radians(lat0))
        return t[ok], (lon[ok] - lon0) * kx, (lat[ok] - lat0) * _EARTH_KY
    e = np.atleast_1d(np.asarray(getattr(tr, "e", ()), dtype=np.float64))
    n = np.atleast_1d(np.asarray(getattr(tr, "n", ()), dtype=np.float64))
    m = min(t.size, e.size, n.size)
    t, e, n = t[:m], e[:m], n[:m]
    ok = np.isfinite(e) & np.isfinite(n)
    if not ok.any():
        return None
    return t[ok], e[ok], n[ok]


def _origin(tracks: Sequence[object]) -> Optional[tuple[float, float]]:
    """共同原点：第一条有有效经纬度的轨迹的首个有效点。"""
    for tr in tracks:
        if _kind(tr) != "geodetic":
            continue
        lat = np.atleast_1d(np.asarray(getattr(tr, "lat", ()), dtype=np.float64))
        lon = np.atleast_1d(np.asarray(getattr(tr, "lon", ()), dtype=np.float64))
        m = min(lat.size, lon.size)
        if m == 0:
            continue
        ok = np.isfinite(lat[:m]) & np.isfinite(lon[:m])
        if ok.any():
            i = int(np.argmax(ok))
            return float(lat[i]), float(lon[i])
    return None


def _length(t: np.ndarray, x: np.ndarray, y: np.ndarray, lo: float, hi: float) -> float:
    """重叠窗内的水平里程（跳变步不计）。"""
    m = (t >= lo) & (t <= hi)
    tt, xx, yy = t[m], x[m], y[m]
    if tt.size < 2:
        return 0.0
    dt = np.diff(tt)
    step = np.hypot(np.diff(xx), np.diff(yy))
    good = (dt > 0) & (step / np.maximum(dt, 1e-9) <= MAX_STEP_MPS)
    return float(step[good].sum())


@dataclass(frozen=True)
class PairDiff:
    """一对轨迹的差异结果。``ok=False`` 时 ``reason`` 说明为什么算不了。"""

    a_key: str = ""
    b_key: str = ""
    a_label: str = ""
    b_label: str = ""
    ok: bool = False
    reason: str = ""
    frame: str = ""             # geodetic / local
    n: int = 0                  # 参与统计的对齐样本数
    dropped: int = 0            # 因间隙/跳变被丢弃的样本数
    t0: float = math.nan
    t1: float = math.nan
    mean: float = math.nan
    median: float = math.nan
    p95: float = math.nan
    max: float = math.nan
    length_a: float = math.nan
    length_b: float = math.nan
    length: float = math.nan    # 两条的平均，作为归一化基准
    pct: float = math.nan       # 均值偏差 ÷ 平均里程 × 100
    pct_p95: float = math.nan
    pct_max: float = math.nan

    @property
    def overlap_s(self) -> float:
        return self.t1 - self.t0 if math.isfinite(self.t0) and math.isfinite(self.t1) else math.nan


def compare(a: object, b: object, *, max_gap: float = MAX_GAP_S) -> PairDiff:
    """算一对轨迹的差异（估计值）。任何异常情况都返回 ``ok=False``，不抛异常。"""
    ka, la = _keys(a)
    kb, lb = _keys(b)
    head = dict(a_key=ka, b_key=kb, a_label=la or ka, b_label=lb or kb)
    kinds = {_kind(a), _kind(b)}
    if "none" in kinds:
        return PairDiff(**head, reason="该轨迹没有有效位置点")
    if len(kinds) > 1:
        return PairDiff(**head, reason="坐标系不同：一条有经纬度、一条只有本地米数")
    frame = "geodetic" if kinds == {"geodetic"} else "local"
    org = _origin((a, b)) if frame == "geodetic" else None
    if frame == "geodetic" and org is None:
        return PairDiff(**head, reason="没有有效经纬度")
    lat0, lon0 = org if org is not None else (0.0, 0.0)
    pa, pb = _plane(a, lat0, lon0), _plane(b, lat0, lon0)
    if pa is None or pb is None:
        return PairDiff(**head, reason="该轨迹没有有效位置点")

    ta, xa, ya = pa
    tb, xb, yb = pb
    lo = max(float(ta[0]), float(tb[0]))
    hi = min(float(ta[-1]), float(tb[-1]))
    if not (hi > lo):
        return PairDiff(**head, frame=frame, reason="两条轨迹没有时间重叠")

    # 以点少的一条为时间基准（插值误差只会落在稠密的一侧，方向更保守）
    if ta.size <= tb.size:
        tq, xq, yq = ta, xa, ya
        td, xd, yd = tb, xb, yb
    else:
        tq, xq, yq = tb, xb, yb
        td, xd, yd = ta, xa, ya
    m = (tq >= lo) & (tq <= hi)
    tq, xq, yq = tq[m], xq[m], yq[m]
    if tq.size < MIN_SAMPLES:
        return PairDiff(**head, frame=frame, reason=f"重叠样本太少（{tq.size} 点）")

    # 插值 + 守卫：空档过大、落在跳变上、超出对方时间范围的样本一律不用
    n_d = int(td.size)
    idx = np.clip(np.searchsorted(td, tq, side="right") - 1, 0, n_d - 2)
    gap = td[idx + 1] - td[idx]
    step = np.hypot(xd[idx + 1] - xd[idx], yd[idx + 1] - yd[idx])
    span_ok = (tq >= td[0]) & (tq <= td[-1])
    use = span_ok & (gap <= float(max_gap)) & (step / np.maximum(gap, 1e-9) <= MAX_STEP_MPS)
    dropped = int(np.count_nonzero(~use))
    if int(np.count_nonzero(use)) < MIN_SAMPLES:
        return PairDiff(**head, frame=frame, dropped=dropped,
                        reason="对齐后可用样本不足（空档/跳变太多）")
    xi = np.interp(tq, td, xd)
    yi = np.interp(tq, td, yd)
    d = np.hypot(xq - xi, yq - yi)[use]
    d = d[np.isfinite(d)]
    if d.size == 0:
        return PairDiff(**head, frame=frame, dropped=dropped, reason="对齐后没有有效偏差")

    len_a = _length(ta, xa, ya, lo, hi)
    len_b = _length(tb, xb, yb, lo, hi)
    length = 0.5 * (len_a + len_b)
    mean = float(d.mean())
    p95 = float(np.percentile(d, 95))
    dmax = float(d.max())
    if length >= MIN_LENGTH_M:
        pct, pct95, pctmax = (mean / length * 100.0, p95 / length * 100.0,
                              dmax / length * 100.0)
    else:
        pct = pct95 = pctmax = math.nan
    return PairDiff(**head, ok=True, frame=frame, n=int(d.size), dropped=dropped,
                    t0=lo, t1=hi, mean=mean, median=float(np.median(d)), p95=p95,
                    max=dmax, length_a=len_a, length_b=len_b, length=length,
                    pct=pct, pct_p95=pct95, pct_max=pctmax)


def compare_many(tracks: Sequence[object], keys: Iterable[str]) -> list[PairDiff]:
    """按给定 key 顺序两两比较（只算这些轨迹之间的所有组合）。"""
    want = [str(k) for k in keys]
    by_key = {str(getattr(tr, "key", "") or ""): tr for tr in (tracks or [])}
    picked = [by_key[k] for k in want if k in by_key]
    out: list[PairDiff] = []
    for i in range(len(picked)):
        for j in range(i + 1, len(picked)):
            out.append(compare(picked[i], picked[j]))
    return out


@dataclass(frozen=True)
class OverallDiff:
    """所有可比轨迹对的合计。"""

    ok: bool = False
    n_pairs: int = 0
    n_used: int = 0
    mean_sum: float = math.nan
    length_sum: float = math.nan
    pct: float = math.nan


def overall(diffs: Sequence[PairDiff]) -> OverallDiff:
    """总体差异% = Σ均值偏差 ÷ Σ平均里程 × 100（各对按里程加权）。"""
    used = [d for d in diffs if d.ok and math.isfinite(d.pct) and math.isfinite(d.length)]
    if not used:
        return OverallDiff(ok=False, n_pairs=len(diffs))
    ms = float(sum(d.mean for d in used))
    ls = float(sum(d.length for d in used))
    pct = ms / ls * 100.0 if ls >= MIN_LENGTH_M else math.nan
    return OverallDiff(ok=math.isfinite(pct), n_pairs=len(diffs), n_used=len(used),
                       mean_sum=ms, length_sum=ls, pct=pct)


def fmt_pct(v: float) -> str:
    """百分比显示。

    瑞机日志里"偏差 ÷ 里程"经常只有 0.00x%（24 km 航程 / 0.8 m 平均偏差），
    固定两位小数会全变成 "<0.01%"、几对轨迹看起来一模一样，所以小值多给几位有效数字。
    """
    if not math.isfinite(v):
        return "—"
    if v <= 0.0:
        return "0%"
    if v < 0.00005:
        return "<0.0001%"
    if v < 0.01:
        return f"{v:.4f}%"
    if v < 0.1:
        return f"{v:.3f}%"
    if v < 10.0:
        return f"{v:.2f}%"
    return f"{v:.1f}%"


def fmt_m(v: float, nd: int = 2) -> str:
    if not math.isfinite(v):
        return "—"
    if abs(v) >= 1000.0:
        return f"{v / 1000.0:.{max(1, nd - 1)}f} km"
    return f"{v:.{nd}f} m"
