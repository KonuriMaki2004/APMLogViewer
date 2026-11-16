"""trackmap —— 真实地图航迹组件 TrackMapWidget（QGraphicsView + XYZ 瓦片，不用 QtWebEngine）。

单一真值来源
------------
缩放级 **只从视图变换派生**：``zoom() = REF_Z + log2(transform().m11())``。
所有缩放操作一律通过 ``view.scale(f, f)`` 施加（配合 ``AnchorUnderMouse`` 由 Qt 保证
"鼠标点不动"），绝不另存一份 z，避免双份真值漂移。

两种模式
--------
- geodetic：场景单位 = REF_Z(18) 级瓦片像素，Web Mercator，y 向下（北向朝上）。
- local：场景单位 = 米，north 取负折叠进坐标以保持 y 向下一致（用户 6 份 ZHT 视觉定位日志、
  以及任何没有经纬度的日志走这里）。

GCJ-02 对齐
-----------
绘制前把 WGS-84 轨迹/事件点做正加偏（wgs_to_gcj），读数时做反变换（gcj_to_wgs），
因此轨迹与高德底图严格对齐；境外点不加偏。

数据缺失
--------
空 TrackResult / 全 nan → 显示占位提示，不发瓦片请求，不抛异常。
"""
from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..core.trackdiff import (MIN_LENGTH_M, PairDiff, compare, fmt_m, fmt_pct,
                              overall)
from . import _tilemath as tm
from ._tiles import (BUILTIN_SOURCES, DEFAULT_SOURCE, TileSource, TileStore, default_cache_dir)
from .trackdiffpanel import TrackDiffPanel
from .tracklegend import TRACK_COLORS, TrackLegendPanel

log = logging.getLogger(__name__)

MODE_GEODETIC = "geodetic"
MODE_LOCAL = "local"
MODE_EMPTY = "empty"

#: 严重度配色（与 model.SEVERITY_LEVELS 对应）
SEV_COLORS: dict[str, str] = {
    "info": "#4ea1ff", "hint": "#ffd166", "warn": "#f4a259",
    "error": "#e76f51", "critical": "#d62828",
}
TRACK_FALLBACK_COLOR = "#00e5ff"

# 字体助手抽到了 _font（仪表浮层也要用，两边互相 import 会成环），
# 这里保留同名导出，existing 调用方和测试不受影响。
from ._font import panel_qss as _panel_qss, ui_font, ui_font_family   # noqa: E402
from .gauges import GaugePanel                                        # noqa: E402



_RAMP: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.00, (44, 123, 182)), (0.25, (171, 217, 233)), (0.50, (255, 255, 191)),
    (0.75, (253, 174, 97)), (1.00, (215, 25, 28)),
)

#: 本地模式像素/米允许范围。
# 本地坐标就是米，没有瓦片缩放级那种天然上限。以前上限于 400 px/m，
# 而纯视觉日志（炸机复盘）位移往往只有一米量级，fit 完就到 375，
# 上滚只能再放大 6% 就触顶——看上去又像“滚了没反应”。
# 下限 1e-4 px/m（1 万像素一公里）到上限 1e6 px/m（1 像素一微米），
# 笔画宽度/网格间距均除以 k 计算，不会因放大而变粗。
LOCAL_PPM_MIN, LOCAL_PPM_MAX = 1e-4, 1_000_000.0


def ramp_color(v: float) -> QtGui.QColor:
    """[0,1] → 蓝→青→黄→橙→红；nan 返回高对比青绿。"""
    if not math.isfinite(v):
        return QtGui.QColor(TRACK_FALLBACK_COLOR)
    x = min(1.0, max(0.0, v))
    for (p0, c0), (p1, c1) in zip(_RAMP, _RAMP[1:]):
        if p0 <= x <= p1:
            f = 0.0 if p1 == p0 else (x - p0) / (p1 - p0)
            return QtGui.QColor(int(c0[0] + (c1[0] - c0[0]) * f),
                                int(c0[1] + (c1[1] - c0[1]) * f),
                                int(c0[2] + (c1[2] - c0[2]) * f))
    return QtGui.QColor(_RAMP[-1][1])


def sev_color(sev: str) -> QtGui.QColor:
    return QtGui.QColor(SEV_COLORS.get(str(sev), "#b0b8bf"))


def _fmt(v: float, unit: str = "", nd: int = 1) -> str:
    return "-" if not math.isfinite(v) else f"{v:.{nd}f}{unit}"


# --------------------------------------------------------------------------- #
# 绘制项
# --------------------------------------------------------------------------- #


@dataclass
class _TrackEntry:
    """一条轨迹的展示信：键、中文名、配色、原始 TrackResult、是否主轨迹。"""

    key: str
    label: str
    color: str
    tr: Any
    primary: bool = False
    n: int = 0
    xy: tuple = field(default=(np.empty(0), np.empty(0)))
    drawn: bool = False


class _Layer:
    """图层容器：记录属于本层的图元，便于整层清除。"""

    def __init__(self, scene: QtWidgets.QGraphicsScene) -> None:
        self._scene = scene
        self.items: list[QtWidgets.QGraphicsItem] = []

    def add(self, item: QtWidgets.QGraphicsItem) -> QtWidgets.QGraphicsItem:
        self.items.append(item)
        return item

    def clear(self) -> None:
        for it in self.items:
            try:
                self._scene.removeItem(it)
            except Exception:              # 已被场景回收时静默
                pass
        self.items.clear()


class _ScreenItem(QtWidgets.QGraphicsItem):
    """屏幕尺寸恒定的图元（游标/徽标/圆点）的基类。

    **不能用 ``ItemIgnoresTransformations``**。那个标志下 Qt 用“场景坐标的
    boundingRect × 视图变换”算脏区，而 ``paint()`` 是按固定像素画的，两者对不上：
    缩到 z=19（≈0.27 px/m）时脏区只剩 32×0.27 ≈ 8.6 px，实际却画了 32 px，
    每次移动都留下约 23 px 的残影——倍速播放（一帧跳几十像素）时就看成一串箭头的
    “拖影”。改成显式 ``setScale(1/k)``：画多大、脏区就多大，天然对齐。

    ``k`` 由宿主在缩放后统一同步（``_rescale_pens``）与新建时顺带设一次。
    """

    def _sync_scale(self, k: float) -> None:
        kk = float(k) if k and math.isfinite(float(k)) else 1.0
        sc = 1.0 / max(kk, 1e-9)
        # 值没变就别调 setScale：它会触发变换更新 + 图元重绘，
        # 每次滚动 500 多次的无效调用合计几毫秒，而且会白白标脏
        if abs(sc - float(self.scale())) > 1e-9:
            self.setScale(sc)


class _CursorItem(_ScreenItem):
    """播放指针：圆点 + 朝向箭头，恒定屏幕尺寸。"""

    def __init__(self, color: Optional[QtGui.QColor] = None) -> None:
        super().__init__()
        self.setZValue(900)
        self._color = color or QtGui.QColor("#ffffff")
        self._heading = 0.0
        self.setVisible(False)

    def set_heading(self, deg: float) -> None:
        if math.isfinite(deg) and abs(deg - self._heading) > 0.5:
            self._heading = float(deg)
            self.update()

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-16, -16, 32, 32)

    def paint(self, p: QtGui.QPainter, opt, widget=None) -> None:      # noqa: D102
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.rotate(self._heading)
        pen = QtGui.QPen(QtGui.QColor("#0d1115"), 1.6)
        p.setPen(pen)
        p.setBrush(self._color)
        p.drawEllipse(QtCore.QRectF(-5, -5, 10, 10))
        p.setBrush(QtGui.QColor(self._color.red(), self._color.green(), self._color.blue(), 225))
        p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(9, 0), QtCore.QPointF(2, 5), QtCore.QPointF(2, -5)]))
        p.restore()


class _Badge(_ScreenItem):
    """带底色的小标签（起点/终点/HOME/区间标题），恒定屏幕尺寸。"""

    def __init__(self, text: str, color: QtGui.QColor) -> None:
        super().__init__()
        self.setZValue(820)
        self._text = text
        self._color = color
        self._font = ui_font(8)
        fm = QtGui.QFontMetricsF(self._font)
        self._w = max(20.0, fm.horizontalAdvance(text) + 14.0)
        self._h = 17.0

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-self._w / 2.0, -self._h - 6.0, self._w, self._h)

    def paint(self, p: QtGui.QPainter, opt, widget=None) -> None:      # noqa: D102
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = self.boundingRect()
        p.setPen(QtGui.QPen(self._color, 1.0))
        p.setBrush(QtGui.QColor(14, 18, 22, 200))
        p.drawRoundedRect(r, 4, 4)
        p.setFont(self._font)
        p.setPen(QtGui.QColor("#eef3f6"))
        p.drawText(r, QtCore.Qt.AlignmentFlag.AlignCenter, self._text)
        p.restore()


class _Dot(_ScreenItem):
    """事件标记圆点，恒定屏幕尺寸。"""

    def __init__(self, color: QtGui.QColor, radius: float = 4.5, tip: str = "") -> None:
        super().__init__()
        self.setZValue(810)
        self._color = color
        self._r = radius
        self._flash = 0.0
        if tip:
            self.setToolTip(tip)

    def set_active(self, on: bool) -> None:
        self._flash = 1.0 if on else 0.0
        self.update()

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-self._r - 4, -self._r - 4, 2 * self._r + 8, 2 * self._r + 8)

    def paint(self, p: QtGui.QPainter, opt, widget=None) -> None:      # noqa: D102
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        if self._flash:
            p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.4))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawEllipse(QtCore.QRectF(-self._r - 3, -self._r - 3, 2 * self._r + 6, 2 * self._r + 6))
        pen = QtGui.QPen(QtGui.QColor("#0d1115"), 1.0)
        p.setPen(pen)
        p.setBrush(self._color)
        p.drawEllipse(QtCore.QRectF(-self._r, -self._r, 2 * self._r, 2 * self._r))
        p.restore()


class _PluginDot(_ScreenItem):
    """插件图层里的标记点（恒定屏幕尺寸，可带一行文字）。

    不用 ``_Badge``：插件标记可能很多，带背景框会遮住地图；这里只画小圆点 +
    右侧文字，字下面着地图也看得清（描边 + 实心）。
    """

    def __init__(self, color: QtGui.QColor, label: str = "", radius: float = 4.0) -> None:
        super().__init__()
        self.setZValue(812)
        self._color = color
        self._label = str(label or "")
        self._r = float(radius)
        if self._label:
            self.setToolTip(self._label)

    def _text_w(self) -> float:
        if not self._label:
            return 0.0
        return max(18.0, 6.4 * len(self._label))

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(-self._r - 3, -self._r - 3,
                             2 * self._r + 6 + self._text_w(), 2 * self._r + 6)

    def paint(self, p: QtGui.QPainter, opt, widget=None) -> None:      # noqa: D102
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.setPen(QtGui.QPen(QtGui.QColor("#0d1115"), 1.0))
        p.setBrush(self._color)
        p.drawEllipse(QtCore.QRectF(-self._r, -self._r, 2 * self._r, 2 * self._r))
        if self._label:
            f = p.font()
            f.setPointSizeF(8.0)
            p.setFont(f)
            pos = QtCore.QPointF(self._r + 4, self._r * 0.5)
            p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 190), 2.6))
            p.drawText(pos + QtCore.QPointF(0.8, 0.8), self._label)
            p.setPen(QtGui.QPen(QtGui.QColor("#f4f7fa"), 0.9))
            p.drawText(pos, self._label)
        p.restore()


# --------------------------------------------------------------------------- #
# 视图
# --------------------------------------------------------------------------- #


class _MapView(QtWidgets.QGraphicsView):
    """只做事件转发与视口绘制，业务全在宿主组件。"""

    def __init__(self, owner: "TrackMapWidget", scene: QtWidgets.QGraphicsScene) -> None:
        super().__init__(scene)
        self._owner = owner
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # 地图自己接管平移，滚动条只会吃掉视口并出现灰色边条。
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setRenderHints(QtGui.QPainter.RenderHint.Antialiasing |
                            QtGui.QPainter.RenderHint.TextAntialiasing)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._press: Optional[QtCore.QPoint] = None
        self._moved = False

    # ---- Qt 回调统一先取 owner：QGraphicsView.__init__ 内部就会触发
    #      scrollContentsBy/resizeEvent/drawForeground，那时 _owner 尚未赋值。
    def _o(self):
        return getattr(self, "_owner", None)

    def wheelEvent(self, ev: QtGui.QWheelEvent) -> None:
        steps = ev.angleDelta().y() / 120.0
        owner = self._o()
        if owner is None or abs(steps) < 1e-6:
            ev.ignore()
            return
        owner.zoom_by(+1.0 if steps > 0 else -1.0, ev.position().toPoint())
        ev.accept()

    def mousePressEvent(self, ev: QtGui.QMouseEvent) -> None:
        self._press = ev.position().toPoint()
        self._moved = False
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev: QtGui.QMouseEvent) -> None:
        if (self._press is not None and (ev.buttons() & QtCore.Qt.MouseButton.LeftButton)
                and (ev.position().toPoint() - self._press).manhattanLength() > 4):
            self._moved = True
        owner = self._o()
        if owner is not None:
            owner.on_mouse_move(ev.position().toPoint())
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev: QtGui.QMouseEvent) -> None:
        click = (not self._moved) and ev.button() == QtCore.Qt.MouseButton.LeftButton
        pos = ev.position().toPoint()
        super().mouseReleaseEvent(ev)
        owner = self._o()
        if click and owner is not None:
            owner.on_view_click(pos)

    def mouseDoubleClickEvent(self, ev: QtGui.QMouseEvent) -> None:
        owner = self._o()
        if owner is not None:
            owner.fit_to_track()
        ev.accept()

    def keyPressEvent(self, ev: QtGui.QKeyEvent) -> None:
        owner = self._o()
        if owner is None or not owner.on_key(ev.key()):
            super().keyPressEvent(ev)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        owner = self._o()
        if owner is not None:
            owner.schedule_layer_refresh()

    def resizeEvent(self, ev: QtGui.QResizeEvent) -> None:
        super().resizeEvent(ev)
        owner = self._o()
        if owner is not None:
            owner.schedule_layer_refresh()

    def drawForeground(self, p: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        super().drawForeground(p, rect)
        owner = self._o()
        if owner is not None:
            owner.paint_scale_bar(p, self.viewport().rect())

    def leaveEvent(self, ev: QtCore.QEvent) -> None:
        super().leaveEvent(ev)
        owner = self._o()
        if owner is not None:
            owner.on_mouse_leave()


# --------------------------------------------------------------------------- #
# 主组件
# --------------------------------------------------------------------------- #


class TrackMapWidget(QtWidgets.QWidget):
    """真实地图 + 航迹 + 事件 + 播放指针 + 告警区间。"""

    timeChanged = QtCore.Signal(float)          # 点击轨迹 seek
    sourceChanged = QtCore.Signal(str)          # 瓦片源切换
    trackVisibilityChanged = QtCore.Signal(list)   # 勾选变化 → 可见轨迹 key

    MAX_TRACK_POINTS = 20000                    # 抽稀上限
    TRACK_PX = 3.0
    MARGIN_TILES = 1
    CLICK_RADIUS_PX = 14.0
    GAUGE_MARGIN = 10
    LEGEND_MARGIN = 10

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 source_name: str = DEFAULT_SOURCE,
                 cache_dir: Optional[str] = None) -> None:
        super().__init__(parent)
        # ---------- 实例状态（先建，后续任何回调都不得遇到缺失属性） ----------
        self._mode = MODE_EMPTY
        self._tr: Optional[Any] = None
        self._t = np.empty(0)
        self._sx = np.empty(0)
        self._sy = np.empty(0)
        self._alt = np.empty(0)
        self._spd = np.empty(0)
        self._heading = np.empty(0)
        self._events: list[dict[str, Any]] = []
        self._regions: list[dict[str, Any]] = []
        self._follow = False
        # 轨迹跟随时间线：True 时只画到播放指针处（用户 2026-10-24 的"轨迹播放"）
        self._follow_time = False
        self._primary_on = True           # 图例里主轨迹是否勾选（决定要不要画它）
        self._playhead: float = float("inf")
        self._ph_timer = QtCore.QTimer(self)      # 播放 20fps，重画合并到 ~8fps
        self._ph_timer.setSingleShot(True)
        self._ph_timer.setInterval(self.FOLLOW_REDRAW_MS)
        self._ph_timer.timeout.connect(self._redraw_follow)
        self._cursor_idx = -1
        self._tile_items: dict[tuple[int, int, int], QtWidgets.QGraphicsItem] = {}
        self._placeholder_keys: set[tuple[int, int, int]] = set()
        self._local_ppm = 1.0

        # ---------- 场景 / 图层 ----------
        self._scene = QtWidgets.QGraphicsScene(self)
        self._scene.setBackgroundBrush(QtGui.QColor("#1e242a"))
        world = tm.TILE_SIZE * (1 << tm.REF_Z)
        self._scene.setSceneRect(0, 0, world, world)
        self.view = _MapView(self, self._scene)
        self._layer_tiles = _Layer(self._scene)
        self._layer_grid = _Layer(self._scene)
        self._layer_track = _Layer(self._scene)
        self._layer_track2 = _Layer(self._scene)     # 副轨迹（不跟光标，只对比形状）
        # 插件自定义图层：画在轨迹之上、区域/徽标之下（用户 2026-10-28）
        self._layer_plugin = _Layer(self._scene)
        self._plugin_layers: list[dict[str, Any]] = []
        self._layer_region = _Layer(self._scene)
        self._layer_badge = _Layer(self._scene)
        self._layer_event = _Layer(self._scene)
        self._cursor = _CursorItem()
        self._scene.addItem(self._cursor)

        # ---------- 瓦片源 ----------
        self._sources: dict[str, TileSource] = {s.name: s for s in BUILTIN_SOURCES}
        self._order: list[str] = [s.name for s in BUILTIN_SOURCES]
        self._src = self._sources.get(source_name) or self._sources[DEFAULT_SOURCE]
        self._tiles = TileStore(cache_dir=cache_dir or default_cache_dir(), parent=self)
        self._tiles.tile_ready.connect(self._on_tile_ready)

        # ---------- 布局与覆盖层 ----------
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view)
        self.status = QtWidgets.QLabel(self)
        self.status.setTextFormat(QtCore.Qt.TextFormat.RichText)
        # 这一行状态文字很长（瓦片/坐标/游标…），默认 QLabel 会把自己的文字宽度当成
        # 最小宽，进而把整个标签区的最小宽顶到 1200+，左侧分隔条就拉不动了。
        # 横向上让尺寸诉求失效：布局不再按它算最小宽，窗窄时字被截断也不抢地盘。
        self.status.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                  QtWidgets.QSizePolicy.Policy.Preferred)
        self.status.setMinimumWidth(0)
        self.status.setToolTip("地图状态：数据源 / 缩放 / 光标经纬度 / 游标时刻 / 瓦片缓存与失败数")
        self.status.setStyleSheet(_panel_qss("background:#141a1f; color:#c9d4db; padding:3px 8px;"
                                             "border-top:1px solid #2b3540; font-size:11px;"))
        lay.addWidget(self.status)
        self._attr = QtWidgets.QLabel(self.view.viewport())
        self._attr.setStyleSheet(_panel_qss("background:rgba(12,16,20,175); color:#e8eef2;"
                                            "padding:1px 6px; border-radius:3px; font-size:11px;"))
        self._attr.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._placeholder = QtWidgets.QLabel(self.view.viewport())
        self._placeholder.setStyleSheet(_panel_qss("background:rgba(12,16,20,195); color:#ffd166;"
                                                   "padding:6px 12px; border-radius:4px; font-size:12px;"))
        self._placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._placeholder.hide()
        self._attr.show()

        # ---------- 左下角仪表（罗盘 + 姿态球） ----------
        self._att_t = self._att_pitch = self._att_roll = self._att_yaw = np.empty(0)
        self._alt_t = self._alt_v = np.empty(0)
        self._alt_src = ""
        # 电池原始量（电压/电流）时间线，只用于仪表读数
        self._bat_t = np.empty(0)
        self._bat_v = np.empty(0)
        self._bat_a = np.empty(0)
        self._last_time = float("nan")
        self.gauges = GaugePanel(self.view.viewport())
        self.gauges.setStyleSheet(_panel_qss("color:#c9d4db; font-size:11px;"))
        self.gauges.lbl.setStyleSheet(_panel_qss("color:#c9d4db; font-size:11px;"))
        self.gauges.lbl_elec.setStyleSheet(_panel_qss("color:#c9d4db; font-size:10px;"))
        self.gauges.collapseToggled.connect(lambda _on: self._reposition_overlays())
        self.gauges.show()

        # ---------- 右侧轨迹勾选栏（多条轨迹时才出现） ----------
        self._entries: list[_TrackEntry] = []
        self._primary_key = ""
        self.legend = TrackLegendPanel(self.view.viewport())
        self.legend.visibilityChanged.connect(self._on_legend)
        self.legend.hide()
        # 轨迹差异面板：挂在勾选栏正下方（未满 2 条可比轨迹时给一句提示）
        self.diff = TrackDiffPanel(self.view.viewport())
        self.diff.hide()
        self._diff_cache: dict[tuple[str, str], PairDiff] = {}

        self._refresh = QtCore.QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(60)
        self._refresh.timeout.connect(self._refresh_layers)
        # 抽稀是按「当时的缩放」做的；放大到 1.6 倍以上后点距就超过 2.5 px 会发棱。
        # 重建要等缩放**停下来**再做（抽稀 + 重画约 90 ms），否则滚轮连滚会一顿一顿。
        self._built_k = 0.0
        self._rebuild = QtCore.QTimer(self)
        self._rebuild.setSingleShot(True)
        self._rebuild.setInterval(400)
        self._rebuild.timeout.connect(self._rebuild_tracks)

        # ---------- 初始视图 ----------
        self._set_zoom(15.0)
        self.view.centerOn(world / 2, tm.lat_toMercatorY(35.0) * world)
        self._attr.setText(self._src.attribution)
        self._show_placeholder("")
        self._refresh_layers()
        self._update_status()

    # ================================================================ 公共 API
    def set_tracks(self, tracks) -> None:
        """载入全部轨迹（TrackResult 列表）。

        主轨迹（列表第一个，即 POS→GPS#0→XKF1→VISP 的优先结果）走原来的单轨迹机制：
        光标、仪表盘、高度带、fit 全都只跟它走，语义与以前一致；其余轨迹画成
        纯色副轨迹线，只在右侧栏里勾选对比。只有一条轨迹时栏子自动隐藏。
        """
        items = [t for t in (tracks or []) if t is not None]
        self._entries = []
        self._primary_key = ""
        self._diff_cache.clear()                     # 换日志：旧的一对差值作废
        if not items:
            self.legend.set_tracks([])
            self.legend.hide()
            self.diff.hide()
            self.set_track(None)
            return
        for i, tr in enumerate(items):
            key = str(getattr(tr, "key", "") or f"T{i}")
            label = str(getattr(tr, "label", "") or key)
            self._entries.append(_TrackEntry(
                key=key, label=label, color=TRACK_COLORS[i % len(TRACK_COLORS)],
                tr=tr, primary=(i == 0),
                n=int(getattr(tr, "n_points", 0) or 0)))
        self._primary_key = self._entries[0].key
        self.set_track(items[0])                    # 既有单轨迹链路（含 fit）
        self._update_legend()                       # 先把勾选栏填好（默认全选）
        self._draw_others()                         # 再按勾选状态画副轨迹
        self._expand_local_rect()                   # 本地模式撑开矩形，副轨迹不被裁剪
        if any(not e.primary for e in self._entries):
            self.fit_to_track()                     # 收拢到并集，k 又变了
            self._rebuild_tracks()                  # 按最终缩放重画，别留过期抽稀

    def visible_tracks(self) -> list[str]:
        """当前勾选的轨迹。

        唯一真源是勾选栏的**行状态**，不能用 ``legend.isVisible()`` 判断：地图页不是
        当前 tab 时整个页面被 Qt 隐藏，``isVisible()`` 会变成 False；以前据此回退成
        "全部轨迹"，于是每次切页（`_apply` 会重建轨迹）都会按全选算一遍轨迹差异，
        切回地图后还留着——面板会列出未勾选轨迹的组合（真机复现）。
        """
        if self._entries and self.legend.keys():
            return self.legend.visible()
        return [e.key for e in self._entries]

    def showEvent(self, ev: QtGui.QShowEvent) -> None:
        super().showEvent(ev)
        # 从别的页切回来：按当前勾选补算一次，不留隐藏期间算出的旧结果
        self._update_diff()

    def set_track(self, tr) -> None:
        """载入 TrackResult。空/全 nan 会降级为占位提示。"""
        self._tr = tr
        if not self._valid_track(tr):
            self._mode = MODE_EMPTY
            self._reset_data()
            self._show_placeholder("该日志无位置数据")
            self._update_status()
            return
        kind = str(getattr(tr, "kind", "local"))
        self._mode = MODE_GEODETIC if kind == "geodetic" and bool(getattr(tr, "has_geodetic", False)) \
            else MODE_LOCAL
        self._compute_xy()
        if self._sx.size < 2:
            self._mode = MODE_EMPTY
            self._reset_data()
            self._show_placeholder("位置数据全部无效（全为 nan）")
            self._update_status()
            return
        if self._mode == MODE_GEODETIC:
            self._show_placeholder("")
        else:
            note = str(getattr(tr, "note", "") or "")
            self._show_placeholder(note or "无经纬度，仅相对轨迹（本地 ENU，单位 m）")
        self._rebuild_regions()
        self._rebuild_markers()
        # 先定缩放再绘制：抽稀步长是按 k 算的，先画后 fit 会白白多画一遍，
        # 而且 _built_k 立刻变得过期 → 400 ms 后还会自己重建一次（白多一次卡顿）。
        self.fit_to_track()
        self._draw_track()
        self._update_status()

    def set_events(self, events: Sequence[dict]) -> None:
        self._events = [dict(e) for e in (events or [])]
        self._rebuild_markers()

    def set_regions(self, regions: Sequence[dict]) -> None:
        self._regions = [dict(r) for r in (regions or [])]
        self._rebuild_regions()

    # ---- 插件自定义图层 -------------------------------------------------- #
    def set_plugin_layers(self, layers: Sequence[dict]) -> None:
        """接收插件图层（图元已由 PluginManager 校验过颜色/坐标/点数）。

        ``layers`` 形如 ``[{"name": "…", "color": "#2f6fed", "prims": [...]}, …]``，
        图元三类：line（经纬度折线）、point（标记）、circle（半径圆）。
        """
        self._plugin_layers = [dict(x) for x in (layers or [])]
        self._draw_plugin_layers()

    def plugin_layers(self) -> list[dict]:
        """当前插件图层（给自测/排查用）。"""
        return [dict(x) for x in self._plugin_layers]

    def _meters_to_scene(self, lat: float) -> float:
        """该纬度上 1 米等于多少场景单位（场景 = REF_Z 级像素）。"""
        mpp = 156543.03392 * max(1e-6, math.cos(math.radians(lat))) / (1 << tm.REF_Z)
        return float(mpp) if mpp > 0 else 1.0

    def _draw_plugin_layers(self) -> None:
        """重画插件图层。不猜数据：坐标不合法就跳过那个图元，整个层空了就不画。"""
        self._layer_plugin.clear()
        if not self._plugin_layers:
            return
        k = self._k() or 1.0
        for layer in self._plugin_layers:
            default = QtGui.QColor(str(layer.get("color") or "#2f6fed"))
            if not default.isValid():
                default = QtGui.QColor("#2f6fed")
            for prim in layer.get("prims") or []:
                kind = str(prim.get("kind") or "")
                col = QtGui.QColor(str(prim.get("color") or ""))
                if not col.isValid():
                    col = default
                if kind == "line":
                    self._emit_plugin_line(prim, col, k)
                elif kind == "point":
                    self._emit_plugin_point(prim, col, k)
                elif kind == "circle":
                    self._emit_plugin_circle(prim, col, k)

    def _emit_plugin_line(self, prim: dict, col: QtGui.QColor, k: float) -> None:
        pts = list(prim.get("points") or [])
        if len(pts) < 2:
            return
        xs = np.empty(len(pts), dtype=np.float64)
        ys = np.empty(len(pts), dtype=np.float64)
        for i, (lat, lon) in enumerate(pts):
            sx, sy = self._scene_of_lonlat(float(lat), float(lon))
            if not (math.isfinite(sx) and math.isfinite(sy)):
                return                          # 有一个点就算不出来：整条不画（不猜位置）
            xs[i], ys[i] = sx, sy
        width = float(prim.get("width") or 2)
        if not prim.get("dash"):
            self._emit(self._layer_plugin, xs, ys, col, width, 11, k)
            return
        # 虚线：_emit 只会画实线，这里单独建一条 path
        path = QtGui.QPainterPath(QtCore.QPointF(float(xs[0]), float(ys[0])))
        for i in range(1, xs.size):
            path.lineTo(float(xs[i]), float(ys[i]))
        it = QtWidgets.QGraphicsPathItem(path)
        pen = QtGui.QPen(col, max(width / max(k, 1e-9), 1e-6))
        pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        it.setPen(pen)
        it.setZValue(11)
        it._base_px = width                     # 缩放时由 _rescale_pens 重算线宽
        self._layer_plugin.add(it)

    def _emit_plugin_point(self, prim: dict, col: QtGui.QColor, k: float) -> None:
        sx, sy = self._scene_of_lonlat(float(prim.get("lat")), float(prim.get("lon")))
        if not (math.isfinite(sx) and math.isfinite(sy)):
            return
        dot = _PluginDot(col, str(prim.get("label") or ""))
        dot._sync_scale(k)
        dot.setPos(float(sx), float(sy))
        self._layer_plugin.add(dot)

    def _emit_plugin_circle(self, prim: dict, col: QtGui.QColor, k: float) -> None:
        lat, lon = float(prim.get("lat")), float(prim.get("lon"))
        sx, sy = self._scene_of_lonlat(lat, lon)
        if not (math.isfinite(sx) and math.isfinite(sy)):
            return
        r = float(prim.get("radius_m") or 0.0) / self._meters_to_scene(lat)
        if not (math.isfinite(r) and r > 0):
            return
        it = QtWidgets.QGraphicsEllipseItem(QtCore.QRectF(-r, -r, 2 * r, 2 * r))
        pen = QtGui.QPen(col, max(2.0 / max(k, 1e-9), 1e-6))
        pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        it.setPen(pen)
        fill = QtGui.QColor(col)
        fill.setAlpha(28)
        it.setBrush(QtGui.QBrush(fill))
        it.setZValue(10)
        it.setPos(float(sx), float(sy))
        self._layer_plugin.add(it)

    def set_attitude(self, t, pitch, roll, yaw) -> None:
        """喂姿态时间线（度）。传空数组就回到“无姿态数据”状态。

        和航迹是两套时间轴（ANG 往往比 POS 密），所以单独存、单独查，
        不假装它们同长。
        """
        arr = lambda x: np.asarray(x if x is not None else [], dtype=float)   # noqa: E731
        self._att_t, self._att_pitch = arr(t)[:], arr(pitch)
        self._att_roll, self._att_yaw = arr(roll), arr(yaw)
        n = min(self._att_t.size, self._att_pitch.size, self._att_roll.size, self._att_yaw.size)
        self._att_t, self._att_pitch = self._att_t[:n], self._att_pitch[:n]
        self._att_roll, self._att_yaw = self._att_roll[:n], self._att_yaw[:n]
        if n == 0:
            self.gauges.set_no_data()
        else:
            self.gauges.setToolTip("播放或点航迹时，这里的罗盘、姿态球、高度带会跟着走")
        self._push_gauge(self._last_time)

    def set_altitude(self, t, alt, source: str = "气压") -> None:
        """喂一条备选高度时间线（米，相对起飞点），只在没航迹时用它。

        航迹本身带高度，而且地图游标读数用的就是它；两个地方报不一样的数
        用户只会觉得其中一个错了，所以有航迹时优先走航迹。
        """
        arr = lambda x: np.asarray(x if x is not None else [], dtype=float)   # noqa: E731
        self._alt_t, self._alt_v = arr(t)[:], arr(alt)[:]
        n = min(self._alt_t.size, self._alt_v.size)
        self._alt_t, self._alt_v = self._alt_t[:n], self._alt_v[:n]
        self._alt_src = source or "气压"
        self._push_gauge(self._last_time)

    def set_battery(self, t, volt, curr=None) -> None:
        """喂电池电压/电流时间线（BATT.Volt / BATT.Curr）。

        只用于给仪表显示原始量：剩余电量是按电压推的估计值，地图上不再按它标区域
        （用户 2026-10-24）。电流可不给，那就只显示电压。
        """
        arr = lambda x: np.asarray(x if x is not None else [], dtype=float)   # noqa: E731
        ts, vs, cs = arr(t), arr(volt), arr(curr)
        n = min(ts.size, vs.size)
        self._bat_t, self._bat_v = ts[:n], vs[:n]
        if cs.size >= n:
            self._bat_a = cs[:n]
        else:
            self._bat_a = np.full(n, np.nan)
        self._push_gauge(self._last_time)

    def set_track_follow(self, follow: bool) -> None:
        """轨迹是否只画到播放指针处。

        ``False``（默认）= 画完整轨迹；``True`` = 只画到指针（时间线拖动/播放时逐点长出来）。
        切换后立即重画一次，不必等下一次时间更新。
        """
        was = self._follow_time
        self._follow_time = bool(follow)
        self._ph_timer.stop()
        if was != self._follow_time:
            for e in self._entries:                 # 缓存里可能是裁剪过的坐标，作废
                e.xy = (np.empty(0), np.empty(0))
            if self._follow_time and self._last_time is not None                     and math.isfinite(float(self._last_time)):
                self._playhead = float(self._last_time)
        self._redraw_follow()

    def track_follow(self) -> bool:
        return self._follow_time

    def set_playhead(self, t: float) -> None:
        """设置"画到哪里"。跟随模式下合并高频调用后重画一次。"""
        try:
            tt = float(t)
        except (TypeError, ValueError):
            return
        if not math.isfinite(tt):
            return
        if tt == self._playhead:
            return
        self._playhead = tt
        if self._follow_time and not self._ph_timer.isActive():
            self._ph_timer.start()

    def _redraw_follow(self) -> None:
        if self._mode == MODE_EMPTY:
            return
        self._draw_track()
        self._draw_others()
        self._sync_screen_items()
        self.schedule_layer_refresh()

    def set_gauges_visible(self, on: bool) -> None:
        self.gauges.setVisible(bool(on))
        self._reposition_overlays()

    def gauges_visible(self) -> bool:
        return self.gauges.isVisible()

    def set_time(self, t: float) -> None:
        """移动播放指针；**不回发 timeChanged**。"""
        self._last_time = float(t)
        if self._follow_time and math.isfinite(float(t)):
            self.set_playhead(float(t))          # 跟随模式下轨迹跟着长出来
        if self._t.size == 0 or not math.isfinite(t):
            self._cursor.setVisible(False)
            self._cursor_idx = -1
            self._blink(-1)
            self._push_gauge(t)          # 无航迹但有姿态的日志，仪表仍要动
            return
        i = self._index_at(t)
        self._cursor_idx = i
        self._cursor.setVisible(True)
        self._cursor.setPos(float(self._sx[i]), float(self._sy[i]))
        if self._heading.size > i:
            self._cursor.set_heading(float(self._heading[i]))
        self._blink(i)
        if self._follow:
            self.view.centerOn(float(self._sx[i]), float(self._sy[i]))
            self.schedule_layer_refresh()
        self._update_status(idx=i)
        self._push_gauge(t)

    def showEvent(self, ev: QtGui.QShowEvent) -> None:      # noqa: N802
        super().showEvent(ev)
        # 隐藏期间不推仪表（省功），所以“先加载后显示”的路径（批量、自检、
        # 从对话框里选完文件）要在真正可见后补一次，否则会停在“无姿态”。
        self._reposition_overlays()
        self._push_gauge(self._last_time)

    def _push_gauge(self, t: float) -> None:
        """把当前时刻的姿态和高度推给左下角仪表。

        两者各自独立推：姿态可能只能退回地迹方位，而高度仍能从航迹拿到，
        不能因为一个没数据就连另一个也不推。
        """
        try:
            if not self.gauges.isVisible():
                return
            self._push_pose(t)
            self._push_alt(t)
            self._push_batt(t)
        except Exception:                # noqa: BLE001 仪表是锦上添花，不能拖垮地图
            log.debug("仪表更新失败", exc_info=True)

    def _push_pose(self, t: float) -> None:
        """姿态：优先 ANG/ATT，没有就退回地面航迹方位。"""
        if self._att_t.size and math.isfinite(t):
            i = int(np.searchsorted(self._att_t, t))
            i = max(0, min(i, self._att_t.size - 1))
            # 拿不到就近点就算了，宁可显示“无”也不拿半分钟前的姿态骗入
            if abs(float(self._att_t[i]) - float(t)) <= 2.0:
                self.gauges.set_pose(float(self._att_pitch[i]), float(self._att_roll[i]),
                                     float(self._att_yaw[i]), "飞控")
                return
        i = int(self._cursor_idx) if self._cursor_idx is not None else -1
        if 0 <= i < self._heading.size and math.isfinite(float(self._heading[i])):
            self.gauges.set_pose(float("nan"), float("nan"),
                                 float(self._heading[i]), "地迹")
            return
        self.gauges.set_pose_missing()

    def _push_batt(self, t: float) -> None:
        """电池电压/电流：取最近一次采样（不插值）——电压电流是实测点，
        插值出来的数反而像真的。超过 3s 没有新数据就不显示，避免拿落地前的电压冒充。"""
        if not (self._bat_t.size and math.isfinite(t)):
            self.gauges.set_battery(None, None)
            return
        j = int(np.searchsorted(self._bat_t, t))
        j = max(0, min(j, self._bat_t.size - 1))
        if abs(float(self._bat_t[j]) - float(t)) > 3.0:
            self.gauges.set_battery(None, None)
            return
        v = float(self._bat_v[j]) if j < self._bat_v.size else float("nan")
        a = float(self._bat_a[j]) if j < self._bat_a.size else float("nan")
        self.gauges.set_battery(v, a)

    def _push_alt(self, t: float) -> None:
        """高度：有航迹就用航迹的（和地图读数同源），否则用喂进来的气压高度。"""
        i = int(self._cursor_idx) if self._cursor_idx is not None else -1
        if 0 <= i < self._alt.size and math.isfinite(float(self._alt[i])):
            self.gauges.set_altitude(float(self._alt[i]), "航迹")
            return
        if self._alt_t.size and math.isfinite(t):
            j = int(np.searchsorted(self._alt_t, t))
            j = max(0, min(j, self._alt_t.size - 1))
            v = float(self._alt_v[j])
            if abs(float(self._alt_t[j]) - float(t)) <= 2.0 and math.isfinite(v):
                self.gauges.set_altitude(v, self._alt_src)
                return
        self.gauges.set_altitude(float("nan"), "")

    def fit_to_track(self) -> None:
        """视野收拢到轨迹外框 + 10% 余量（多轨迹时按可见轨迹的并集算）。"""
        if self._sx.size == 0:
            return
        xs = [self._sx]
        ys = [self._sy]
        vis = set(self.legend.visible()) if self._entries else set()
        for e in self._entries:
            if e.primary or e.key not in vis or not self._drawable(e):
                continue
            if e.xy[0].size:
                xs.append(e.xy[0])
                ys.append(e.xy[1])
        x0 = float(np.nanmin(np.concatenate([a for a in xs if a.size])))
        x1 = float(np.nanmax(np.concatenate([a for a in xs if a.size])))
        y0 = float(np.nanmin(np.concatenate([a for a in ys if a.size])))
        y1 = float(np.nanmax(np.concatenate([a for a in ys if a.size])))
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        vp = self.view.viewport().size()
        vw, vh = max(64, vp.width() - 24), max(64, vp.height() - 24)
        k = min(vw / max(x1 - x0, 1e-6) * 0.9, vh / max(y1 - y0, 1e-6) * 0.9)
        if self._mode == MODE_GEODETIC:
            self._set_zoom(tm.REF_Z + math.log2(max(k, 1e-12)))
        else:
            self._set_local_ppm(max(LOCAL_PPM_MIN, min(LOCAL_PPM_MAX, k)))
        self.view.centerOn(cx, cy)
        self._refresh_layers()

    def add_source(self, name: str, url_template: str, attribution: str,
                   datum: str = "WGS84", max_zoom: int = 19,
                   subdomains: Sequence[str] = ()) -> None:
        """注册自定义 XYZ 源。datum 只接受 WGS84 / GCJ02。"""
        d = (datum or "WGS84").upper()
        if d not in ("WGS84", "GCJ02"):
            raise ValueError(f"datum 只支持 WGS84/GCJ02，收到 {datum!r}")
        src = TileSource(name=name, url_template=url_template, attribution=attribution,
                         datum=d, kind="vector", max_zoom=int(max_zoom),
                         subdomains=tuple(subdomains))
        self._sources[name] = src
        self._tiles.register_source(src)
        if name not in self._order:
            self._order.append(name)

    def set_source(self, name: str) -> None:
        src = self._sources.get(name)
        if src is None:
            log.warning("未知瓦片源 %s；可用: %s", name, ", ".join(self._order))
            return
        if src.name == self._src.name:
            return
        self._tiles.cancel_all()
        self._tiles.clear_failures()
        self._src = src
        self._tiles.register_source(src)
        self._attr.setText(src.attribution)
        if self._mode == MODE_GEODETIC:
            z = self._clamp_zoom(self.zoom())
            if abs(z - self.zoom()) > 1e-6:
                self._set_zoom(z)
        # datum 可能变化 → 几何与瓦片都要重算
        self._hide_all_tiles()
        if self._sx.size:
            self._compute_xy()
            self._draw_track()
            self._rebuild_regions()
            self._rebuild_markers()
        self._refresh_layers()
        self.sourceChanged.emit(src.name)

    def sources(self) -> list[str]:
        return list(self._order)

    def current_source(self) -> TileSource:
        return self._src

    def set_playback_follow(self, on: bool) -> None:
        """播放时地图是否跟随游标平移。"""
        self._follow = bool(on)

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:
        """关窗时确定性地中止网络回调，避免退出阶段析构竞态。"""
        try:
            self._refresh.stop()
            self._tiles.shutdown()
            self._hide_all_tiles()
        except RuntimeError:
            pass
        super().closeEvent(ev)

    def export_png(self, path: str) -> bool:
        try:
            ok = bool(self.grab().save(path, "PNG"))
            if not ok:
                log.warning("PNG 保存失败: %s", path)
            return ok
        except Exception as exc:
            log.exception("导出 PNG 异常: %s", exc)
            return False

    def zoom(self) -> float:
        """当前缩放级（唯一真值：来自视图变换）。"""
        k = self.view.transform().m11()
        if not k or not math.isfinite(k):
            return 1.0
        return tm.REF_Z + math.log2(float(k))

    def state(self) -> dict[str, Any]:
        """内部状态快照，供自测与状态栏。"""
        lat = lon = float("nan")
        if self._mode == MODE_GEODETIC:
            c = self.view.mapToScene(self.view.viewport().rect().center())
            lat, lon = tm.scene_to_lonlat(c.x(), c.y())
            lat, lon = tm.convert_pair(lat, lon, self._src.datum, to_wgs=True)
        return {"mode": self._mode, "source": self._src.name, "datum": self._src.datum,
                "zoom": round(self.zoom(), 2), "ppm": round(self._k(), 4),
                "center_wgs84": (round(lat, 6), round(lon, 6)),
                "points": int(self._sx.size), "tiles_visible": len(self._tile_items),
                "tile_stats": dict(self._tiles.stats), "inflight": self._tiles.inflight(),
                "pending": self._tiles.pending()}

    # ================================================================ 交互回调
    def zoom_by(self, dz: float, anchor: QtCore.QPoint) -> None:
        """以 anchor（视口坐标）为不动点缩放 dz 级：上滚放大、下滚缩小。

        坑（真 bug）：本地模式以前拿 ``zoom()`` 当当前值，而 ``zoom()`` 返回的是
        **缩放级** ``REF_Z + log2(k)``（≈ 27），不是 ppm。用 ppm 去除以它算比例，
        结果不管上滚还是下滚比例都 > 1，表现为“没有卫星图时怎么滚都是放大”。
        本地模式没有瓦片缩放级这个概念，基准必须是视图变换的实际 ppm ``_k()``，
        并且算完要把真值回写 ``_local_ppm``，否则下一轮又拿旧值算。
        """
        if self._mode == MODE_LOCAL:
            cur = self._k() or 1.0
            want = max(LOCAL_PPM_MIN, min(LOCAL_PPM_MAX, cur * (2.0 ** dz)))
            self._local_ppm = want                 # 回写真值
            f = want / cur
        else:
            cur = self.zoom()
            f = 2.0 ** (self._clamp_zoom(cur + dz) - cur)
        if abs(f - 1.0) < 1e-9:
            return
        self.view.scale(f, f)
        self._sync_screen_items()          # 滚轮缩放后必须重算屏幕恒定尺寸图元的比例
        self.schedule_layer_refresh()
        self._update_status()

    def on_mouse_move(self, pos: QtCore.QPoint) -> None:
        self._update_status(widget_pos=pos)

    def on_mouse_leave(self) -> None:
        self._update_status()

    def on_view_click(self, pos: QtCore.QPoint) -> None:
        """点击轨迹 14 px 内 → 发 timeChanged。"""
        if self._sx.size == 0:
            return
        p = self.view.mapToScene(pos)
        k = self._k() or 1.0
        d = np.hypot(self._sx - p.x(), self._sy - p.y()) * k
        i = int(np.argmin(d))
        if d[i] <= self.CLICK_RADIUS_PX:
            self.set_time(float(self._t[i]))
            self.timeChanged.emit(float(self._t[i]))
        else:
            self._update_status(widget_pos=pos)

    def on_key(self, key: int) -> bool:
        c = self.view.viewport().rect().center()
        if key in (QtCore.Qt.Key.Key_Plus, QtCore.Qt.Key.Key_Equal):
            self.zoom_by(+1.0, c)
        elif key in (QtCore.Qt.Key.Key_Minus, QtCore.Qt.Key.Key_Underscore):
            self.zoom_by(-1.0, c)
        elif key == QtCore.Qt.Key.Key_F:
            self.fit_to_track()
        elif key == QtCore.Qt.Key.Key_Home and self._t.size:
            self.set_time(float(self._t[0]))
            self.timeChanged.emit(float(self._t[0]))
        elif key == QtCore.Qt.Key.Key_End and self._t.size:
            self.set_time(float(self._t[-1]))
            self.timeChanged.emit(float(self._t[-1]))
        elif key in (QtCore.Qt.Key.Key_1, QtCore.Qt.Key.Key_2, QtCore.Qt.Key.Key_3):
            names = [self._order[i] for i in range(len(self._order)) if i % 3 + 1 == int(key - 48)]
            if names:
                self.set_source(names[0])
        elif key == QtCore.Qt.Key.Key_Left:
            self.view.translate(60, 0)
            self.schedule_layer_refresh()
        elif key == QtCore.Qt.Key.Key_Right:
            self.view.translate(-60, 0)
            self.schedule_layer_refresh()
        elif key == QtCore.Qt.Key.Key_Up:
            self.view.translate(0, 60)
            self.schedule_layer_refresh()
        elif key == QtCore.Qt.Key.Key_Down:
            self.view.translate(0, -60)
            self.schedule_layer_refresh()
        else:
            return False
        return True

    def scale_bar_y(self, vp: QtCore.QRect) -> int:
        """比例尺基线 y：仪表可见时抬到面板上方，不钻到它底下。"""
        inset = 0
        try:
            if self.gauges.isVisible():
                inset = self.gauges.sizeHint().height() + 2 * self.GAUGE_MARGIN
        except Exception:                # noqa: BLE001 仪表异常不影响比例尺
            inset = 0
        return vp.bottom() - 18 - inset

    def paint_scale_bar(self, p: QtGui.QPainter, vp: QtCore.QRect) -> None:
        k = self._k()
        if k <= 0:
            return
        mpp = (1.0 / k) if self._mode != MODE_GEODETIC else tm.meters_per_pixel(self._center_lat(), self.zoom())
        step_m, step_px = tm.nice_scale_step(mpp, 90.0)
        if not math.isfinite(step_px) or step_px < 6:
            return
        # 左下角仪表会盖住比例尺，所以比例尺要给仪表腾位
        x0, y0 = 14, self.scale_bar_y(vp)
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        # 先垫一个半暗胶囊：影像屋面很亮时，光画线条会直接消失（真机图上撞过）
        chip = QtCore.QRectF(x0 - 7, y0 - 21, step_px + 122, 33)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(12, 16, 20, 165))
        p.drawRoundedRect(chip, 5, 5)
        # 浅色光晕 + 深色芯：两种底图上都看得清
        p.setPen(QtGui.QPen(QtGui.QColor(238, 243, 246, 150), 5.2))
        p.drawLine(QtCore.QPoint(x0, y0), QtCore.QPoint(x0 + int(step_px), y0))
        for xx in (x0, x0 + int(step_px)):
            p.drawLine(QtCore.QPoint(xx, y0 - 5), QtCore.QPoint(xx, y0 + 5))
        p.setPen(QtGui.QPen(QtGui.QColor("#0d1115"), 3.0))
        p.drawLine(QtCore.QPoint(x0, y0), QtCore.QPoint(x0 + int(step_px), y0))
        for xx in (x0, x0 + int(step_px)):
            p.drawLine(QtCore.QPoint(xx, y0 - 5), QtCore.QPoint(xx, y0 + 5))
        p.setFont(ui_font(8))
        p.setPen(QtGui.QPen(QtGui.QColor("#eef3f6"), 1.0))
        txt = f"{step_m:,.0f} m" if step_m >= 1 else f"{step_m * 1000:.0f} cm"
        p.drawText(QtCore.QRectF(x0 - 4, y0 - 20, step_px + 120, 15),
                   QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, txt)
        p.restore()

    def _maybe_rebuild_tracks(self) -> None:
        """放大到抽稀步长不够细了就安排一次重建（防抖 400 ms）。

        缩小方向永远不需要重建：点只会更密，视觉不变，只是多描几段。
        """
        k = self._k() or 1.0
        if self._built_k and k > self._built_k * 1.6 and self._t.size > 1:
            if not self._rebuild.isActive():
                self._rebuild.start()

    def _rebuild_tracks(self) -> None:
        """按当前缩放重新抽稀并重画主/副轨迹。"""
        if self._mode == MODE_EMPTY:
            return
        self._draw_track()
        self._draw_others()
        self._sync_screen_items()
        self.schedule_layer_refresh()

    def schedule_layer_refresh(self) -> None:
        if not self._refresh.isActive():
            self._refresh.start()

    # ================================================================ 变换辅助
    def _k(self) -> float:
        k = self.view.transform().m11()
        return float(k) if k and math.isfinite(k) else 1.0

    def _clamp_zoom(self, z: float) -> float:
        return float(max(float(self._src.min_zoom), min(float(self._src.max_zoom), z)))

    def _set_zoom(self, z: float, keep_center: bool = True) -> None:
        """把缩放级设为 z（地理模式）。

        坑：QGraphicsView.setTransform() 会重置滚动条（value 归 0），视图中心会
        瞬移到场景左上角。因此必须先记下当前中心场景坐标，变换后再 centerOn 回去。
        """
        z = self._clamp_zoom(z)
        k = tm.scale_at(z)
        c = self.view.mapToScene(self.view.viewport().rect().center()) if keep_center else None
        t = QtGui.QTransform()
        t.scale(k, k)
        self.view.setTransform(t)
        self._sync_screen_items()          # 变换变了，游标/徽标的 1/k 比例必须跟着变
        if c is not None:
            self.view.centerOn(c)

    def _set_local_ppm(self, ppm: float, keep_center: bool = True) -> None:
        c = self.view.mapToScene(self.view.viewport().rect().center()) if keep_center else None
        t = QtGui.QTransform()
        t.scale(ppm, ppm)
        self.view.setTransform(t)
        self._local_ppm = ppm
        self._sync_screen_items()
        if c is not None:
            self.view.centerOn(c)

    def _center_lat(self) -> float:
        c = self.view.mapToScene(self.view.viewport().rect().center())
        return tm.scene_to_lonlat(c.x(), c.y())[0]

    # ================================================================ 数据换算
    @staticmethod
    def _valid_track(tr: Any) -> bool:
        if tr is None:
            return False
        try:
            if int(tr.n_points) == 0:
                return False
        except Exception:
            return False
        kind = str(getattr(tr, "kind", "local"))
        if kind == "geodetic":
            lat = np.asarray(getattr(tr, "lat", np.empty(0)), dtype=float)
            lon = np.asarray(getattr(tr, "lon", np.empty(0)), dtype=float)
            return bool(lat.size and lon.size and np.isfinite(lat).any() and np.isfinite(lon).any())
        e = np.asarray(getattr(tr, "e", np.empty(0)), dtype=float)
        n = np.asarray(getattr(tr, "n", np.empty(0)), dtype=float)
        return bool(e.size and n.size and np.isfinite(e).any() and np.isfinite(n).any())

    def _reset_data(self) -> None:
        self._t = self._sx = self._sy = self._alt = self._spd = self._heading = np.empty(0)
        self._bat_t = self._bat_v = self._bat_a = np.empty(0)
        self._cursor_idx = -1
        self._playhead = float("inf")        # 换日志：旧的播放指针作废
        self._cursor.setVisible(False)
        self._layer_track.clear()
        self._layer_track2.clear()
        self._layer_region.clear()
        self._layer_badge.clear()
        self._layer_event.clear()
        self._layer_grid.clear()
        self._hide_all_tiles()

    def _track_arrays(self):
        """抽稀后的 (t, lat, lon, alt, speed) 等长数组。"""
        tr = self._tr
        n0 = int(tr.n_points)
        if n0 > self.MAX_TRACK_POINTS:
            tr = tr.resample(self.MAX_TRACK_POINTS)
        g = lambda name: np.asarray(getattr(tr, name, np.empty(0)), dtype=float)   # noqa: E731
        t, lat, lon, alt, u, spd = g("t"), g("lat"), g("lon"), g("alt"), g("u"), g("speed")
        if alt.size == 0 or not np.isfinite(alt).any():
            alt = -u if u.size else np.full(t.size, np.nan)
        m = min(t.size, lat.size, lon.size, alt.size, spd.size) if spd.size else min(t.size, lat.size, lon.size, alt.size)
        if m == 0:
            return (np.empty(0),) * 5
        out = [t[:m], lat[:m], lon[:m], alt[:m]]
        out.append(spd[:m] if spd.size >= m else np.full(m, np.nan))
        return tuple(out)

    def _compute_xy(self) -> None:
        """把轨迹换成场景坐标（含按源 datum 加偏）并剔掉无效点。"""
        t, lat, lon, alt, spd = self._track_arrays()
        if self._mode == MODE_GEODETIC:
            xs = np.full(t.size, np.nan)
            ys = np.full(t.size, np.nan)
            good = np.isfinite(lat) & np.isfinite(lon)
            if good.any():
                la, lo = lat[good], lon[good]
                if self._src.datum == "GCJ02":
                    conv = [tm.wgs_to_gcj(a, b) for a, b in zip(la.tolist(), lo.tolist())]
                    la = np.array([c[0] for c in conv])
                    lo = np.array([c[1] for c in conv])
                xs[good], ys[good] = tm.scene_from_lonlat_np(la, lo)
            keep = good & np.isfinite(xs) & np.isfinite(ys)
            if keep.any():
                self._t, self._sx, self._sy = t[keep], xs[keep], ys[keep]
                self._alt, self._spd = alt[keep], spd[keep]
            else:
                self._t = self._sx = self._sy = self._alt = self._spd = np.empty(0)
            world = tm.TILE_SIZE * (1 << tm.REF_Z)
            self._scene.setSceneRect(0, 0, world, world)
        else:
            tr = self._tr
            e = np.asarray(getattr(tr, "e", np.empty(0)), dtype=float)
            n = np.asarray(getattr(tr, "n", np.empty(0)), dtype=float)
            tt = np.asarray(getattr(tr, "t", np.empty(0)), dtype=float)
            aa = np.asarray(getattr(tr, "u", np.empty(0)), dtype=float)
            ss = np.asarray(getattr(tr, "speed", np.empty(0)), dtype=float)
            m = min(tt.size, e.size, n.size)
            if m == 0:
                self._t = self._sx = self._sy = self._alt = self._spd = np.empty(0)
                return
            e, n, tt = e[:m], n[:m], tt[:m]
            aa = -aa[:m] if aa.size >= m else np.full(m, np.nan)
            ss = ss[:m] if ss.size >= m else np.full(m, np.nan)
            keep = np.isfinite(e) & np.isfinite(n)
            self._t, self._sx, self._sy = tt[keep], e[keep], -n[keep]
            self._alt, self._spd = aa[keep], ss[keep]
            x0, x1 = (float(np.nanmin(self._sx)), float(np.nanmax(self._sx))) if self._sx.size else (0.0, 1.0)
            y0, y1 = (float(np.nanmin(self._sy)), float(np.nanmax(self._sy))) if self._sy.size else (0.0, 1.0)
            self._scene.setSceneRect(x0 - 500, y0 - 500, max(1.0, x1 - x0 + 1000), max(1.0, y1 - y0 + 1000))
        # 朝向角（屏幕角度，0=东，顺时针）
        if self._sx.size >= 2:
            dx = np.gradient(self._sx)
            dy = np.gradient(self._sy)
            h = np.degrees(np.arctan2(dy, dx))
            h[~np.isfinite(h)] = 0.0
            self._heading = h
        else:
            self._heading = np.zeros(self._sx.size)

    def _scene_of_lonlat(self, lat: float, lon: float) -> tuple[float, float]:
        if not (math.isfinite(lat) and math.isfinite(lon)):
            return float("nan"), float("nan")
        if self._src.datum == "GCJ02":
            lat, lon = tm.wgs_to_gcj(lat, lon)
        world = tm.TILE_SIZE * (1 << tm.REF_Z)
        return (tm.clamp_lon(lon) + 180.0) / 360.0 * world, tm.lat_toMercatorY(lat) * world

    def _index_at(self, t: float) -> int:
        if self._t.size == 0:
            return 0
        i = int(np.searchsorted(self._t, t))
        if i <= 0:
            return 0
        if i >= self._t.size:
            return int(self._t.size - 1)
        return i if abs(self._t[i] - t) <= abs(self._t[i - 1] - t) else i - 1

    # ================================================================ 绘制
    def _draw_track(self) -> None:
        self._layer_track.clear()
        self._layer_badge.clear()
        n_all = self._sx.size
        if n_all < 2:
            return
        # 跟随模式：只画到播放指针（时间线拖到哪，轨迹就长到哪）
        n = n_all
        if self._follow_time and math.isfinite(self._playhead) and self._t.size == n_all:
            n = int(np.searchsorted(self._t, self._playhead, side="right"))
            n = max(0, min(n_all, n))
        if n < 2:
            return
        sx, sy = self._sx[:n], self._sy[:n]
        alt = self._alt[:n] if self._alt.size == n_all else self._alt
        spd = self._spd[:n] if self._spd.size == n_all else self._spd
        use_alt = bool(np.isfinite(alt).any())
        val = alt if use_alt else (spd if np.isfinite(spd).any() else None)
        k = self._k() or 1.0
        groups: list[tuple[QtGui.QColor, np.ndarray]] = []
        if val is None:
            groups.append((QtGui.QColor(TRACK_FALLBACK_COLOR), np.ones(n, dtype=bool)))
        else:
            v = np.where(np.isfinite(val), val, np.nan)
            lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
            span = (hi - lo) or 1.0
            # 档数随点数收缩：跟随播放刚开始只有几十个点，24 档会让每档只剩 1 点，
            # 而单点连不成线（_runs 会丢掉），结果就是"轨迹该有却没有"。
            nb = 24 if n >= 240 else max(2, min(24, n // 4))
            bkt = np.full(n, -1, dtype=int)
            fin = np.isfinite(v)
            bkt[fin] = np.clip(((v[fin] - lo) / span * nb).astype(int), 0, nb - 1)
            for b in range(nb):
                m = bkt == b
                if m.any():
                    groups.append((ramp_color(b / max(1, nb - 1)), m))
            # 单点桶并入最近邻，避免碎片
            if not groups:
                groups.append((QtGui.QColor(TRACK_FALLBACK_COLOR), np.ones(n, dtype=bool)))
        for color, mask in groups:
            for seg in self._runs(mask):
                self._emit(self._layer_track, sx[seg], sy[seg], color, self.TRACK_PX, 10, k)
        if not self._layer_track.items and n >= 2:
            # 兜底：分档/抽稀再极端也不许"该有轨迹却空着"
            self._emit(self._layer_track, sx, sy, QtGui.QColor(TRACK_FALLBACK_COLOR),
                       self.TRACK_PX, 10, k)
        # 端点标记：跟随模式下"终点"就是游标所在地，不再多画一个会跟游标抢位置的标签
        marks = [(0, "起点", QtGui.QColor("#2ecc71"))]
        if not self._follow_time:
            marks.append((n - 1, "终点", QtGui.QColor("#e74c3c")))
        for (idx, text, col) in marks:
            b = _Badge(text, col)
            b._sync_scale(k)                 # 新建即对齐：否则缩放下首帧脏区偏小
            b.setPos(float(sx[idx]), float(sy[idx]))
            self._scene.addItem(b)
            self._layer_badge.add(b)
        # 多轨迹时在曲线上标出名称（单轨迹有标题栏，不用再标，免得挡住地图）
        if len(self._entries) > 1 and not self._primary_hidden():
            label = self._primary_label()
            if label:
                nb = _Badge(label, QtGui.QColor(self._primary_color()))
                nb._sync_scale(k)
                nb.setPos(float(sx[n - 1]), float(sy[n - 1]))
                self._scene.addItem(nb)
                self._layer_badge.add(nb)
        # 取消勾选主轨迹后，任何一次重画都必须保持"不画"：图例不许和画面对不上
        if not self._primary_on:
            self._set_layer_visible(self._layer_track, False)
            self._set_layer_visible(self._layer_badge, False)
        home = getattr(self._tr, "home", None)
        if home and self._mode == MODE_GEODETIC:
            hx, hy = self._scene_of_lonlat(float(home[0]), float(home[1]))
            if math.isfinite(hx):
                b = _Badge("HOME", QtGui.QColor("#f1c40f"))
                b._sync_scale(k)
                b.setPos(hx, hy)
                self._scene.addItem(b)
                self._layer_badge.add(b)

    def _primary_label(self) -> str:
        for e in self._entries:
            if e.primary:
                return str(e.label or "")
        return ""

    def _primary_color(self) -> str:
        for e in self._entries:
            if e.primary:
                return str(e.color or TRACK_FALLBACK_COLOR)
        return TRACK_FALLBACK_COLOR

    def _primary_hidden(self) -> bool:
        """主轨迹当前是否被取消勾选（唯一真源是 _on_legend 记下的 _primary_on）。"""
        return not bool(self._primary_on)

    # ------------------------------------------------------------ 多轨迹 ----    # ------------------------------------------------------------ 多轨迹 ----
    #: 跟随播放时重画的最小间隔（毫秒）：20fps 的播放不会 20 次都重画整条轨迹
    FOLLOW_REDRAW_MS = 110

    SEC_BREAK_PX = 2000.0        # 副轨迹跳点断开阈值（REF_Z 场景像素，约 460 m）

    def _xy_for(self, tr, until: Optional[float] = None
                ) -> tuple[np.ndarray, np.ndarray]:
        """把任意一条轨迹换成场景坐标。

        geodetic → Web Mercator 场景坐标（含按源 datum 加偏）；
        local    → 与主轨迹同一套 ENU 映射（``x=东米``、``y=-北米``）。

        纯视觉日志（无 GPS、无 ORGN）只有 local 轨迹，以前这里直接返回空，
        导致“勾选有反应、图上没东西”——多条本地轨迹本来就该能叠在一起对比。

        ``until`` 不为 None 时只保留 t ≤ until 的点（轨迹播放：画到游标处）。
        """
        tt = np.asarray(getattr(tr, "t", np.empty(0)), dtype=float)
        if str(getattr(tr, "kind", "local")) != "geodetic":
            e = np.asarray(getattr(tr, "e", np.empty(0)), dtype=float)
            n = np.asarray(getattr(tr, "n", np.empty(0)), dtype=float)
            m = min(e.size, n.size)
            if m == 0:
                return np.empty(0), np.empty(0)
            e, n = e[:m], n[:m]
            keep = np.isfinite(e) & np.isfinite(n)
            if until is not None and tt.size == m:
                keep &= tt <= float(until)
            if not keep.any():
                return np.empty(0), np.empty(0)
            return e[keep], -n[keep]
        lat = np.asarray(getattr(tr, "lat", np.empty(0)), dtype=float)
        lon = np.asarray(getattr(tr, "lon", np.empty(0)), dtype=float)
        if lat.size == 0 or lon.size == 0:
            m = min(lat.size, lon.size)
            lat, lon = lat[:m], lon[:m]
        good = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90.0)
        if until is not None and tt.size == good.size:
            good &= tt <= float(until)
        if not good.any():
            return np.empty(0), np.empty(0)
        la, lo = lat[good], lon[good]
        if self._src.datum == "GCJ02":
            conv = [tm.wgs_to_gcj(a, b) for a, b in zip(la.tolist(), lo.tolist())]
            la = np.array([c[0] for c in conv])
            lo = np.array([c[1] for c in conv])
        x, y = tm.scene_from_lonlat_np(la, lo)
        ok = np.isfinite(x) & np.isfinite(y)
        return x[ok], y[ok]

    def _draw_others(self) -> None:
        """画副轨迹（主轨迹以外的、当前勾选可见的）。地理/本地两种模式都支持。"""
        self._layer_track2.clear()
        if self._mode == MODE_EMPTY:
            return
        k = self._k() or 1.0
        vis = set(self.legend.visible()) if self._entries else set()
        for e in self._entries:
            e.drawn = False
            if e.primary or e.key not in vis:
                continue
            if not self._drawable(e):
                continue                        # 坐标系不同，强叠会把米当度
            if self._follow_time and math.isfinite(self._playhead):
                xs, ys = self._xy_for(e.tr, until=self._playhead)   # 跟随：每帧现算，不用缓存
            else:
                if e.xy[0].size == 0:
                    e.xy = self._xy_for(e.tr)
                xs, ys = e.xy
            if xs.size < 2:
                continue
            d = np.hypot(np.diff(xs), np.diff(ys))
            cuts = np.nonzero(d > self.SEC_BREAK_PX)[0] + 1
            for seg in np.split(np.arange(xs.size), cuts):
                if seg.size < 2:
                    continue
                self._emit(self._layer_track2, xs[seg], ys[seg],
                           QtGui.QColor(e.color), self.TRACK_PX, 9, k)
            # 名称标签贴在当前画到的末端：多条轨迹同屏时一眼看出哪条是哪条
            label = str(e.label or e.key or "")
            if label:
                b = _Badge(label, QtGui.QColor(e.color))
                b._sync_scale(k)
                b.setPos(float(xs[-1]), float(ys[-1]))
                self._scene.addItem(b)
                self._layer_track2.add(b)
            e.drawn = True

    def _drawable(self, e: "_TrackEntry") -> bool:
        """副轨迹能不能画在当前模式上：地理与本地两套坐标系不能互叠。"""
        kind = str(getattr(e.tr, "kind", "local"))
        if self._mode == MODE_GEODETIC:
            return kind == "geodetic"
        if self._mode == MODE_LOCAL:
            return kind != "geodetic"
        return False

    #: 差异面板最多列几对（再多就只列前几对，总体仍按全部算）
    DIFF_MAX_ROWS = 4

    @staticmethod
    def _pair_tip(d: PairDiff) -> str:
        """一对轨迹的完整口径说明（鼠标悬停看明细）。"""
        head = f"{d.a_label} ↔ {d.b_label}"
        if not d.ok:
            return head + "\n" + (d.reason or "无法比较")
        lines = [
            head,
            f"均值 {fmt_m(d.mean)}｜中位 {fmt_m(d.median)}｜P95 {fmt_m(d.p95)}｜最大 {fmt_m(d.max)}",
            f"重叠 {d.overlap_s:.0f} s｜对齐样本 {d.n}（丢弃 {d.dropped}）",
            f"里程：{d.a_key} {fmt_m(d.length_a)}｜{d.b_key} {fmt_m(d.length_b)}"
            f"｜归一化基准 {fmt_m(d.length)}",
            f"占里程：均值 {fmt_pct(d.pct)}｜P95 {fmt_pct(d.pct_p95)}｜最大 {fmt_pct(d.pct_max)}",
        ]
        if not math.isfinite(d.pct):
            lines.append(f"重叠段里程不足 {MIN_LENGTH_M:.0f} m，百分比不具参考性")
        return "\n".join(lines)

    def _update_diff(self) -> None:
        """算并显示「单航线差异 / 总体差异」——跟着勾选栏走。"""
        if not hasattr(self, "diff"):
            return
        if self._mode == MODE_EMPTY or len(self._entries) < 2:
            self.diff.hide()
            self._reposition_overlays()
            return
        vis = set(self.visible_tracks())
        chosen = [e for e in self._entries if e.key in vis]
        if len(chosen) < 2:
            self.diff.set_hint(
                "勾选 2 条及以上轨迹，显示差异预估",
                note="差异是两条轨迹之间的量；只勾一条时没有第二组位置可比。")
            self.diff.show()
            self._reposition_overlays()
            return
        diffs: list[PairDiff] = []
        for a, b in itertools.combinations(chosen, 2):
            key = (a.key, b.key)
            got = self._diff_cache.get(key)
            if got is None:
                try:
                    got = compare(a.tr, b.tr)
                except Exception as exc:                # 估算不能把地图拖垮
                    log.debug("轨迹差异计算失败 %s/%s: %s", a.key, b.key, exc)
                    got = PairDiff(a_key=a.key, b_key=b.key, a_label=a.label,
                                   b_label=b.label, reason=f"计算失败：{exc}")
                self._diff_cache[key] = got
            diffs.append(got)

        rows = []
        for d in diffs[:self.DIFF_MAX_ROWS]:
            rows.append((f"{d.a_key} ↔ {d.b_key}",
                         fmt_pct(d.pct) if d.ok else "—", self._pair_tip(d)))
        total = overall(diffs)
        n_ok = sum(1 for d in diffs if d.ok)
        total_row = (f"总体（{n_ok} 对）" if len(diffs) > 1 else "总体",
                     fmt_pct(total.pct),
                     "全部可比轨迹对按里程加权：Σ均值偏差 ÷ Σ平均里程\n"
                     f"Σ均值偏差 {fmt_m(total.mean_sum)}｜Σ平均里程 {fmt_m(total.length_sum)}\n"
                     f"参与 {n_ok}/{len(diffs)} 对（不可比的已跳过）")
        bits = [f"已选 {len(chosen)} 条 → {len(diffs)} 对；基准=各对重叠段平均里程。"]
        if len(diffs) > self.DIFF_MAX_ROWS:
            bits.append(f"只列前 {self.DIFF_MAX_ROWS} 对，总体含全部。")
        if any(d.ok and d.frame == "local" for d in diffs):
            bits.append("本地 ENU（无经纬度）。")
        if n_ok < len(diffs):
            bits.append(f"{len(diffs) - n_ok} 对坐标系不同，未计入总体。")
        self.diff.set_pairs(rows, total_row, note=" ".join(bits))
        self.diff.show()
        self._reposition_overlays()

    def _update_legend(self) -> None:
        specs = [(e.key, e.label, e.color, e.n, e.primary, self._drawable(e))
                 for e in self._entries]
        self.legend.set_tracks(specs)
        many = len(self._entries) > 1
        # 本地模式（纯视觉日志）同样要给勾选栏：多条 ENU 轨迹叠在一起对比才有意义
        self.legend.setVisible(bool(many) and self._mode != MODE_EMPTY)
        self._update_diff()
        if many:
            self.legend.setToolTip(
                "勾选要显示的轨迹。主轨迹（光标的跟从对象）用高度渐变色，\n"
                "其余用纯色对比。POS 与 GPS 在有干扰时会差几十米，\n"
                "同时勾上就能看出哪个是飞控算出来的、哪个是接收机原始值。\n"
                "纯视觉日志没有经纬度，多条轨迹会在本地 ENU 坐标系里叠画。")
        self._reposition_overlays()

    def _expand_local_rect(self) -> None:
        """本地模式下把场景矩形撑到可见轨迹的并集。

        QGraphicsView 默认 clipToSceneRect，副轨迹伸出主轨迹算出的矩形会被剪掉。
        """
        if self._mode != MODE_LOCAL or self._sx.size == 0:
            return
        xs = [self._sx]
        ys = [self._sy]
        for e in self._entries:
            if e.xy[0].size and self._drawable(e):
                xs.append(e.xy[0])
                ys.append(e.xy[1])
        x0 = float(np.nanmin(np.concatenate(xs))) - 500.0
        x1 = float(np.nanmax(np.concatenate(xs))) + 500.0
        y0 = float(np.nanmin(np.concatenate(ys))) - 500.0
        y1 = float(np.nanmax(np.concatenate(ys))) + 500.0
        self._scene.setSceneRect(x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))

    def _on_legend(self, keys) -> None:
        """勾选变化：重画副轨迹 + 主轨迹整层开关 + 光标跟随隐藏。"""
        want = {str(k) for k in (keys or [])}
        prim_on = self._primary_key in want if self._primary_key else True
        self._primary_on = bool(prim_on)
        self._set_layer_visible(self._layer_track, prim_on)
        self._set_layer_visible(self._layer_badge, prim_on)
        try:
            self._cursor.setVisible(prim_on and self._cursor_idx >= 0)
        except Exception:                                    # pragma: no cover
            pass
        self._draw_others()
        self._expand_local_rect()
        self._update_diff()                          # 勾选变了，差异预估值同步刷新
        self.trackVisibilityChanged.emit(sorted(want))

    def _set_layer_visible(self, layer: "_Layer", on: bool) -> None:
        for it in layer.items:
            try:
                it.setVisible(bool(on))
            except RuntimeError:                             # pragma: no cover
                pass

    # ------------------------------------------------------------ 折线绘制 ----
    #: 折线抽稀步长（屏幕像素）。两倍子像素以下的点画上去也看不见，
    #: 但每段都要真实描边：一条 6 万点的轨迹能让一帧从 1.5 ms 涨到 500 ms。
    THIN_PX = 1.6
    #: 单条 QPainterPath 最多多少段。切小了 Qt 才能按包围盒把视口外的段整块裁掉；
    #: 一刀不切的单【大）路径会每帧全量描边，拖地图就卡。
    CHUNK_SEGMENTS = 400

    @staticmethod
    def _thin_mask(xs: np.ndarray, ys: np.ndarray, step: float) -> np.ndarray:
        """按屏幕距离抽稀：保留与「上一个保留点」相距 >= step 的点。

        必须贪心保序（不能用网格去重）：网格去重会让相邻保留点靠得很近，
        抽不掉多少点，还容易在拐弯处留下尖角。
        """
        n = int(xs.size)
        keep = np.zeros(n, dtype=bool)
        if n == 0:
            return keep
        keep[0] = True
        lx, ly = float(xs[0]), float(ys[0])
        step2 = float(step) * float(step)
        for i in range(1, n):
            dx = float(xs[i]) - lx
            dy = float(ys[i]) - ly
            if dx * dx + dy * dy >= step2:
                keep[i] = True
                lx, ly = float(xs[i]), float(ys[i])
        keep[n - 1] = True
        return keep

    def _runs(self, mask: np.ndarray) -> list[np.ndarray]:
        """mask → 连续索引段（不连续就断开，免得假连线）。"""
        idx = np.nonzero(mask)[0]
        if idx.size == 0:
            return []
        parts = np.split(idx, np.nonzero(np.diff(idx) != 1)[0] + 1)
        return [p for p in parts if p.size >= 2]

    def _emit(self, layer, xs: np.ndarray, ys: np.ndarray, color: QtGui.QColor,
              base_px: float, z: float, k: float) -> None:
        """一条连续折线 → 抽稀 → 切块 → 若干小 path 图元。"""
        if xs.size < 2:
            return
        kk = max(float(k), 1e-9)
        keep = self._thin_mask(xs, ys, self.THIN_PX / kk)
        xk, yk = xs[keep], ys[keep]
        if xk.size < 2:
            return
        pen = QtGui.QPen(color, float(base_px) / kk)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        n_seg = xk.size - 1
        for a in range(0, n_seg, self.CHUNK_SEGMENTS):
            b = min(a + self.CHUNK_SEGMENTS, n_seg)
            path = QtGui.QPainterPath()
            path.moveTo(float(xk[a]), float(yk[a]))
            for x, y in zip(xk[a + 1:b + 1].tolist(), yk[a + 1:b + 1].tolist()):
                path.lineTo(x, y)
            it = self._scene.addPath(path)
            it.setPen(pen)
            it.setZValue(z)
            it._base_px = float(base_px)          # type: ignore[attr-defined]
            layer.add(it)
        self._built_k = kk

    def _path_for(self, mask: np.ndarray) -> QtGui.QPainterPath:
        """把满足 mask 的点按连续段连成线（跨段断开，避免假连线）。"""
        path = QtGui.QPainterPath()
        idx = np.nonzero(mask)[0]
        if idx.size == 0:
            return path
        for seg in np.split(idx, np.nonzero(np.diff(idx) != 1)[0] + 1):
            if seg.size < 2:
                continue
            xs, ys = self._sx[seg], self._sy[seg]
            path.moveTo(float(xs[0]), float(ys[0]))
            for x, y in zip(xs[1:].tolist(), ys[1:].tolist()):
                path.lineTo(x, y)
        return path

    def _rebuild_regions(self) -> None:
        self._layer_region.clear()
        if not self._regions or self._t.size < 2:
            return
        k = self._k() or 1.0
        for rg in self._regions:
            try:
                a = float(rg.get("t0", float("nan")))
                b = float(rg.get("t1", a))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(a) and math.isfinite(b)):
                continue
            lo, hi = min(a, b), max(a, b)
            m = (self._t >= lo) & (self._t <= hi)
            if int(m.sum()) < 2:
                continue
            path = self._path_for(m)
            if path.isEmpty():
                continue
            col = sev_color(rg.get("severity", "warn"))
            col.setAlpha(150)
            it = self._scene.addPath(path)
            pen = QtGui.QPen(col, (self.TRACK_PX + 6.0) / k)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            it.setPen(pen)
            it.setZValue(20)
            it._base_px = self.TRACK_PX + 6.0     # type: ignore[attr-defined]
            self._layer_region.add(it)
            title = str(rg.get("title") or "").strip()
            if title:
                i = int(np.nonzero(m)[0][len(np.nonzero(m)[0]) // 2])
                b = _Badge(title[:24], col)
                b._sync_scale(k)
                b.setPos(float(self._sx[i]), float(self._sy[i]))
                self._scene.addItem(b)
                self._layer_badge.add(b)

    def _rebuild_markers(self) -> None:
        self._layer_event.clear()
        if not self._events or self._t.size == 0:
            return
        k = self._k() or 1.0
        for ev in self._events:
            pos = self._pos_of_event(ev)
            if pos is None:
                continue
            sev = str(ev.get("severity", "info"))
            tip = f"t={_fmt(float(ev.get('t', float('nan'))), 's')} {ev.get('display') or ev.get('text') or ev.get('kind', '')}"
            d = _Dot(sev_color(sev), 4.5 if sev in ("warn", "error", "critical") else 3.5, tip)
            d._sync_scale(k)
            d.setPos(pos[0], pos[1])
            d._ev_t = float(ev.get("t", float("nan")))          # type: ignore[attr-defined]
            self._scene.addItem(d)
            self._layer_event.add(d)

    def _pos_of_event(self, ev: dict) -> Optional[tuple[float, float]]:
        """事件落点：优先经纬度，缺失则按时间插值到轨迹点。"""
        lat, lon = ev.get("lat"), ev.get("lon")
        if lat is not None and lon is not None and self._mode == MODE_GEODETIC:
            try:
                x, y = self._scene_of_lonlat(float(lat), float(lon))
                return None if not math.isfinite(x) else (x, y)
            except (TypeError, ValueError):
                return None
        t = ev.get("t")
        if t is not None:
            try:
                i = self._index_at(float(t))
                return float(self._sx[i]), float(self._sy[i])
            except (TypeError, ValueError, IndexError):
                return None
        return None

    def _blink(self, idx: int) -> None:
        for it in self._layer_event.items:
            try:
                ev_t = getattr(it, "_ev_t", float("nan"))
                on = idx >= 0 and self._t.size and abs(ev_t - float(self._t[idx])) < 1.0
                it.set_active(bool(on))
            except Exception:
                pass

    # ================================================================ 瓦片/网格
    def _refresh_layers(self) -> None:
        if self._mode == MODE_GEODETIC:
            self._layer_grid.clear()
            self._refresh_tiles()
        elif self._mode == MODE_LOCAL:
            self._hide_all_tiles()
            self._refresh_local_grid()
        else:
            self._hide_all_tiles()
            self._layer_grid.clear()
        self._rescale_pens()
        self._maybe_rebuild_tracks()
        self._reposition_overlays()
        self._update_status()

    def _hide_all_tiles(self) -> None:
        for key, it in list(self._tile_items.items()):
            try:
                self._scene.removeItem(it)
            except Exception:
                pass
        self._tile_items.clear()
        self._placeholder_keys.clear()

    def _tile_size(self, z: int) -> float:
        return tm.TILE_SIZE * (2.0 ** (tm.REF_Z - z))

    def _refresh_tiles(self) -> None:
        if self._mode != MODE_GEODETIC:
            return
        z = int(max(self._src.min_zoom, min(self._src.max_zoom, math.floor(self.zoom() + 1e-9))))
        size = self._tile_size(z)
        vis = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        n = tm.tile_count(z)
        tx0 = max(0, int(math.floor(vis.left() / size)) - self.MARGIN_TILES)
        tx1 = min(n - 1, int(math.floor(vis.right() / size)) + self.MARGIN_TILES)
        ty0 = max(0, int(math.floor(vis.top() / size)) - self.MARGIN_TILES)
        ty1 = min(n - 1, int(math.floor(vis.bottom() / size)) + self.MARGIN_TILES)
        want = {(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)}
        for key in [k for k in self._tile_items if k[0] != z or (k[1], k[2]) not in want]:
            try:
                self._scene.removeItem(self._tile_items.pop(key))
            except Exception:
                self._tile_items.pop(key, None)
            self._placeholder_keys.discard(key)
        for (x, y) in sorted(want):
            key = (z, x, y)
            if key in self._tile_items:
                continue
            pm, _ = self._tiles.cached((self._src.name, z, x, y))
            if pm is not None:
                it = self._scene.addPixmap(pm)
                it.setTransformationMode(QtCore.Qt.TransformationMode.SmoothTransformation)
                it.setScale(size / tm.TILE_SIZE)
                it.setPos(x * size, y * size)
                it.setZValue(-1000)
                self._tile_items[key] = it
            else:
                it = self._make_placeholder(x * size, y * size, size)
                self._tile_items[key] = it
                self._placeholder_keys.add(key)
            self._tiles.request(self._src, z, x, y)

    def _make_placeholder(self, x: float, y: float, size: float) -> QtWidgets.QGraphicsItem:
        """缺瓦片降级：灰底 + 细网格 + 中心标注，绝不白屏。"""
        pen = QtGui.QPen(QtGui.QColor("#404b54"), 1.0 / max(self._k(), 1e-9))
        pen.setStyle(QtCore.Qt.PenStyle.DotLine)
        it = self._scene.addRect(x, y, size, size, pen, QtGui.QBrush(QtGui.QColor("#2b333a")))
        it.setZValue(-1000)
        it._base_px = 1.0                          # type: ignore[attr-defined]
        return it

    def _on_tile_ready(self, name: str, z: int, x: int, y: int, pm: QtGui.QPixmap) -> None:
        if name != self._src.name or self._mode != MODE_GEODETIC:
            return
        key = (z, x, y)
        size = self._tile_size(z)
        if pm.isNull():
            return                                   # 占位图已就位
        item = self._tile_items.get(key)
        if isinstance(item, QtWidgets.QGraphicsPixmapItem):
            item.setPixmap(pm)
            item.setScale(size / tm.TILE_SIZE)
            return
        if item is not None:
            try:
                self._scene.removeItem(item)
            except Exception:
                pass
        it = self._scene.addPixmap(pm)
        it.setTransformationMode(QtCore.Qt.TransformationMode.SmoothTransformation)
        it.setScale(size / tm.TILE_SIZE)
        it.setPos(x * size, y * size)
        it.setZValue(-1000)
        self._tile_items[key] = it
        # 状态栏的瓦片计数要跟着到达刷新（不能用 schedule_layer_refresh，
        # 那会形成 refresh→tile_ready→refresh 的 60ms 永动循环）。
        self._update_status()
        self._placeholder_keys.discard(key)

    def _refresh_local_grid(self) -> None:
        self._layer_grid.clear()
        k = self._k() or 1.0
        step, _ = tm.nice_scale_step(1.0 / k, 110.0)
        if not math.isfinite(step) or step <= 0:
            return
        vis = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        pen = QtGui.QPen(QtGui.QColor("#39434c"), 1.0 / k)
        gx0 = int(math.floor(vis.left() / step)) * step
        gx1 = int(math.ceil(vis.right() / step)) * step
        gy0 = int(math.floor(vis.top() / step)) * step
        gy1 = int(math.ceil(vis.bottom() / step)) * step
        if (gx1 - gx0) / step > 400 or (gy1 - gy0) / step > 400:
            return                                   # 过稀时不画网格
        gx = gx0
        while gx <= gx1:
            self._layer_grid.add(self._scene.addLine(gx, vis.top(), gx, vis.bottom(), pen))
            gx += step
        gy = gy0
        while gy <= gy1:
            self._layer_grid.add(self._scene.addLine(vis.left(), gy, vis.right(), gy, pen))
            gy += step
        pen2 = QtGui.QPen(QtGui.QColor("#7f8c8d"), 1.6 / k)
        self._layer_grid.add(self._scene.addLine(-step * 3, 0, step * 3, 0, pen2))
        self._layer_grid.add(self._scene.addLine(0, -step * 3, 0, step * 3, pen2))

    def _rescale_pens(self) -> None:
        """缩放后把线宽（以屏幕像素定义）换算回当前场景尺度；同时同步屏幕恒定尺寸的图元。"""
        k = self._k() or 1.0
        for it in (*self._layer_track.items, *self._layer_track2.items,
                   *self._layer_plugin.items,
                   *self._layer_region.items, *self._layer_grid.items):
            base = getattr(it, "_base_px", None)
            if base:
                want = float(base) / k
                pen = it.pen()
                if abs(pen.widthF() - want) > 1e-9:      # 没变就别擦脏重绘
                    pen.setWidthF(want)
                    it.setPen(pen)
        # 游标/徽标/圆点：否则它们的脏区与实际绘制尺寸对不上，移动时留拖影
        self._sync_screen_items()

    def _sync_screen_items(self) -> None:
        """把当前 k 同步给所有“屏幕尺寸恒定”的图元。

        任何改变视图变换的地方（``_set_zoom`` / ``_set_local_ppm`` / ``zoom_by``）
        都要调它一次，否则图元保留旧比例，脏区与实际绘制差一个 k 倍→ 拖影。
        """
        k = self._k() or 1.0
        self._cursor._sync_scale(k)
        for it in (*self._layer_badge.items, *self._layer_event.items):
            it._sync_scale(k)

    # ================================================================ 覆盖层/状态
    def _reposition_overlays(self) -> None:
        vp = self.view.viewport().size()
        self._attr.adjustSize()
        self._attr.move(max(0, vp.width() - self._attr.width() - 8),
                        max(0, vp.height() - self._attr.height() - 30))
        self._placeholder.adjustSize()
        self._placeholder.move(max(0, (vp.width() - self._placeholder.width()) // 2),
                               max(0, (vp.height() - self._placeholder.height()) // 3))
        self.gauges.adjustSize()
        gw, gh = self.gauges.sizeHint().width(), self.gauges.sizeHint().height()
        self.gauges.move(self.GAUGE_MARGIN, max(0, vp.height() - gh - self.GAUGE_MARGIN))
        self.gauges.resize(gw, gh)
        self.gauges.raise_()
        if hasattr(self, "legend"):
            self.legend.adjustSize()
            lw, lh = self.legend.sizeHint().width(), self.legend.sizeHint().height()
            lh = min(lh, max(60, vp.height() - 2 * self.LEGEND_MARGIN))
            self.legend.move(max(0, vp.width() - lw - self.LEGEND_MARGIN), self.LEGEND_MARGIN)
            self.legend.resize(lw, lh)
            self.legend.raise_()
        if hasattr(self, "diff") and self.diff.isVisible():
            # 紧贴在勾选栏下方，右边对齐；窗口矮的话往上提，不能顶出可视区
            self.diff.adjustSize()
            dw = max(self.diff.sizeHint().width(),
                     self.legend.width() if self.legend.isVisible() else 0)
            dh = self.diff.sizeHint().height()
            top = (self.legend.y() + self.legend.height() + 6
                   if self.legend.isVisible() else self.LEGEND_MARGIN)
            dh = min(dh, max(40, vp.height() - top - 8))
            if top + dh > vp.height() - 8:
                top = max(self.LEGEND_MARGIN, vp.height() - dh - 8)
            dw = min(dw, max(160, vp.width() - 2 * self.LEGEND_MARGIN))
            self.diff.move(max(0, vp.width() - dw - self.LEGEND_MARGIN), top)
            self.diff.resize(dw, dh)
            self.diff.raise_()

    def _show_placeholder(self, text: str) -> None:
        if text:
            self._placeholder.setText(text)
            self._placeholder.show()
            self._placeholder.raise_()
        else:
            self._placeholder.hide()
        self._reposition_overlays()

    def _update_status(self, idx: Optional[int] = None, widget_pos: Optional[QtCore.QPoint] = None) -> None:
        parts: list[str] = [f"<b>{self._src.name}</b> ({self._src.datum})"]
        if self._mode == MODE_GEODETIC:
            pos = widget_pos or self.view.viewport().rect().center()
            sc = self.view.mapToScene(pos)
            la, lo = tm.scene_to_lonlat(sc.x(), sc.y())
            la, lo = tm.convert_pair(la, lo, self._src.datum, to_wgs=True)
            mpp = tm.meters_per_pixel(self._center_lat(), self.zoom())
            parts.append(f"z={self.zoom():.1f} · {mpp:.2f} m/px")
            parts.append(f"光标 {la:.6f}, {lo:.6f}")
        elif self._mode == MODE_LOCAL:
            pos = widget_pos or self.view.viewport().rect().center()
            sc = self.view.mapToScene(pos)
            k = self._k() or 1.0
            parts.append(f"本地模式 · {1.0 / k:.2f} m/px")
            parts.append(f"光标 E={sc.x():.1f} N={-sc.y():.1f}")
        else:
            parts.append("无航迹数据")
        i = idx if idx is not None else self._cursor_idx
        if i is not None and i >= 0 and self._t.size:
            alt = float(self._alt[i]) if i < self._alt.size else float("nan")
            spd = float(self._spd[i]) if i < self._spd.size else float("nan")
            parts.append(f"游标 <b>t={self._t[i]:.1f}s</b> 高度 {_fmt(alt, ' m')} 速度 {_fmt(spd, ' m/s')}")
        st = self._tiles.stats
        parts.append(f"瓦片 内存{st['mem_hit']} 磁盘{st['disk_hit']} 下载{st['fetched']} 失败{st['failed']}")
        if self._tiles.suspect(self._src.name):
            parts.append("<span style='color:#ffd166'>⚠ 该源不同瓦片返回相同内容（疑限流/占位图），建议换源</span>")
        elif st["failed"]:
            parts.append(f"<span style='color:#ffd166'>⚠ {st['failed']} 张失败，已用占位显示</span>")
        self.status.setText(" ｜ ".join(parts))


__all__ = ["TrackMapWidget", "SEV_COLORS", "ramp_color", "sev_color", "ui_font",
           "ui_font_family", "MODE_GEODETIC", "MODE_LOCAL", "MODE_EMPTY"]
