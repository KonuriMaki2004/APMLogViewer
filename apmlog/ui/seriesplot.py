"""apmlog.ui.seriesplot —— 多泳道共享时间轴的时序图组件（C 工作流）。

设计要点
--------
- N 个泳道严格共享 X 轴（时间，秒，boot 基准），Y 轴各自独立，可固定量程 / 对数。
- 大数据策略：**外层画分桶包络（min/max 填充 + 均值线），放大到可见点数低于阈值时
  自动切回真实折线**。抽稀实现在 ``apmlog.ui._decim``，所有泳道用同一时间栅格，
  保证游标读数不错位。
- 交互：滚轮以鼠标为中心缩放 X、左键拖拽平移、**Ctrl+左键框选区间**、单击定位游标、
  双击复位、方向键微调游标（±0.1s，Shift ±1s）、单道右键菜单。
- 组件不依赖内核 ``LogStore``：所有输入走参数（numpy 数组 + dict），可独立自测。
- 数据缺失（0 点 / 全 nan / 通道不存在）显示占位文字，不抛异常。

公共 API 见 docs/CONTRACT.md 第 5 节 C 工作流，主窗按该签名调用。
"""
from __future__ import annotations

import csv
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from apmlog.core.model import Envelope, Series
from apmlog.ui._decim import (EnvTower, clean_series, envelope_for_range, finite_stats,
                              parse_key_tolerant)
from apmlog.ui.settings import hide_tip, show_tip

LOG = logging.getLogger(__name__)

__all__ = ["LaneSpec", "SeriesPlot", "PALETTES", "SEVERITY_COLORS"]

# --------------------------------------------------------------------------- #
# 调色板：固定可读顺序，深浅主题各 16 色
# --------------------------------------------------------------------------- #

PALETTES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "light": (
        (31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40), (148, 103, 189),
        (140, 86, 75), (227, 119, 194), (127, 127, 127), (188, 189, 34), (23, 190, 207),
        (174, 199, 232), (255, 187, 120), (152, 223, 138), (255, 152, 150), (197, 176, 240),
        (199, 199, 199),
    ),
    "dark": (
        (86, 180, 233), (251, 164, 79), (94, 214, 119), (247, 105, 105), (186, 154, 248),
        (205, 150, 105), (241, 163, 209), (171, 171, 171), (222, 220, 96), (77, 214, 225),
        (138, 180, 232), (255, 200, 140), (140, 230, 160), (255, 170, 170), (210, 190, 250),
        (200, 200, 200),
    ),
}

#: 严重度取色（与内核 SEVERITY_LEVELS 对齐）
SEVERITY_COLORS: dict[str, tuple[int, int, int]] = {
    "info": (130, 140, 150),
    "hint": (40, 130, 200),
    "warn": (232, 168, 0),
    "error": (222, 64, 44),
    "critical": (150, 0, 20),
}

# 徽标只分两档颜色：警告=黄、错误/严重=红（用户要求“标识”而不是满屏文字）
SEVERITY_CN: dict[str, str] = {"info": "信息", "hint": "提示", "warn": "警告",
                               "error": "错误", "critical": "严重"}
_SEV_RANK: dict[str, int] = {"info": 0, "hint": 1, "warn": 2, "error": 3, "critical": 4}

#: 每道默认最小像元高；低于此值时图区出纵向滚动条而不是把曲线压扁
LANE_MIN_H_DEFAULT = 132

_THEME = {
    "light": {"bg": QtGui.QColor(255, 255, 255), "fg": QtGui.QColor(30, 30, 30),
              "grid": (0, 0, 0, 40), "cursor": (70, 70, 70), "band": (60, 120, 220)},
    "dark": {"bg": QtGui.QColor(28, 30, 34), "fg": QtGui.QColor(220, 222, 226),
             "grid": (255, 255, 255, 35), "cursor": (200, 200, 200), "band": (90, 160, 255)},
}


@dataclass
class LaneSpec:
    """一条泳道的输入描述（主窗/内核按此组装）。"""

    label: str
    color: tuple[int, int, int]
    unit: str = ""
    t: Optional[np.ndarray] = None
    y: Optional[np.ndarray] = None
    # 外部预算好的包络（内核 LogStore.envelope 的结果），概览视图优先使用
    env_t: Optional[np.ndarray] = None
    env_min: Optional[np.ndarray] = None
    env_max: Optional[np.ndarray] = None
    env_mean: Optional[np.ndarray] = None
    y_min: Optional[float] = None
    y_max: Optional[float] = None
    band: Optional[tuple[float, float]] = None
    threshold: Optional[float] = None
    style: str = "line"          # line|step|scatter|area
    log_y: bool = False
    hidden: bool = False
    note: str = ""
    key: str = ""                # ChannelKey.key（如 VIBE#0.VibeX）；放大时靠它回内核重新取数
    # 同轴叠加：同一泳道里的对比曲线（目标值/指令值，如 ATT.DesRoll），共用 Y 轴与游标，
    # 但**各有一条胶囊按钮**、各自独立显隐。空列表=单曲线泳道，行为与以前完全一致。
    overlays: list["LaneSpec"] = field(default_factory=list)
    # 全时段恒为 0 / 无有效点：默认隐藏并置灰胶囊（四旋翼不用看 RCOU.C5~C8）
    empty: bool = False

    @staticmethod
    def from_series(s: Series, color: tuple[int, int, int]) -> "LaneSpec":
        """便捷构造：内核 Series -> LaneSpec。"""
        return LaneSpec(label=s.label, color=color, unit=s.unit, t=s.t, y=s.v, note=s.key.label,
                        key=s.key.key)

    @staticmethod
    def from_envelope(env: Envelope, color: tuple[int, int, int]) -> "LaneSpec":
        """便捷构造：内核 Envelope -> 带预包络的 LaneSpec（原始点不给，放大时现算需 t/y）。"""
        return LaneSpec(label=env.label, color=color, unit=env.unit,
                        env_t=env.t, env_min=env.vmin, env_max=env.vmax, env_mean=env.vmean)


# --------------------------------------------------------------------------- #
# 内部：单条泳道的运行时状态
# --------------------------------------------------------------------------- #


#: 稳健量程：1%~99% 分位 + 15% 外扩，专门对付"个别 -180° 跳变点把 Y 轴拉爆"
Y_ROBUST_PCT = (1.0, 99.0)
Y_ROBUST_MARGIN = 0.15
Y_ROBUST_MIN_POINTS = 20          # 点太少时分位数没有意义，退回 min/max


def robustness_range(v: np.ndarray) -> Optional[tuple[float, float]]:
    """按 1%~99% 分位数 + 15% 外扩算 Y 视野；点太少/全 nan 返回 None。

    为什么不用 min/max：ATT.Roll 里偶尔一两个 -180° 的点会把整个泳道压成一条平线，
    真正要看的姿态变化只剩几个像素。分位数区间保住主体，尖峰仍能通过"恢复全量"看。
    """
    a = np.asarray(v, np.float64)
    f = a[np.isfinite(a)]
    if f.size == 0:
        return None
    if f.size >= Y_ROBUST_MIN_POINTS:
        lo, hi = np.percentile(f, Y_ROBUST_PCT)
        span = float(hi) - float(lo)
        if span > 0:
            pad = span * Y_ROBUST_MARGIN
            return (float(lo) - pad, float(hi) + pad)
    lo, hi = float(f.min()), float(f.max())
    if hi - lo <= 0:
        pad = abs(hi) * 0.1 if hi else 1.0
        return (hi - pad, hi + pad)
    pad = (hi - lo) * 0.06
    return (lo - pad, hi + pad)


def _log10_or_identity(y: np.ndarray, log_y: bool) -> np.ndarray:
    """对数显示变换（log_y=False 时原样返回）；非正值置 nan。"""
    if not log_y:
        return y
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.isfinite(y) & (y > 0), np.log10(np.where(y > 0, y, 1.0)), np.nan)


#: 叠加曲线（目标值/指令值）的默认配色：橙色系，和实测曲线一眼分开
OVERLAY_COLORS: tuple[tuple[int, int, int], ...] = (
    (232, 120, 40), (150, 90, 200), (20, 170, 160), (200, 60, 120),
)


class _Ghost:
    """同一泳道里的叠加曲线（目标/对比）：共用主曲线的 X/Y 轴与游标，独立显隐。

    只画一条虚线（raw 模式）或均值包络（概览模式），不画 min/max 填充——
    叠加曲线的作用是"和主曲线比形状"，画满填充会盖住主曲线。
    """

    def __init__(self, spec: LaneSpec, host: "_Lane", theme: str) -> None:
        self.spec = spec
        self.host = host
        self.label = spec.label or "对比"
        self.unit = spec.unit
        self.hidden = bool(spec.hidden)
        self.t = np.empty(0, np.float64)
        self.y = np.empty(0, np.float64)
        self.pre_env: Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None
        self.tower: Optional[EnvTower] = None
        self.mode = "raw"
        self.has_data = False
        # color=None 表示"还没定"（运行时叠加会交给 OVERLAY_COLORS 按序分配）
        if spec.color is None or len(spec.color) != 3:
            spec.color = OVERLAY_COLORS[0]
        c = tuple(spec.color)
        pen = pg.mkPen(c, width=1.5, style=QtCore.Qt.DashLine)
        self.raw = pg.PlotCurveItem(pen=pen, connect="finite", skipFiniteCheck=False,
                                    autoDownsample=False)
        self.raw.setZValue(3)
        host.plot.addItem(self.raw)
        self.env_mean = pg.PlotCurveItem(pen=pg.mkPen(c, width=1.7, style=QtCore.Qt.DashLine))
        self.env_mean.setZValue(-4)
        host.plot.addItem(self.env_mean)

    def load(self) -> None:
        s = self.spec
        if s.t is not None and s.y is not None:
            self.t, self.y = clean_series(s.t, s.y)
        if s.env_t is not None and s.env_min is not None and s.env_max is not None:
            mean = s.env_mean if s.env_mean is not None else (
                (np.asarray(s.env_min, float) + np.asarray(s.env_max, float)) / 2.0)
            self.pre_env = (np.asarray(s.env_t, np.float64), np.asarray(s.env_min, np.float64),
                            np.asarray(s.env_max, np.float64), np.asarray(mean, np.float64))
        self.has_data = (self.t.size and bool(np.isfinite(self.y).any())) or (
            self.pre_env is not None and bool(np.isfinite(self.pre_env[1]).any()
                                              or np.isfinite(self.pre_env[2]).any()))
        self.tower = None
        if self.t.size >= 20000:
            try:
                self.tower = EnvTower(self.t, self.y, ppb=128)
            except Exception:                  # noqa: BLE001
                LOG.exception("%s 建塔失败，退回直接分桶", self.label)
                self.tower = None

    def _tf(self, y: np.ndarray) -> np.ndarray:
        return _log10_or_identity(y, bool(self.host.log_y))

    def span(self) -> tuple[float, float]:
        cands: list[tuple[float, float]] = []
        if self.t.size:
            cands.append((float(self.t[0]), float(self.t[-1])))
        if self.pre_env is not None and self.pre_env[0].size:
            cands.append((float(self.pre_env[0][0]), float(self.pre_env[0][-1])))
        if not cands:
            return (float("nan"), float("nan"))
        return (min(a for a, _ in cands), max(b for _, b in cands))

    def visible_counts(self, x0: float, x1: float) -> int:
        if self.t.size == 0:
            return 0
        i0 = int(np.searchsorted(self.t, x0, side="left"))
        i1 = int(np.searchsorted(self.t, x1, side="right"))
        return max(0, i1 - i0)


class _EventStrips(pg.GraphicsObject):
    """一个泳道上所有事件竖标的**单一**图元（2026-10-29 性能修复）。

    以前每个事件一条 InfiniteLine：103 MB / 2.6M 报文的日志上有 643 个事件 × 5 道
    = 3300+ 图元，一次重绘就要 0.8 s，缩放、切页、悬停全部跟着卡。
    现在改为自绘：图元恒为 1 个，``paint()`` 只画当前视图范围内的事件，代价是
    O(可见事件)，与日志总事件数无关。
    """

    def __init__(self) -> None:
        super().__init__()
        self._ev: list[tuple[float, tuple[int, int, int]]] = []
        self.setZValue(10)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def set_events(self, ev: list[tuple[float, tuple[int, int, int]]]) -> None:
        if ev == self._ev:
            return
        self.prepareGeometryChange()
        self._ev = ev
        self.update()

    def count(self) -> int:
        return len(self._ev)

    def boundingRect(self) -> QtCore.QRectF:      # noqa: N802  Qt 命名
        # 图元挂在 ViewBox 的数据坐标系里；范围给大，实际不参与自动量程（ignoreBounds）
        return QtCore.QRectF(-1e9, -1e9, 2e9, 2e9)

    def paint(self, p: QtGui.QPainter, *args: Any) -> None:      # noqa: D401
        if not self._ev:
            return
        vb = self.getViewBox()
        if vb is None:
            return
        (x0, x1), (y0, y1) = vb.viewRange()
        if not (math.isfinite(x0) and math.isfinite(x1)) or x1 <= x0:
            return
        h = y1 - y0
        if not math.isfinite(h) or h <= 0:
            return
        top = y1
        bottom = y1 - 0.16 * h          # 只在泳道顶部画一段，与旧实现一致
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        pen = QtGui.QPen()
        pen.setWidth(0)                 # cosmetic pen：无论缩放都是 1 像素
        p.setPen(pen)
        for t, col in self._ev:
            if t < x0 or t > x1:
                continue
            pen.setColor(QtGui.QColor(*col))
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(t, bottom), QtCore.QPointF(t, top))


class _Lane:
    """一个泳道：PlotItem + 真实折线 + 包络图元 + 游标 + 读数。"""

    def __init__(self, spec: LaneSpec, index: int, theme: str):
        self.spec = spec
        self.index = index
        self.label = spec.label or f"lane{index}"
        self.key_text = spec.note or self.label
        self.t = np.empty(0, np.float64)
        self.y = np.empty(0, np.float64)
        self.pre_env: Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None
        self.y_fixed = (spec.y_min is not None and spec.y_max is not None)
        self.log_y = bool(spec.log_y)
        self.hidden = bool(spec.hidden)
        self.key = str(getattr(spec, "key", "") or "")
        self.has_data = False

        th = _THEME[theme]
        color = spec.color

        self.plot = pg.PlotItem(menuEnabled=False)
        # pyqtgraph LabelItem 的 size 必须是字符串（'8pt'），不能传 None
        self.plot.setTitle(self.label, color=th["fg"].name(), size="8pt")
        # 标题/轴标签也是中文，给它们显式设中文字体（Windows 默认 Segoe UI 无 CJK）
        for _axis in ("top", "left"):
            _ai = self.plot.getAxis(_axis)
            if getattr(_ai, "label", None) is not None:
                _ai.label.setFont(_ui_font(8))
        self.plot.showGrid(x=False, y=True, alpha=0.30)
        self.plot.setMenuEnabled(False)          # 用我们自己的右键菜单
        # 量程由组件自己管（固定量程/自动缩放），不留 pyqtgraph 的悬停“A”按钮
        self.plot.hideButtons()
        ax = self.plot.getAxis("left")
        ax.setWidth(64)                               # 左侧标尺窄一点，把宽度让给曲线
        ax.setLabel(text=self.label, units=spec.unit)

        pen = pg.mkPen(color=color, width=1.9)        # 低分辨率屏上 1.2px 几乎看不见
        self.raw = pg.PlotCurveItem(pen=pen, connect="finite", skipFiniteCheck=False, autoDownsample=False)
        self.plot.addItem(self.raw)

        fill = pg.mkBrush(color[0], color[1], color[2], 70)
        ep = pg.mkPen(color=color, width=1.3)
        self.env_lo = pg.PlotCurveItem(pen=ep)
        self.env_hi = pg.PlotCurveItem(pen=ep)
        self.env_mean = pg.PlotCurveItem(pen=pg.mkPen(color=color, width=1.7))
        self.env_fill = pg.FillBetweenItem(self.env_lo, self.env_hi, brush=fill)
        for it in (self.env_fill, self.env_lo, self.env_hi, self.env_mean):
            it.setZValue(-5)
            self.plot.addItem(it)

        # 参考 band（半透明区间）
        self.band_item: Optional[pg.LinearRegionItem] = None
        if spec.band is not None:
            lo, hi = float(spec.band[0]), float(spec.band[1])
            self.band_item = pg.LinearRegionItem(values=(min(lo, hi), max(lo, hi)),
                                                 orientation="horizontal", movable=False,
                                                 brush=pg.mkBrush(th["band"][0], th["band"][1],
                                                                  th["band"][2], 34),
                                                 pen=pg.mkPen(th["band"], style=QtCore.Qt.DashLine))
            self.band_item.setZValue(-20)
            self.plot.addItem(self.band_item)

        # 阈值线（红虚线 + 数值标注）
        self.thr_item: Optional[pg.InfiniteLine] = None
        if spec.threshold is not None:
            self.thr_item = pg.InfiniteLine(pos=float(spec.threshold), angle=0, movable=False,
                                            pen=pg.mkPen(SEVERITY_COLORS["error"], width=1.2,
                                                         style=QtCore.Qt.DashLine),
                                            label=f"{spec.threshold:g}",
                                            labelOpts={"color": SEVERITY_COLORS["error"],
                                                       "position": 0.02, "movable": False})
            self.thr_item.setZValue(-15)
            self.plot.addItem(self.thr_item)

        # 注意：InfiniteLine 的 bounds 是"允许值范围"（会钳住数值），
        # 视觉只画一段要用 span。游标不能限幅，否则时间一超过 1.0 就被钉住。
        self.cursor = pg.InfiniteLine(angle=90, movable=True,
                                      pen=pg.mkPen(th["cursor"], width=1),
                                      hoverPen=pg.mkPen(th["cursor"], width=2))
        self.cursor.setZValue(20)
        self.plot.addItem(self.cursor, ignoreBounds=True)
        self.cursor_label = pg.TextItem(color=th["fg"], anchor=(0.0, 0.0))
        self.cursor_label.setFont(_small_font())
        self.cursor_label.setZValue(21)
        self.plot.addItem(self.cursor_label, ignoreBounds=True)

        self.empty_txt = pg.TextItem("无数据", color=(150, 150, 150), anchor=(0.5, 0.5))
        self.empty_txt.setZValue(30)
        self.plot.addItem(self.empty_txt, ignoreBounds=True)

        self.event_items: list[Any] = []      # 兼容旧接口：现在存的是事件清单，不是图元
        self.event_strips = _EventStrips()
        self.plot.addItem(self.event_strips, ignoreBounds=True)
        self.badge_items: list[pg.TextItem] = []      # 顶部感叹号徽标（只挂在首泳道）
        self.region_items: list[pg.LinearRegionItem] = []
        self.selection: Optional[pg.LinearRegionItem] = None
        self.mode = "raw"
        self.tower: Optional[EnvTower] = None      # 大数组的分层包络塔（缩放不扫全量）
        self.tower_ms: float = 0.0
        ty, inst, fld = parse_key_tolerant(spec.note or self.label)
        self.key_text = f"{ty}[{inst}].{fld}" if inst is not None else f"{ty}.{fld}"
        # 同轴叠加曲线（若预设给了）：共用本泳道的 PlotItem，各自一条胶囊
        self.ghosts: list[_Ghost] = [
            _Ghost(g, self, theme) for g in (getattr(spec, "overlays", None) or ())
        ]

    # ---- 数据装载 ------------------------------------------------------- #

    def load(self) -> None:
        """清洗输入数据；乱序时间轴内部排序（不改动调用方数组）。"""
        s = self.spec
        if s.t is not None and s.y is not None:
            self.t, self.y = clean_series(s.t, s.y)
        else:
            self.t, self.y = np.empty(0, np.float64), np.empty(0, np.float64)
        if s.env_t is not None and s.env_min is not None and s.env_max is not None:
            mean = s.env_mean if s.env_mean is not None else (
                (np.asarray(s.env_min, float) + np.asarray(s.env_max, float)) / 2.0)
            self.pre_env = (np.asarray(s.env_t, np.float64), np.asarray(s.env_min, np.float64),
                            np.asarray(s.env_max, np.float64), np.asarray(mean, np.float64))
        else:
            self.pre_env = None
        self.has_data = self._check_has_data()
        for g in self.ghosts:
            g.load()
            if g.has_data:
                self.has_data = True          # 主曲线空、但叠加曲线有数据时，整道仍算有数据
        # 大数组就一次建塔：之后缩放只合并 O(输出桶) 个桶，不再扫百万可见点
        self.tower = None
        self.tower_ms = 0.0
        if self.t.size >= 20000:
            t0 = time.perf_counter()
            try:
                self.tower = EnvTower(self.t, self.y, ppb=128)
            except Exception:                      # noqa: BLE001
                LOG.exception("建包络塔失败，退回直接分桶")
                self.tower = None
            self.tower_ms = (time.perf_counter() - t0) * 1000.0
            if self.tower is not None:
                LOG.debug("%s 建塔 %.1fms 层=%d", self.label, self.tower_ms,
                          self.tower.levels if hasattr(self.tower, "levels") else -1)

    def _check_has_data(self) -> bool:
        """"有数据" = 至少一个有限值；全 nan / 只有时间轴 算无数据，要显示占位文字。"""
        if self.t.size and np.isfinite(self.y).any():
            return True
        if self.pre_env is not None:
            _t, vmin, vmax, vmean = self.pre_env
            return bool(vmin.size and (np.isfinite(vmin).any() or np.isfinite(vmax).any()
                                       or np.isfinite(vmean).any()))
        return False

    def span(self) -> tuple[float, float]:
        cands: list[tuple[float, float]] = []
        if self.t.size:
            cands.append((float(self.t[0]), float(self.t[-1])))
        if self.pre_env is not None and self.pre_env[0].size:
            cands.append((float(self.pre_env[0][0]), float(self.pre_env[0][-1])))
        if not cands:
            return (float("nan"), float("nan"))
        return (min(a for a, _ in cands), max(b for _, b in cands))

    # ---- 变换 ----------------------------------------------------------- #

    def _tf(self, y: np.ndarray) -> np.ndarray:
        """对数显示变换：只取正值，其余置 nan。"""
        if not self.log_y:
            return y
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(np.isfinite(y) & (y > 0), np.log10(np.where(y > 0, y, 1.0)), np.nan)
        return out

    def visible_counts(self, x0: float, x1: float) -> int:
        if self.t.size == 0:
            return 0
        i0 = int(np.searchsorted(self.t, x0, side="left"))
        i1 = int(np.searchsorted(self.t, x1, side="right"))
        return max(0, i1 - i0)


def _rng_bounds(rng: Any) -> tuple[float, float]:
    """兼容 pyqtgraph 的各种范围对象：list/tuple、AxisRange(有 left/right)。"""
    if hasattr(rng, "left") and hasattr(rng, "right"):
        return (float(rng.left()), float(rng.right()))
    a = list(rng)
    return (float(a[0]), float(a[1]))


def _ev_pos(ev: QtCore.QEvent) -> QtCore.QPoint:
    """鼠标/滚轮事件的局部坐标；非鼠标事件或取不到时返回 (0,0)，绝不抛异常。"""
    for attr in ("position", "pos"):          # position() 是 Qt6 正路，pos() 已废弃但兼容老事件
        getter = getattr(ev, attr, None)
        if getter is None:
            continue
        try:
            val = getter()
            return val.toPoint() if hasattr(val, "toPoint") else val
        except Exception:                      # noqa: BLE001
            continue
    return QtCore.QPoint(0, 0)


# 中文字体回退表：offscreen 平台字体库可能为空（只剩豆腐块），真机上按此顺序命中
_CJK_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
                 "Source Han Sans SC", "WenQuanYi Micro Hei", "SimHei", "Segoe UI")


def _ui_font(point_size: int = 9, mono: bool = False) -> QtGui.QFont:
    """带中文回退的字体；mono=True 时优先等宽（读数条对齐好看）。"""
    f = QtGui.QFont()
    fams = (["Consolas", "Cascadia Mono", "Menlo"] if mono else []) + list(_CJK_FAMILIES)
    f.setFamilies(fams)
    f.setPointSize(point_size)
    return f


def _badge_font() -> QtGui.QFont:
    """感叹号徽标用的粗体小字。"""
    f = _ui_font(9)
    f.setBold(True)
    return f


def _small_font() -> QtGui.QFont:
    """读数/事件文字用的小字号。"""
    return _ui_font(7)


# --------------------------------------------------------------------------- #
# 主组件
# --------------------------------------------------------------------------- #


class SeriesPlot(QtWidgets.QWidget):
    """多泳道时间序列组件。公共 API 见模块 docstring 与 CONTRACT.md。"""

    timeChanged = QtCore.Signal(float)              # 游标位置变化（用户操作）
    regionChanged = QtCore.Signal(float, float)     # 框选区间确定
    laneVisibilityChanged = QtCore.Signal(str, bool)
    viewChanged = QtCore.Signal(float, float)       # 视图时间窗变化（滚轮缩放/平移，去抖后发）
    themeChanged = QtCore.Signal(str)               # 主题切换（主窗据此写回设置）
    #: 插件曲线工具被点中：``(工具名, {"lane_index","lane_label","keys","t0","t1","whole"})``
    #: 这里只发意图，真正的调用（含异常隔离）由主窗交给 PluginManager
    plotToolRequested = QtCore.Signal(str, object)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None, theme: str = "light",
                 raw_point_limit: Optional[int] = None, nbuckets: Optional[int] = None):
        super().__init__(parent)
        self._theme = theme if theme in _THEME else "light"
        self._lanes: list[_Lane] = []
        self._root_plot: Optional[pg.PlotItem] = None
        self._suspend = 0                     # >0 时不再发信号，防止递归
        self._t_span: tuple[float, float] = (float("nan"), float("nan"))
        self._view: tuple[float, float] = (float("nan"), float("nan"))
        self._cursor_t: float = float("nan")
        self._raw_limit_override = raw_point_limit
        self._nbuckets_override = nbuckets
        self._last_counts: dict[int, np.ndarray] = {}   # 泳道号 -> 当前可见桶的点数
        self._events: list[dict[str, Any]] = []
        self._regions: list[dict[str, Any]] = []
        self._press_pos: Optional[QtCore.QPoint] = None
        self._badges: list[dict[str, Any]] = []      # {'t','lines','sev','n'}，随视图重算
        self._badge_lane: Any = None                 # 徽标挂在第一条可见泳道上
        self._badge_px_tol = 9.0                     # 悬停命中容差（像素）
        self._lane_min_h = LANE_MIN_H_DEFAULT        # 每道最小像元高，不够就出滚动条
        self._selecting: Optional[_Lane] = None
        self._plot_tools: list[str] = []      # 插件注册的右键工具名（本体在主窗里）
        self._sel_x0: float = float("nan")
        self._y_full: set[int] = set()          # 被"恢复全量范围"钉住的泳道号
        self._follow = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        # 顶部可点击图例条（比 pyqtgraph 内建 Legend 更适合做显隐切换）
        self._header = QtWidgets.QWidget(self)
        hl = QtWidgets.QHBoxLayout(self._header)
        hl.setContentsMargins(6, 2, 6, 2)
        hl.setSpacing(6)
        self._chips: list[QtWidgets.QPushButton] = []
        self._chip_map: dict[tuple[int, int], QtWidgets.QPushButton] = {}   # (道号, 曲线号) -> 胶囊
        # 切页/缩放时用来“只换数据、不重建控件”的指纹（见 set_lanes）
        self._lane_sig: Optional[tuple] = None
        self._events_sig: Optional[tuple] = None
        self._regions_sig: Optional[tuple] = None
        self.setFont(_ui_font(9))
        self._theme_btn = QtWidgets.QToolButton(self._header)
        self._theme_btn.setText("主题")
        self._theme_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self._theme_btn.setToolTip("深/浅主题切换")
        for name in ("light", "dark"):
            act = QtGui.QAction(name, self._theme_btn)
            act.triggered.connect(lambda _c=False, n=name: self.set_theme(n))
            self._theme_btn.addAction(act)
        hl.addWidget(self._theme_btn)
        hl.addStretch(1)
        # 胶囊行不能直接进页面布局：一条曲线一个胶囊，日志泳道/幽灵曲线一多，
        # 这一行就长到 1800+，把整个标签区的最小宽顶起来，左侧分隔条就拉不动了
        # （2026-10-29 实测：画过曲线页后 page.min 906 → 1836）。
        # 包一层横向滚动宿主：装不下就出横向滚动条，胶囊仍可滚到。
        self._header_host = QtWidgets.QScrollArea(self)
        self._header_host.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._header_host.setWidgetResizable(True)
        self._header_host.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._header_host.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._header_host.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._header_host.setWidget(self._header)
        self._header_host.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                        QtWidgets.QSizePolicy.Policy.Fixed)
        self._header_host.setMinimumHeight(self._header.sizeHint().height())
        self._header_host.setMaximumHeight(self._header.sizeHint().height())
        self._header_host.horizontalScrollBar().rangeChanged.connect(
            lambda _a, _b: QtCore.QTimer.singleShot(0, self._sync_header_height))
        lay.addWidget(self._header_host)

        self._graphics = pg.GraphicsLayoutWidget(None)
        self._graphics.setBackground(_THEME[self._theme]["bg"])
        self._graphics.ci.setSpacing(4)
        # 注意：mkPen(0) 会被 pyqtgraph 当成调色板序号 0 = 红色实线，“无边框”必须传 None
        self._graphics.ci.setBorder(None)
        self._graphics.setMinimumWidth(560)
        # 背景（网格、坐标轴、刻度）缓存成一张 pixmap：每次切回曲线页 Qt 都要重画一遍
        # viewport，103 MB 日志 5 道实测重画 78 ms，缓存后只剩重画数据项。
        # 主题变了/泳道重建时必须 resetCachedContent（见 set_theme / _clear_lanes）。
        self._graphics.setCacheMode(QtWidgets.QGraphicsView.CacheModeFlag.CacheBackground)
        # 泳道多时不能把它们压扁：给每道一个最小像元高，装不下就纵向滚动。
        # （1366×768 上开 10 道时，不滚动的话每道只剩 80px，曲线基本看不出来）
        self._scroll = QtWidgets.QScrollArea(self)
        self._scroll.setWidgetResizable(True)         # 宽度跟随视口，高度不低于 minimumHeight
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setWidget(self._graphics)
        lay.addWidget(self._scroll, 1)
        # 交互全部用 viewport（QWidget）上的事件过滤器实现：
        # ViewBox 是 QGraphicsItem，不是 QObject，不能直接装事件过滤器。
        vp = self._graphics.viewport()
        vp.installEventFilter(self)
        vp.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        vp.customContextMenuRequested.connect(self._on_context_menu)

        # 底部读数条
        self._readout = QtWidgets.QLabel("游标：未设置", self)
        self._readout.setFont(_ui_font(9, mono=True))
        self._readout.setTextFormat(QtCore.Qt.PlainText)
        self._readout.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self._readout.setToolTip("游标时刻与各泳道读数；Ctrl+左键可框选区间")
        lay.addWidget(self._readout)

        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(16)             # ~60fps 去抖
        self._timer.timeout.connect(self._refresh)
        # 视图变化稳定 350ms 后再告诉外部，避免滚轮一路放大就一路回内核取数
        self._refine_timer = QtCore.QTimer(self)
        self._refine_timer.setSingleShot(True)
        self._refine_timer.setInterval(350)
        self._refine_timer.timeout.connect(self._emit_view_changed)

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def set_lanes(self, lanes: Sequence[LaneSpec], t_span: Optional[tuple[float, float]] = None) -> None:
        """重建全部泳道；**同一批通道只是换了窗口时只换数据，不重建控件**。

        切换页签 / 缩放后都会走到这里。以前无条件是“拆光重建”（5 道 × PlotItem +
        胶囊 + 坐标轴），103 MB 日志实测 302 ms／次，切页卡顿就是这个。
        现在先比签名：通道集合、颜色、单位、阈值、band、叠加曲线都没变，就只把新数据
        装回现有 ``_Lane``，把控件创建全部省掉。
        """
        hide_tip()          # 提示可以“不自动消失”，重建泳道时旧的读数必须关掉
        # 重建过程中先关绘制：一次 set_lanes 会触发 setData / 范围变化 / set_events 好几轮
        # 请求重画，103 MB 日志实测能画三整遍（0.3 s 全花在重复 paintEvent 上）。
        g = self._graphics
        was_on = g.updatesEnabled()
        g.setUpdatesEnabled(False)
        try:
            self._set_lanes_inner(lanes, t_span)
        finally:
            g.setUpdatesEnabled(was_on)
            if was_on:
                g.viewport().update()

    def _set_lanes_inner(self, lanes: Sequence[LaneSpec],
                         t_span: Optional[tuple[float, float]] = None) -> None:
        sig = self._lanes_signature(lanes)
        if lanes and sig == self._lane_sig and len(lanes) == len(self._lanes):
            self._update_lanes_in_place(lanes)
            self._view = (float("nan"), float("nan"))
            self._apply_min_height()
            span = self._compute_span()
            if t_span is not None and len(t_span) == 2 and all(math.isfinite(v) for v in t_span):
                self._t_span = (float(t_span[0]), float(t_span[1]))
            else:
                self._t_span = span
            self._set_view(self._t_span, emit=False)
            self.set_events(self._events)
            self.set_regions(self._regions)
            self._refresh()
            return
        self._clear_lanes()
        if not lanes:
            self._refresh()
            return
        n = len(lanes)
        for i, spec in enumerate(lanes):
            if spec.color is None or len(spec.color) != 3:
                spec.color = PALETTES[self._theme][i % 16]
            lane = _Lane(spec, i, self._theme)
            lane.load()
            lane.plot.setMinimumHeight(self._row_h())     # 不让网格布局把它压回 90px
            self._lanes.append(lane)
            self._graphics.ci.addItem(lane.plot, row=i, col=0)
            if i > 0:
                lane.plot.setXLink(self._lanes[0].plot)
            else:
                self._root_plot = lane.plot
            axb = lane.plot.getAxis("bottom")
            last = (i == n - 1)
            axb.setStyle(showValues=last)
            # 时间轴标题只给最下面一条，否则八道日志会被八行“时间”挤爆
            axb.setLabel(text="时间 (s, 自 boot)" if last else "")
            axb.setHeight(34 if last else 4)   # 留出刻度+轴标题两行，否则"时间"会和数字叠在一起
            lane.cursor.setValue(0.0)
            lane.cursor.sigPositionChanged.connect(self._on_cursor_moved)
            self._add_chip(i, lane)
            # 叠加曲线也各有一个胶囊：点谁切谁，互不影响
            for k in range(len(getattr(lane, "ghosts", ()))):
                self._add_chip(i, lane, curve=k + 1)
            lane.plot.sigXRangeChanged.connect(self._on_view_changed)
            self._apply_lane_style(lane)

        self._view = (float("nan"), float("nan"))
        self._apply_min_height()
        span = self._compute_span()
        if t_span is not None and len(t_span) == 2 and all(math.isfinite(v) for v in t_span):
            self._t_span = (float(t_span[0]), float(t_span[1]))
        else:
            self._t_span = span
        self._set_view(self._t_span, emit=False)
        self.set_events(self._events)
        self.set_regions(self._regions)
        self._refresh()
        self._lane_sig = sig

    # ---- 原地换数据（不重建控件）---------------------------------------- #
    def _lanes_signature(self, lanes: Sequence[LaneSpec]) -> tuple:
        """描述“这批泳道长什么样”——只包含影响控件结构的字段，不含数据本身。"""
        def color_of(c: Any) -> tuple:
            try:
                return tuple(int(v) for v in (c or ()))
            except Exception:                      # noqa: BLE001
                return ()

        def one(s: LaneSpec) -> tuple:
            return (str(s.label), str(s.unit or ""), str(getattr(s, "note", "") or ""),
                    color_of(s.color),
                    None if s.threshold is None else float(s.threshold),
                    None if s.band is None else (float(s.band[0]), float(s.band[1])),
                    None if s.y_min is None else float(s.y_min),
                    None if s.y_max is None else float(s.y_max),
                    bool(s.log_y),
                    tuple((str(g.label), str(g.unit or ""), color_of(g.color))
                          for g in (getattr(s, "overlays", None) or ())))
        return tuple(one(s) for s in lanes)

    def _update_lanes_in_place(self, lanes: Sequence[LaneSpec]) -> None:
        """把新窗口的数据装回现有泳道。控件、胶囊、坐标轴、事件条带全部保留。

        ``hidden`` 不进签名，也不在这里重置：用户自己在胶囊上取消勾选的通道，
        不该因为切了一次页又自己回来。
        """
        for lane, spec in zip(self._lanes, lanes):
            if spec.color is None or len(spec.color) != 3:
                spec.color = PALETTES[self._theme][lane.index % 16]
            lane.spec = spec
            lane.key = str(getattr(spec, "key", "") or "")
            for gi, (ghost, gspec) in enumerate(
                    zip(lane.ghosts, getattr(spec, "overlays", None) or ())):
                if gspec.color is None or len(gspec.color) != 3:
                    gspec.color = OVERLAY_COLORS[gi % len(OVERLAY_COLORS)]
                ghost.spec = gspec
            lane.load()
            lane.mode = "raw"
            self._apply_lane_style(lane)
            self._restyle_chip(lane, spec)

    def _restyle_chip(self, lane: _Lane, spec: LaneSpec) -> None:
        """“空通道”标记变了只改胶囊的置灰/勾选，**不**为它重建泳道。

        空通道是按当前窗口判的（某个通道在放大后可能变成全 0），以前它一变
        整页就拆了重建，切页时的 1 s 卡顿就是这么来的。
        """
        btn = self._chip_map.get((lane.index, 0))
        if btn is None:
            return
        empty = bool(getattr(spec, "empty", False))
        cur_empty = bool(btn.property("empty"))
        if cur_empty == empty and btn.isChecked() == (not lane.hidden):
            return
        c = tuple(spec.color)
        if empty:
            btn.setStyleSheet(
                "QPushButton{padding:1px 6px;border:1px solid #b9bcc2;border-radius:8px;"
                "background:#e9eaec;color:#8a8f98;}"
                "QPushButton::checked{background:#c9ccd2;color:#5a5f68;}")
            btn.setToolTip(f"{lane.label}：该通道当前窗口内恒为 0（空通道）；点击可显示")
        else:
            btn.setStyleSheet(
                f"QPushButton{{padding:1px 6px;border:1px solid #{c[0]:02x}{c[1]:02x}{c[2]:02x};"
                f"border-radius:8px;background:#{c[0]:02x}{c[1]:02x}{c[2]:02x}22;}}"
                f"QPushButton::checked{{background:#{c[0]:02x}{c[1]:02x}{c[2]:02x};color:white;}}"
                f"QPushButton::!checked{{color:gray;}}")
            btn.setToolTip(f"{lane.label}｜主曲线｜点击单独隐藏/显示")
        btn.setProperty("empty", empty)
        if btn.isChecked() != (not lane.hidden):
            btn.blockSignals(True)
            btn.setChecked(not lane.hidden)
            btn.blockSignals(False)
        self._sync_header_height()

    def lane_labels(self) -> list[str]:
        """当前泳道的标签（按显示顺序），给"叠加到哪条道"的下拉用。"""
        return [l.label for l in self._lanes]

    # ---- 插件曲线工具 --------------------------------------------------- #
    def set_plot_tools(self, names: Sequence[str]) -> None:
        """设置右键菜单里的插件工具名（空列表 = 不显示这一段）。"""
        self._plot_tools = [str(n) for n in names if str(n).strip()]

    def plot_tools(self) -> list[str]:
        return list(self._plot_tools)

    def _tool_payload(self, lane: "_Lane") -> dict[str, Any]:
        """组装选区载荷：有框选就给选区，否则整段（whole=True）。"""
        t0 = t1 = float("nan")
        whole = True
        try:
            if lane.selection is not None:
                a, b = lane.selection.getRegion()
                t0, t1 = float(min(a, b)), float(max(a, b))
                whole = False
            else:
                vb = lane.plot.getViewBox()
                rng = vb.viewRange() if vb is not None else None
                if rng:
                    t0, t1 = float(rng[0][0]), float(rng[0][1])
        except Exception:                            # noqa: BLE001 拿不到范围就给 nan
            t0 = t1 = float("nan")
        keys: list[str] = []
        specs = [lane.spec] + [g.spec for g in getattr(lane, "ghosts", ())]
        for spec in specs:
            k = str(getattr(spec, "key", "") or "")
            if k and k not in keys:
                keys.append(k)
        return {"lane_index": lane.index, "lane_label": lane.label, "keys": keys,
                "t0": t0, "t1": t1, "whole": whole}

    def add_overlay(self, idx: int, spec: LaneSpec) -> bool:
        """给**已存在**的泳道追加一条叠加曲线（连它自己的胶囊一起建）。

        与预设里声明的 overlays 走同一套 `_Ghost`：同一个 ViewBox、同一个 Y 轴，
        但显隐独立。用于运行时的「叠加绘制」。
        """
        if idx < 0 or idx >= len(self._lanes):
            return False
        lane = self._lanes[idx]
        if spec.color is None or len(spec.color) != 3:
            spec.color = OVERLAY_COLORS[len(lane.ghosts) % len(OVERLAY_COLORS)]
        if not spec.unit:
            spec.unit = lane.spec.unit      # 同一根 Y 轴，单位跟着主曲线
        if not spec.label:
            spec.label = "对比"
        g = _Ghost(spec, lane, self._theme)
        lane.ghosts.append(g)
        g.load()
        if g.has_data:
            lane.has_data = True
        self._add_chip(idx, lane, curve=len(lane.ghosts))
        self._apply_lane_style(lane)
        self._schedule_refresh()
        return True

    def overlay_count(self, idx: int) -> int:
        if idx < 0 or idx >= len(self._lanes):
            return 0
        return len(self._lanes[idx].ghosts)

    def set_time(self, t: float) -> None:
        """外部移动游标：**不得**回发 timeChanged。"""
        if t is None or not math.isfinite(float(t)):
            return
        self._suspend += 1
        try:
            self._cursor_t = float(t)
            for lane in self._lanes:
                lane.cursor.blockSignals(True)
                lane.cursor.setValue(self._cursor_t)
                lane.cursor.blockSignals(False)
            self._update_readout()
            if self._follow:
                self._keep_cursor_in_view()
        finally:
            self._suspend -= 1

    def set_events(self, events: Sequence[dict]) -> None:
        """事件小竖标：{'t','severity','display'}。

        竖标由每道一个 ``_EventStrips`` 自绘，不再每事件一个 InfiniteLine；
        ``lane.event_items`` 保留为“本道收到的事件清单”（旧接口语义）。
        """
        self._events = [dict(e) for e in (events or [])]
        strips: list[tuple[float, tuple[int, int, int]]] = []
        for ev in self._events[:2000]:
            t = ev.get("t")
            if t is None or not math.isfinite(float(t)):
                continue
            sev = str(ev.get("severity", "info"))
            strips.append((float(t), SEVERITY_COLORS.get(sev, SEVERITY_COLORS["info"])))
        # 同一批事件（切页、拖动窗口、缩放都会重调）就不要再装一遗
        if strips == self._events_sig and self._lanes:
            return
        self._events_sig = strips
        for lane in self._lanes:
            lane.event_strips.set_events(strips)
            lane.event_items = list(strips)
        # 文字不直接画在图上（会挤成一团），改用顶部感叹号徽标 + 悬停提示，
        # 徽标位置依赖当前视图范围，所以在 _refresh() 的 _relayout_badges() 里生成。
        self._badges = []
        self._relayout_badges()

    def set_regions(self, regions: Sequence[dict]) -> None:
        """告警/选中区间纵向半透明带：{'t0','t1','severity','title'}。"""
        self._regions = [dict(r) for r in (regions or [])]
        key = [(float(r.get("t0", float("nan"))), float(r.get("t1", float("nan"))),
                str(r.get("severity", ""))) for r in self._regions[:400]]
        if key == self._regions_sig and self._lanes:
            return                       # 告警区间没变：160 个 LinearRegionItem 不要重建
        self._regions_sig = key
        for lane in self._lanes:
            for it in lane.region_items:
                lane.plot.removeItem(it)
            lane.region_items = []
        for rg in self._regions[:400]:
            t0 = rg.get("t0", float("nan"))
            t1 = rg.get("t1", float("nan"))
            if not (math.isfinite(float(t0)) and math.isfinite(float(t1))) or float(t1) <= float(t0):
                continue
            col = SEVERITY_COLORS.get(str(rg.get("severity", "warn")), SEVERITY_COLORS["warn"])
            for lane in self._lanes:
                it = pg.LinearRegionItem(values=(float(t0), float(t1)), movable=False,
                                         brush=pg.mkBrush(col[0], col[1], col[2], 28),
                                         pen=pg.mkPen(col, width=1, style=QtCore.Qt.DotLine))
                it.setZValue(-30)
                lane.plot.addItem(it)
                lane.region_items.append(it)

    def zoom_to(self, t0: float, t1: float) -> None:
        if not (math.isfinite(float(t0)) and math.isfinite(float(t1))) or float(t1) <= float(t0):
            return
        self._set_view((float(t0), float(t1)))

    def reset_view(self) -> None:
        self._set_view(self._t_span)
        self._y_full.clear()            # 整体重置时不再保留"全量范围"的钉住状态
        for lane in self._lanes:
            lane.plot.enableAutoRange(axis="y")

    def get_region(self) -> tuple[float, float]:
        """当前框选区间；没有框选则返回当前可视范围。"""
        for lane in self._lanes:
            if lane.selection is not None:
                a, b = lane.selection.getRegion()
                return (float(a), float(b))
        return tuple(self._view)                      # type: ignore[return-value]

    def set_cursor_visible(self, on: bool) -> None:
        for lane in self._lanes:
            lane.cursor.setVisible(bool(on))
            lane.cursor_label.setVisible(bool(on))

    def lanes(self) -> list[str]:
        return [lane.label for lane in self._lanes]

    def set_theme(self, theme: str) -> None:
        """light/dark 主题切换（重建泳道样式，保留数据与视图）。"""
        if theme not in _THEME or theme == self._theme:
            return
        specs = []
        for lane in self._lanes:
            s = lane.spec
            specs.append(s)
        span = self._view if self._is_finite(self._view) else self._t_span
        self._theme = theme
        self._graphics.setBackground(_THEME[theme]["bg"])
        self._graphics.resetCachedContent()          # 背景缓存跟着换色，否则残留旧底色
        self.set_lanes(specs, t_span=span)
        if math.isfinite(self._cursor_t):
            self.set_time(self._cursor_t)
        self.themeChanged.emit(theme)

    def set_playback_follow(self, on: bool) -> None:
        """set_time 时自动把游标保持在视野内。"""
        self._follow = bool(on)

    # ---------------- 可由“设置”面板调的旋钮 ----------------
    def theme(self) -> str:
        return self._theme

    def set_badge_tol_px(self, px: float) -> None:
        """鼠标离感叹号多远还算悬停在它上面（像元）。"""
        self._badge_px_tol = float(max(2.0, min(40.0, float(px))))

    def badge_tol_px(self) -> float:
        return float(self._badge_px_tol)

    def set_refine_debounce_ms(self, ms: int) -> None:
        """滚轮缩放/平移后回内核重取数的去抖时长。"""
        self._refine_timer.setInterval(int(max(0, min(3000, int(ms)))))

    def refine_debounce_ms(self) -> int:
        return int(self._refine_timer.interval())

    def export_png(self, path: str, width: int = 0, height: int = 0) -> bool:
        """整组件存 PNG。offscreen 平台可用。"""
        try:
            QtWidgets.QApplication.processEvents()
            if width > 0 and height > 0:
                self.resize(width, height)
                QtWidgets.QApplication.processEvents()
            pm = self.grab()
            ok = pm.save(path, "PNG")
            if ok:
                LOG.info("导出 PNG %s %dx%d", path, pm.width(), pm.height())
            return bool(ok)
        except Exception:                              # noqa: BLE001
            LOG.exception("export_png 失败")
            return False

    def export_csv(self, path: str, t0: float | None = None, t1: float | None = None,
                   only_lane: int | None = None) -> int:
        """长表导出：``t,label,unit,value``。返回写出的数据行数。"""
        x0, x1 = self._clip_range(t0, t1)
        rows = 0
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["t", "label", "unit", "value"])
                for i, lane in enumerate(self._lanes):
                    if only_lane is not None and i != only_lane:
                        continue
                    if lane.t.size == 0:
                        continue
                    a = int(np.searchsorted(lane.t, x0, "left"))
                    b = int(np.searchsorted(lane.t, x1, "right"))
                    if b <= a:
                        continue
                    tt = lane.t[a:b]
                    yy = lane.y[a:b]
                    for tv, yv in zip(tt.tolist(), yy.tolist()):
                        if not math.isfinite(yv):
                            continue
                        w.writerow([f"{tv:.6f}", lane.label, lane.spec.unit, repr(yv)])
                        rows += 1
        except Exception:                              # noqa: BLE001
            LOG.exception("export_csv 失败")
            return 0
        return rows

    # ------------------------------------------------------------------ #
    # Qt 事件：游标 / 框选 / 键盘 / 右键
    # ------------------------------------------------------------------ #

    def leaveEvent(self, ev: QtCore.QEvent) -> None:  # noqa: N802
        """提示可以“不自动消失”，以鼠标离开整个曲线组件时必须主动收掉。"""
        hide_tip()
        super().leaveEvent(ev)

    def eventFilter(self, obj: QtCore.QObject, ev: QtCore.QEvent) -> bool:  # noqa: N802
        if obj is not self._graphics.viewport():
            return super().eventFilter(obj, ev)
        try:
            return self._filter_viewport_event(ev)
        except Exception:                          # noqa: BLE001
            # 事件过滤器招异常会打断 Qt 事件循环，这里全部兑住，当成未处理
            LOG.exception("SeriesPlot 事件处理异常")
            return False

    def _filter_viewport_event(self, ev: QtCore.QEvent) -> bool:
        lane = self._lane_at_viewport_pos(ev)
        et = ev.type()

        if et == QtCore.QEvent.MouseButtonPress:
            self._press_pos = _ev_pos(ev)
            ctrl = bool(ev.modifiers() & QtCore.Qt.ControlModifier)
            if ctrl and lane is not None and ev.button() == QtCore.Qt.LeftButton:
                x = self._view_x(lane, _ev_pos(ev))
                if not math.isfinite(x):
                    return True
                self._selecting = lane
                self._sel_x0 = x
                self._make_selection(lane, x, x)
                return True             # 吐掉事件，避开 pyqtgraph 平移
            return False                # 普通按下交给 pyqtgraph（滚轮缩放/拖拽平移）

        if et == QtCore.QEvent.MouseMove:
            if self._selecting is not None and lane is not None:
                x = self._view_x(lane, _ev_pos(ev))
                if math.isfinite(x):
                    self._update_selection(lane, self._sel_x0, x)
                return True
            if lane is not None:
                self._update_hover(lane, _ev_pos(ev))
            else:
                # 提示可以“不自动消失”，那鼠标回到泳道间隙/轴区时必须主动关掉，
                # 否则读数会挂在图上不走。
                hide_tip()
            return False

        if et == QtCore.QEvent.Leave:
            hide_tip()
            return False

        if et == QtCore.QEvent.MouseButtonRelease:
            if self._selecting is not None:
                target = self._selecting
                self._selecting = None
                if lane is not None:
                    x = self._view_x(lane, _ev_pos(ev))
                    if not math.isfinite(x):
                        return True
                    a, b = sorted((self._sel_x0, x))
                    self._update_selection(target, a, b)
                    if b - a > 1e-9:
                        self._apply_selection_to_all(target, a, b)
                        if self._suspend == 0:
                            self.regionChanged.emit(a, b)
                return True
            if (self._press_pos is not None and ev.button() == QtCore.Qt.LeftButton
                    and lane is not None):
                gp = _ev_pos(ev)
                if (gp - self._press_pos).manhattanLength() <= 3:
                    x = self._view_x(lane, _ev_pos(ev))
                    if math.isfinite(x):
                        self.set_time(x)
                        if self._suspend == 0:
                            self.timeChanged.emit(float(x))
                        self.setFocus()
                self._press_pos = None
            return False

        if et == QtCore.QEvent.MouseButtonDblClick:
            pos = _ev_pos(ev)
            row = self._lane_row_at_pos(pos)
            if row is not None and self._is_on_y_axis(row, pos):
                # 双击 Y 轴：在「1%~99% 自适应」与「全量范围」之间切换
                self._toggle_y_full(row.index)
                return True
            self.reset_view()
            return True

        if et == QtCore.QEvent.KeyPress:
            return self._handle_key(ev)

        return False                               # 其他事件一律不拦（这里不能引用 obj，作用域里没有）

    def keyPressEvent(self, ev: QtGui.QKeyEvent) -> None:  # noqa: N802
        if not self._handle_key(ev):
            super().keyPressEvent(ev)

    def _handle_key(self, ev: QtCore.QEvent) -> bool:
        k = ev.key()
        step = 1.0 if (ev.modifiers() & QtCore.Qt.ShiftModifier) else 0.1
        t = self._cursor_t if math.isfinite(self._cursor_t) else (
            self._t_span[0] if math.isfinite(self._t_span[0]) else 0.0)
        nt: Optional[float] = None
        if k == QtCore.Qt.Key_Left:
            nt = t - step
        elif k == QtCore.Qt.Key_Right:
            nt = t + step
        elif k == QtCore.Qt.Key_Home:
            nt = self._t_span[0]
        elif k == QtCore.Qt.Key_End:
            nt = self._t_span[1]
        if nt is None or not math.isfinite(nt):
            return False
        self.set_time(nt)
        if self._suspend == 0:
            self.timeChanged.emit(float(nt))
        ev.accept()
        return True

    # ---------------- 每道最小高（防压扁） ----------------
    def lane_min_height(self) -> int:
        return int(self._lane_min_h)

    def _row_h(self) -> int:
        """PlotItem 行高 = 想要的 ViewBox 高 + 标题/刻度边距（实测约 40px）。"""
        return int(self._lane_min_h + 40)

    def set_lane_min_height(self, px: int) -> None:
        """设定每道最小像元高（70–400）；泳道多时靠滚动而不是压扁。"""
        self._lane_min_h = int(max(70, min(400, int(px))))
        for lane in self._lanes:
            lane.plot.setMinimumHeight(self._row_h())
        self._apply_min_height()
        self._refresh()

    def _apply_min_height(self) -> None:
        """按**可见**泳道数算图区最小高度，交给 QScrollArea 决定要不要滚动。"""
        n = max(1, sum(1 for l in self._lanes if not l.hidden))
        bottom_axis = 36                      # 最底行刻度 + “时间”标题
        self._graphics.setMinimumHeight(int(bottom_axis + n * self._row_h()))
        self._graphics.updateGeometry()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _clear_lanes(self) -> None:
        """拆掉所有泳道与顶部图例条（保留主题按钮）。"""
        self._graphics.ci.clear()
        self._graphics.resetCachedContent()          # 泳道重排后背景缓存不再代表当前画面
        self._lane_sig = None          # 拆光了：下次 set_lanes 必须真重建
        self._events_sig = None
        self._regions_sig = None
        self._lanes = []
        self._chips = []
        self._chip_map = {}
        self._y_full = set()
        self._badges = []
        self._apply_min_height()
        self._root_plot = None
        for btn in self._header.findChildren(QtWidgets.QPushButton):
            btn.deleteLater()
        self._sync_header_height()

    def _sync_header_height(self) -> None:
        """胶囊行高度跟着“有没有横向滚动条”变，否则胶囊会被切掉一半。"""
        host = getattr(self, "_header_host", None)
        if host is None:
            return
        h = self._header.sizeHint().height()
        if host.horizontalScrollBar().isVisible():
            h += host.horizontalScrollBar().sizeHint().height()
        if host.minimumHeight() != h:
            host.setMinimumHeight(h)
            host.setMaximumHeight(h)

    def _add_chip(self, i: int, lane: _Lane, curve: int = 0) -> None:
        """一条曲线一个胶囊。curve=0 是主曲线，1.. 对应 lane.ghosts。

        「空通道」（全时段恒 0 / 无有效点）的胶囊**初始关闭并置灰**，
        但保持可点——用户偶尔要看它，只是不该默认占一行。
        """
        spec = lane.spec if curve == 0 else lane.ghosts[curve - 1].spec
        label = lane.label if curve == 0 else lane.ghosts[curve - 1].label
        hidden = lane.hidden if curve == 0 else lane.ghosts[curve - 1].hidden
        btn = QtWidgets.QPushButton(self._header)
        btn.setCheckable(True)
        btn.setChecked(not hidden)
        btn.setFlat(True)
        btn.setCursor(QtCore.Qt.PointingHandCursor)
        c = tuple(spec.color)
        empty = bool(getattr(spec, "empty", False))
        btn.setProperty("empty", empty)
        if empty:
            # 置灰：灰边框 + 灰字，一眼看出"这条默认不看"
            btn.setStyleSheet(
                "QPushButton{padding:1px 6px;border:1px solid #b9bcc2;border-radius:8px;"
                "background:#e9eaec;color:#8a8f98;}"
                "QPushButton::checked{background:#c9ccd2;color:#5a5f68;}")
            btn.setToolTip(f"{label}：该通道全时段恒为 0（空通道），已默认隐藏；点击可显示")
        else:
            btn.setStyleSheet(
                f"QPushButton{{padding:1px 6px;border:1px solid #{c[0]:02x}{c[1]:02x}{c[2]:02x};"
                f"border-radius:8px;background:#{c[0]:02x}{c[1]:02x}{c[2]:02x}22;}}"
                f"QPushButton::checked{{background:#{c[0]:02x}{c[1]:02x}{c[2]:02x};color:white;}}"
                f"QPushButton::!checked{{color:gray;}}")
            kind = "主曲线" if curve == 0 else "叠加曲线（同轴）"
            btn.setToolTip(f"{label}｜{kind}｜点击单独隐藏/显示")
        btn.setText(label if len(label) <= 26 else label[:25] + "…")
        btn.toggled.connect(lambda on, idx=i, k=curve: self._toggle_curve(idx, k, on))
        self._headerLayout().insertWidget(self._headerLayout().count() - 2, btn)
        self._chips.append(btn)
        self._chip_map[(i, curve)] = btn
        self._sync_header_height()          # 胶囊数变了，行高要跟着滚动条重算

    def _headerLayout(self) -> QtWidgets.QHBoxLayout:
        lay = self._header.layout()
        assert isinstance(lay, QtWidgets.QHBoxLayout)
        return lay

    def _toggle_lane(self, idx: int, on: bool, from_user: bool = True) -> None:
        """整道显隐（= 主曲线的胶囊）。叠加曲线请用 _toggle_curve。"""
        self._toggle_curve(idx, 0, on, from_user=from_user)

    def _toggle_curve(self, idx: int, curve: int, on: bool, from_user: bool = True) -> None:
        """单独切换某条曲线。curve=0 是主曲线；隐藏主曲线会连带整道隐藏。"""
        if idx < 0 or idx >= len(self._lanes):
            return
        lane = self._lanes[idx]
        if curve == 0:
            lane.hidden = not on
            label = lane.label
        else:
            gi = curve - 1
            if gi >= len(getattr(lane, "ghosts", ())):
                return
            g = lane.ghosts[gi]
            g.hidden = not on
            label = g.label
        self._apply_lane_style(lane)
        btn = self._chip_map.get((idx, curve))
        if btn is not None and btn.isChecked() != on:
            btn.blockSignals(True)
            btn.setChecked(on)
            btn.blockSignals(False)
        if from_user and self._suspend == 0:
            self._suspend += 1
            self.laneVisibilityChanged.emit(label, bool(on))
            self._suspend -= 1
        self._schedule_refresh()

    def _apply_lane_style(self, lane: _Lane) -> None:
        vis = not lane.hidden
        for it in (lane.raw, lane.env_lo, lane.env_hi, lane.env_mean, lane.env_fill):
            it.setVisible(vis)
        # 叠加曲线各管各的显隐：主曲线藏了它也能单独留着对比
        for g in getattr(lane, "ghosts", ()):
            gv = vis and not g.hidden
            g.raw.setVisible(gv)
            g.env_mean.setVisible(gv)
        if lane.band_item is not None:
            lane.band_item.setVisible(vis)
        if lane.thr_item is not None:
            lane.thr_item.setVisible(vis)
        lane.empty_txt.setVisible(vis and not lane.has_data)
        if not lane.has_data:
            lane.empty_txt.setText("无数据（通道缺失或全 nan）")
        ax = lane.plot.getAxis("left")
        ax.setLabel(text=lane.label, units=lane.spec.unit)
        if lane.log_y:
            ax.setLabel(text=f"log10 {lane.label}", units="")
        # 隐藏的道整行收起：否则 10 道里隐 5 道，剩下 5 道还是被压在同一高度
        lane.plot.setVisible(vis)
        self._sync_bottom_axis()
        self._apply_min_height()

    def _sync_bottom_axis(self) -> None:
        """时间刻度与“时间”标题只给最后一条**可见**泳道，否则隐掉末道后轴会消失。"""
        vis = [l for l in self._lanes if not l.hidden]
        last = vis[-1] if vis else None
        for lane in self._lanes:
            axb = lane.plot.getAxis("bottom")
            is_last = lane is last
            axb.setStyle(showValues=is_last)
            axb.setLabel(text="时间 (s, 自 boot)" if is_last else "")
            axb.setHeight(34 if is_last else 4)

    def _lane_of_viewport(self, obj: QtCore.QObject) -> Optional[_Lane]:
        """兼容旧调用：由 ViewBox 反查泳道。"""
        for lane in self._lanes:
            if obj is lane.plot.getViewBox():
                return lane
        return None

    def _lane_at_viewport_pos(self, ev: QtCore.QEvent) -> Optional[_Lane]:
        """由鼠标事件的 viewport 坐标定位落在哪个泳道（非鼠标事件返回 None）。"""
        if not isinstance(ev, (QtGui.QMouseEvent, QtGui.QWheelEvent)):
            return None
        return self._lane_at_pos(_ev_pos(ev))

    def _lane_row_at_pos(self, pos: QtCore.QPoint) -> Optional[_Lane]:
        """按**整行**（含左侧 Y 轴与标题）定位泳道；双击 Y 轴要用这个。"""
        try:
            sc = self._graphics.mapToScene(pos)
        except Exception:                          # noqa: BLE001
            return None
        for lane in self._lanes:
            if lane.hidden:
                continue
            try:
                if lane.plot.sceneBoundingRect().contains(sc):
                    return lane
            except Exception:                      # noqa: BLE001
                continue
        return None

    def _is_on_y_axis(self, lane: "_Lane", pos: QtCore.QPoint) -> bool:
        """点击是否落在左侧 Y 轴那一竖条上（不是绘图区里）。"""
        try:
            sc = self._graphics.mapToScene(pos)
            vbr = lane.plot.getViewBox().sceneBoundingRect()
        except Exception:                          # noqa: BLE001
            return False
        if vbr.width() <= 0 or vbr.height() <= 0:
            return False
        if not (vbr.top() - 6.0 <= float(sc.y()) <= vbr.bottom() + 6.0):
            return False
        return float(sc.x()) < float(vbr.left())

    def _lane_at_pos(self, pos: QtCore.QPoint) -> Optional[_Lane]:
        scene_pos = self._graphics.mapToScene(pos)
        for lane in self._lanes:
            # 用 sceneBoundingRect()：ViewBox.boundingRect() 在布局完成前可能是空矩形
            if lane.plot.getViewBox().sceneBoundingRect().contains(scene_pos):
                return lane
        return None

    def _on_context_menu(self, pos: QtCore.QPoint) -> None:
        lane = self._lane_at_pos(pos)
        if lane is None:
            return
        self._show_lane_menu(lane.index, pos)

    def _view_x(self, lane: _Lane, pos: QtCore.QPoint) -> float:
        vb = lane.plot.getViewBox()
        try:
            scene = self._graphics.mapToScene(pos)
            pt = vb.mapSceneToView(scene)               # 返回 QPointF，不能解包
            x = float(pt.x() if hasattr(pt, "x") else pt[0])
            return x if math.isfinite(x) else float("nan")
        except Exception:                              # noqa: BLE001
            LOG.debug("_view_x 映射失败", exc_info=True)
            return float("nan")

    def _make_selection(self, lane: _Lane, a: float, b: float) -> None:
        if lane.selection is None:
            lane.selection = pg.LinearRegionItem(values=(a, b), movable=False,
                                                 brush=pg.mkBrush(90, 140, 230, 60),
                                                 pen=pg.mkPen((60, 110, 200), width=1))
            lane.selection.setZValue(-25)
            lane.plot.addItem(lane.selection)

    def _update_selection(self, lane: _Lane, a: float, b: float) -> None:
        if lane.selection is None:
            self._make_selection(lane, a, b)
        else:
            lane.selection.setRegion((a, b))

    def _apply_selection_to_all(self, src: _Lane, a: float, b: float) -> None:
        for lane in self._lanes:
            if lane is src:
                continue
            self._update_selection(lane, a, b)

    def _compute_span(self) -> tuple[float, float]:
        lo = hi = None
        for lane in self._lanes:
            t0, t1 = lane.span()
            if math.isfinite(t0) and math.isfinite(t1):
                lo = t0 if lo is None else min(lo, t0)
                hi = t1 if hi is None else max(hi, t1)
        if lo is None or hi is None or hi <= lo:
            return (0.0, 1.0)
        pad = (hi - lo) * 0.005
        return (lo - pad, hi + pad)

    def _root_view(self) -> Optional[pg.ViewBox]:
        return self._root_plot.getViewBox() if self._root_plot is not None else None

    def _is_finite(self, pair: tuple[float, float]) -> bool:
        try:
            return all(math.isfinite(float(v)) for v in pair)
        except Exception:                              # noqa: BLE001
            return False

    def _set_view(self, span: tuple[float, float], emit: bool = True) -> None:
        if not self._is_finite(span):
            return
        x0, x1 = float(span[0]), float(span[1])
        if x1 <= x0:
            return
        if self._root_plot is None:
            return
        self._suspend += 1 if not emit else 0
        try:
            self._root_plot.getViewBox().setXRange(x0, x1, padding=0)
            self._view = (x0, x1)
        finally:
            if not emit:
                self._suspend -= 1
        self._schedule_refresh()

    def _clip_range(self, t0: float | None, t1: float | None) -> tuple[float, float]:
        base = self._t_span if self._is_finite(self._t_span) else (0.0, 1.0)
        x0 = base[0] if t0 is None or not math.isfinite(float(t0)) else float(t0)
        x1 = base[1] if t1 is None or not math.isfinite(float(t1)) else float(t1)
        return (min(x0, x1), max(x0, x1))

    def _on_view_changed(self, _plot: pg.PlotItem, rng: Any) -> None:
        """任一泳道 X 范围变化（含用户滚轮缩放/拖拽平移）都要把 _view 对齐真实视图。

        以前这里错把 sigXRangeChanged 的 (x0, x1) 元组又取了 [0]（一个 float），
        转换失败被 except 吞掉，导致用户交互后 _view 一直是旧窗口，
        _refresh 按旧窗口算包络、export_csv 默认范围也错。
        现在直接读根泳道 ViewBox 的实际范围，不依赖信号参数形式。
        """
        try:
            vb = self._root_plot.getViewBox() if self._root_plot is not None else None
            if vb is not None:
                x0, x1 = _rng_bounds(vb.viewRange()[0])
            else:
                x0, x1 = _rng_bounds(rng)
            if self._is_finite((x0, x1)) and x1 > x0:
                self._view = (x0, x1)
        except Exception:                              # noqa: BLE001
            LOG.debug("读取视图范围失败", exc_info=True)
        self._schedule_refresh()
        self._refine_timer.start()

    def _emit_view_changed(self) -> None:
        x0, x1 = self._view
        if self._is_finite((x0, x1)) and x1 > x0:
            self.viewChanged.emit(float(x0), float(x1))

    def _on_cursor_moved(self, line: pg.InfiniteLine) -> None:
        if self._suspend > 0:
            return
        t = float(line.value())
        self._suspend += 1
        try:
            self._cursor_t = t
            for lane in self._lanes:
                if lane.cursor is line:
                    continue
                lane.cursor.blockSignals(True)
                lane.cursor.setValue(t)
                lane.cursor.blockSignals(False)
            self._update_readout()
        finally:
            self._suspend -= 1
        self.timeChanged.emit(t)

    def _keep_cursor_in_view(self) -> None:
        if self._root_plot is None or not math.isfinite(self._cursor_t):
            return
        x0, x1 = self._view
        if x0 <= self._cursor_t <= x1:
            return
        span = x1 - x0 if self._is_finite(self._view) else (self._t_span[1] - self._t_span[0])
        self._set_view((self._cursor_t - span * 0.2, self._cursor_t + span * 0.8))

    def _schedule_refresh(self) -> None:
        self._timer.start()

    # ---- 绘图主流程 ---------------------------------------------------- #

    def _refresh(self) -> None:
        if not self._lanes:
            return
        x0, x1 = self._view if self._is_finite(self._view) else self._t_span
        if not self._is_finite((x0, x1)):
            return
        wpx = max(200, int(self._graphics.width()))
        raw_limit = self._raw_limit_override or max(4000, 4 * wpx)
        nb = self._nbuckets_override or min(2000, max(400, int(wpx)))   # 每像素一对 min/max 就够
        for lane in self._lanes:
            if lane.hidden:
                continue
            self._draw_lane(lane, x0, x1, raw_limit, nb)
            self._draw_ghosts(lane, x0, x1, raw_limit, nb)
        self._autoscale_y(x0, x1)
        # 空泳道占位文字与事件文字按当前范围重排
        for lane in self._lanes:
            vb = lane.plot.getViewBox()
            if not lane.has_data:
                xr, yr = vb.viewRange()
                lane.empty_txt.setPos(sum(_rng_bounds(xr)) / 2.0, sum(_rng_bounds(yr)) / 2.0)
        if self._lanes:
            self._relayout_badges()
        self._update_readout()

    # ---------------- 事件感叹号徽标 ----------------
    def _relayout_badges(self) -> None:
        """警告=黄色感叹号徒标，错误/严重=红色；同一像元簇内的事件合并成一个，
        具体文字按时间顺序放进悬停提示。"""
        first = next((l for l in self._lanes if not l.hidden), None)
        if first is None:
            first = self._lanes[0] if self._lanes else None
        self._badge_lane = first
        if first is None:
            self._badges = []
            return
        for it in first.badge_items:
            first.plot.removeItem(it)
        first.badge_items = []
        self._badges = []
        x0, x1 = self._view if self._is_finite(self._view) else self._t_span
        if not self._is_finite((x0, x1)) or x1 <= x0:
            return
        vb = first.plot.getViewBox()
        wpx = max(60.0, float(vb.width()))
        picked = [e for e in self._events
                  if _SEV_RANK.get(str(e.get("severity", "info")), 0) >= _SEV_RANK["warn"]
                  and math.isfinite(float(e.get("t", float("nan")))) and x0 <= float(e["t"]) <= x1]
        picked.sort(key=lambda d: float(d["t"]))
        clusters: list[dict[str, Any]] = []
        for e in picked:
            t = float(e["t"])
            px = (t - x0) / (x1 - x0) * wpx
            if clusters and px - clusters[-1]["px"] <= 13.0:
                clusters[-1]["items"].append(e)
            else:
                clusters.append({"px": px, "t": t, "items": [e]})
        if len(clusters) > 80:                       # 极端日志下不刷屏
            clusters = clusters[:80]
        lo, hi = _rng_bounds(vb.viewRange()[1])
        ytop = hi - 0.03 * (hi - lo)
        for c in clusters:
            rank = max(_SEV_RANK.get(str(i.get("severity", "info")), 0) for i in c["items"])
            sev = ("critical" if rank >= _SEV_RANK["critical"]
                   else "error" if rank >= _SEV_RANK["error"] else "warn")
            col = SEVERITY_COLORS[sev]
            fg = (58, 40, 0) if sev == "warn" else (255, 255, 255)
            ti = pg.TextItem("!", color=fg, anchor=(0.5, 0.0), fill=pg.mkBrush(col),
                             border=pg.mkPen(pg.mkColor(col).darker(125), width=1))
            ti.setZValue(30)
            ti.setFont(_badge_font())
            ti.setPos(c["t"], ytop)
            first.plot.addItem(ti, ignoreBounds=True)
            first.badge_items.append(ti)
            self._badges.append({"t": c["t"], "sev": sev, "n": len(c["items"]), "item": ti,
                                 "lines": self._badge_text(c["items"])})

    @staticmethod
    def _badge_text(items: list[dict[str, Any]]) -> list[str]:
        """按时间顺序列出簇内事件，超过 12 条只抦前几条并告知总数。"""
        out: list[str] = []
        for i, e in enumerate(items[:12], 1):
            sev = str(e.get("severity", "info"))
            kind = str(e.get("kind") or "EV")
            txt = str(e.get("display") or e.get("text") or "")
            out.append(f"{i}. t={float(e.get('t', float('nan'))):.3f}s  "
                       f"{SEVERITY_CN.get(sev, sev)}｜{kind}｜{txt}")
        if len(items) > 12:
            out.append(f"…另有 {len(items) - 12} 条（缩小选区可展开）")
        return out

    def _badge_tip(self, lane: _Lane, x: float, pos: QtCore.QPoint) -> Optional[str]:
        """鼠标落在首泳道顶部的感叹号上 → 返回多行提示（时间升序）；否则 None。
        命中按**像元距离**算（不依赖视图 y 范围，offscreen 下 ViewBox 高度可能为 0）。"""
        if not self._badges or not self._lanes or lane is not getattr(self, "_badge_lane", None):
            return None
        try:
            sc = self._graphics.mapToScene(pos)
        except Exception:
            LOG.debug("_badge_tip 坐标映射失败", exc_info=True)
            return None
        best: Optional[dict[str, Any]] = None
        bd = float("inf")
        # 用 ViewBox 自己的场景矩形做像元换算：TextItem 在异常布局（offscreen 未切到
        # 曲线页时 ViewBox 高度为 0）下的 sceneBoundingRect 会膨胀到几万像素，不可靠。
        vbr = lane.plot.getViewBox().sceneBoundingRect()
        xr, _yr = lane.plot.getViewBox().viewRange()
        x0, x1 = _rng_bounds(xr)
        if not (self._is_finite((x0, x1)) and x1 > x0) or vbr.width() <= 0:
            return None
        band = vbr.top() + max(20.0, 0.14 * max(vbr.height(), 0.0))
        if not (vbr.left() - 4.0 <= float(sc.x()) <= vbr.right() + 4.0) or float(sc.y()) > band:
            return None
        scale = float(vbr.width()) / (x1 - x0)
        mx = float(sc.x()) - float(vbr.left())
        for b in self._badges:
            d = abs((float(b["t"]) - x0) * scale - mx)
            if d < bd:
                bd, best = d, b
        if best is None or bd > self._badge_px_tol:
            return None
        head = (f"{SEVERITY_CN.get(best['sev'], best['sev'])} × {best['n']} 条（按时间顺序）"
                if best["n"] > 1 else SEVERITY_CN.get(best["sev"], best["sev"]))
        return "\n".join([head] + list(best["lines"]))

    def _draw_ghosts(self, lane: _Lane, x0: float, x1: float, raw_limit: int, nb: int) -> None:
        """画同一泳道里的叠加曲线（与原曲线共用坐标系）。"""
        for g in lane.ghosts:
            if g.hidden:
                g.raw.setData(np.empty(0), np.empty(0))
                g.env_mean.setData(np.empty(0), np.empty(0))
                continue
            if g.t.size and g.visible_counts(x0, x1) <= raw_limit:
                a = max(0, int(np.searchsorted(g.t, x0, "left")) - 2)
                b = min(g.t.size, int(np.searchsorted(g.t, x1, "right")) + 2)
                g.raw.setData(x=g.t[a:b], y=g._tf(g.y[a:b]), connect="finite")
                g.env_mean.setData(np.empty(0), np.empty(0))
                g.mode = "raw"
                continue
            env: Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None
            full = (x0 <= g.span()[0] + 1e-9) and (x1 >= g.span()[1] - 1e-9)
            if g.pre_env is not None and full:
                env = g.pre_env
            if env is None and g.t.size:
                ea = g.tower.envelope(x0, x1, nb) if g.tower is not None else None
                if ea is None:
                    ea = envelope_for_range(g.t, g.y, x0, x1, nb)
                env = (ea.t, ea.vmin, ea.vmax, ea.vmean)
            if env is None:
                z = np.empty(0, np.float64)
                env = (z, z, z, z)
            g.raw.setData(np.empty(0), np.empty(0))
            g.env_mean.setData(env[0], g._tf(env[3]))
            g.mode = "env"

    def _draw_lane(self, lane: _Lane, x0: float, x1: float, raw_limit: int, nb: int) -> None:
        n_view = lane.visible_counts(x0, x1)
        use_raw = lane.t.size > 0 and n_view <= raw_limit
        if use_raw:
            a = int(np.searchsorted(lane.t, x0, "left"))
            b = int(np.searchsorted(lane.t, x1, "right"))
            # 左右各多取 2 点，避免边缘断开
            a = max(0, a - 2)
            b = min(lane.t.size, b + 2)
            yt = lane._tf(lane.y[a:b])
            style = lane.spec.style
            lane.raw.setData(x=lane.t[a:b], y=yt, connect="finite", **_style_kwargs(style))
            for it in (lane.env_lo, lane.env_hi, lane.env_mean):
                it.setData(np.empty(0), np.empty(0))    # FillBetweenItem 自动跟随两条曲线
            lane.mode = "raw"
            return

        env: Optional[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None
        full = (x0 <= lane.span()[0] + 1e-9) and (x1 >= lane.span()[1] - 1e-9)
        if lane.pre_env is not None and full:
            e_t, e_min, e_max, e_mean = lane.pre_env
            if e_t.size and float(e_t[0]) >= x0 - 1e-6 and float(e_t[-1]) <= x1 + 1e-6:
                env = (e_t, e_min, e_max, e_mean)
        if env is None:
            if lane.t.size == 0:
                # 只有外部包络、没有原始点（预设对大数据量通道只给包络）：
                # 放大时绝不能画空，否则曲线整条消失；退化成取重叠的桶。
                env = self._clip_pre_env(lane, x0, x1)
            else:
                ea = lane.tower.envelope(x0, x1, nb) if lane.tower is not None else None
                if ea is None:
                    # 塔太粗（窗口内桶数不够）或没塔：直接分桶，此时可见点已经不多
                    ea = envelope_for_range(lane.t, lane.y, x0, x1, nb)
                env = (ea.t, ea.vmin, ea.vmax, ea.vmean)
                self._last_counts[lane.index] = ea.count
        e_t, e_min, e_max, e_mean = env
        lane.raw.setData(np.empty(0), np.empty(0))
        lane.env_lo.setData(e_t, lane._tf(e_min))
        lane.env_hi.setData(e_t, lane._tf(e_max))
        lane.env_mean.setData(e_t, lane._tf(e_mean))
        lane.mode = "env"

    @staticmethod
    def _clip_pre_env(lane: _Lane, x0: float, x1: float
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """从已有包络里取与 [x0,x1] 重叠的桶；重叠太少就退回整条，绝不返回空。"""
        e = lane.pre_env
        if e is None:
            z = np.empty(0, np.float64)
            return (z, z, z, z)
        et, emin, emax, emean = e
        if et.size == 0:
            return (et, emin, emax, emean)
        a = int(np.searchsorted(et, x0, "left"))
        b = int(np.searchsorted(et, x1, "right"))
        if b - a < 2:
            return (et, emin, emax, emean)
        return (et[a:b], emin[a:b], emax[a:b], emean[a:b])

    def lanes_needing_refine(self) -> list[dict[str, Any]]:
        """只有包络、没有原始点的道：滚轮放大后需要回内核按新视图重新取数。"""
        out: list[dict[str, Any]] = []
        for lane in self._lanes:
            if lane.hidden or lane.t.size or lane.pre_env is None or not lane.key:
                continue
            out.append({"index": lane.index, "key": lane.key, "cov": lane.span()})
        return out

    def refine_envelope(self, idx: int, t: np.ndarray, vmin: np.ndarray, vmax: np.ndarray,
                        vmean: Optional[np.ndarray] = None) -> bool:
        """把一道的包络换成更细的那一份（点太少/尺寸不合则保持原样）。"""
        if idx >= len(self._lanes):
            return False
        lane = self._lanes[idx]
        t = np.asarray(t, np.float64)
        vmin = np.asarray(vmin, np.float64)
        vmax = np.asarray(vmax, np.float64)
        if t.size < 2 or vmin.size != t.size or vmax.size != t.size:
            return False
        mean = (vmin + vmax) / 2.0 if vmean is None else np.asarray(vmean, np.float64)
        lane.pre_env = (t, vmin, vmax, mean)
        lane.spec.env_t, lane.spec.env_min, lane.spec.env_max, lane.spec.env_mean = t, vmin, vmax, mean
        lane.has_data = lane._check_has_data()
        self._refresh()
        return True

    # ---- Y 量程 --------------------------------------------------------- #

    def _curve_values(self, curve: Any, host: "_Lane", x0: float, x1: float) -> np.ndarray:
        """一条曲线在当前视图内的显示值（含叠加曲线），给 Y 量程用。"""
        mode = getattr(curve, "mode", "raw")
        if mode == "raw" and curve.t.size:
            a = max(0, int(np.searchsorted(curve.t, x0, "left")) - 1)
            b = min(curve.t.size, int(np.searchsorted(curve.t, x1, "right")) + 1)
            return curve._tf(curve.y[a:b]) if b > a else np.empty(0)
        if isinstance(curve, _Ghost):
            got = curve.env_mean.getData()[1]
            vals = np.asarray(got, np.float64) if got is not None and len(got) else np.empty(0)
            if curve.pre_env is not None:          # 均值线掩盖尖峰，量程要带上整段包络
                vals = np.concatenate([vals, np.asarray(curve.pre_env[1], np.float64),
                                       np.asarray(curve.pre_env[2], np.float64)])
        else:
            got_lo = curve.env_lo.getData()[1]
            got_hi = curve.env_hi.getData()[1]
            vals = np.empty(0)
            if got_lo is not None and got_hi is not None and len(got_lo) and len(got_hi):
                vals = np.concatenate([np.asarray(got_lo, np.float64),
                                       np.asarray(got_hi, np.float64)])
        if vals.size == 0:
            vals = curve._tf(curve.y) if curve.y.size else np.empty(0)
        return np.asarray(vals, np.float64)

    def _lane_visible_values(self, lane: "_Lane", x0: float, x1: float) -> np.ndarray:
        """该道**所有可见曲线**的值合并：主曲线 + 叠加曲线共用一根 Y 轴，量程必须一起算。"""
        out = [self._curve_values(lane, lane, x0, x1)]
        for g in getattr(lane, "ghosts", ()):
            if not g.hidden:
                out.append(self._curve_values(g, lane, x0, x1))
        arrs = [a for a in out if a.size]
        return np.concatenate(arrs) if arrs else np.empty(0)

    def _lane_full_range(self, lane: "_Lane") -> Optional[tuple[float, float]]:
        """整条曲线的显示值范围（双击 Y 轴"恢复全量"用）。"""
        arrs = [self._curve_full_values(lane)]
        for g in getattr(lane, "ghosts", ()):
            if not g.hidden:
                arrs.append(self._curve_full_values(g))
        arrs = [a for a in arrs if a.size]
        if not arrs:
            return None
        f = np.concatenate(arrs)
        f = f[np.isfinite(f)]
        if f.size == 0:
            return None
        lo, hi = float(f.min()), float(f.max())
        if hi - lo <= 0:
            pad = abs(hi) * 0.1 if hi else 1.0
            return (hi - pad, hi + pad)
        pad = (hi - lo) * 0.02
        return (lo - pad, hi + pad)

    @staticmethod
    def _curve_full_values(curve: Any) -> np.ndarray:
        vals = curve._tf(curve.y) if curve.y.size else np.empty(0)
        if curve.pre_env is not None:
            vals = np.concatenate([np.asarray(vals, np.float64),
                                   np.asarray(curve.pre_env[1], np.float64),
                                   np.asarray(curve.pre_env[2], np.float64)])
        return np.asarray(vals, np.float64)

    def _autoscale_y(self, x0: float, x1: float) -> None:
        for lane in self._lanes:
            if lane.hidden:
                continue
            if not lane.has_data:
                # 空道给个稳定量程，否则 pyqtgraph 会沿用上一次的奇怪范围
                lane.plot.setYRange(-0.5, 0.5, padding=0.0)
                continue
            if lane.y_fixed:
                lo, hi = float(lane.spec.y_min), float(lane.spec.y_max)
                if lane.log_y:
                    lo = math.log10(lo) if lo > 0 else 0.0
                    hi = math.log10(hi) if hi > 0 else 1.0
                lane.plot.setYRange(min(lo, hi), max(lo, hi), padding=0.0)   # 固定量程不加 padding
                continue
            if lane.index in self._y_full:
                full = self._lane_full_range(lane)
                if full is not None:
                    lane.plot.setYRange(full[0], full[1], padding=0.0)
                    continue
            v = self._lane_visible_values(lane, x0, x1)
            rr = robustness_range(v)
            if rr is None:
                continue
            lane.plot.setYRange(rr[0], rr[1], padding=0.0)

    def _toggle_y_full(self, idx: int) -> bool:
        """双击 Y 轴：在"1%~99% 自适应"与"全量范围"之间切换。返回切换后的状态。"""
        if idx < 0 or idx >= len(self._lanes):
            return False
        if idx in self._y_full:
            self._y_full.discard(idx)
            on = False
        else:
            self._y_full.add(idx)
            on = True
        self._schedule_refresh()
        return on

    def _update_hover(self, lane: _Lane, pos: QtCore.QPoint) -> None:
        x = self._view_x(lane, pos)
        if not math.isfinite(x):
            return
        tip = self._badge_tip(lane, x, pos)
        if tip is not None:
            # 悬停感叹号时优先看事件文字，不再被本泳道读数盖掉
            show_tip(self, tip)
            return
        t, val, cnt = self._value_at(lane, x)
        unit = lane.spec.unit
        txt = f"{lane.label}  t={t:.3f}s  {'' if val is None else f'{val:.4f}'} {unit}"
        if cnt > 1:
            txt += f"  (桶内 {cnt} 点)"
        for g in getattr(lane, "ghosts", ()):
            if g.hidden:
                continue
            gv = self._ghost_value_at(g, x)
            gtxt = "-" if gv is None else f"{gv:.4f}"
            txt += chr(10) + f"{g.label}  {gtxt} {g.spec.unit}"
        # 留存时间走统一入口（QtWidgets.QToolTip，写错成 QtGui 会让每次鼠标移动都报错）
        show_tip(self, txt)

    def _value_at(self, lane: _Lane, x: float) -> tuple[float, Optional[float], int]:
        """取 x 附近的显示值：raw 模式取最近样本，env 模式取桶均值与桶点数。"""
        if getattr(lane, "mode", "raw") == "raw" and lane.t.size:
            i = int(np.searchsorted(lane.t, x, side="left"))
            i = min(max(i, 0), lane.t.size - 1)
            return (float(lane.t[i]), _inv_tf(lane, lane.y[i]), 1)
        et = lane.env_lo.getData()[0]
        if et is not None and len(et):
            et = np.asarray(et)
            i = int(np.searchsorted(et, x))
            i = min(max(i, 0), et.size - 1)
            mean = lane.env_mean.getData()[1]
            val = float(np.asarray(mean)[i]) if mean is not None and len(mean) else None
            cnts = self._last_counts.get(lane.index)
            if cnts is not None and cnts.size == et.size:
                cnt = int(cnts[i])
            else:                       # 用内核预算包络时没有点数，用时间轴估算
                cnt = int(np.searchsorted(lane.t, et[i])) if lane.t.size else 0
            return (float(et[i]), _inv_tf(lane, val) if val is not None else None, cnt)
        return (x, None, 0)

    def _ghost_value_at(self, g: "_Ghost", x: float) -> Optional[float]:
        """叠加曲线在 x 处的显示值（与主曲线同一套取值规则）。"""
        if getattr(g, "mode", "raw") == "raw" and g.t.size:
            i = int(np.searchsorted(g.t, x, side="left"))
            i = min(max(i, 0), g.t.size - 1)
            return _inv_tf(g.host, g.y[i])
        et = g.env_mean.getData()[0]
        if et is not None and len(et):
            i = int(np.searchsorted(np.asarray(et), x))
            i = min(max(i, 0), len(et) - 1)
            mean = g.env_mean.getData()[1]
            if mean is not None and len(mean):
                return _inv_tf(g.host, float(np.asarray(mean)[i]))
        return None

    @staticmethod
    def _fmt_readout(name: str, val: Optional[float], unit: str, many: bool) -> str:
        """底部读数条的单元格式：紧凑的「字段名 = 数值 单位」。

        不带"(桶N点)"之类的诊断后缀：多通道时一行会被撑爆，点数只在悬停提示里给。
        """
        try:
            ok = val is not None and math.isfinite(float(val))
        except (TypeError, ValueError):
            ok = False
        txt = f"{float(val):.4g}" if ok else "-"
        nm = name
        if many and len(nm) > 11:
            nm = nm[:10] + "…"          # 道数多时截短，完整名在悬停提示里
        u = f" {unit}" if unit else ""
        return f"{nm} = {txt}{u}"

    def _update_readout(self) -> None:
        t = self._cursor_t
        if not math.isfinite(t):
            self._readout.setText("游标：未设置")
            return
        parts = [f"t = {t:.3f} s"]
        full = [f"t = {t:.3f} s"]
        many = sum(1 for ln in self._lanes if not ln.hidden) > 6
        for lane in self._lanes:
            if lane.hidden:
                continue
            _tt, val, cnt = self._value_at(lane, t)
            cnt_txt = "" if getattr(lane, "mode", "raw") == "raw" else f"（桶 {cnt} 点）"
            full.append(self._fmt_readout(lane.label, val, lane.spec.unit, False) + cnt_txt)
            seg = [self._fmt_readout(lane.label, val, lane.spec.unit, many)]
            # 同一泳道的叠加曲线紧跟其后（共用一套刻度，读数放一起才好比）
            for g in getattr(lane, "ghosts", ()):
                if g.hidden:
                    continue
                gv = self._ghost_value_at(g, t)
                full.append(self._fmt_readout(g.label, gv, g.spec.unit, False))
                seg.append(self._fmt_readout(g.label, gv, g.spec.unit, many))
            parts.append("  ".join(seg))
            # 泳道内游标旁标注（只标主曲线，避免两串数字叠在一起）
            yr = lane.plot.getViewBox().viewRange()[1]
            lo_y, hi_y = _rng_bounds(yr)
            txt = "-" if val is None or not math.isfinite(val) else f"{val:.4g}"
            lane.cursor_label.setText(f"{txt}")
            lane.cursor_label.setPos(t, hi_y - 0.12 * (hi_y - lo_y))
        self._readout.setText("   |   ".join(parts))
        self._readout.setToolTip(chr(10).join(full))

    def _show_lane_menu(self, idx: int, pos: QtCore.QPoint) -> None:
        menu = self._build_lane_menu(idx)
        if menu is None:
            return
        menu.exec(self._menu_pos(idx, pos))

    def _build_lane_menu(self, idx: int) -> Optional[QtWidgets.QMenu]:
        """构建单道右键菜单（拆出来便于 offscreen 自测）。"""
        if idx >= len(self._lanes):
            return None
        lane = self._lanes[idx]
        menu = QtWidgets.QMenu(self)
        act_vis = QAction_visible(menu, lane)
        act_vis.toggled.connect(lambda on: self._toggle_lane(idx, on))
        menu.addAction(act_vis)
        if idx in self._y_full:
            menu.addAction("Y 轴回到 1%~99% 自适应（也可双击 Y 轴）",
                           lambda: self._toggle_y_full(idx))
        else:
            menu.addAction("Y 轴恢复全量范围（也可双击 Y 轴）",
                           lambda: self._toggle_y_full(idx))
        menu.addAction("固定量程…", lambda: self._ask_fixed_range(idx))
        if lane.y_fixed:
            menu.addAction("取消固定量程", lambda: self._clear_fixed_range(idx))
        act_log = QtGui.QAction("Y 轴对数", menu)
        act_log.setCheckable(True)
        act_log.setChecked(lane.log_y)
        act_log.toggled.connect(lambda on: self._set_log_y(idx, on))
        menu.addAction(act_log)
        menu.addSeparator()
        menu.addAction("单独显示该道", lambda: self._isolate(idx))
        menu.addAction("全部显示", lambda: self._show_all())
        menu.addAction("复制通道名", lambda: QtWidgets.QApplication.clipboard().setText(lane.key_text or lane.label))
        menu.addAction("导出该泳道 CSV…", lambda: self._export_lane_dialog(idx))
        if self._plot_tools:
            menu.addSeparator()
            payload = self._tool_payload(lane)
            for name in self._plot_tools:
                menu.addAction(name + "…",
                               lambda n=name, p=payload: self.plotToolRequested.emit(n, p))
        return menu

    # 菜单动作的小助手 --------------------------------------------------- #

    def _menu_pos(self, idx: int, pos: QtCore.QPoint) -> QtCore.QPoint:
        return self._graphics.viewport().mapToGlobal(pos)

    def _ask_fixed_range(self, idx: int) -> None:
        lane = self._lanes[idx]
        lo, hi = finite_stats(lane.y if lane.y.size else np.empty(0))
        ok1, v0 = QtWidgets.QInputDialog.getDouble(self, "固定量程", "下界", lo if math.isfinite(lo) else 0.0, -1e18, 1e18, 6)
        if not ok1:
            return
        ok2, v1 = QtWidgets.QInputDialog.getDouble(self, "固定量程", "上界", hi if math.isfinite(hi) else 1.0, -1e18, 1e18, 6)
        if not ok2:
            return
        lane.spec.y_min, lane.spec.y_max = min(v0, v1), max(v0, v1)
        lane.y_fixed = True
        self._schedule_refresh()

    def _clear_fixed_range(self, idx: int) -> None:
        lane = self._lanes[idx]
        lane.spec.y_min = lane.spec.y_max = None
        lane.y_fixed = False
        lane.plot.enableAutoRange(axis="y")
        self._schedule_refresh()

    def _set_log_y(self, idx: int, on: bool) -> None:
        lane = self._lanes[idx]
        lane.log_y = bool(on)
        lane.spec.log_y = bool(on)
        self._apply_lane_style(lane)
        self._schedule_refresh()

    def _isolate(self, idx: int) -> None:
        for i, lane in enumerate(self._lanes):
            self._toggle_lane(i, i == idx, from_user=False)

    def _show_all(self) -> None:
        for i, lane in enumerate(self._lanes):
            self._toggle_curve(i, 0, True, from_user=False)
            for k in range(len(getattr(lane, "ghosts", ()))):
                self._toggle_curve(i, k + 1, True, from_user=False)

    def _export_lane_dialog(self, idx: int) -> int:
        lane = self._lanes[idx]
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出该泳道 CSV", f"{lane.label}.csv", "CSV (*.csv)")
        if not fn:
            return 0
        return self.export_csv(fn, only_lane=idx)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def QAction_visible(menu: QtWidgets.QMenu, lane: _Lane) -> QtGui.QAction:  # noqa: N802
    act = QtGui.QAction("显示该泳道", menu)
    act.setCheckable(True)
    act.setChecked(not lane.hidden)
    return act


def _style_kwargs(style: str) -> dict[str, Any]:
    """把 LaneSpec.style 翻成 pyqtgraph setData 风格参数。"""
    if style == "step":
        return {"stepMode": "right"}
    if style == "scatter":
        return {"symbol": "o", "symbolSize": 3, "pen": None}
    return {}


def _inv_tf(lane: _Lane, v: Any) -> Optional[float]:
    """把显示值（可能对数）还原成真实值。"""
    try:
        val = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    return 10.0 ** val if lane.log_y else val
