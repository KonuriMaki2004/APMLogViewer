"""apmlog.ui._decim —— 时序抽稀与分桶包络工具（纯 numpy，无 Qt 依赖）。

为什么自己实现分桶，而不用 pyqtgraph 内置 ``setDownsampling``
-----------------------------------------------------------------
1. **需要 mean 线与每桶点数**：内置 peak 降采样只给 min/max，而泳道要画
   "包络填充 + 均值线"，读数气泡也要显示桶内均值/点数（不是原始样本值），必须自己算。
2. **多泳道必须同桶**：共享 X 轴时，游标读数要求所有泳道在**同一时间栅格**上取值，
   内置降采样是按各自像素列独立算的，会出现读数错位。
3. **可复用内核预算好的 Envelope**：``LogStore.envelope()`` 已返回分桶结果，
   概览视图直接用它可以省一次全量扫描；只有用户放大到桶粒度以下时才现算。
4. **交互成本**：直接分桶是 O(可见点数)，2M 点全览一次要 ~80ms，拖一下滑块就飘；
   所以大数组走 ``EnvTower`` 分层桶塔（一次建塔 O(n)，查询只合并 O(输出桶数)），
   实测 2M 点全览重绘从 646ms 降到 ~24ms（含绘图，见 tools/selftest_seriesplot.py）。

因此策略是：**大数组建塔，概览/中等缩放用塔，放大到"可见点数 <= 阈值"时自动切回真实折线**；
塔切不开的小窗口退回 ``bucket_envelope`` 直接分桶（此时可见点已经不多）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

import numpy as np

__all__ = [
    "EnvArrays", "is_monotonic", "sort_time_series", "clean_series",
    "bucket_envelope", "envelope_for_range", "lttb", "finite_stats",
    "parse_key_tolerant", "EnvTower", "build_tower",
]

# 空桶填充值
_NAN = math.nan


@dataclass
class EnvArrays:
    """分桶结果。桶与桶之间允许有空洞（该桶 count==0）。"""

    t: np.ndarray        # 桶中心（秒）
    vmin: np.ndarray
    vmax: np.ndarray
    vmean: np.ndarray
    count: np.ndarray    # 桶内有限样本数

    @property
    def n(self) -> int:
        return int(self.t.size)


def is_monotonic(t: np.ndarray) -> bool:
    """便宜地检查时间轴是否单调不减（全量检查，O(n)，只在装载时调一次）。"""
    if t.size < 2:
        return True
    return bool(np.all(np.diff(t) >= 0))


def sort_time_series(t: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """时间轴乱序时内部排序。**绝不修改输入数组**（需要时 copy）。"""
    if t.size < 2 or is_monotonic(t):
        return t, y
    order = np.argsort(t, kind="stable")       # 高级索引天然产生新数组
    return t[order], y[order]


def clean_series(t, y) -> tuple[np.ndarray, np.ndarray]:
    """统一入口：转 float64、长度对齐、排序、去掉时间轴 nan。

    数据值里的 nan/inf **保留**（画线用 ``connect='finite'`` 断开，统计时再过滤）。
    """
    t = np.asarray(t, dtype=np.float64).ravel()
    y = np.asarray(y, dtype=np.float64).ravel()
    n = min(t.size, y.size)
    if n == 0:
        return np.empty(0, np.float64), np.empty(0, np.float64)
    t, y = t[:n], y[:n]
    ok = np.isfinite(t)
    if not ok.all():
        t, y = t[ok], y[ok]
    return sort_time_series(t, y)


def finite_stats(y: np.ndarray) -> tuple[float, float, float]:
    """返回 (min, max, mean)；全非有限时三个都是 nan。"""
    if y.size == 0:
        return (_NAN, _NAN, _NAN)
    m = np.isfinite(y)
    if not m.any():
        return (_NAN, _NAN, _NAN)
    v = y[m]
    return (float(v.min()), float(v.max()), float(v.mean()))


def bucket_envelope(t: np.ndarray, y: np.ndarray, x0: float, x1: float,
                    nbuckets: int) -> EnvArrays:
    """在 [x0, x1] 上等分 nbuckets 个桶，返回每桶 min/max/mean/count。

    要求 ``t`` 单调不减。空桶（无样本或全 nan）会被丢弃，返回数组可能短于 nbuckets。
    复杂度 O(可见点数)。
    """
    nbuckets = max(1, int(nbuckets))
    if not (math.isfinite(x0) and math.isfinite(x1)) or x1 <= x0 or t.size == 0:
        return EnvArrays(*(np.empty(0) for _ in range(5)))

    i0 = int(np.searchsorted(t, x0, side="left"))
    i1 = int(np.searchsorted(t, x1, side="right"))
    if i1 - i0 <= 0:
        return EnvArrays(*(np.empty(0) for _ in range(5)))
    tt = t[i0:i1]
    yy = y[i0:i1]
    n = tt.size

    if n <= 2 * nbuckets:
        # 点数已经不多了，直接按原始点当"每点一桶"，避免不必要的失真
        cnt = np.isfinite(yy).astype(np.float64)
        v = np.where(np.isfinite(yy), yy, 0.0)
        return EnvArrays(tt, yy.copy(), yy.copy(), yy.copy(), cnt)

    edges = np.linspace(x0, x1, nbuckets + 1)
    # tt 单调、edges 单调 ⇒ b 单调不减，可以用 reduceat 做分段聚合
    b = np.searchsorted(edges, tt, side="right") - 1
    np.clip(b, 0, nbuckets - 1, out=b)
    starts = np.flatnonzero(np.concatenate(([True], b[1:] != b[:-1])))
    bins = b[starts]
    sizes = np.diff(np.concatenate((starts, [n]))).astype(np.float64)

    fin = np.isfinite(yy)
    ysum = np.add.reduceat(np.where(fin, yy, 0.0), starts)
    ycnt = np.add.reduceat(fin.astype(np.float64), starts)
    ymin = np.minimum.reduceat(np.where(fin, yy, np.inf), starts)
    ymax = np.maximum.reduceat(np.where(fin, yy, -np.inf), starts)
    tctr = np.add.reduceat(tt, starts) / sizes

    keep = ycnt > 0
    if not keep.any():
        return EnvArrays(*(np.empty(0) for _ in range(5)))
    return EnvArrays(
        t=tctr[keep],
        vmin=np.where(np.isinf(ymin[keep]), _NAN, ymin[keep]),
        vmax=np.where(np.isinf(ymax[keep]), _NAN, ymax[keep]),
        vmean=ysum[keep] / ycnt[keep],
        count=ycnt[keep],
    )


def envelope_for_range(t: np.ndarray, y: np.ndarray, x0: float, x1: float,
                       nbuckets: int = 1200) -> EnvArrays:
    """语义包装：给绘图用的可见区包络。空结果时 n==0。"""
    return bucket_envelope(t, y, x0, x1, nbuckets)


# --------------------------------------------------------------------------- #
# 分层桶塔：一次构建，缩放时只合并 O(输出桶数) 个桶，不再扫全量可见点
# --------------------------------------------------------------------------- #


class _Level:
    """一层桶：桶按原始下标区间划分，存时间/统计量。"""

    __slots__ = ("t0", "t1", "tc", "vmin", "vmax", "vmean", "count")

    def __init__(self, t0, t1, tc, vmin, vmax, vmean, count) -> None:
        self.t0, self.t1, self.tc = t0, t1, tc
        self.vmin, self.vmax, self.vmean, self.count = vmin, vmax, vmean, count


class EnvTower:
    """min/max/mean/count 分层桶塔。

    背最粗层点数少到可以忽略时，查询就是“挑一层 + 按输出桶数合并”，
    与数据总量无关。2M 点全览缩放从百毫秒降到亚毫秒。
    """

    __slots__ = ("levels", "points_per_bucket", "n")

    def __init__(self, t: np.ndarray, y: np.ndarray, ppb: int = 512, factor: int = 16,
                 max_levels: int = 8) -> None:
        n = int(t.size)
        self.n = n
        self.points_per_bucket = max(8, int(ppb))
        if n == 0:
            self.levels = []
            return
        bounds = [np.arange(0, n + self.points_per_bucket, self.points_per_bucket, dtype=np.int64)]
        if bounds[0][-1] > n:
            bounds[0][-1] = n
        while len(bounds) < max_levels and bounds[-1].size > 2:
            nxt = bounds[-1][::max(2, int(factor))]
            if int(nxt[-1]) != n:
                nxt = np.append(nxt, n)
            if nxt.size < 2 or np.array_equal(nxt, bounds[-1]):
                break
            bounds.append(nxt.astype(np.int64, copy=False))
        self.levels = [self._aggregate(t, y, b) for b in bounds]

    @staticmethod
    def _aggregate(t: np.ndarray, y: np.ndarray, bounds: np.ndarray) -> _Level:
        a, b = bounds[:-1], bounds[1:]
        span = b - a
        keep = span > 0
        a, b = a[keep], b[keep]
        if a.size == 0:
            e = np.empty(0)
            return _Level(e, e.copy(), e.copy(), e.copy(), e.copy(), e.copy(), e.copy())
        starts = a
        fin = np.isfinite(y)
        t0 = t[starts]
        t1 = t[b - 1]
        tc = np.add.reduceat(t, starts) / span[keep]
        ymin = np.minimum.reduceat(np.where(fin, y, np.inf), starts)
        ymax = np.maximum.reduceat(np.where(fin, y, -np.inf), starts)
        vsum = np.add.reduceat(np.where(fin, y, 0.0), starts)
        vcnt = np.add.reduceat(fin.astype(np.float64), starts)
        with np.errstate(invalid="ignore", divide="ignore"):
            vmean = np.where(vcnt > 0, vsum / np.maximum(vcnt, 1.0), _NAN)
        return _Level(t0, t1, tc,
                      np.where(np.isinf(ymin), _NAN, ymin),
                      np.where(np.isinf(ymax), _NAN, ymax),
                      vmean, vcnt)

    def envelope(self, x0: float, x1: float, nbuckets: int) -> Optional[EnvArrays]:
        """返回 [x0,x1] 的不超过 nbuckets 列包络。

        选层规则：
        1. 优先找“可见桶数 >= nbuckets”中最粗的一层，合并到 nbuckets 列；
        2. 没有就直用桶最细层原样输出（桶数不够时列数 < nbuckets，精度更好）；
        3. 连最细层都只有几十桶 -> 返回 None，调用方该画原始点。
        """
        if not self.levels or not (math.isfinite(x0) and math.isfinite(x1)) or x1 <= x0:
            return None
        nbuckets = max(1, int(nbuckets))

        def _vis(lvl: _Level) -> tuple[int, int]:
            if lvl.t0.size == 0:
                return (0, 0)
            a = int(np.searchsorted(lvl.t1, x0, side="left"))
            b = int(np.searchsorted(lvl.t0, x1, side="right"))
            return (a, max(0, b - a))

        picked: Optional[_Level] = None
        i0 = nvis = 0
        for lvl in reversed(self.levels):              # 从粗到细，取第一个桶数够的层
            a, cnt = _vis(lvl)
            if cnt >= nbuckets:
                picked, i0, nvis = lvl, a, cnt
                break
        if picked is None:
            fine = self.levels[0]                       # 桶最细层
            a, cnt = _vis(fine)
            if cnt < max(16, nbuckets // 8):
                return None                             # 数据已经很稀，画原始点更准
            picked, i0, nvis = fine, a, cnt
        i1 = i0 + nvis
        if nvis <= 0:
            return EnvArrays(*(np.empty(0) for _ in range(5)))
        sl = slice(i0, i1)
        tc, vmin, vmax, vmean, cnt = (picked.tc[sl], picked.vmin[sl], picked.vmax[sl],
                                      picked.vmean[sl], picked.count[sl])
        if nvis <= nbuckets:
            keep = cnt > 0                            # 空桶（全 nan）不输出，跟直接分桶一致
            if not keep.any():
                return EnvArrays(*(np.empty(0) for _ in range(5)))
            return EnvArrays(tc[keep], vmin[keep], vmax[keep], vmean[keep], cnt[keep])
        # 把 nvis 个桶再合并成 nbuckets 组（按点数加权，空桶用桶中心时间）
        g = np.arange(nvis, dtype=np.int64) * nbuckets // nvis
        starts = np.flatnonzero(np.concatenate(([True], g[1:] != g[:-1])))
        wsum = np.add.reduceat(np.where(cnt > 0, vmean * cnt, 0.0), starts)
        wc = np.add.reduceat(cnt, starts)
        tsum = np.add.reduceat(np.where(cnt > 0, tc * cnt, 0.0), starts)
        with np.errstate(invalid="ignore", divide="ignore"):
            gmin = np.minimum.reduceat(np.where(np.isfinite(vmin), vmin, np.inf), starts)
            gmax = np.maximum.reduceat(np.where(np.isfinite(vmax), vmax, -np.inf), starts)
            gtc = np.where(wc > 0, tsum / np.maximum(wc, 1e-12), tc[starts])
            gmean = np.where(wc > 0, wsum / np.maximum(wc, 1e-12), _NAN)
        keep = wc > 0
        if not keep.any():
            return EnvArrays(*(np.empty(0) for _ in range(5)))
        return EnvArrays(
            t=gtc[keep],
            vmin=np.where(np.isinf(gmin[keep]), _NAN, gmin[keep]),
            vmax=np.where(np.isinf(gmax[keep]), _NAN, gmax[keep]),
            vmean=gmean[keep],
            count=wc[keep],
        )

    def visible_points(self, x0: float, x1: float) -> int:
        """粗估可见原始点数（用最细层的时间端点做二分，不扫数据）。"""
        if not self.levels:
            return 0
        fine = self.levels[0]
        i0 = int(np.searchsorted(fine.t1, x0, side="left"))
        i1 = int(np.searchsorted(fine.t0, x1, side="right"))
        if i1 <= i0:
            return 0
        return int((i1 - i0) * self.points_per_bucket)

    @property
    def size_bytes(self) -> int:
        per = 7 * 8
        return sum(lvl.t0.size for lvl in self.levels) * per


def build_tower(t: np.ndarray, y: np.ndarray, ppb: int = 512) -> Optional[EnvTower]:
    """安壁构建桶塔；数据太少不值得建塔时返回 None。"""
    if t.size < 4 * ppb:
        return None
    try:
        return EnvTower(t, y, ppb=ppb)
    except Exception:                              # noqa: BLE001
        LOG.exception("建桶塔失败，退回直接分桶")
        return None


def lttb(t: np.ndarray, y: np.ndarray, n_out: int) -> tuple[np.ndarray, np.ndarray]:
    """Largest-Triangle-Three-Buckets 抽稀，适合"看起来像原图"的折线概览。

    组件默认不用它（分桶包络更能保留尖峰，振动/电压毛刺不能丢），
    但导出与"平滑预览"场景需要，所以留在这里。
    """
    n = t.size
    if n_out <= 0 or n <= n_out + 2:
        return t, y
    buckets = np.linspace(0, n - 1, n_out + 1).astype(int)
    out_idx = [0]
    nb = buckets.size
    for i in range(1, n_out):
        a_t, a_y = t[buckets[i - 1]], y[buckets[i - 1]]
        s, e = buckets[i], buckets[i + 1]
        # 下一桶（最后一个输出桶的后继用末点代替，避免越界）
        nxt_s = buckets[i + 1]
        nxt_e = buckets[i + 2] if (i + 2) < nb else n
        if nxt_e <= nxt_s:
            nxt_s, nxt_e = max(nxt_s, n - 1), n
        bt = t[s:e]
        by = np.where(np.isfinite(y[s:e]), y[s:e], 0.0)
        mt_t = float(bt.mean()) if bt.size else 0.0
        mt_y = float(by.mean()) if by.size else 0.0
        bx = t[nxt_s:nxt_e]
        byy = np.where(np.isfinite(y[nxt_s:nxt_e]), y[nxt_s:nxt_e], 0.0)
        if bx.size == 0:
            out_idx.append(s)
            continue
        mb_x, mb_y = float(bx.mean()), float(byy.mean())
        area = np.abs((a_t - mb_x) * (by - a_y) - (a_t - bt) * (mb_y - a_y)) * 0.5
        j = int(np.argmax(area)) if area.size else 0
        out_idx.append(s + j)
    out_idx.append(n - 1)
    idx = np.unique(np.array(out_idx, dtype=int))
    return t[idx], y[idx]


# --------------------------------------------------------------------------- #
# 通道键容错解析
# --------------------------------------------------------------------------- #

# 本模块要求“纯 numpy、零依赖”（方便单测与复用），所以不把 apmlog.core.model 拉进来，
# 自实现一个与 ChannelKey 兼容的宽松解析：支持 TYPE.field / TYPE#N.field / TYPE[N].field
# 三种写法（model.py 的 ChannelKey.parse 已修正为同样兼容这三种，两边结果一致）。
_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)(?:[#\[](\d+)[\]]?)?\.([A-Za-z0-9_]+)\s*$")


def parse_key_tolerant(text: str) -> tuple[str, Optional[int], str]:
    """返回 (type, inst, field)；解析失败返回 (text, None, text)。"""
    m = _KEY_RE.match(text or "")
    if not m:
        return (text or "", None, text or "")
    inst = m.group(2)
    return (m.group(1), None if inst is None else int(inst), m.group(3))
