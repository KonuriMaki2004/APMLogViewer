"""apmlog.app —— APMLogViewer 主窗（M2/M3 集成版）。

布局：
    工具栏
    ├── 左：日志列表（同目录自动扫描） + 通道树（双击加进当前视图）
    ├── 中：轨迹地图 / 曲线（6 个预设） / 告警 / 事件 / 参数
    └── 下：事件时间线（模式色带 + 事件刻度 + 告警区间 + 播放指针）

时间联动：曲线游标 ↔ 地图指针 ↔ 时间线 ↔ 状态栏，四处双向同步；
曲线里 Ctrl+拖拽框选区间后，所有视图都收敛到该区间（"复位视图"回到全程）。

自检：``--selftest`` 在 QT_QPA_PLATFORM=offscreen 下加载真日志并出整窗 PNG，
不需要显示器也能验收。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from apmlog import __version__
from apmlog.core import reader as _rd
from apmlog import paramexport as pe
from apmlog.core.model import AlertHit, ChannelKey, EventRec, human_size, severity_rank
from apmlog.core.knowledge import EventHelp, load_help
from apmlog.core.store import LogStore
from apmlog.ui._font import ui_font
from apmlog.ui.linkpanel import LinkPanel
from apmlog.ui.presets import PRESETS, PRESET_BY_ID, build_lanes
from apmlog.ui.loading import LoadingOverlay
from apmlog.ui.scandialog import ScanController
from apmlog.ui.seriesplot import OVERLAY_COLORS, SEVERITY_CN, LaneSpec, SeriesPlot
from apmlog.ui.settings import (Settings, SettingsDialog, apply_tip_font, hide_tip,
                                install_tip_style, settings as get_settings, show_tip)
from apmlog.ui import modecolors as _modecolors
from apmlog.ui import theme as ui_theme
from apmlog.ui.overlaydialog import OverlayDialog
from apmlog.ui.eventhelp import EventHelpDialog
from apmlog.ui.trackmap import TrackMapWidget
from apmlog.plugins.api import PlotSelection
from apmlog.plugins.manager import FAILED, PluginManager
from apmlog.ui.plugindialog import PluginDialog

log = logging.getLogger(__name__)

SEV_ZH = {"info": "信息", "hint": "提示", "warn": "警告", "error": "错误", "critical": "严重"}
SEV_RGB = {"info": "#4b7bec", "hint": "#7f8fa6", "warn": "#f0a500", "error": "#e05c3a",
           "critical": "#c0392b"}


def _sev_color(sev: str) -> QtGui.QColor:
    return QtGui.QColor(SEV_RGB.get(sev, SEV_RGB["info"]))


def _event_lines(items: Sequence[dict[str, Any]]) -> list[str]:
    """把一簇事件拼成悬停提示的行（时间升序，超过 12 条只截前几条）。"""
    out: list[str] = []
    for i, e in enumerate(items[:12], 1):
        sev = str(e.get("severity", "info"))
        out.append(f"{i}. t={float(e.get('t', 0.0) or 0.0):.3f}s  "
                   f"{SEVERITY_CN.get(sev, sev)}｜{e.get('kind', 'EV')}｜{e.get('display', '')}")
    if len(items) > 12:
        out.append(f"…另有 {len(items) - 12} 条（缩小选区可展开）")
    return out


# --------------------------------------------------------------------------- #
# 加载线程
# --------------------------------------------------------------------------- #

class _LoadWorker(QtCore.QThread):
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(float, str)

    def __init__(self, path: str, force: bool, cache: Optional[str], parent=None):
        super().__init__(parent)
        self.path, self.force, self.cache = path, force, cache

    def run(self) -> None:      # noqa: D102
        t0 = time.time()
        try:
            st = LogStore.open(self.path, cache_dir=self.cache, force=self.force,
                               progress=lambda fr, msg: self.progress.emit(fr, msg))
            log.info("加载 %s 用时 %.2fs", Path(self.path).name, time.time() - t0)
            self.done.emit(st)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 时间线
# --------------------------------------------------------------------------- #

class MiniTimeline(QtWidgets.QWidget):
    """紧凑时间线：模式色带 + 事件刻度 + 告警区间 + 播放指针。

    交互：单击＝定位游标并弹出该时刻附近的事件菜单（选一条就跳到「事件」页对应行）；
    Ctrl+单击＝只定位；悬停感叹号/告警带＝看详情。
    """

    timeChanged = QtCore.Signal(float)
    eventChosen = QtCore.Signal(object)        # 菜单里选中的一条事件（空 dict = 只要切页）
    notice = QtCore.Signal(str)                # 需要告诉用户的话（此处无事件等）

    #: 模式色带的配色来源（用户 2026-10-24）：颜色是**模式的属性**，
    #: 不再是"第几段"的属性。默认值见 ui/modecolors.py 的难度阶梯。
    _BAND_TOP, _BAND_H = 6, 16
    _MODE_FALLBACK = "#9aa4ad"
    _WINDOWS = (1.0, 5.0, 30.0)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(96)
        self.setMouseTracking(True)
        # hoverMoveEvent 只有在 WA_Hover 打开时才会被 Qt 投递（光 setMouseTracking 不管用）
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)
        self.setToolTip("单击＝定位并查看此处事件；Ctrl+单击＝只定位；"
                        "悬停感叹号/告警带＝看详情")
        self._t0, self._t1 = 0.0, 1.0
        self._empty = True                      # 未加载日志：不画假刻度，只给提示
        self._click_window = 5.0                # 点击取事件的±时间窗口（秒）
        self._events: list[dict[str, Any]] = []
        self._modes: list[tuple[float, float, str]] = []
        self._regions: list[dict[str, Any]] = []
        self._sel: Optional[tuple[float, float]] = None
        self._t: Optional[float] = None
        # 悬停命中表（在 paintEvent 里按当前像元重建）
        self._badge_hits: list[tuple[float, float, list[str]]] = []
        self._region_hits: list[tuple[float, float, list[str]]] = []
        self._mode_hits: list[tuple[float, float, str, float, float]] = []
        self._mode_colors: dict[str, str] = {}     # 模式名 → #rrggbb（由主窗推过来）

    # 数据
    def set_span(self, t0: float, t1: float) -> None:
        self._t0, self._t1 = float(t0), max(float(t1), float(t0) + 1e-3)
        self._empty = False
        self.update()

    def is_empty(self) -> bool:
        return bool(self._empty)

    def span(self) -> tuple[float, float]:
        """当前可见时间区间（缩放后就是缩放区间，播放就在这一段里跑）。"""
        return float(self._t0), float(self._t1)

    def set_events(self, events: Sequence[dict]) -> None:
        self._events = [dict(e) for e in events or []]
        self.update()

    def set_modes(self, modes: Sequence[tuple[float, float, str]]) -> None:
        self._modes = list(modes or [])
        self.update()

    def set_mode_colors(self, mapping: dict[str, str]) -> None:
        """设置模式配色（模式名 → #rrggbb）。缺失的模式回落到难度阶梯默认值。"""
        self._mode_colors = {str(k).strip().upper(): str(v)
                             for k, v in (mapping or {}).items() if str(k or "").strip()}
        self.update()

    def mode_colors(self) -> dict[str, str]:
        return dict(self._mode_colors)

    def mode_color_of(self, name: str) -> str:
        """某模式当前颜色：优先用主窗推过来的表，否则用默认阶梯。"""
        nm = str(name or "").strip().upper()
        hit = self._mode_colors.get(nm)
        return hit or _modecolors.default_color(nm)

    def mode_boundary_widths(self) -> list[float]:
        """每段左边界线宽：1=正常；2=与上一段颜色太像（靠加粗保住可读性）。

        用户要求：自定义配色再接近，时间线也要靠分段边界/描边读得清。
        """
        out: list[float] = []
        prev = ""
        for _a, _b, name in self._modes:
            hexv = self.mode_color_of(name)
            out.append(2.0 if (prev and _modecolors.too_close(prev, hexv)) else 1.0)
            prev = hexv
        return out

    def current_mode(self, t: Optional[float] = None) -> str:
        """某时刻处于哪个模式（默认取播放指针）；不在任何段内返回空串。"""
        tt = self._t if t is None else t
        if tt is None or not math.isfinite(float(tt)):
            return ""
        for a, b, name in self._modes:
            if float(a) <= float(tt) <= float(b):
                return str(name)
        return ""

    def set_regions(self, regions: Sequence[dict]) -> None:
        self._regions = [dict(r) for r in regions or []]
        self.update()

    def set_selection(self, t0: Optional[float], t1: Optional[float]) -> None:
        self._sel = None if t0 is None or t1 is None else (float(t0), float(t1))
        self.update()

    def set_time(self, t: float) -> None:
        self._t = None if t is None or not math.isfinite(t) else float(t)
        self.update()

    def mousePressEvent(self, ev: QtGui.QMouseEvent) -> None:  # noqa: N802
        if ev.button() != QtCore.Qt.MouseButton.LeftButton:
            super().mousePressEvent(ev)
            return
        t = self._t_of(ev.position().x(), self.width())
        self.timeChanged.emit(t)
        ev.accept()
        if ev.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            return                              # Ctrl+单击：只定位，不弹菜单
        if self._empty:
            self.notice.emit("还没加载日志：用「打开日志」/「打开目录」选一份 .BIN，或从飞控下载")
            return
        self._popup_events(t, ev.globalPosition().toPoint())

    # ---------------- 点击查事件 ----------------
    def _events_near(self, t: float, half: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in self._events:
            try:
                et = float(e.get("t", float("nan")))
            except (TypeError, ValueError):
                continue
            if math.isfinite(et) and abs(et - t) <= half:
                out.append(e)
        out.sort(key=lambda e: (abs(float(e.get("t", 0.0) or 0.0) - t),
                                float(e.get("t", 0.0) or 0.0)))
        return out

    @staticmethod
    def _event_action_text(e: dict[str, Any], t: float) -> str:
        sev = str(e.get("severity", "info"))
        et = float(e.get("t", 0.0) or 0.0)
        txt = str(e.get("display") or e.get("text") or "")
        if len(txt) > 88:
            txt = txt[:87] + "…"
        return (f"{SEVERITY_CN.get(sev, sev)}　t={et:.3f}s（{et - t:+.2f}s）　"
                f"{e.get('kind', 'EV')}｜{txt}")

    def set_click_window(self, seconds: float) -> None:
        self._click_window = float(seconds)

    def click_window(self) -> float:
        return float(self._click_window)

    def event_menu(self, t: float) -> Optional[QtWidgets.QMenu]:
        """构造“此处±窗口内的事件”菜单；没事件时返回 None 并发 notice（不弹空菜单）。"""
        half = float(self._click_window)
        near = self._events_near(t, half)
        if not near:
            self.notice.emit(f"t={t:.2f}s 附近 ±{half:g}s 内没有事件（Ctrl+单击可只定位不弹菜单）")
            return None
        menu = QtWidgets.QMenu(self)
        head = menu.addAction(f"t = {t:.2f}s　±{half:g}s 内共 {len(near)} 条事件")
        head.setEnabled(False)
        menu.addSeparator()
        shown = near[:60]
        for e in shown:
            menu.addAction(self._event_action_text(e, t)).setData(e)
        if len(near) > len(shown):
            menu.addAction(f"…另有 {len(near) - len(shown)} 条，去「事件」页看全部").setData(None)
        menu.addSeparator()
        win_menu = menu.addMenu("时间窗口")
        for sec in self._WINDOWS:
            a = win_menu.addAction(f"± {sec:g} 秒")
            a.setCheckable(True)
            a.setChecked(abs(sec - half) < 1e-9)
            a.setData(float(sec))
        menu.addAction("在「事件」页中选中此处").setData(False)
        return menu

    def handle_menu_result(self, chosen: Optional[QtWidgets.QAction],
                           t: float, gpos: QtCore.QPoint) -> None:
        """菜单选中后的分派（单独拆出来：测试不用真开模态菜单）。"""
        if chosen is None:
            return
        data = chosen.data()
        if isinstance(data, dict):
            self.eventChosen.emit(data)
            return
        if data is None or isinstance(data, bool):
            self.eventChosen.emit({})           # 空 dict = 只切到事件页
            return
        try:
            self._click_window = float(data)
        except (TypeError, ValueError):
            return
        QtCore.QTimer.singleShot(0, lambda: self._popup_events(t, gpos))

    def _popup_events(self, t: float, gpos: QtCore.QPoint) -> None:
        menu = self.event_menu(t)
        if menu is None:
            return
        try:
            chosen = menu.exec(gpos)
        finally:
            menu.deleteLater()
        self.handle_menu_result(chosen, t, gpos)

    # 绘制
    def _theme_colors(self) -> None:
        """背景/前景跟着系统主题走（以前写死白色，深色下整条时间线是一块白）。"""
        pal = self.palette()
        bg = pal.color(QtGui.QPalette.ColorRole.Base)
        fg = pal.color(QtGui.QPalette.ColorRole.WindowText)
        if not bg.isValid():
            bg = QtGui.QColor("#fbfbfd")
        if not fg.isValid():
            fg = QtGui.QColor("#3a4150")
        muted = QtGui.QColor(fg)
        muted.setAlpha(170)
        self._bg, self._fg, self._muted = bg, fg, muted
    def _x_of(self, t: float, w: float) -> float:
        return (t - self._t0) / (self._t1 - self._t0) * w

    @staticmethod
    def _region_lines(rg: dict[str, Any], a: float, b: float) -> list[str]:
        """告警带悬停文本：标题/区间/实测值与阈值/说明。"""
        sev = str(rg.get("severity", "warn"))
        out = [f"{SEVERITY_CN.get(sev, sev)}｜{rg.get('title', '告警区')}｜{a:.1f}–{b:.1f}s"]
        val, thr, unit = rg.get("value"), rg.get("threshold"), rg.get("unit") or ""
        if isinstance(val, (int, float)) and isinstance(thr, (int, float)):
            out.append(f"  实测 {val:.2f}{unit}／阈值 {thr:.2f}{unit}")
        ch = str(rg.get("channel_label") or "")
        if ch:
            out.append(f"  通道 {ch}")
        msg = str(rg.get("message") or "")
        if msg:
            out.append(f"  {msg}")
        return out

    def _t_of(self, x: float, w: float) -> float:
        return self._t0 + x / max(w, 1.0) * (self._t1 - self._t0)

    def paintEvent(self, ev: QtGui.QPaintEvent) -> None:  # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        self._badge_hits = []
        self._region_hits = []
        self._theme_colors()
        p.fillRect(0, 0, w, h, self._bg)
        if self._empty:
            # 没数据时不画 0s/1s 的假刻度，直接说清楚该干什么
            p.setPen(self._muted)
            p.drawText(QtCore.QRectF(8, 8, max(1, w - 16), max(1, h - 16)),
                       QtCore.Qt.AlignmentFlag.AlignCenter,
                       "时间线：未加载日志。用「打开日志」/「打开目录」选一份 .BIN，或从飞控下载")
            p.end()
            return
        band_top, band_h = 6, 16
        axis_h = 16
        tick_top, tick_h = band_top + band_h + 4, h - axis_h - band_h - 10

        # 模式色带：颜色按**模式**取（同模式处处同色），不按段序号轮换
        self._mode_hits = []
        accent = self.palette().color(QtGui.QPalette.ColorRole.Highlight)
        if not accent.isValid():
            accent = QtGui.QColor("#2f6fed")
        prev_hex = ""
        widths = self.mode_boundary_widths()
        for i_seg, (a, b, name) in enumerate(self._modes):
            x0 = self._x_of(max(a, self._t0), w)
            x1 = self._x_of(min(b, self._t1), w)
            if x1 <= x0:
                continue
            hexv = self.mode_color_of(name)
            col = QtGui.QColor(hexv)
            p.fillRect(int(x0), band_top, max(1, int(x1 - x0)), band_h, col)
            ink = QtGui.QColor(_modecolors.ink_on(hexv))
            # 当前时刻所在的段：加一圈强调色描边（"当前选中状态"不能只靠颜色差）
            is_current = (self._t is not None and float(a) <= float(self._t) <= float(b))
            # 分段边界：永远画一条线；相邻两色太像时加粗——自定义配色再像也读得清
            boundary_w = widths[i_seg] if i_seg < len(widths) else 1.0
            edge = QtGui.QColor(ink)
            edge.setAlpha(150 if boundary_w < 2 else 220)
            p.setPen(QtGui.QPen(edge, boundary_w))
            p.drawLine(int(round(x0)), band_top, int(round(x0)), band_top + band_h)
            if is_current:
                pen = QtGui.QPen(accent, 2.0)
                p.setPen(pen)
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                p.drawRect(QtCore.QRectF(x0 + 1.0, band_top + 0.5,
                                         max(1.0, x1 - x0 - 1.0), band_h - 1.0))
            p.setPen(ink)
            f = self.font()
            f.setBold(bool(is_current))
            p.setFont(f)
            if x1 - x0 > 34:
                p.drawText(QtCore.QRectF(x0 + 3, band_top, x1 - x0 - 4, band_h),
                           QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                           str(name)[:22])
            else:                       # 太窄就不写字，但悬停仍能问出是哪个模式
                p.setFont(self.font())
            self._mode_hits.append((float(x0), float(x1), str(name), float(a), float(b)))
            prev_hex = hexv
        p.setFont(self.font())

        # 告警区间
        for rg in self._regions:
            a, b = float(rg.get("t0", float("nan"))), float(rg.get("t1", float("nan")))
            if not math.isfinite(a):
                continue
            b = a if not math.isfinite(b) else b
            x0, x1 = self._x_of(max(a, self._t0), w), self._x_of(min(b, self._t1), w)
            if x1 <= x0:
                x1 = x0 + 2
            col = _sev_color(str(rg.get("severity", "warn")))
            col.setAlpha(90)
            p.fillRect(int(x0), tick_top, max(2, int(x1 - x0)), tick_h, col)
            self._region_hits.append((x0, max(x1, x0 + 3.0),
                                      self._region_lines(rg, a, b)))

        # 事件刻度（只画竖线，不写文字）
        warn_events: list[dict[str, Any]] = []
        rows = {"high": 0, "low": 1}
        for e in self._events:
            t = float(e.get("t", float("nan")))
            if not math.isfinite(t) or t < self._t0 or t > self._t1:
                continue
            sev = str(e.get("severity", "info"))
            x = self._x_of(t, w)
            lane = 0 if severity_rank(sev) >= severity_rank("warn") else 1
            y0 = tick_top + lane * (tick_h // 2)
            p.setPen(QtGui.QPen(_sev_color(sev), 1.4))
            p.drawLine(int(x), y0, int(x), y0 + tick_h // 2 - 2)
            if severity_rank(sev) >= severity_rank("warn"):
                warn_events.append(e)

        # 警告/错误 → 感叹号徒标（同像元簇合并），文字全部放进悬停提示
        clusters: list[dict[str, Any]] = []
        for e in sorted(warn_events, key=lambda d: float(d.get("t", 0.0) or 0.0)):
            x = self._x_of(float(e["t"]), w)
            if clusters and x - clusters[-1]["x"] <= 11.0:
                clusters[-1]["items"].append(e)
            else:
                clusters.append({"x": x, "items": [e]})
        cy = tick_top + max(7, (tick_h // 2) // 2)
        r = 6.5
        badge_font = self.font()
        badge_font.setBold(True)
        badge_font.setPointSize(8)
        for c in clusters[:160]:
            top = max(c["items"], key=lambda d: severity_rank(str(d.get("severity", "info"))))
            sev = str(top.get("severity", "warn"))
            cx = c["x"]
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 220), 1))
            p.setBrush(_sev_color(sev))
            p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
            p.setFont(badge_font)
            p.setPen(QtGui.QPen(QtGui.QColor("#3a2a00") if sev == "warn" else QtGui.QColor("#ffffff")))
            p.drawText(QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r),
                       QtCore.Qt.AlignmentFlag.AlignCenter, "!")
            lines = _event_lines(c["items"])
            if len(c["items"]) > 1:
                lines = [f"{len(c['items'])} 条事件（按时间顺序）"] + lines
            self._badge_hits.append((cx, cy, lines))
        p.setFont(self.font())

        # 选区
        if self._sel:
            x0, x1 = sorted((self._x_of(self._sel[0], w), self._x_of(self._sel[1], w)))
            p.setPen(QtGui.QPen(QtGui.QColor("#2f6fed"), 1, QtCore.Qt.PenStyle.DashLine))
            p.setBrush(QtGui.QColor(47, 111, 237, 28))
            p.drawRect(int(x0), 2, max(2, int(x1 - x0)), h - 4)

        # 播放指针
        if self._t is not None and self._t0 <= self._t <= self._t1:
            x = int(self._x_of(self._t, w))
            p.setPen(QtGui.QPen(self._fg, 1))
            p.drawLine(x, 2, x, h - axis_h)

        # 时间刻度
        p.setPen(self._muted)
        n = max(2, min(10, w // 120))
        for i in range(n + 1):
            t = self._t0 + (self._t1 - self._t0) * i / n
            x = self._x_of(t, w)
            p.drawLine(int(x), h - axis_h, int(x), h - axis_h + 3)
            p.drawText(int(x) - 22, h - 2, f"{t:.0f}s")
        p.end()

    def hoverMoveEvent(self, ev: QtGui.QHoverEvent) -> None:  # noqa: N802
        """悬停感叹号→事件文字（时间序）；悬停告警带→告警标题。"""
        pt = ev.position()
        x, y = float(pt.x()), float(pt.y())
        best: Optional[list[str]] = None
        bd = float("inf")
        for cx, cy, lines in self._badge_hits:
            d = math.hypot(x - cx, y - cy)
            if d < bd:
                bd, best = d, lines
        if best is not None and bd <= 9.5:
            show_tip(self, "\n".join(best))
            ev.accept()
            return
        if self._BAND_TOP - 2 <= y <= self._BAND_TOP + self._BAND_H + 2:
            for x0, x1, name, a, b in self._mode_hits:
                if x0 - 1.0 <= x <= max(x1, x0 + 1.0) + 1.0:
                    dur = max(0.0, float(b) - float(a))
                    show_tip(self, f"模式 {name}　{float(a):.1f}–{float(b):.1f}s"
                                   f"（{dur:.1f}s）")
                    ev.accept()
                    return
        bands = [lines for x0, x1, lines in self._region_hits if x0 - 1.0 <= x <= x1 + 1.0]
        if bands:
            flat = [ln for grp in sorted(bands, key=lambda g: g[0]) for ln in grp]
            show_tip(self, "\n".join(flat[:14]))
            ev.accept()
            return
        hide_tip()
        # QWidget 本身没有 hoverMoveEvent 可以 super()（PySide6 里这个属性不存在），别调

    def leaveEvent(self, ev: QtCore.QEvent) -> None:  # noqa: N802
        hide_tip()
        super().leaveEvent(ev)


class PlayBar(QtWidgets.QWidget):
    """时间线上方那一行播放控制：播放/暂停、倍速、读数、地图跟随。

    播放起点就是当前游标（也就是你刚点过的那个位置）；播放范围是当前**可见**
    区间，缩放以后就只在那一段里跑，不会跳到整条日志末尾。
    """

    playToggled = QtCore.Signal(bool)
    rateChanged = QtCore.Signal(float)
    followToggled = QtCore.Signal(bool)
    trackModeToggled = QtCore.Signal(bool)     # True=全部轨迹显示，False=跟随时间线

    RATES: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 rate: float = 4.0, follow: bool = False) -> None:
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 1, 6, 1)
        lay.setSpacing(8)

        self.btn_play = QtWidgets.QPushButton("▶ 播放")
        self.btn_play.setCheckable(True)
        self.btn_play.setMinimumWidth(84)
        self.btn_play.setToolTip("从当前游标位置开始播放，再点一次暂停；地图上的飞机会跟着走")
        self.btn_play.clicked.connect(lambda: self.playToggled.emit(self.btn_play.isChecked()))
        lay.addWidget(self.btn_play)

        lay.addWidget(QtWidgets.QLabel("倍速"))
        self.cb_rate = QtWidgets.QComboBox()
        for r in self.RATES:
            self.cb_rate.addItem(f"{r:g}×", r)
        self.cb_rate.setCurrentIndex(max(0, self.cb_rate.findData(float(rate))))
        self.cb_rate.setToolTip("日志本身是真实时间：4× 表示 10 分钟的飞行 2.5 分钟播完")
        self.cb_rate.currentIndexChanged.connect(
            lambda _i: self.rateChanged.emit(float(self.cb_rate.currentData())))
        lay.addWidget(self.cb_rate)

        # 轨迹播放开关：默认"全部轨迹显示"（打开日志先看完整航迹）
        self.btn_all_tracks = QtWidgets.QPushButton("全部轨迹显示")
        self.btn_all_tracks.setCheckable(True)
        self.btn_all_tracks.setChecked(True)
        self.btn_all_tracks.setMinimumWidth(104)
        self.btn_all_tracks.setToolTip(
            "按下：地图画完整轨迹（默认）。" + chr(10)
            + "弹起：轨迹只画到时间线游标处，拖动/播放时逐点长出来。" + chr(10)
            + "多条轨迹会同时裁剪，便于按时刻对比各条走到哪。")
        self.btn_all_tracks.toggled.connect(self.trackModeToggled.emit)
        lay.addWidget(self.btn_all_tracks)

        self.chk_follow = QtWidgets.QCheckBox("地图跟随")
        self.chk_follow.setToolTip("播放时把地图视野跟着飞机走（关掉就只动游标不挪图）")
        self.chk_follow.setChecked(bool(follow))
        self.chk_follow.toggled.connect(self.followToggled.emit)
        lay.addWidget(self.chk_follow)

        self.lbl_time = QtWidgets.QLabel("t=—")
        self.lbl_time.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        lay.addWidget(self.lbl_time)
        lay.addStretch(1)

    # 状态
    def set_playing(self, on: bool) -> None:
        self.btn_play.blockSignals(True)
        self.btn_play.setChecked(bool(on))
        self.btn_play.setText("⏸ 暂停" if on else "▶ 播放")
        self.btn_play.blockSignals(False)

    def set_rate(self, rate: float) -> None:
        i = self.cb_rate.findData(float(rate))
        if i >= 0 and self.cb_rate.currentIndex() != i:
            self.cb_rate.setCurrentIndex(i)

    def rate(self) -> float:
        return float(self.cb_rate.currentData() or 4.0)

    def set_readout(self, t: float, t_start: float, t_end: float, playing: bool) -> None:
        mark = "▶" if playing else "⏸"
        span = t_end - t_start
        frac = ((t - t_start) / span) if span > 1e-9 else (1.0 if t >= t_end else 0.0)
        # 百分比按当前可见区间算：日志往往不从 0s 开始，用 t/t_end 会一上来就“13%”
        self.lbl_time.setText(f"{mark} t={t:.2f}s / {t_end:.1f}s　{max(0.0, min(1.0, frac)) * 100:.0f}%")

    def set_available(self, ok: bool) -> None:
        self.btn_play.setEnabled(bool(ok))
        self.cb_rate.setEnabled(bool(ok))


# --------------------------------------------------------------------------- #
# 主窗
# --------------------------------------------------------------------------- #

class _PluginPageHost(QtWidgets.QScrollArea):
    """插件标签页的滚动宿主（2026-10-29：修“插件把左侧栏挤扁拉不回来”）。

    QSplitter 给两侧分配宽度时尊重各侧的 minimumSizeHint。插件控件一旦带很大的
    minimumWidth（典型的：聊天面板 `setMinimumWidth(560)` + 一排宽按钮），
    QTabWidget 的最小宽就被顶到上千像素，左侧栏只能被压到几十像素；更难受的是
    分隔条再也拉不回来——把右侧压到它最小宽以下是不可能的。

    QScrollArea 的最小宽只取决于滚动条，所以插件的“最小宽”降级成“内容宽”：
    页窄了出横向滚动条，而不是去抢左侧栏的地盘。宿主对插件控件完全透明
    （无边框、无外边距、widgetResizable），插件自己照旧拿到整块视口。
    """

    def __init__(self, inner: QtWidgets.QWidget) -> None:
        super().__init__()
        self._inner = inner
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setContentsMargins(0, 0, 0, 0)
        self.setWidgetResizable(True)
        self.setWidget(inner)
        self.setMinimumWidth(0)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Expanding)
        # 滚轮归插件自己用（曲线/表格里的滚轮缩放不能被宿主抢走）
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.viewport().setAutoFillBackground(False)

    def plugin_widget(self) -> QtWidgets.QWidget:
        return self._inner

    def minimumSizeHint(self) -> QtCore.QSize:      # noqa: N802   Qt 命名
        """只保证能放下滚动条，不传插件的尺寸诉求。"""
        bar = self.verticalScrollBar().sizeHint().width()
        return QtCore.QSize(bar + 8, bar + 8)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, cache_dir: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle(f"APMLogViewer {__version__} —— ArduCopter DataFlash 日志分析")
        self.resize(1500, 900)
        self.cache_dir = cache_dir
        self.store: Optional[LogStore] = None
        self.worker: Optional[_LoadWorker] = None
        self._preset = "vibe"
        self._extra: list[ChannelKey] = []
        # 「叠加绘制」建立的分组：每组一条泳道（第 1 条主曲线，其余同轴叠加）
        self._groups: list[list[ChannelKey]] = []
        # 追加到**已有泳道**的叠加曲线：[(泳道标签, [通道键, ...]), ...]
        self._lane_overlays: list[tuple[str, list[ChannelKey]]] = []
        self._win: Optional[tuple[float, float]] = None
        self._track_vis: Optional[list[str]] = None   # 地图轨迹勾选记忆（views.json）
        self._alerts: list[AlertHit] = []
        self._events: list[EventRec] = []
        self.theme = "light"
        # 插件：注册表变化时重建预设框/标签页/地图图层（见 _sync_plugin_ui）
        self._plugin_presets: dict[str, Any] = {}
        self._plugin_widgets: set[Any] = set()
        # 插件控件 → (插件 id, 外层宿主)。宿主是 QScrollArea，见 _PluginPageHost
        self._plugin_hosts: dict[Any, tuple[str, Any]] = {}
        self._last_mode = ""
        self._applying_theme = False

        self._build_widgets()
        self._build_layout()
        self._wire()
        # 插件要在恢复视图之前加载：views.json 里存的插件预设 id 才能被认出来
        self.plugins = PluginManager(self)
        try:
            self.plugins.pluginError.connect(self._on_plugin_error)
            self.plugins.changed.connect(self._sync_plugin_ui)
            self.plot.plotToolRequested.connect(self._on_plot_tool)
        except Exception:                            # noqa: BLE001 接不上信号也不许影响启动
            log.debug("插件信号连接失败", exc_info=True)
        self._load_plugins_safely()
        self._restore_view()
        self.set_status("打开一份 .BIN 日志开始分析（支持拖拽）")
        self.log_list.refresh_requested.connect(lambda p: self.load(p))

    # ---------------- 构建 ----------------
    def _build_widgets(self) -> None:
        self.map = TrackMapWidget(cache_dir=None)
        s = get_settings()
        self.plot = SeriesPlot(theme=str(s.get("theme")))
        # 三个旋钮从设置里拉，建完立刻拉，后面控件的初值就是它们
        self.plot.set_lane_min_height(int(s.get("lane_min_h")))
        self.plot.set_badge_tol_px(float(s.get("badge_tol_px")))
        self.plot.set_refine_debounce_ms(int(s.get("refine_debounce_ms")))
        self.timeline = MiniTimeline()
        _st = get_settings()
        self.play_bar = PlayBar(rate=float(_st.get("play_rate")),
                                follow=bool(_st.get("play_follow")))
        self.play_bar.set_available(False)          # 没加载日志时不能播
        self._playing = False
        self._play_t = 0.0
        self._play_wall = 0.0
        self._play_rate = self.play_bar.rate()
        # 阈值改动会重算告警 + 整窗重画；拖动数字框时不能每格都算一次
        self._alert_timer = QtCore.QTimer(self)
        self._alert_timer.setSingleShot(True)
        self._alert_timer.setInterval(250)
        self._alert_timer.timeout.connect(self._reload_alerts)
        self._play_timer = QtCore.QTimer(self)
        self._play_timer.setInterval(50)            # 20fps：地图够顺，也不会把瓦片请求刷爆
        self._play_timer.timeout.connect(self._play_step)
        self._play_clock = QtCore.QElapsedTimer()
        self.preset_box = QtWidgets.QComboBox()
        for pr in PRESETS:
            self.preset_box.addItem(f"{pr.name}", pr.id)
        idx = max(0, [p.id for p in PRESETS].index(self._preset))
        self.preset_box.setCurrentIndex(idx)
        self.skipped_label = QtWidgets.QLabel("")
        self.skipped_label.setStyleSheet("color:#8a6d1b;")   # 初值，_refresh_hint_colors 会按主题重算
        self.skipped_label.setWordWrap(True)

        self.log_list = _LogListDock()
        self.channel_tree = _ChannelTreeDock()
        self.alert_table = _AlertTable()
        self.event_table = _EventTable()
        self.param_table = _ParamTable()
        self._param_filter = QtWidgets.QLineEdit()
        self._param_filter.setPlaceholderText("过滤参数名，如 BATT_ / WP_ / EK3_ / INS_ / PSC_ / ATC_")
        self._param_diff_only = QtWidgets.QCheckBox("只看与出厂默认不同")
        self._param_count = QtWidgets.QLabel("")
        self.btn_param_export = QtWidgets.QPushButton("导出参数…")
        self.btn_param_export.setToolTip(
            "把日志里的参数导成文件：\n"
            "• .param（NAME,VALUE）——Mission Planner / QGC 直接可读\n"
            "• .csv（带出厂默认与差异列）——给人看、进 Excel\n"
            "可选完整参数、只导与出厂默认不同的，或只导当前筛选结果。\n"
            "数值来自日志 PARM 块，不是从飞控实时读的。")
        self.btn_param_export.clicked.connect(self._export_params)
        self._param_view = QtWidgets.QWidget()
        pv = QtWidgets.QVBoxLayout(self._param_view)
        pv.setContentsMargins(4, 4, 4, 0)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(self._param_filter, 1)
        prow.addWidget(self._param_diff_only)
        prow.addWidget(self._param_count)
        prow.addWidget(self.btn_param_export)
        pv.addLayout(prow)
        pv.addWidget(self.param_table, 1)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.map, "轨迹地图")
        self.tabs.addTab(self._chart_panel(), "曲线")
        self.tabs.addTab(self.alert_table, "告警")
        self.tabs.addTab(self.event_table, "事件")
        self.tabs.addTab(self._param_view, "参数")

    def _chart_panel(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("预设视图"))
        row.addWidget(self.preset_box)
        btn_reset = QtWidgets.QPushButton("复位视图")
        btn_zoom = QtWidgets.QPushButton("缩放到选区")
        btn_png = QtWidgets.QPushButton("导出当前图 PNG")
        btn_csv = QtWidgets.QPushButton("导出当前图 CSV")
        for b in (btn_reset, btn_zoom, btn_png, btn_csv):
            row.addWidget(b)
        row.addWidget(QtWidgets.QLabel("每道最小高"))
        self.lane_h_box = QtWidgets.QSpinBox()
        self.lane_h_box.setRange(70, 400)
        self.lane_h_box.setSingleStep(10)
        self.lane_h_box.setSuffix(" px")
        self.lane_h_box.setToolTip("泳道总高超出可视区时图区出纵向滚动条，而不是把曲线压扁。\n"
                                   "调大这个值看得更清楚；滚动条/PageUp·PageDown 上下翻。\n"
                                   "注意：图区内的滚轮仍是缩放（旧行为）。")
        self.lane_h_box.setValue(self.plot.lane_min_height())
        self.lane_h_box.valueChanged.connect(self._lane_height_changed)
        row.addWidget(self.lane_h_box)
        _nl = chr(10)
        self.btn_overlay = QtWidgets.QPushButton("叠加绘制")
        self.btn_overlay.setToolTip(
            "把若干通道叠加到一条泳道上，共用一根 Y 轴（各有一条胶囊）。" + _nl
            + "先在左侧通道树里 Ctrl/Shift 多选，再点这里；" + _nl
            + "也可以在这里选「新建一条泳道」或「叠加到已有泳道」。")
        self.btn_overlay.clicked.connect(self._open_overlay_dialog)
        row.addWidget(self.btn_overlay)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.skipped_label)
        lay.addWidget(self.plot, 1)
        self.btn_reset, self.btn_zoom = btn_reset, btn_zoom
        self.btn_png, self.btn_csv = btn_png, btn_csv
        return w

    def _build_layout(self) -> None:
        outer = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        inner = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        left = QtWidgets.QWidget()
        llay = QtWidgets.QVBoxLayout(left)
        llay.setContentsMargins(0, 0, 0, 0)
        llay.addWidget(self.log_list, 3)
        llay.addWidget(self.channel_tree, 2)
        inner.addWidget(left)
        inner.addWidget(self.tabs)
        inner.setSizes([300, 1100])
        outer.addWidget(inner)
        outer.addWidget(self.play_bar)
        outer.addWidget(self.timeline)
        outer.setSizes([748, 26, 132])
        self.setCentralWidget(outer)
        self.setAcceptDrops(True)
        # 读取遮罩：盖在右侧标签区内容区上，父级是 tabs，所以窗口/分隔条拖动时
        # 由 eventFilter 跟着重算几何；标签条不盖，用户仍能看到自己在哪页。
        self.loading = LoadingOverlay(self.tabs)

        tb = QtWidgets.QToolBar("主工具栏")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(18, 18))
        self.act_open = tb.addAction("打开日志…")
        self.act_dir = tb.addAction("打开目录…")
        self.act_force = tb.addAction("强制重新索引")
        self.act_link = tb.addAction("连接飞控（下载日志）…")
        tb.addSeparator()
        self.act_prev = tb.addAction("← 上一告警")
        self.act_next = tb.addAction("下一告警 →")
        tb.addSeparator()
        self.act_refresh = tb.addAction("刷新视图")
        self.act_forget = tb.addAction("清除视图记忆")
        tb.addSeparator()
        self.act_settings = tb.addAction("设置")
        self.act_plugins = tb.addAction("插件")
        self.act_plugins.setToolTip("插件：从外部目录加载扩展（告警规则、合成通道、预设、曲线工具、"
                                    "地图图层、标签页、导出器）")
        tb.addAction("关于", self._about)
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, tb)
        sb = self.statusBar()
        # 状态文本只走左侧的 showMessage。以前还挂了一个右侧 permanent QLabel，
        # 同一句话左石各一份，用户要求只留左侧。
        self._status_text = ""
        sb.messageChanged.connect(self._on_status_message)

    def _wire(self) -> None:
        self.act_open.triggered.connect(self._choose_file)
        self.act_settings.triggered.connect(self._open_settings)
        self.act_plugins.triggered.connect(self._open_plugins)
        Settings.instance().changed.connect(self._apply_setting)
        self.plot.themeChanged.connect(lambda t: Settings.instance().set("theme", t))
        # “跟随系统”：系统深浅色切换时跟着换肤（Qt 6.5+ 才有这个信号，没有就安静降级）
        try:
            _app = QtWidgets.QApplication.instance()
            if _app is not None:
                _app.styleHints().colorSchemeChanged.connect(self._on_system_scheme)
        except Exception:                              # noqa: BLE001
            log.debug("系统深浅色变化监听不可用", exc_info=True)
        self._apply_ui_theme()                         # 建窗口时再刷一次，单独用 MainWindow 也正常
        self._refresh_hint_colors()
        self.act_dir.triggered.connect(self._choose_dir)
        self.act_force.triggered.connect(lambda: self.load(self.store.path, force=True) if self.store else None)
        self.act_link.triggered.connect(self._open_link_dialog)
        self.act_refresh.triggered.connect(lambda: self._apply(keep_window=True))
        self.act_forget.triggered.connect(self._forget_view)
        self.act_prev.triggered.connect(lambda: self._step_alert(-1))
        self.act_next.triggered.connect(lambda: self._step_alert(1))
        self.preset_box.currentIndexChanged.connect(self._preset_changed)
        self.btn_reset.clicked.connect(self._reset_window)
        self.btn_zoom.clicked.connect(self._zoom_to_selection)
        self.btn_png.clicked.connect(self._export_png)
        self.btn_csv.clicked.connect(self._export_csv)
        self.plot.timeChanged.connect(self._on_time)
        self.plot.viewChanged.connect(self._refine_view)
        self.plot.regionChanged.connect(self._on_region)
        self.map.timeChanged.connect(self._on_time)
        self.map.sourceChanged.connect(lambda s: self.set_status(f"瓦片源：{s}"))
        self.map.trackVisibilityChanged.connect(self._on_track_vis)
        try:
            self.map.set_gauges_visible(bool(get_settings().get("gauge_show")))
        except Exception:                            # noqa: BLE001 仪表开关不影响主流程
            log.debug("仪表初值设置失败", exc_info=True)
        self.timeline.timeChanged.connect(self._on_timeline_seek)
        self.timeline.eventChosen.connect(self._on_timeline_event)
        self.timeline.notice.connect(self.set_status)
        self.play_bar.playToggled.connect(self._toggle_play)
        self.play_bar.rateChanged.connect(self._play_rate_changed)
        self.play_bar.followToggled.connect(self._play_follow_changed)
        self.play_bar.trackModeToggled.connect(self._track_mode_changed)
        try:
            self.map.set_playback_follow(bool(get_settings().get("play_follow")))
        except Exception:                              # noqa: BLE001 跟随开关失败不影响主流程
            log.debug("地图跟随初值设置失败", exc_info=True)
        self.channel_tree.channelChosen.connect(self._add_channel)
        self._param_filter.textChanged.connect(self._refilter_params)
        self._param_diff_only.toggled.connect(self._refilter_params)
        self.alert_table.seekRequested.connect(self._seek_alert)
        self.alert_table.helpRequested.connect(self._open_alert_help)
        self.event_table.seekRequested.connect(self._on_time)
        self.event_table.helpRequested.connect(self._open_event_help)
        self.tabs.currentChanged.connect(lambda i: self._apply(keep_window=True) if i in (1, 2, 3, 4) else None)

    # ---------------- 文件 ----------------
    def _choose_file(self) -> None:
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "打开 DataFlash 日志", "",
                                                     "DataFlash 日志 (*.BIN *.bin);;所有文件 (*)")
        if p:
            self.load(p)

    def _choose_dir(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择日志目录")
        if d:
            self.log_list.scan(d)

    def dragEnterEvent(self, ev: QtGui.QDragEnterEvent) -> None:  # noqa: N802
        if any(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() == ".bin"
               for u in ev.mimeData().urls()):
            ev.acceptProposedAction()

    def dropEvent(self, ev: QtGui.QDropEvent) -> None:  # noqa: N802
        for u in ev.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.suffix.lower() == ".bin":
                self.log_list.scan(str(p.parent), modal=False)
                self.load(str(p))
                break

    # ---------------- 加载 ----------------
    def _on_load_progress(self, path: str, fraction: float, message: str) -> None:
        """解析进度同时送遮罩进度条和状态栏。

        两处显示是刻意的：遮罩有 180ms 延迟显示（防缓存命中时白闪），
        没显示的那段时间里状态栏是唯一能看到进展的地方。
        """
        self.set_status(f"{Path(path).name}: {message}")
        try:
            self.loading.set_progress(fraction, message)
        except Exception:                            # pragma: no cover
            pass

    def load(self, path: str, force: bool = False) -> None:
        self._stop_play("已停止播放（重新加载日志）")
        if self.worker is not None and self.worker.isRunning():
            self.set_status("上一个日志还在解析中，请稍候…")
            return
        self.set_status(f"开始解析 {Path(path).name}")
        QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WaitCursor))
        self.loading.begin(Path(path).name)
        self.worker = _LoadWorker(path, force, self.cache_dir, self)
        self.worker.progress.connect(lambda fr, msg: self._on_load_progress(path, fr, msg))
        self.worker.done.connect(self._on_loaded)
        self.worker.failed.connect(lambda m: self._on_failed(m, path))
        self.worker.start()

    def load_sync(self, path: str, force: bool = False) -> LogStore:
        """同步加载（自检/脚本用，不弹进度）。"""
        st = LogStore.open(path, cache_dir=self.cache_dir, force=force)
        self._on_loaded(st)
        return st

    def _on_failed(self, msg: str, path: Optional[str] = None) -> None:
        QtWidgets.QApplication.restoreOverrideCursor()
        self.loading.end()                 # 失败也必须收遮罩，不能一直转
        self.set_status(f"解析失败：{msg}")
        # 路径单独列一行并允许选中复制：中文目录名出问题时，这是唯一能看清真实字符串的地方
        text = "这份日志没能打开，界面上保留原来的状态。"
        detail = msg if not path else f"{msg}\n\n尝试打开的路径（可选中复制）：\n{path}"
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("解析失败")
        box.setText(text)
        box.setInformativeText(detail)
        box.setDetailedText(detail)
        box.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
                                   | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard)
        box.exec()

    def _on_loaded(self, store: LogStore) -> None:
        QtWidgets.QApplication.restoreOverrideCursor()
        if self.store is not None and self.store is not store:
            # 换日志：插件先收到“旧日志已卸载”，再收 log_loaded
            self._emit_plugin("log_closed")
        self.store = store
        self._win = None
        self._extra = []
        s = store.stats
        self.setWindowTitle(f"APMLogViewer {__version__} —— {store.name}")
        self.log_list.set_current(store.path)
        self.channel_tree.set_store(store)
        self._restore_extra_channels()
        self._events = store.events()
        self._alerts = self._compute_alerts()
        self.alert_table.set_alerts(self._alerts)
        self.event_table.set_events(self._events)
        self.param_table.set_params(store.params())
        self._refilter_params()
        self._apply()
        self.play_bar.set_available(True)
        self._refresh_channel_tree_plugins()
        t0, t1 = self.timeline.span()
        self.play_bar.set_readout(self._cursor_t or t0, t0, t1, False)
        # 用户 2026-10-24：默认打开日志显示**全部**轨迹（按钮回到"按下"，地图关掉跟随裁剪）
        self.play_bar.btn_all_tracks.setChecked(True)
        self.map.set_track_follow(False)
        self._feed_altitude()          # 先喂高度，再喂姿态：后者会把游标推到开头并统一刷新
        self._feed_battery()
        self._feed_attitude()
        crit = sum(1 for a in self._alerts if a.severity == "critical")
        err = sum(1 for a in self._alerts if a.severity == "error")
        warn = sum(1 for a in self._alerts if a.severity == "warn")
        self.set_status(
            f"{s.firmware or '未知固件'}｜{s.frame_type or '未知机型'}｜时长 {s.duration_str}"
            f"｜位置源 {s.pos_source or '无'}({s.pos_kind})｜参数 {s.n_params}｜报文 {s.n_messages}"
            f"｜告警 C{crit}/E{err}/W{warn}"
            + (f"｜★炸机 {len(s.crashes)} 处" if s.crashes else "")
            + self._track_quality_note(s)
            + (f"｜{'; '.join(s.warnings)}" if s.warnings else ""))

        # 界面填好了才收遮罩：早一步会看见“空图表闪一下再填数据”
        self.loading.end()
        self._emit_plugin("alerts_updated", [a.as_dict() for a in self._alerts])
        self._emit_plugin_log_loaded(store)

    # ---------------- 仪表 ----------------
    def _feed_attitude(self) -> None:
        """把 ANG/ATT 姿态时间线喂给地图左下角仪表；都没有就交给地迹方位退回。"""
        if self.store is None:
            self.map.set_attitude([], [], [], [])
            return
        for ty in ("ANG", "ATT"):                    # 4.x 用 ANG，老固件用 ATT
            try:
                got = self.store.series_many([f"{ty}.Pitch", f"{ty}.Roll", f"{ty}.Yaw"],
                                             max_points=6000)
            except Exception:                        # noqa: BLE001 取不到就试下一个报文
                log.debug("读 %s 姿态失败，尝试下一个报文类", ty, exc_info=True)
                continue
            by_field = {str(getattr(k, "field", "")): v for k, v in got.items()}
            need = ("Pitch", "Roll", "Yaw")
            if not all(f in by_field and by_field[f].t.size for f in need):
                continue
            n = min(by_field[f].t.size for f in need)
            t = by_field["Pitch"].t[:n]
            # 同一报文类三个字段共用时间轴，长度不一致时按最短截断，不插值造假数据
            self.map.set_attitude(t, by_field["Pitch"].v[:n],
                                  by_field["Roll"].v[:n], by_field["Yaw"].v[:n])
            if self._cursor_t is None:
                # 刚加载完还没人点过时刻：把游标放到区间开头。否则仪表会报
                # “该日志没有姿态报文”，而这份日志其实有 ANG —— 那是假话。
                self._on_time(self.timeline.span()[0])
            return
        self.map.set_attitude([], [], [], [])

    def _feed_altitude(self) -> None:
        """给仪表高度带备一条兑底时间线（只在没航迹时才会被用到）。

        有航迹时地图用的是航迹自带的高度，仪表再拿另一个源就会出现
        “同一个时刻两个不同数”，所以这里只当备胎。
        """
        if self.store is None:
            self.map.set_altitude([], [])
            return
        for key, src in (("BARO.Alt", "气压"), ("CTUN.Alt", "EKF")):
            try:
                got = self.store.series_many([key], max_points=6000)
            except Exception:                        # noqa: BLE001 取不到就试下一个
                log.debug("读 %s 失败，尝试下一个高度源", key, exc_info=True)
                continue
            for _, ser in got.items():
                if ser.t.size and np.isfinite(ser.v).any():
                    self.map.set_altitude(ser.t, ser.v, src)
                    return
        self.map.set_altitude([], [])

    def _feed_battery(self) -> None:
        """把电池电压/电流喂给左下角仪表。

        只取**原始测量量**（BATT.Volt / BATT.Curr）：RemPct 已退出判定（它是电压反推的估计值）
        估计值，拿它去标区域或下“电量偏低”的结论等于把估计当事实（用户 2026-10-24）。
        """
        if self.store is None:
            self.map.set_battery([], [])
            return

        def _ser(key: str):
            try:
                return self.store.series(key, max_points=6000)
            except Exception:                        # noqa: BLE001 缺这个通道太正常
                log.debug("读 %s 失败", key, exc_info=True)
                return None

        # 报文名是 **BAT**（Volt/Curr/RemPct…），BATT_ 只是参数前缀，不是报文名；
        # 写成 BATT.Volt 会直接 KeyError。BATT 作为兼容候选留在后面。
        for vkey, akey in (("BAT.Volt", "BAT.Curr"), ("BATT.Volt", "BATT.Curr")):
            v = _ser(vkey)
            if v is not None and v.t.size and np.isfinite(v.v).any():
                a = _ser(akey)
                self.map.set_battery(v.t, v.v, a.v if a is not None and a.v.size else None)
                return
        self.map.set_battery([], [])

    # ---------------- 视图刷新 ----------------
    def _refine_view(self, t0: float, t1: float) -> None:
        """滚轮放大后，把“只有包络”的道按当前视图回内核重新取一次数。

        预设对大数据量通道只给整窗包络（几千桶），以前一放大就画不出东西，
        表现为“曲线全不见了”。现在图表端保证不画空，这里再补上细节能看到。"""
        if self.store is None or not (math.isfinite(t0) and math.isfinite(t1)) or t1 <= t0:
            return
        try:
            need = self.plot.lanes_needing_refine()
        except Exception:
            log.debug("查询待细化泳道失败", exc_info=True)
            return
        if not need:
            return
        c0, c1 = need[0].get("cov") or (float("nan"), float("nan"))
        if math.isfinite(c0) and math.isfinite(c1) and c1 > c0:
            cspan = c1 - c0
            if (t1 - t0) >= 0.85 * cspan and t0 <= c0 + 0.05 * cspan and t1 >= c1 - 0.05 * cspan:
                return                      # 现有包络已经覆盖这个视图，不白取数
        self._refined_win = (t0, t1)
        done = 0
        for item in need:
            try:
                env = self.store.envelope(str(item["key"]), t0, t1, buckets=2400)
            except Exception as exc:                        # 单道失败不能影响其他道
                log.debug("按视图取包络失败 %s: %s", item["key"], exc)
                continue
            try:
                if self.plot.refine_envelope(int(item["index"]), env.t, env.vmin, env.vmax, env.vmean):
                    done += 1
            except Exception:
                log.debug("写回包络失败", exc_info=True)
        if done:
            self.set_status(f"已按当前视图重新取数 {t0:.1f}–{t1:.1f}s（{done} 道）")

    def _open_settings(self) -> None:
        """设置对话框：里面改一项就落盘并即时生效，不用重启。"""
        dlg = SettingsDialog(self, cells_provider=self.cells_info,
                             themes_provider=self.plugins.themes_available)
        dlg.exec()

    # ---------------- 插件 ----------------

    def _emit_plugin(self, name: str, *args: Any) -> None:
        """把宿主事件推给插件。插件槽函数抛出也不许冒回主流程。"""
        try:
            sig = getattr(self.plugins.signals, name, None)
            if sig is not None:
                sig.emit(*args)
        except Exception:                            # noqa: BLE001
            log.debug("插件信号 %s 发送失败", name, exc_info=True)

    def _emit_plugin_log_loaded(self, store: LogStore) -> None:
        """日志加载完成事件（纯数据，不带 store 对象）。"""
        try:
            s = store.stats
            self._emit_plugin("log_loaded", {
                "name": store.name, "path": str(store.path),
                "stats": {"firmware": s.firmware, "frame_type": s.frame_type,
                          "duration": s.duration, "t_start": s.t_start, "t_end": s.t_end,
                          "n_messages": s.n_messages, "n_params": s.n_params,
                          "pos_source": s.pos_source, "mode_seq": list(s.mode_seq)},
                "types": list(store.types()), "n_alerts": len(self._alerts),
            })
        except Exception:                            # noqa: BLE001
            log.debug("发送 log_loaded 失败", exc_info=True)

    def _emit_mode_change(self, t: float) -> None:
        """当前飞行模式变了才发（每帧都发会把插件刷爆）。"""
        try:
            mode = self.timeline.current_mode(float(t))
        except Exception:                            # noqa: BLE001
            return
        if mode and mode != self._last_mode:
            self._last_mode = str(mode)
            self._emit_plugin("mode_changed", str(mode))

    def _on_plugin_error(self, pid: str, msg: str) -> None:
        self.set_status(f"插件 {pid}：{msg}", temporary=True)

    def _load_plugins_safely(self) -> None:
        """插件系统自身故障（目录不可读、缓存损坏……）也不许影响主程序启动。"""
        try:
            self.plugins.load_all()
        except Exception:                            # noqa: BLE001
            log.exception("插件系统初始化失败，按无插件继续")
        self._sync_plugin_ui()

    def _sync_plugin_ui(self) -> None:
        """插件注册表变了（加载/停用/重载）→ 把四处界面刷一致。"""
        for step in (self._refresh_preset_box, self._sync_plugin_tabs,
                     self._refresh_map_layers, self._refresh_plot_tools,
                     self._refresh_channel_tree_plugins, self._sync_active_theme):
            try:
                step()
            except Exception:                        # noqa: BLE001 单个环节失败继续做下一个
                log.exception("同步插件界面失败：%s", getattr(step, "__name__", step))

    def _refresh_preset_box(self) -> None:
        """内置预设 + 插件预设（保住当前选择，否则重载插件会把用户看的泳道换掉）。"""
        cur = str(self._preset)
        self.preset_box.blockSignals(True)
        try:
            self.preset_box.clear()
            for pr in PRESETS:
                self.preset_box.addItem(pr.name, pr.id)
            self._plugin_presets = {}
            for _pid, pr in self.plugins.presets():
                pid = str(getattr(pr, "id", ""))
                if not pid or pid in PRESET_BY_ID:
                    continue                     # 与内置撞 id 的已在注册时拒掉，这里再措一道
                self._plugin_presets[pid] = pr
                self.preset_box.addItem(f"{getattr(pr, 'name', pid)}（插件）", pid)
            i = self.preset_box.findData(cur)
            self.preset_box.setCurrentIndex(max(0, i))
        finally:
            self.preset_box.blockSignals(False)

    def _sync_plugin_tabs(self) -> None:
        """插件标签页增删（停用插件 => 它的页马上消失）。

        页不直接放插件控件，而是套一层 `_PluginPageHost`：第三方控件常带很大的
        minimumWidth（聊天面板、宽表格），直接 addTab 会把 QTabWidget 的最小宽顶到
        上千像素，横向分隔条就只能把左侧栏压扁，而且再也拉不回来（分隔条不能让
        右侧小于它的最小宽）。套滚动宿主后，页的最小宽只看滚动条。
        """
        want: list[tuple[str, str, Any]] = []
        for pid, title, w in self.plugins.tabs():
            entry = self._plugin_hosts.get(w)
            if entry is None:
                entry = (pid, _PluginPageHost(w))
                self._plugin_hosts[w] = entry
            want.append((pid, title, entry[1]))
        keep = {host for _p, _t, host in want}
        for i in reversed(range(self.tabs.count())):
            w = self.tabs.widget(i)
            if w in self._plugin_widgets and w not in keep:
                self.tabs.removeTab(i)
                w.setParent(None)          # 宿主留着：插件可能再次注册同一个控件
        for _pid, title, host in want:
            if self.tabs.indexOf(host) < 0:
                self.tabs.addTab(host, title)
        self._plugin_widgets = keep
        # 注：不回收宿主。宿主按“插件控件对象”缓存，插件停用后再启用往往拿同一个控件重新
        # 注册；此时去删旧宿主很容易把已经交给插件用的控件一并 deleteLater（悬空对象崩
        # 溃）。一个宿主就是一块隐藏的壳，留着比删错便宜。

    def _refresh_plot_tools(self) -> None:
        """曲线右键菜单里的“插件工具”条目。"""
        names = [name for _pid, name, _fn in self.plugins.plot_tools()]
        self.plot.set_plot_tools(names)

    def _sync_active_theme(self) -> None:
        """插件主题的“活性”检查：能用的就应用，失效的就回退默认。

        覆盖两种场景：
        * 启动时设置里存着插件主题，但那时插件还没加载（palette 暂时是默认）——加载完补上；
        * 主题插件被停用/卸载/重载失败——必须马上换回内置主题，不能挂着不存在的外观。
        """
        cur = str(get_settings().get("ui_theme"))
        if not ui_theme.is_plugin_theme(cur):
            return
        if self.plugins.theme_by_value(cur) is None:
            self._theme_fallback(cur, "提供该主题的插件已停用或已移除")
            return
        self._apply_ui_theme(push_curve=True)

    def _refresh_map_layers(self) -> None:
        """跑一遍插件地图图层，把校验后的图元交给地图。"""
        if not hasattr(self.map, "set_plugin_layers"):
            return
        layers: list[dict[str, Any]] = []
        if self.store is not None:
            try:
                layers = self.plugins.map_layer_primitives()
            except Exception:                        # noqa: BLE001
                log.exception("插件地图图层失败")
        self.map.set_plugin_layers(layers)

    def _refresh_channel_tree_plugins(self) -> None:
        """把合成通道挂到通道树（独立一组，不混在真报文里）。"""
        chans: list[dict[str, Any]] = []
        for _pid, syn in self.plugins.synthetics():
            try:
                n = self.plugins.synthetic_count(syn)
            except Exception:                        # noqa: BLE001
                n = 0
            chans.append({"key": syn.key, "label": syn.display(),
                          "unit": syn.unit, "count": n})
        self.channel_tree.set_plugin_channels(chans)

    def _open_plugins(self) -> None:
        dlg = PluginDialog(self.plugins, self)
        dlg.exec()
        self._sync_plugin_ui()
        if self.store is not None:
            self._apply(keep_window=True)

    def _on_plot_tool(self, name: str, payload: Any) -> None:
        """曲线右键点了插件工具：组装选区 → 沙箱调用 → 有文本就弹窗。"""
        if not isinstance(payload, dict):
            return
        try:
            sel = PlotSelection(
                lane_index=int(payload.get("lane_index", -1)),
                lane_label=str(payload.get("lane_label", "")),
                keys=tuple(str(k) for k in (payload.get("keys") or ())),
                t0=float(payload.get("t0", math.nan)),
                t1=float(payload.get("t1", math.nan)),
                whole=bool(payload.get("whole", True)))
        except (TypeError, ValueError):
            return
        for pid, tool_name, fn in self.plugins.plot_tools():
            if tool_name != str(name):
                continue
            res = self.plugins.run_plot_tool(pid, fn, sel)
            if res is FAILED or res is None:
                continue
            text = res if isinstance(res, str) else str(res)
            if text.strip():
                QtWidgets.QMessageBox.information(self, str(name), text[:4000])

    # ---- 插件控制口（插件调用宿主的唯一入口）----
    def plugin_open_log(self, path: str) -> bool:
        p = Path(str(path))
        if not p.exists():
            self.set_status(f"插件请求打开的日志不存在：{p.name}")
            return False
        self.load(str(p))
        return True

    def plugin_seek(self, t: float) -> bool:
        if self.store is None:
            return False
        try:
            t0, t1 = self.timeline.span()
            tv = float(t)
        except Exception:                            # noqa: BLE001
            return False
        if not (math.isfinite(tv) and math.isfinite(t0) and math.isfinite(t1) and t0 <= tv <= t1):
            return False
        self._on_time(tv)
        return True

    def plugin_zoom(self, t0: float, t1: float) -> bool:
        if self.store is None:
            return False
        try:
            a, b = sorted((float(t0), float(t1)))
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(a) and math.isfinite(b)) or b <= a:
            return False
        self._on_region(a, b)
        return True

    def plugin_play(self, on: bool) -> bool:
        if self.store is None:
            return False
        self._toggle_play(bool(on))
        return True

    def plugin_set_preset(self, preset_id: str) -> bool:
        i = self.preset_box.findData(str(preset_id))
        if i < 0:
            return False
        self.preset_box.setCurrentIndex(i)
        return True

    def plugin_add_channel(self, key: str) -> bool:
        try:
            k = ChannelKey.parse(str(key))
        except ValueError:
            return False
        self._add_channel(k)
        return True

    def plugin_select_tab(self, name: str) -> bool:
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == str(name):
                self.tabs.setCurrentIndex(i)
                return True
        return False

    def plugin_refresh(self) -> bool:
        self._apply(keep_window=True)
        return True

    def plugin_export_png(self, path: str) -> bool:
        try:
            return bool(self.plot.export_png(str(path)))
        except Exception:                            # noqa: BLE001
            log.exception("插件导出 PNG 失败")
            return False

    def plugin_apply_theme(self, value: str) -> bool:
        """插件请求换主题（主题插件/普通插件都能调）。失败已就地回退默认主题。"""
        return bool(self._apply_theme_value(str(value), push_curve=True))

    def _alert_overrides(self) -> dict[str, dict[str, float]]:
        """界面「设置 → 告警阈值」对 yaml 的覆盖层。

        yaml 仍是配置文件真源（命令行、无界面场景照旧读它）；界面只覆盖用户在
        设置里能改的那几项，且两个阀值不会调反（严重必须不低于警告）。
        """
        s = get_settings()
        out: dict[str, dict[str, float]] = {}
        try:
            warn = float(s.get("vibe_warn"))
            crit = float(s.get("vibe_crit"))
        except (TypeError, ValueError):
            pass
        else:
            out["vibration"] = {"rms_warn": warn, "rms_crit": max(crit, warn)}

        # 电池：0 = 自动/不启用 → **不放进覆盖层**，这样 yaml 里的参考值还能当下限兜底
        batt: dict[str, float] = {}
        try:
            ncells = int(float(s.get("cell_count") or 0))
        except (TypeError, ValueError):
            ncells = 0
        if ncells > 0:
            batt["cell_count"] = float(ncells)   # 手填电芯数：引擎里优先于自动识别
        for key in ("cell_warn", "cell_crit", "pack_warn_v", "pack_crit_v"):
            try:
                v = float(s.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                batt[key] = v
        if batt:
            # 欠压是"越低越严重"：严重档不许高于警告档，否则警告区间被吃掉
            if "cell_warn" in batt and "cell_crit" in batt:
                batt["cell_crit"] = min(batt["cell_crit"], batt["cell_warn"])
            if "pack_warn_v" in batt and "pack_crit_v" in batt:
                batt["pack_crit_v"] = min(batt["pack_crit_v"], batt["pack_warn_v"])
            out["battery"] = batt
        return out

    def cells_info(self) -> dict:
        """当前日志按几 S 判定（给设置对话框的"当前采用"用）。

        只读：先看界面里手填的，再退到自动识别；识别不出来就返回 cells=None，
        让对话框明确告诉用户"去手填"，绝不假装有个默认串数。
        """
        if self.store is None:
            return {"cells": None, "src": "none", "detail": "还没有打开日志"}
        try:
            from apmlog.core.alerts import resolve_cells
            sem = dict((getattr(self.store, "meta", {}) or {}).get("bat_semantics") or {})
            cfg = {"cell_count": get_settings().get("cell_count") or 0}
            n, src, detail = resolve_cells(cfg, sem)
            return {"cells": n, "src": src, "detail": detail,
                    "mode": sem.get("mode"), "volt_max": sem.get("volt_max")}
        except Exception:
            log.debug("取电芯数信息失败", exc_info=True)
            return {"cells": None, "src": "none", "detail": "读取电池信息失败"}

    def _compute_alerts(self) -> list:
        """按当前阈值计算告警（失败时返回空并报告，不把界面弄黑）。"""
        if self.store is None:
            return []
        try:
            hits = list(self.store.alerts(overrides=self._alert_overrides()))
        except TypeError:
            # 旧版 store 不认 overrides：退回原调用，别因为阈值功能把告警弄丢
            try:
                hits = list(self.store.alerts())
            except Exception as exc:            # noqa: BLE001
                log.exception("告警计算失败")
                self.set_status(f"告警计算失败：{type(exc).__name__}: {exc}")
                return []
        except Exception as exc:                # noqa: BLE001
            log.exception("告警计算失败")
            self.set_status(f"告警计算失败：{type(exc).__name__}: {exc}")
            return []
        # 插件告警规则：引擎自己的结果排序在前，插件的不抢宿主结论；单条出错只丢它自己
        try:
            hits += self.plugins.run_alert_rules()
        except Exception:                       # noqa: BLE001 插件故障不许把告警页清空
            log.exception("插件告警规则异常")
        return hits

    def _reload_alerts(self) -> None:
        """改阈值后立即重算，并只刷新**与告警有关**的四处（告警页/时间线/地图/曲线区域）。

        不走 `_apply()`：那会把所有泳道重新取数、重画整窗（实测 1 s），
        调一个阈值没必要付这个代价。窗口用当前时间线可见区间。
        """
        if self.store is None:
            return
        self._alerts = self._compute_alerts()
        self.alert_table.set_alerts(self._alerts)
        rg = [a.as_dict() for a in self._alerts]
        try:
            t0, t1 = self.timeline.span()
        except Exception:                            # pragma: no cover
            t0, t1 = 0.0, float("inf")
        self.timeline.set_regions(rg)
        self.map.set_regions(rg)
        try:
            self.plot.set_regions([r for r in rg
                                   if r.get("t1", r["t0"]) >= t0 and r["t0"] <= t1])
        except Exception:                            # pragma: no cover
            log.debug("刷新曲线告警区域失败", exc_info=True)
        crit = sum(1 for a in self._alerts if a.severity == "critical")
        err = sum(1 for a in self._alerts if a.severity == "error")
        warn = sum(1 for a in self._alerts if a.severity == "warn")
        s = get_settings()
        info = self.cells_info()
        cells_txt = (f"{info['cells']}S {'手动' if info.get('src') == 'manual' else '自动'}"
                     if info.get("cells") else "电芯数未识别")
        self.set_status(f"告警已重算：振动 {float(s.get('vibe_warn')):g}/"
                        f"{float(s.get('vibe_crit')):g} m/s²｜单芯 {float(s.get('cell_warn')):g}/"
                        f"{float(s.get('cell_crit')):g} V（{cells_txt}） → C{crit}/E{err}/W{warn}")
        self._emit_plugin("alerts_updated", [a.as_dict() for a in self._alerts])

    def _apply_setting(self, key: str, value: Any) -> None:
        """设置变化 → 推到对应组件。对话框、曲线页控件、右键菜单都能改，这里统一收。"""
        self._emit_plugin("settings_changed", str(key), value)
        if key == "lane_min_h":
            if int(self.lane_h_box.value()) != int(value):
                self.lane_h_box.setValue(int(value))
            self.plot.set_lane_min_height(int(value))
        elif key == "theme":
            self.plot.set_theme(str(value))
        elif key == "ui_theme" or key in ui_theme.CUSTOM_KEYS:
            self._apply_ui_theme(push_curve=True)
        elif key == "badge_tol_px":
            self.plot.set_badge_tol_px(float(value))
        elif key == "refine_debounce_ms":
            self.plot.set_refine_debounce_ms(int(value))
        elif key == "mode_colors":
            self._push_mode_colors()
        elif key == "gauge_show":
            self.map.set_gauges_visible(bool(value))
        elif key == "play_rate":
            self.play_bar.set_rate(float(value))
        elif key == "play_follow":
            if self.play_bar.chk_follow.isChecked() != bool(value):
                self.play_bar.chk_follow.setChecked(bool(value))
        elif key == "tip_font_pt":
            apply_tip_font(QtWidgets.QApplication.instance(), float(value))
        elif key in ("vibe_warn", "vibe_crit", "cell_count", "cell_warn",
                     "cell_crit", "pack_warn_v", "pack_crit_v"):
            self._alert_timer.start()          # 合并连续改动，停手 250ms 后重算一次

    def _on_system_scheme(self, *_args: Any) -> None:
        if str(get_settings().get("ui_theme")) == "system":
            self._apply_ui_theme(push_curve=True)

    def _refresh_hint_colors(self) -> None:
        """把少数写死的辅助色按当前外壳底色重算一遗，避免“暗底暗字”。"""
        try:
            self.skipped_label.setStyleSheet(
                f"color:{ui_theme.readable_on(self.skipped_label, '#8a6d1b')};")
        except Exception:                              # noqa: BLE001 提示色算失败不能影响主流程
            log.debug("辅助文字配色重算失败", exc_info=True)

    def _apply_ui_theme(self, push_curve: bool = False) -> None:
        """把界面主题刷到整个应用（内置或插件）。``push_curve`` 时把曲线配色写回设置。

        插件主题出任何问题都回退内置默认主题，并把出错的插件停用——
        绝不允许“界面换了一半”或“主窗起不来”。
        """
        if getattr(self, "_applying_theme", False):
            return                                     # 主题回退时设 setting 会再进来
        self._applying_theme = True
        try:
            value = str(get_settings().get("ui_theme"))
            try:
                self._apply_theme_value(value, push_curve=push_curve)
            except Exception as exc:                   # noqa: BLE001 换肤失败不能弄挂主窗
                log.exception("界面主题应用异常: %s", exc)
                self._theme_fallback(value, f"应用时异常：{type(exc).__name__}: {exc}")
        finally:
            self._applying_theme = False

    def _apply_theme_value(self, value: str, push_curve: bool = True) -> bool:
        """应用一个主题值；插件主题失败时就地回退并停用它（返回 False）。"""
        s = get_settings()
        app = QtWidgets.QApplication.instance()
        text = str(value or "")
        if ui_theme.is_plugin_theme(text):
            found = self.plugins.theme_by_value(text)
            if found is None:
                self._theme_fallback(text, "主题不存在（插件未加载、已停用或已移除）")
                return False
            owner, spec = found
            try:
                colors = ui_theme.spec_from_theme(spec)
                curve = ui_theme.apply_spec(app, colors, spec.curve)
            except Exception as exc:                   # noqa: BLE001 主题不受支持
                self._theme_fallback(text, f"主题「{spec.name}」不受支持：{exc}", owner)
                return False
            if push_curve and curve and str(s.get("theme")) != curve:
                s.set("theme", curve)
            s.set("ui_theme", text)
            self._refresh_hint_colors()
            self._emit_plugin("theme_changed", text)
            return True

        try:
            curve = ui_theme.apply(app, text, {k: s.get(k) for k in ui_theme.CUSTOM_KEYS})
        except Exception as exc:                       # noqa: BLE001
            log.warning("内置主题应用失败: %s", exc)
            self._theme_fallback(text, f"内置主题应用失败：{exc}")
            return False
        if push_curve and curve and str(s.get("theme")) != curve:
            s.set("theme", curve)
        s.set("ui_theme", text)
        self._refresh_hint_colors()
        self._emit_plugin("theme_changed", text)
        return True

    def _theme_fallback(self, value: str, reason: str, owner: str = "") -> None:
        """插件主题不可用：立即回退内置默认主题、记日志、只停用出事的插件。"""
        log.warning("主题不可用，回退默认（%s）：%s", value, reason)
        self._emit_plugin("theme_error", str(value), str(reason))
        try:
            s = get_settings()
            s.set("ui_theme", ui_theme.DEFAULT_THEME)
            ui_theme.apply(QtWidgets.QApplication.instance(), ui_theme.DEFAULT_THEME,
                           {k: s.get(k) for k in ui_theme.CUSTOM_KEYS})
        except Exception:                              # noqa: BLE001 回退本身失败也要可预测
            log.exception("回退默认主题失败")
        self._refresh_hint_colors()
        if owner:
            try:
                self.plugins.disable(owner, f"主题不可用：{reason}")
            except Exception:                          # noqa: BLE001
                log.exception("停用主题插件失败：%s", owner)
        self.set_status(f"主题不可用，已回退默认主题：{reason}")
        self._emit_plugin("theme_changed", ui_theme.DEFAULT_THEME)

    def _lane_height_changed(self, v: int) -> None:
        self.plot.set_lane_min_height(v)
        # 每道最小高是“工具偏好”，归设置管（跨日志有数），不再挤在视图记忆里
        Settings.instance().set("lane_min_h", int(v))

    def _preset_changed(self, i: int) -> None:
        self._preset = self.preset_box.itemData(i)
        self._apply(keep_window=True)
        self._save_view()

    def _apply(self, keep_window: bool = False) -> None:
        if self.store is None:
            return
        t_start = float(self.store.meta.get("stats", {}).get("t_start", 0.0) or 0.0)
        t_end = float(self.store.meta.get("t_end", 0.0) or 0.0)
        win = self._win if (keep_window and self._win) else (t_start, t_end)
        try:
            # 通道源认插件合成通道：插件预设里引用自己算的通道也能出图
            src = self.plugins.channel_source(self.store)
            preset_arg = self._plugin_presets.get(str(self._preset), self._preset)
            lanes, skipped = build_lanes(src, preset_arg, win[0], win[1])
        except Exception as exc:
            log.exception("预设取数失败")
            self.set_status(f"预设取数失败：{type(exc).__name__}: {exc}")
            return
        for k in self._extra:
            spec = self._spec_for_key(k, win[0], win[1], suffix="（手动）")
            if spec is None:
                skipped.append(f"{k.label}（手动添加失败: 无有效点）")
            else:
                lanes.append(spec)
        # 「叠加绘制」的分组：每组一条泳道，第 1 条主曲线 + 其余同轴叠加
        lanes.extend(self._group_lane_specs(win, skipped))
        ev_dicts = [e.as_dict() for e in self._events]
        rg_dicts = [a.as_dict() for a in self._alerts]
        self.plot.set_lanes(lanes, t_span=win)
        self._apply_lane_overlays(win)          # set_lanes 会清空运行时叠加，这里补回
        self._refined_win = None
        self.plot.set_events([e for e in ev_dicts if win[0] <= float(e["t"]) <= win[1]])
        self.plot.set_regions([r for r in rg_dicts if r.get("t1", r["t0"]) >= win[0]
                               and r["t0"] <= win[1]])
        self.skipped_label.setText("已跳过：" + "、".join(skipped) if skipped else "")
        # 多轨迹：一次把全部位置源交给地图（主轨迹跟光标，其余可勾选对比）。
        # 数据层不支持 tracks() 时优雅退回单轨迹，不让旧缓存或半成品把界面弄黑。
        try:
            trs = self.store.tracks() if hasattr(self.store, "tracks") else []
        except Exception:                                     # noqa: BLE001
            log.exception("读取多轨迹失败，退回单轨迹")
            trs = []
        if trs:
            self.map.set_tracks(trs)
        else:
            self.map.set_track(self.store.track())
        if self._track_vis is not None:
            self.map.legend.set_visible(self._track_vis)
            self.map._on_legend(self._track_vis)
        self.map.set_events(self._events_with_pos(ev_dicts))
        self.map.set_regions(rg_dicts)
        self.timeline.set_span(*win)
        self.timeline.set_modes(self.store.modes())
        self._push_mode_colors()
        self.timeline.set_events(ev_dicts)
        self.timeline.set_regions(rg_dicts)
        self.timeline.set_selection(*self._win) if self._win else self.timeline.set_selection(None, None)
        if not self._playing:
            self.play_bar.set_readout(self._cursor_t or win[0], win[0], win[1], False)

    def _events_with_pos(self, ev_dicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给事件补上经纬度（只在有 geodetic 轨迹时）。"""
        tr = self.store.track() if self.store else None
        if tr is None or not tr.has_geodetic or tr.n_points < 2:
            return ev_dicts
        out = []
        for e in ev_dicts:
            t = float(e.get("t", float("nan")))
            d = dict(e)
            if math.isfinite(t) and tr.t[0] <= t <= tr.t[-1]:
                d["lat"] = float(np.interp(t, tr.t, tr.lat))
                d["lon"] = float(np.interp(t, tr.t, tr.lon))
            out.append(d)
        return out

    # ---------------- 联动 ----------------
    def _on_timeline_event(self, e: object) -> None:
        """时间线点击菜单里选了某条事件 → 切到「事件」页并把那一行选出来。"""
        idx = self.tabs.indexOf(self.event_table)
        if idx >= 0:
            self.tabs.setCurrentIndex(idx)
        if not isinstance(e, dict) or not e:
            return
        try:
            t = float(e.get("t", float("nan")))
        except (TypeError, ValueError):
            return
        if math.isfinite(t):
            self._on_time(t)
        row = self.event_table.row_for(t, str(e.get("kind") or ""),
                                      str(e.get("display") or e.get("text") or ""))
        if row >= 0:
            item = self.event_table.item(row, 0)
            if item is not None:
                self.event_table.scrollToItem(item)
            self.event_table.setCurrentCell(row, 0)
            self.set_status(f"已定位到「事件」页第 {row + 1} 行（t={t:.3f}s）")
        else:
            self.set_status(f"已切到「事件」页（t={t:.3f}s 没找到对应行）")

    def _on_time(self, t: float) -> None:
        if self.store is None or not math.isfinite(t):
            return
        self._cursor_t = float(t)
        self.plot.set_time(t)
        self.map.set_time(t)
        self.timeline.set_time(t)
        tr = self.store.track()
        extra = ""
        if tr.n_points > 1 and tr.t[0] <= t <= tr.t[-1]:
            i = int(np.searchsorted(tr.t, t))
            extra = (f"｜高度 {tr.alt[i] if tr.has_geodetic else tr.u[i]:.1f}m"
                     f" 速度 {tr.speed[i]:.1f}m/s")
        self.set_status(f"游标 t={t:.2f}s{extra}", temporary=True)
        self._emit_plugin("time_changed", float(t))
        self._emit_mode_change(t)

    # ---------------- 播放 ----------------
    def _push_mode_colors(self) -> None:
        """把"模式 → 颜色"推给时间线（含日志里出现、但阶梯没登记的模式）。"""
        modes: list[str] = []
        if self.store is not None:
            try:
                modes = [str(m[2]) for m in self.store.modes()]
            except Exception:                            # noqa: BLE001
                log.debug("取模式列表失败", exc_info=True)
        try:
            self.timeline.set_mode_colors(
                get_settings().mode_color_palette(modes))
        except Exception:                                # noqa: BLE001 主题色失败不能弄挂主窗
            log.debug("推送模式配色失败", exc_info=True)

    def _track_mode_changed(self, full: bool) -> None:
        """「全部轨迹显示」按下=完整轨迹；弹起=跟随时间线（只画到游标处）。"""
        try:
            self.map.set_track_follow(not bool(full))
        except Exception:                            # noqa: BLE001 老地图组件没有该能力
            log.debug("地图不支持轨迹跟随", exc_info=True)
            self.set_status("当前地图组件不支持轨迹跟随")
            return
        if not full:
            cur = self._cursor_t
            if cur is None or not math.isfinite(float(cur)):
                t0, _t1 = self.timeline.span()
                cur = float(t0)
            self.map.set_time(float(cur))
        self.set_status("地图：完整轨迹" if full
                        else "地图：轨迹跟随时间线（只画到游标处）", temporary=True)

    def _on_timeline_seek(self, t: float) -> None:
        """鼠标点时间线：先停下播放再定位，否则人和播放器会抢游标。"""
        if self._playing:
            self._stop_play("已暂停（手动定位）")
        self._on_time(t)

    def _toggle_play(self, on: bool) -> None:
        if on:
            self._start_play()
        else:
            self._stop_play("已暂停")

    def _start_play(self) -> None:
        if self.store is None:
            self.play_bar.set_playing(False)
            self.set_status("先加载一份日志再播放")
            return
        t0, t1 = self.timeline.span()
        cur = self._cursor_t
        start = float(cur) if cur is not None and t0 <= float(cur) < t1 else float(t0)
        self._play_t = start
        self._play_rate = self.play_bar.rate()
        self._play_wall = 0.0
        self._playing = True
        self._play_clock.start()
        self._play_timer.start()
        self.play_bar.set_playing(True)
        map_i, plot_i = self.tabs.indexOf(self.map), 1
        if self.tabs.currentIndex() not in (map_i, plot_i):
            self.tabs.setCurrentIndex(map_i)   # 播放主要为了看地图上的飞机走
        self.set_status(f"播放中 {self._play_rate:g}×：从 t={start:.2f}s 起，到 t={t1:.1f}s 停")
        self._emit_plugin("play_state", True)

    def _stop_play(self, reason: str = "") -> None:
        was = self._playing
        self._playing = False
        self._play_timer.stop()
        self.play_bar.set_playing(False)
        if was:                                      # 没在播就不要刷读数：刚启动时区间还是默认的 (0,1)
            t0, t1 = self.timeline.span()
            cur = self._cursor_t if self._cursor_t is not None else t0
            self.play_bar.set_readout(cur, t0, t1, False)
        if reason:
            self.set_status(reason, temporary=True)
        if was:
            self._emit_plugin("play_state", False)

    def _play_step(self) -> None:
        if not self._playing:
            self._play_timer.stop()
            return
        if self.store is None:
            self._stop_play("已停止（日志已关闭）")
            return
        t0, t1 = self.timeline.span()
        now = self._play_clock.elapsed() / 1000.0
        dt = max(0.0, now - self._play_wall) * self._play_rate
        self._play_wall = now
        t = self._play_t + dt
        if t0 > t or t1 <= t:                        # 缩放窗口变了，或跑到末尾
            if t1 <= t:
                self._play_t = t1
                self._on_time(t1)
                self.play_bar.set_readout(t1, t0, t1, False)
                self._stop_play("已到当前区间末尾，自动暂停")
            else:
                self._stop_play("当前区间不包含游标，已暂停")
            return
        self._play_t = t
        self._on_time(t)
        self.play_bar.set_readout(t, t0, t1, True)

    def _play_rate_changed(self, rate: float) -> None:
        # 变速不能跳位：把当前时刻当成新速率的起点重新走时
        self._play_rate = float(rate)
        self._play_wall = self._play_clock.elapsed() / 1000.0 if self._play_clock.isValid() else 0.0
        get_settings().set("play_rate", float(rate))
        t0, t1 = self.timeline.span()
        cur = self._play_t if self._playing else (self._cursor_t or t0)
        self.play_bar.set_readout(cur, t0, t1, self._playing)

    def _play_follow_changed(self, on: bool) -> None:
        try:
            self.map.set_playback_follow(bool(on))
        except Exception:                            # noqa: BLE001
            log.warning("地图跟随开关失败", exc_info=True)
            return
        get_settings().set("play_follow", bool(on))
        self.set_status("地图视野将跟随飞机" if on else "地图视野不再跟随，仅移动游标",
                        temporary=True)

    def _on_region(self, t0: float, t1: float) -> None:
        if t1 <= t0:
            return
        self._win = (t0, t1)
        self._apply(keep_window=True)
        self.set_status(f"已收敛到区间 {t0:.1f}s – {t1:.1f}s（点“复位视图”回到全程）")
        self._emit_plugin("selection_changed", {"t0": float(t0), "t1": float(t1),
                                                "whole_log": False})

    def _reset_window(self) -> None:
        self._win = None
        self._apply()
        # 回到全段也要告知插件（否则它们以为还在刚才的选区里）
        t0 = t1 = float("nan")
        try:
            if self.store is not None:
                t0, t1 = float(self.store.stats.t_start), float(self.store.stats.t_end)
        except Exception:                            # noqa: BLE001 取不到就给 nan，不编造
            pass
        self._emit_plugin("selection_changed", {"t0": t0, "t1": t1, "whole_log": True})

    def _zoom_to_selection(self) -> None:
        try:
            t0, t1 = self.plot.get_region()
        except Exception:
            return
        if t1 > t0:
            self._on_region(t0, t1)

    # ---------------- 叠加绘制 ----------------

    def _spec_for_key(self, k: ChannelKey, t0: float, t1: float, suffix: str = "",
                      color: Optional[tuple[int, int, int]] = (10, 10, 10)
                      ) -> Optional[LaneSpec]:
        """一条通道 → LaneSpec；取不到（0 点/全 nan/异常）返回 None，绝不抛。

        color=None 表示"交给 set_lanes 按主题调色板分配"（叠加曲线用默认叠加色）。
        """
        if self.store is None:
            return None
        try:
            sd = self.plugins.channel_source(self.store).series(k, t0, t1, max_points=20000)
        except Exception as exc:                       # noqa: BLE001
            log.debug("取通道失败 %s: %s", k.key, exc)
            return None
        if sd.n == 0 or not np.isfinite(sd.v).any():
            return None
        spec = LaneSpec.from_series(sd, color if color is not None else (0, 0, 0))
        spec.label = k.label + suffix
        spec.key = k.key
        spec.note = k.key
        if color is None:
            spec.color = None
        return spec

    def _open_overlay_dialog(self) -> None:
        """「叠加绘制」：把若干通道叠成一条泳道，或叠到已有泳道上。"""
        if self.store is None:
            self.set_status("先打开一份日志，再叠加绘制")
            return
        try:
            infos = self.store.channel_infos()
        except Exception as exc:                       # noqa: BLE001
            log.exception("读取通道列表失败")
            self.set_status(f"读取通道列表失败：{type(exc).__name__}: {exc}")
            return
        chans = [(c.key.key,
                  f"{c.key.type}.{c.key.field}" + (f" [#{c.key.inst}]"
                                                   if c.key.inst is not None else ""))
                 for c in infos]
        pre: list = []
        try:
            pre = list(self.channel_tree.selected_keys())
        except Exception:                              # noqa: BLE001
            log.debug("读取通道树选中项失败", exc_info=True)
        dlg = OverlayDialog(chans, self.plot.lane_labels(), pre, self)
        dlg.setWindowTitle("叠加绘制")
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        keys: list[ChannelKey] = []
        for raw in dlg.checked_keys():
            try:
                k = ChannelKey.parse(str(raw))
            except ValueError:
                continue
            if k not in keys:
                keys.append(k)
        if not keys:
            return
        if dlg.is_new_lane():
            self._add_overlay_group(keys)
        else:
            self._add_lane_overlays(dlg.target(), keys)

    def _group_lane_specs(self, win: tuple[float, float],
                          skipped: Optional[list] = None) -> list[LaneSpec]:
        """把「叠加绘制」的分组翻成 LaneSpec：每组一条泳道，其余同轴叠加。

        抽成独立方法是为了能单独测（`_apply` 里那一段以前无法在 offscreen 下验证）。
        """
        out: list[LaneSpec] = []
        sink = skipped if skipped is not None else []
        for grp in self._groups:
            if not grp:
                continue
            head = self._spec_for_key(grp[0], win[0], win[1], color=None)
            if head is None:
                sink.append(f"{grp[0].label}（叠加绘制主曲线取数失败）")
                continue
            for j, k in enumerate(grp[1:]):
                ov = self._spec_for_key(k, win[0], win[1], color=None)
                if ov is None:
                    sink.append(f"{k.label}（叠加绘制取数失败）")
                    continue
                ov.color = OVERLAY_COLORS[j % len(OVERLAY_COLORS)]
                ov.hidden = False          # 用户主动叠加的，默认就显示
                head.overlays.append(ov)
            out.append(head)
        return out

    def _add_overlay_group(self, keys: list[ChannelKey]) -> None:
        """新建一条叠加泳道：第 1 条主曲线，其余同轴叠加（先不重建，直接追加更跟手）。"""
        if len(keys) < 2:
            return
        self._groups.append(list(keys))
        self._apply(keep_window=True)
        self.tabs.setCurrentIndex(1)
        self.set_status(f"已叠加绘制：{keys[0].label} + {len(keys) - 1} 条对比曲线（共用 Y 轴）")
        self._save_view()

    def _add_lane_overlays(self, label: str, keys: list[ChannelKey]) -> None:
        """把通道追加成某条已有泳道的叠加曲线（不重建整页，只加曲线）。"""
        labels = self.plot.lane_labels()
        if label not in labels:
            self.set_status(f"没找到泳道「{label}」，可能刚被切走了")
            return
        idx = labels.index(label)
        merged: list[ChannelKey] = []
        for i, (lbl, old) in enumerate(self._lane_overlays):
            if lbl == label:
                merged = list(old)
                for k in keys:
                    if k not in merged:
                        merged.append(k)
                self._lane_overlays[i] = (lbl, merged)
                break
        else:
            merged = list(keys)
            self._lane_overlays.append((label, merged))
        win = self._win
        if not win:
            win = (float(self.store.meta.get("stats", {}).get("t_start", 0.0) or 0.0),
                   float(self.store.meta.get("t_end", 0.0) or 0.0))
        added = 0
        for k in keys:
            spec = self._spec_for_key(k, win[0], win[1], color=None)
            if spec is None:
                self.set_status(f"{k.label} 在当前离线窗口里没有有效点，未叠加")
                continue
            if self.plot.add_overlay(idx, spec):
                added += 1
        if added:
            self.set_status(f"已把 {added} 条曲线叠加到「{label}」（虚线，各自一条胶囊）")
            self._save_view()

    def _apply_lane_overlays(self, win: tuple[float, float]) -> None:
        """重建泳道后把「叠加到已有泳道」的曲线补回去（set_lanes 会清空运行时叠加）。"""
        if not self._lane_overlays:
            return
        labels = self.plot.lane_labels()
        for label, keys in list(self._lane_overlays):
            if label not in labels:
                continue
            idx = labels.index(label)
            for k in keys:
                spec = self._spec_for_key(k, win[0], win[1], color=None)
                if spec is not None:
                    self.plot.add_overlay(idx, spec)

    def _add_channel(self, key: ChannelKey) -> None:
        if key not in self._extra:
            self._extra.append(key)
            self._apply(keep_window=True)
            self.tabs.setCurrentIndex(1)
            self.set_status(f"已加入通道 {key.label}")
            self._save_view()

    # ---------------- 视图记忆 ----------------
    def _on_track_vis(self, keys) -> None:
        """地图轨迹勾选变化：记住它，下次打开同一份日志还是那个组合。"""
        self._track_vis = [str(k) for k in (keys or [])]
        self._save_view()

    def _views_path(self) -> Path:
        return _rd.cache_root().parent / "views.json"

    def _save_view(self) -> None:
        """把预设选择 + 手动通道 + 参数过滤记住（存用户目录，不碰日志目录）。"""
        try:
            data: dict[str, Any] = {}
            p = self._views_path()
            if p.exists():
                loaded = json.loads(p.read_text(encoding="utf-8") or "{}")
                data = loaded if isinstance(loaded, dict) else {}
            data["preset"] = self._preset
            data["channels"] = [k.key for k in self._extra]
            data["channel_groups"] = [[k.key for k in g] for g in self._groups]
            data["lane_overlays"] = [[lbl, [k.key for k in ks]]
                                     for lbl, ks in self._lane_overlays]
            data["param_filter"] = {"text": self._param_filter.text(),
                                    "diff_only": self._param_diff_only.isChecked()}
            try:
                if self.map.legend.isVisible():
                    data["track_vis"] = self.map.visible_tracks()
            except Exception:                                 # noqa: BLE001
                pass
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            log.debug("视图记忆保存失败: %s", exc)

    def _restore_view(self) -> None:
        try:
            data = json.loads(self._views_path().read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        pid = data.get("preset")
        if isinstance(pid, str) and (pid in PRESET_BY_ID or pid in self._plugin_presets):
            i = self.preset_box.findData(pid)
            if i >= 0:
                self.preset_box.setCurrentIndex(i)
                self._preset = pid
        lh = data.get("lane_h")     # 旧版视图记忆里可能有；现在归 settings.json 管，这里兼容一次
        if isinstance(lh, (int, float)) and 70 <= float(lh) <= 400 \
                and int(lh) != int(Settings.instance().get("lane_min_h")):
            Settings.instance().set("lane_min_h", int(lh))
        pf = data.get("param_filter")
        tv = data.get("track_vis")
        if isinstance(tv, list) and all(isinstance(x, str) for x in tv):
            self._track_vis = [str(x) for x in tv]
        if isinstance(pf, dict):
            self._param_filter.setText(str(pf.get("text") or ""))
            self._param_diff_only.setChecked(bool(pf.get("diff_only")))
        for raw in (data.get("channels") or []):
            try:
                k = ChannelKey.parse(str(raw))
            except ValueError:
                continue
            if k not in self._extra:
                self._extra.append(k)
        # 叠加绘制的分组与"叠加到已有泳道"的曲线
        for grp in (data.get("channel_groups") or []):
            keys = []
            for raw in (grp or []):
                try:
                    keys.append(ChannelKey.parse(str(raw)))
                except ValueError:
                    continue
            if len(keys) >= 2:
                self._groups.append(keys)
        for item in (data.get("lane_overlays") or []):
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            lbl, raw_keys = str(item[0]), (item[1] or [])
            keys = []
            for raw in raw_keys:
                try:
                    keys.append(ChannelKey.parse(str(raw)))
                except ValueError:
                    continue
            if lbl and keys:
                self._lane_overlays.append((lbl, keys))

    def _restore_extra_channels(self) -> None:
        """记住的通道在另一份日志里可能不存在，丢掉多余的，别刷错误提示。"""
        have = {(c.key.type, c.key.field) for c in self.store.channel_infos()}
        # 插件合成通道也算"存在"，否则每次开日志都会把用户存的合成通道丢掉
        for _pid, syn in self.plugins.synthetics():
            try:
                k = ChannelKey.parse(syn.key)
            except ValueError:
                continue
            have.add((k.type, k.field))
        keep = [k for k in self._extra if (k.type, k.field) in have]
        if len(keep) != len(self._extra):
            self._extra = keep
        # 分组：整组过滤后仍要有 ≥2 条，否则整组丢掉（一条不构成叠加）
        groups = []
        for grp in self._groups:
            g = [k for k in grp if (k.type, k.field) in have]
            if len(g) >= 2:
                groups.append(g)
        self._groups = groups
        self._lane_overlays = [(lbl, [k for k in ks if (k.type, k.field) in have])
                               for lbl, ks in self._lane_overlays]
        self._lane_overlays = [(lbl, ks) for lbl, ks in self._lane_overlays if ks]

    def _forget_view(self) -> None:
        self._extra = []
        self._groups = []
        self._lane_overlays = []
        self._param_filter.clear()
        self._param_diff_only.setChecked(False)
        try:
            self._views_path().unlink(missing_ok=True)
        except Exception as exc:
            log.debug("清除视图记忆失败: %s", exc)
        if self.store:
            self._apply()
        self.set_status("已清除视图记忆（预设/手动通道/参数过滤回到默认；"
                        "每道最小高属于全局设置，在“设置”里改）")

    def _export_params(self) -> None:
        """导出参数：选范围与格式 → 选保存位置 → 落盘。全程只读日志，不碰飞控。"""
        if self.store is None:
            QtWidgets.QMessageBox.information(self, "导出参数", "还没有打开任何日志。")
            return
        rows = self.param_table.all_param_rows()
        vis = self.param_table.visible_names()
        default_scope = pe.SCOPE_DIFF if self._param_diff_only.isChecked() else pe.SCOPE_ALL
        dlg = _ParamExportDialog(rows, vis, default_scope, self)
        if dlg.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
            return
        scope, fmt = dlg.scope, dlg.fmt
        picked = pe.select(rows, scope, vis)
        if not picked:
            # 空结果不弹保存框：否则用户选了路径、写了个只有头的空文件，还以为成功了
            self.set_status(f"{pe.SCOPE_CN[scope]}：0 条，没有导出")
            QtWidgets.QMessageBox.information(
                self, "导出参数", f"{pe.SCOPE_CN[scope]}：一条参数也没有，没有导出。")
            return
        st = self.store.stats
        meta = {"log_name": self.store.name, "firmware": getattr(st, "firmware", ""),
                "frame": getattr(st, "frame_type", ""),
                "duration": getattr(st, "duration_str", "")}
        header = pe.header_lines(meta, pe.SCOPE_CN[scope], len(picked), len(rows),
                                 pe.counts(rows)["no_default"])
        flt = ("参数文件 (*.param);;所有文件 (*)" if fmt == pe.FMT_PARAM
               else "CSV (*.csv);;所有文件 (*)")
        path, _sel = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出参数", pe.suggested_name(self.store.name, scope, fmt), flt)
        if not path:
            return
        ext = pe.FMT_EXT[fmt]
        if not str(path).lower().endswith("." + ext):
            path = f"{path}.{ext}"          # 用户手打名字不带后缀时补上，别静默改格式
        try:
            n = pe.write_export(path, picked, fmt, header)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "导出失败",
                                          f"{type(exc).__name__}: {exc}\n\n{path}")
            return
        self.set_status(f"已导出 {n} 条参数（{pe.SCOPE_CN[scope]}）→ {path}")

    def _refilter_params(self, *_a: Any) -> None:
        if self.store:
            self.param_table.apply_filter(self._param_filter.text(), self._param_diff_only.isChecked())
            self._param_count.setText(f"显示 {self.param_table.visible_rows()}/{len(self.store.params())}")

    def _track_quality_note(self, s) -> str:
        """位置质量决定地图能不能信，直接把结论说在状态栏。"""
        tm = (self.store.meta.get("track") or {}) if self.store else {}
        jumps = int(tm.get("jumps") or 0)
        if s.pos_kind == "none":
            return "｜⚠ 无有效位置数据（地图页为空，不要拿轨迹做判断）"
        out = ""
        if jumps:
            out += f"｜已剔除 {jumps} 处位置跳变"
        if s.pos_source == "GPS" and tm.get("rows", 0) < max(1, int(s.n_messages * 0.01)):
            out += "｜GPS 有效点比例偏低，轨迹仅供参考"
        return out

    def _seek_alert(self, hit: AlertHit) -> None:
        self.tabs.setCurrentIndex(1)
        span = 6.0
        mid = (hit.t0 + hit.t1) / 2.0 if math.isfinite(hit.t0) and math.isfinite(hit.t1) else hit.t0
        self._win = (mid - span, mid + span)
        self._apply(keep_window=True)
        self._on_time(mid)
        self.alert_table.select_hit(hit)
        self.set_status(f"告警：{hit.title} — {hit.message}")

    # ---------------- 说明弹窗（双击事件/告警） ----------------
    def _codebook_text(self, ev: EventRec) -> str:
        """从码表拿这条 ERR 的类型名；拿不到就给空串（不编造）。"""
        if ev.kind != "ERR" or self.store is None:
            return ""
        cb = getattr(self.store, "cb", None)
        if cb is None:
            return ""
        try:
            txt, _sev = cb.err_info(ev.code, ev.ecode)
            return str(txt or "")
        except Exception:                        # noqa: BLE001 码表异常不影响弹窗
            log.debug("取码表文本失败", exc_info=True)
            return ""

    def _open_event_help(self, ev: object) -> None:
        """双击事件行 → 该类型的问题说明与处理建议。只读，不改游标/不重算视图。"""
        if not isinstance(ev, EventRec):
            return
        codebook_text = self._codebook_text(ev)
        kwargs = dict(kind=ev.kind, code=ev.code, ecode=ev.ecode, text=ev.text,
                      decoded=ev.decoded, severity=ev.severity,
                      codebook_text=codebook_text)
        try:
            entry = load_help().explain_event(**kwargs)
        except Exception:                            # noqa: BLE001 知识库坏了也得给出说明
            log.exception("知识库匹配失败，退回内置兜底文案")
            entry = EventHelp().explain_event(**kwargs)

        sev_cn = SEV_ZH.get(ev.severity, ev.severity)
        if ev.kind == "ERR":
            subject = f"ERR 子系统 {ev.code}"
            if ev.ecode is not None:
                subject += f" / 错误码 {ev.ecode}"
            if codebook_text:
                short = codebook_text if len(codebook_text) <= 48 else codebook_text[:48] + "…"
                subject += f" · {short}"
        else:
            subject = f"{ev.kind} · {(ev.decoded or ev.text)[:90]}"
        meta = f"t = {ev.t:.3f} s ｜ 严重度：{sev_cn} ｜ 事件类型：{ev.kind}"
        self._show_help(entry, subject=subject, meta=meta, raw=ev.display)

    def _open_alert_help(self, hit: object) -> None:
        """双击告警行 → 该告警类型的说明与处理建议。"""
        if not isinstance(hit, AlertHit):
            return
        kwargs = dict(rule=hit.rule, title=hit.title, severity=hit.severity)
        # 告警原文已经在弹窗的「日志原文」行，不再塞进 note（否则同一句话出现两遍）
        try:
            entry = load_help().explain_alert(**kwargs)
        except Exception:                            # noqa: BLE001
            log.exception("知识库匹配失败，退回内置兜底文案")
            entry = EventHelp().explain_alert(**kwargs)

        parts = [f"严重度：{SEV_ZH.get(hit.severity, hit.severity)}"]
        if math.isfinite(hit.t0):
            if math.isfinite(hit.t1) and hit.t1 > hit.t0:
                parts.append(f"t = {hit.t0:.1f}–{hit.t1:.1f} s")
            else:
                parts.append(f"t = {hit.t0:.1f} s")
        if math.isfinite(hit.value):
            parts.append(f"值 {hit.value:g}{hit.unit}")
        if math.isfinite(hit.threshold):
            parts.append(f"阈值 {hit.threshold:g}{hit.unit}")
        self._show_help(entry, subject=f"{hit.title}（规则 {hit.rule}）",
                        meta=" ｜ ".join(parts), raw=hit.message or "")

    def _show_help(self, entry: object, *, subject: str, meta: str, raw: str) -> None:
        """弹说明。构造失败也要把文字端出来，绝不给空白窗口。"""
        try:
            dlg = EventHelpDialog(entry, subject=subject, meta=meta, raw=raw, parent=self)
        except Exception:                            # noqa: BLE001
            log.exception("说明弹窗构造失败，改用文本框")
            text = getattr(entry, "as_text", lambda: "")()
            QtWidgets.QMessageBox.information(self, "说明与处理建议",
                                              f"{subject}\n{meta}\n\n{text}")
            return
        dlg.exec()

    def _step_alert(self, step: int) -> None:
        """按时间顺序在告警之间跳转（以当前游标为起点）。"""
        if not self.store:
            return
        ordered = sorted([a for a in self._alerts if math.isfinite(a.t0)], key=lambda a: a.t0)
        if not ordered:
            self.set_status("当前没有带时间区的告警")
            return
        cur = self._cursor_t if self._cursor_t is not None else -math.inf
        if step >= 0:
            nxt = [a for a in ordered if a.t0 > cur + 1e-6]
            self._seek_alert(nxt[0] if nxt else ordered[0])
        else:
            prev = [a for a in ordered if a.t0 < cur - 1e-6]
            self._seek_alert(prev[-1] if prev else ordered[-1])

    _cursor_t: Optional[float] = None

    # ---------------- 导出 ----------------
    def _export_png(self) -> None:
        if self.store is None:
            return
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 PNG", f"{self.store.stats.duration_str.replace(':', '-')}.png", "PNG (*.png)")
        if p and not self.plot.export_png(p):
            QtWidgets.QMessageBox.warning(self, "导出失败", "无法写出 PNG")

    def _export_csv(self) -> None:
        if self.store is None:
            return
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出 CSV", "channels.csv", "CSV (*.csv)")
        if p:
            n = self.plot.export_csv(p)
            self.set_status(f"已导出 {n} 行到 {p}")

    def set_status(self, text: str, temporary: bool = False) -> None:
        """状态栏只有一处显示（左侧）。

        temporary 的消息超时后由 ``_on_status_message`` 把常驻文本恢复回来，
        不会一遇临时提示就把当前状态顶掉后留白。
        """
        if temporary:
            self.statusBar().showMessage(text, 4000)
            return
        self._status_text = text
        self.statusBar().showMessage(text)

    def _on_status_message(self, msg: str) -> None:
        if not msg and self._status_text:
            self.statusBar().showMessage(self._status_text)

    def _open_link_dialog(self) -> None:
        """弹出 MAVLink 连接与日志下载面板（全程只读：不写参数、不发指令）。"""
        out_dir = _rd.cache_root().parent / "downloads"
        out_dir.mkdir(parents=True, exist_ok=True)
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("连接飞控 / 下载 DataFlash 日志（只读）")
        dlg.resize(940, 660)
        lay = QtWidgets.QVBoxLayout(dlg)
        tip = QtWidgets.QLabel(
            "只做日志列表与下载：不写参数、不校准、不解锁、不发任何控制指令。"
            f"下载落在 {out_dir}；单条下完直接打开分析，勾选多条时全部下完再打开编号最新的一条。")
        tip.setWordWrap(True)
        tip.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)   # 跟着主题走，不写死灰色
        lay.addWidget(tip)
        panel = LinkPanel(default_target="", out_dir=str(out_dir))
        lay.addWidget(panel, 1)
        got: dict[str, str] = {}
        opened: dict[str, bool] = {"done": False}

        def _open(path: str) -> None:
            """只认第一次：单条走 logDownloaded，批量走 batchFinished，不重复加载。"""
            if opened["done"] or not path:
                return
            opened["done"] = True
            dlg.accept()
            self.load(str(path))

        def _on_one(path: str) -> None:
            got["p"] = str(path)
            if not panel.batch_active:              # 批量进行中先不关窗，不然面板会被销毁
                _open(got["p"])

        def _on_batch(ok: list, bad: list) -> None:
            paths = [str(x) for x in ok if x]
            if paths:
                _open(max(paths))                   # 文件名就是 00000NNN.BIN，取编号最大的
            if len(paths) > 1 or bad:
                msg = f"批量下载结束：{len(paths)} 条完成"
                if bad:
                    msg += f"，{len(bad)} 条未完成（断点已保留，可重下）"
                msg += f"；文件在 {out_dir}"
                self.statusBar().showMessage(msg, 12000)

        panel.logDownloaded.connect(_on_one)
        panel.batchFinished.connect(_on_batch)
        panel.connectionChanged.connect(lambda s: dlg.setWindowTitle(f"连接飞控 —— {s}"))
        dlg.exec()

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:  # noqa: N802
        """收尾：组件自己关（不关 TileStore 会在退出时段错误），后台线程等一下。"""
        try:
            self.log_list._scanner.shutdown()      # 扫描线程不停会在退出时吊死/崩溃
        except Exception:
            log.exception("扫描线程收尾失败")
        try:
            self.map.close()
        except Exception:
            log.exception("地图组件关闭失败")
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait(3000)
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                log.exception("LogStore 关闭失败")
            self.store = None
        super().closeEvent(ev)

    def _about(self) -> None:
        QtWidgets.QMessageBox.about(self, "关于", (
            f"APMLogViewer {__version__}\n\n"
            "ArduCopter 4.6.x / 4.7.x DataFlash .BIN 日志桌面分析器。\n"
            f"码表：{getattr(self.store, 'cb', None) and self.store.cb.provenance or '未加载'}\n"
            f"缓存：{str(Path(self.cache_dir) if self.cache_dir else '')}"
        ))


# --------------------------------------------------------------------------- #
# 简易面板（后续由 panels 工作流的组件替换）
# --------------------------------------------------------------------------- #

class _LogListDock(QtWidgets.QWidget):
    refresh_requested = QtCore.Signal(str)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        row = QtWidgets.QHBoxLayout()
        self.dir_edit = QtWidgets.QLineEdit()
        self.dir_edit.setPlaceholderText("日志目录")
        self.btn_scan = QtWidgets.QPushButton("扫描")
        self.btn_scan.setToolTip("递归扫描该目录及其子目录下的日志文件。\n"
                                 "扫描在后台线程进行，可选错整个磁盘也能随时取消。")
        row.addWidget(self.dir_edit, 1)
        row.addWidget(self.btn_scan)
        lay.addLayout(row)
        # 筛选行。默认只列能打开的 .BIN：实测一个真日志目录 1461 个文件里只有
        # 106 个可打开，其余 .rlog/.tlog 全部列出来就是刷屏，根本找不着要看的那条。
        frow = QtWidgets.QHBoxLayout()
        self.flt_name = QtWidgets.QLineEdit()
        self.flt_name.setPlaceholderText("按文件名筛选，如 2026-10-21")
        self.flt_name.setClearButtonEnabled(True)
        self.flt_name.setToolTip("只筛名字，不重新扫盘：输入子串即可（不区分大小写）。\n"
                                 "与右边的“只看 .BIN”是叠加关系。")
        self.flt_bin = QtWidgets.QCheckBox("只看 .BIN")
        self.flt_bin.setToolTip("本版本只能打开 DataFlash .BIN。\n"
                                "勾上时 .tlog/.rlog 等直接不列（它们只会显示“暂不支持”）；\n"
                                "取消勾选可以看到全部文件，不可打开的仍然置灰且双击无反应。\n"
                                "这个选择会记住。")
        frow.addWidget(self.flt_name, 1)
        frow.addWidget(self.flt_bin)
        lay.addLayout(frow)
        # 扫描结果汇总：日志总数 + 各类型数量（以前只有一行“共 N 个 .BIN”）
        self.lbl_sum = QtWidgets.QLabel("尚未扫描")
        self.lbl_sum.setWordWrap(True)
        self.lbl_sum.setStyleSheet("font-size: 8.5pt;")
        lay.addWidget(self.lbl_sum)
        self.list = QtWidgets.QListWidget()
        lay.addWidget(self.list, 1)
        self.btn_scan.clicked.connect(lambda: self.scan(self.dir_edit.text()))
        self.dir_edit.returnPressed.connect(lambda: self.scan(self.dir_edit.text()))
        self.list.itemDoubleClicked.connect(self._activated)
        self.flt_bin.toggled.connect(self._filter_changed)
        self.flt_name.textChanged.connect(lambda _t: self._apply_filter())
        self._sum_base = "尚未扫描"
        try:
            from apmlog.ui.settings import Settings
            self.flt_bin.setChecked(bool(Settings.instance().get("list_only_bin")))
        except Exception:                            # pragma: no cover
            self.flt_bin.setChecked(True)
        self._scanner = ScanController(self)
        self._scanner.busyChanged.connect(self._on_busy)
        self._scanner.done.connect(self._fill)

    # ------------------------------------------------------------------ 筛选
    _R_PATH = int(QtCore.Qt.ItemDataRole.UserRole)        # 可打开项的绝对路径
    _R_OK = _R_PATH + 1                                    # 能不能打开
    _R_NAME = _R_PATH + 2                                  # 小写文件名（筛选用）

    def _filter_changed(self, on: bool) -> None:
        """“只看 .BIN”开关：立即生效并记住（与其他设置一致，不弹“保存”）。"""
        try:
            from apmlog.ui.settings import Settings
            Settings.instance().set("list_only_bin", bool(on))
        except Exception:                            # pragma: no cover
            pass
        self._apply_filter()

    def visible_count(self) -> int:
        return sum(1 for i in range(self.list.count()) if not self.list.item(i).isHidden())

    def _apply_filter(self, *_a: Any) -> None:
        want = self.flt_name.text().strip().lower()
        only_bin = self.flt_bin.isChecked()
        lst = self.list
        # 一千多条逐项 setHidden 会逐次重绘，关掉更新避免闪烁
        lst.setUpdatesEnabled(False)
        try:
            for i in range(lst.count()):
                it = lst.item(i)
                ok = bool(it.data(self._R_OK))
                hide = (only_bin and not ok) or bool(want) and want not in str(it.data(self._R_NAME))
                if it.isHidden() != hide:
                    it.setHidden(hide)
            shown = self.visible_count()
            # 选中行必须是可见行，否则光标会落在看不见的条目上
            cur = lst.currentItem()
            if cur is not None and cur.isHidden():
                first = next((i for i in range(lst.count()) if not lst.item(i).isHidden()), -1)
                lst.setCurrentRow(first)
        finally:
            lst.setUpdatesEnabled(True)
        self._refresh_summary(shown)

    def _refresh_summary(self, shown: Optional[int] = None) -> None:
        """汇总行要同时说清“扫到什么”和“现在给你看多少”，不能只报一个数。"""
        total = self.list.count()
        if shown is None:
            shown = self.visible_count()
        if shown == total:
            self.lbl_sum.setText(self._sum_base)
            return
        hidden = total - shown
        why = []
        if self.flt_bin.isChecked():
            why.append("不可打开的")
        if self.flt_name.text().strip():
            why.append("名字不匹配的")
        self.lbl_sum.setText(f"{self._sum_base}\n"
                             f"当前显示 {shown} 个，已筛选掉 {hidden} 个（{'、'.join(why) or '其他'}）")

    # ------------------------------------------------------------------ 扫描
    def scan(self, directory: str, modal: bool = True) -> bool:
        """开一次后台扫描。modal=True 时弹进度框（用户主动扫）；
        启动/拖拽这类“顺手扫一下”用 modal=False，只往状态栏报进度。"""
        d = str(directory or "").strip().strip('"')
        if not d:
            self.lbl_sum.setText("请先选择日志目录")
            return False
        return self._scanner.start(d, modal=modal)

    def _on_busy(self, on: bool) -> None:
        self.btn_scan.setEnabled(not on)
        self.btn_scan.setText("扫描中…" if on else "扫描")
        if on:
            self.lbl_sum.setText("正在扫描…（可取消，已扫到的会保留）")

    def _fill(self, res) -> None:
        """扫描完成回调。res 为 None 表示出错；取消时仍是合法结果。"""
        from apmlog.scan import SUPPORTED_EXTS, summarize
        self.list.clear()
        if res is None:
            # 基线文本也要一起换掉：不然事后一勾筛选会把上一次扫描的旧汇总顶回来
            self._sum_base = "扫描失败，见状态栏提示"
            self.lbl_sum.setText(self._sum_base)
            return
        self.dir_edit.setText(str(getattr(res, "root", "") or self.dir_edit.text()))
        sup = tuple(e.lower() for e in SUPPORTED_EXTS)
        n_open = 0
        for f in getattr(res, "files", ()) or ():
            ok = str(f.ext).lower() in sup
            it = QtWidgets.QListWidgetItem(
                f"{f.name}  ({human_size(f.size)})" + ("" if ok else "  暂不支持"))
            it.setToolTip(f.path)
            it.setData(self._R_OK, bool(ok))
            it.setData(self._R_NAME, str(f.name).lower())
            if ok:
                it.setData(QtCore.Qt.ItemDataRole.UserRole, f.path)
                n_open += 1
            else:
                # QListWidgetItem 没有 setEnabled：置灰要去掉 ItemIsEnabled 标志
                it.setFlags(it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(it)
        self._sum_base = summarize(res)
        self._apply_filter()
        if n_open and self.visible_count():
            # 选第一项要选“可见的”第一项：默认只看 .BIN 时 row 0 往往是隐藏的 .rlog
            self.list.setCurrentRow(next(i for i in range(self.list.count())
                                         if not self.list.item(i).isHidden()))

    def _activated(self, it) -> None:
        p = it.data(QtCore.Qt.ItemDataRole.UserRole)
        if p:
            self.refresh_requested.emit(p)

    def set_current(self, path: str) -> None:
        p = Path(path)
        self.dir_edit.setText(str(p.parent))
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(QtCore.Qt.ItemDataRole.UserRole) == str(p):
                self.list.setCurrentRow(i)


class _ChannelTreeDock(QtWidgets.QWidget):
    channelChosen = QtCore.Signal(object)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("过滤报文/字段，如 VIBE")
        lay.addWidget(self.search)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["类型 / 字段 / 实例", "点数", "单位"])
        # 多选：Ctrl/Shift 选若干通道后点「叠加绘制」，合成一条同轴泳道
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setToolTip("双击=加入曲线；Ctrl/Shift 多选后点「叠加绘制」= 合成一条同轴泳道")
        lay.addWidget(self.tree, 1)
        self.search.textChanged.connect(self._filter)
        self.tree.itemDoubleClicked.connect(self._on_dbl)

    def set_store(self, store: LogStore) -> None:
        self.tree.clear()
        self._plugin_item = None
        top: dict[str, QtWidgets.QTreeWidgetItem] = {}
        for c in store.channel_infos():
            it = top.get(c.key.type)
            if it is None:
                it = top[c.key.type] = QtWidgets.QTreeWidgetItem([c.key.type, "", ""])
                self.tree.addTopLevelItem(it)
            child = QtWidgets.QTreeWidgetItem([c.key.label, str(c.count), c.unit])
            child.setData(0, QtCore.Qt.ItemDataRole.UserRole, c.key.key)
            it.addChild(child)
        self.tree.collapseAll()

    def set_plugin_channels(self, chans: Sequence[dict]) -> None:
        """插件合成通道：单独一组挂在树顶，双击同样能加进曲线页。"""
        old = getattr(self, "_plugin_item", None)
        if old is not None:
            idx = self.tree.indexOfTopLevelItem(old)
            if idx >= 0:
                self.tree.takeTopLevelItem(idx)
        self._plugin_item = None
        if not chans:
            return
        top = QtWidgets.QTreeWidgetItem(["插件通道", "", ""])
        top.setToolTip(0, "插件算出来的派生通道；算法与单位由插件负责（只读）")
        self.tree.addTopLevelItem(top)
        for c in chans:
            child = QtWidgets.QTreeWidgetItem([str(c.get("label") or c.get("key")),
                                               str(int(c.get("count") or 0)),
                                               str(c.get("unit") or "")])
            child.setData(0, QtCore.Qt.ItemDataRole.UserRole, str(c.get("key")))
            top.addChild(child)
        top.setExpanded(True)
        self._plugin_item = top

    def _filter(self, text: str) -> None:
        t = (text or "").strip().upper()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            hit = (not t) or t in it.text(0).upper()
            shown = 0
            for j in range(it.childCount()):
                ch = it.child(j)
                ok = (not t) or t in ch.text(0).upper()
                ch.setHidden(not ok)
                shown += int(ok)
            it.setHidden(not hit and shown == 0)
            if t and shown:
                it.setExpanded(True)

    def selected_keys(self) -> list:
        """选中的通道，**按树里的显示顺序**（选中顺序在 QTreeWidget 里不可靠）。"""
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                ch = top.child(j)
                if not ch.isSelected():
                    continue
                raw = ch.data(0, QtCore.Qt.ItemDataRole.UserRole)
                if not isinstance(raw, str):
                    continue
                try:
                    out.append(ChannelKey.parse(raw))
                except ValueError:
                    continue
        return out

    def _on_dbl(self, item: QtWidgets.QTreeWidgetItem, _col: int) -> None:
        raw = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(raw, str):
            try:
                self.channelChosen.emit(ChannelKey.parse(raw))
            except ValueError as exc:
                log.warning("通道键解析失败: %s", exc)


class _CsvMenuMixin:
    """表格右键→导出可见行为 CSV（utf-8-sig，Excel 直接能开）。"""

    def _enable_csv_export(self, suggested: str) -> None:
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos, s=suggested: self._csv_menu(pos, s))

    def _menu_extra(self, menu: QtWidgets.QMenu) -> list[tuple[Any, Any]]:
        """子类可加自己的右键项：返回 ``[(QAction, 回调), ...]``。"""
        return []

    def _csv_menu(self, pos, suggested: str) -> None:
        menu = QtWidgets.QMenu(self)
        act = menu.addAction(f"导出可见行（{self.rowCount()} 行）为 CSV…")
        extras = self._menu_extra(menu)
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is None:
            return
        for a, cb in extras:
            if chosen is a:
                cb()
                return
        if chosen is not act:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self.window(), "导出 CSV", suggested,
                                                        "CSV (*.csv)")
        if not path:
            return
        try:
            n = self._dump_csv(path)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self.window(), "导出失败", str(exc))
            return
        win = self.window()
        if hasattr(win, "set_status"):
            win.set_status(f"已导出 {n} 行→{path}")

    def _dump_csv(self, path: str) -> int:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            wr = csv.writer(fh)
            hdr = self.horizontalHeaderItem
            wr.writerow([(hdr(i).text() if hdr(i) else f"列{i}") for i in range(self.columnCount())])
            n = 0
            for r in range(self.rowCount()):
                if self.isRowHidden(r):
                    continue
                wr.writerow([(self.item(r, c).text() if self.item(r, c) else "")
                             for c in range(self.columnCount())])
                n += 1
        return n


class _AlertTable(_CsvMenuMixin, QtWidgets.QTableWidget):
    seekRequested = QtCore.Signal(object)
    #: 双击一条告警：``AlertHit``（主窗弹说明）
    helpRequested = QtCore.Signal(object)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(0, 7, parent)
        self.setHorizontalHeaderLabels(["严重度", "标题", "区间(s)", "值", "阈值", "出处", "说明"])
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.setToolTip("双击一行：查看该类型的问题说明与处理建议（单击仍只定位时间线）")
        self.itemSelectionChanged.connect(self._on_sel)
        self.itemDoubleClicked.connect(self._on_dbl)
        self._enable_csv_export("alerts.csv")
        self._hits: list[AlertHit] = []

    def set_alerts(self, hits: Sequence[AlertHit]) -> None:
        self._hits = list(hits)
        self.setRowCount(len(self._hits))
        for r, h in enumerate(self._hits):
            cells = [SEV_ZH.get(h.severity, h.severity), h.title,
                     f"{h.t0:.1f}–{h.t1:.1f}", _num(h.value), _num(h.threshold),
                     {"param": "日志参数", "default-ref": "参考值", "user": "用户配置",
                      "n/a": "无阈值"}.get(h.source, h.source), h.message]
            for c, v in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(str(v))
                if c == 0:
                    it.setBackground(QtGui.QColor(SEV_RGB.get(h.severity, "#eeeeee")))
                if c == 6:
                    it.setToolTip(str(v))
                self.setItem(r, c, it)
        self.resizeColumnsToContents()
        self.setColumnWidth(6, 520)

    def _on_sel(self) -> None:
        r = self.currentRow()
        if 0 <= r < len(self._hits):
            self.seekRequested.emit(self._hits[r])

    def _on_dbl(self, item: QtWidgets.QTableWidgetItem, _col: int = 0) -> None:
        self._help_at(item.row() if item is not None else self.currentRow())

    def _help_at(self, row: int) -> None:
        if 0 <= row < len(self._hits):
            self.helpRequested.emit(self._hits[row])

    def _menu_extra(self, menu: QtWidgets.QMenu) -> list[tuple[Any, Any]]:
        act = menu.addAction("查看该类型的问题说明…")
        return [(act, lambda: self._help_at(self.currentRow()))]

    def select_hit(self, hit: AlertHit) -> None:
        for r, h in enumerate(self._hits):
            if h is hit:
                self.selectRow(r)
                return


class _EventTable(_CsvMenuMixin, QtWidgets.QTableWidget):
    seekRequested = QtCore.Signal(float)
    #: 双击一条事件：``EventRec``（主窗弹说明）
    helpRequested = QtCore.Signal(object)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(0, 3, parent)
        self._keys: list[tuple[float, str, str]] = []
        self._evs: list[EventRec] = []
        self.setHorizontalHeaderLabels(["时间(s)", "类型/严重度", "内容"])
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.verticalHeader().setVisible(False)
        self.setToolTip("双击一行：查看该类型的问题说明与处理建议（单击仍只定位时间线）")
        self._enable_csv_export("events.csv")
        self.itemSelectionChanged.connect(lambda: self.seekRequested.emit(float(
            self.item(self.currentRow(), 0).text())) if self.currentRow() >= 0 else None)
        self.itemDoubleClicked.connect(self._on_dbl)

    def set_events(self, evs: Sequence[EventRec]) -> None:
        self._evs = list(evs)
        self._keys = [(float(e.t), str(e.kind), str(e.display)) for e in evs]
        self.setRowCount(len(evs))
        for r, e in enumerate(evs):
            it0 = QtWidgets.QTableWidgetItem(f"{e.t:.3f}")
            it1 = QtWidgets.QTableWidgetItem(f"{e.kind} / {SEV_ZH.get(e.severity, e.severity)}")
            it1.setBackground(QtGui.QColor(SEV_RGB.get(e.severity, "#eeeeee")))
            it2 = QtWidgets.QTableWidgetItem(e.display)
            it2.setToolTip(e.display)
            self.setItem(r, 0, it0)
            self.setItem(r, 1, it1)
            self.setItem(r, 2, it2)
        self.setColumnWidth(0, 90)
        self.setColumnWidth(1, 130)
        self.setColumnWidth(2, 640)

    def row_for(self, t: float, kind: str = "", text: str = "") -> int:
        """按 (时间,类型,内容) 找回填表时的那一行；先精确再退到只认时间，都没有返回 -1。"""
        if not math.isfinite(t):
            return -1
        for i, (rt, rk, rx) in enumerate(self._keys):
            if abs(rt - t) < 1e-3 and (not kind or rk == kind) and (not text or rx == text):
                return i
        for i, (rt, _k, _x) in enumerate(self._keys):
            if abs(rt - t) < 1e-3:
                return i
        return -1

    # ---- 说明弹窗（双击/右键） ----
    def _on_dbl(self, item: QtWidgets.QTableWidgetItem, _col: int = 0) -> None:
        self._help_at(item.row() if item is not None else self.currentRow())

    def _help_at(self, row: int) -> None:
        if 0 <= row < len(self._evs):
            self.helpRequested.emit(self._evs[row])

    def _menu_extra(self, menu: QtWidgets.QMenu) -> list[tuple[Any, Any]]:
        act = menu.addAction("查看该类型的问题说明…")
        return [(act, lambda: self._help_at(self.currentRow()))]


class _ParamTable(_CsvMenuMixin, QtWidgets.QTableWidget):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["参数名", "记录值", "出厂默认", "与默认差异"])
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self._enable_csv_export("params.csv")
        self._rows: list[tuple[str, float, Optional[float]]] = []

    def set_params(self, params: dict[str, tuple[float, Optional[float]]]) -> None:
        self._rows = sorted(params.items())
        self.setRowCount(len(self._rows))
        for r, (name, (val, dfv)) in enumerate(self._rows):
            diff = "" if dfv is None or not math.isfinite(val) else ("相同" if abs(val - dfv) < 1e-9 else f"{val - dfv:+g}")
            for c, v in enumerate((name, f"{val:g}", "" if dfv is None else f"{dfv:g}", diff)):
                self.setItem(r, c, QtWidgets.QTableWidgetItem(str(v)))
        for c, w in enumerate((260, 120, 120, 120)):
            self.setColumnWidth(c, w)

    @staticmethod
    def _is_changed(val: float, dfv: Optional[float]) -> bool:
        """判定交给 :class:`apmlog.paramexport.ParamRow`：表格与导出共用一个真源。

        两边各写一套容差的话，会出现“表格里显示相同、却被导进差异文件”这种自相矛盾。
        """
        return pe.ParamRow("", val, dfv).changed

    def apply_filter(self, text: str, diff_only: bool) -> None:
        t = (text or "").strip().upper()
        for r, (name, (val, dfv)) in enumerate(self._rows):
            hide = bool(diff_only) and not self._is_changed(val, dfv)
            if t and t not in name.upper():
                hide = True
            self.setRowHidden(r, hide)

    def visible_rows(self) -> int:
        return sum(1 for r in range(self.rowCount()) if not self.isRowHidden(r))

    def all_param_rows(self) -> list[pe.ParamRow]:
        return [pe.ParamRow(n, v, d) for n, (v, d) in self._rows]

    def visible_names(self) -> list[str]:
        """当前没被过滤掉的参数名（“只导筛选结果”用）。"""
        return [self._rows[r][0] for r in range(self.rowCount()) if not self.isRowHidden(r)]


class _ParamExportDialog(QtWidgets.QDialog):
    """导出范围/格式选择。**条数直接写在选项上**，不让用户猜会导出多少条。"""

    def __init__(self, rows: Sequence[pe.ParamRow], visible_names: Sequence[str],
                 default_scope: str = pe.SCOPE_ALL,
                 parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出参数")
        self.setModal(True)
        c = pe.counts(rows)
        lay = QtWidgets.QVBoxLayout(self)

        g1 = QtWidgets.QGroupBox("导出哪些")
        v1 = QtWidgets.QVBoxLayout(g1)
        self.rb_all = QtWidgets.QRadioButton(f"完整参数（{c['total']} 条）")
        self.rb_diff = QtWidgets.QRadioButton(
            f"只导与出厂默认不同的（{c['changed']} 条）")
        self.rb_filt = QtWidgets.QRadioButton(
            f"只导当前筛选结果（{len(visible_names)} 条）")
        for rb in (self.rb_all, self.rb_diff, self.rb_filt):
            v1.addWidget(rb)
        (self.rb_diff if default_scope == pe.SCOPE_DIFF else self.rb_all).setChecked(True)
        if c["no_default"]:
            tip = QtWidgets.QLabel(f"（其中 {c['no_default']} 条日志没写 Default，"
                                   "无法判断是否相同，不算在“不同”里）")
            tip.setWordWrap(True)
            tip.setFont(ui_font(8.5))
            v1.addWidget(tip)
        lay.addWidget(g1)

        g2 = QtWidgets.QGroupBox("导成什么格式")
        v2 = QtWidgets.QVBoxLayout(g2)
        self.rb_param = QtWidgets.QRadioButton(
            ".param　　NAME,VALUE（Mission Planner / QGC 可直接读）")
        self.rb_csv = QtWidgets.QRadioButton(
            ".csv　　　带出厂默认与差异列（给人看，Excel 直接开）")
        self.rb_param.setChecked(True)
        v2.addWidget(self.rb_param)
        v2.addWidget(self.rb_csv)
        lay.addWidget(g2)

        note = QtWidgets.QLabel(
            "数值与出厂默认都取自日志自身的 PARM 块，不是从飞控实时读的；\n"
            "那是那次飞行时的快照，落地后可能又改过。仅用于对比与存档，\n"
            "不要盲目批量写回飞控（尤其是校准类与统计类参数）。")
        note.setWordWrap(True)
        note.setFont(ui_font(8.5))
        lay.addWidget(note)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.btn_ok = QtWidgets.QPushButton("下一步：选保存位置…")
        self.btn_ok.setDefault(True)
        self.btn_cancel = QtWidgets.QPushButton("取消")
        row.addWidget(self.btn_ok)
        row.addWidget(self.btn_cancel)
        lay.addLayout(row)
        self.btn_ok.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

    @property
    def scope(self) -> str:
        if self.rb_diff.isChecked():
            return pe.SCOPE_DIFF
        if self.rb_filt.isChecked():
            return pe.SCOPE_FILTERED
        return pe.SCOPE_ALL

    @property
    def fmt(self) -> str:
        return pe.FMT_CSV if self.rb_csv.isChecked() else pe.FMT_PARAM


def _num(v: float) -> str:
    return "-" if v is None or not math.isfinite(v) else f"{v:.3g}"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def _pump(app: QtWidgets.QApplication, ms: int = 2500) -> None:
    """把事件循环空转 ms 毫秒，让瓦片/网络回复有机会落地（无头自检用）。"""
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 100)
        time.sleep(0.01)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="apmlog-gui", description="APMLogViewer 桌面主窗")
    ap.add_argument("logs", nargs="*", help="要打开的 .BIN 日志")
    ap.add_argument("--cache", help="缓存目录")
    ap.add_argument("--selftest", action="store_true", help="offscreen 自检：加载首个日志并出 PNG")
    ap.add_argument("--png", default="selftest/app.png")
    ap.add_argument("--preset", default="vibe")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    app = QtWidgets.QApplication(list(sys.argv[:1]))
    # 提示留存/延迟要同时管到按钮那种“被动 tooltip”，靠样式代理改 styleHint；
    # setStyle 会接管旧 style，所以必须在建窗口之前装一次。
    install_tip_style(app)
    apply_tip_font(app)
    # 界面主题要在建窗口之前刷一次，否则首帧会闪一下默认色
    ui_theme.apply_settings(app, get_settings())
    win = MainWindow(cache_dir=args.cache)
    if args.selftest:
        if not args.logs:
            print("selftest 需要一个日志路径", file=sys.stderr)
            return 2
        win.preset_box.setCurrentIndex(max(0, [p.id for p in PRESETS].index(args.preset)))
        win._preset = args.preset
        t0 = time.time()
        win.load_sync(args.logs[0])
        dt = time.time() - t0
        win.resize(1600, 950)
        win.show()
        _pump(app, 4000)
        out = Path(args.png)
        out.parent.mkdir(parents=True, exist_ok=True)
        stem, suf = out.with_suffix("").name, out.suffix or ".png"
        # 先出轨迹页（当前默认 tab），再切到曲线页出第二张
        ok_map = win.grab().save(str(out.parent / f"{stem}_map{suf}"))
        win.tabs.setCurrentIndex(1)
        _pump(app, 600)
        ok = win.grab().save(str(out))
        s = win.store.stats
        rep: list[str] = []
        rep.append(f"selftest: 加载 {s.path!r} 用时 {dt:.2f}s")
        rep.append(f"  固件 {s.firmware}｜机型 {s.frame_type}｜时长 {s.duration:.1f}s"
                   f"｜类型 {s.n_types}｜报文 {s.n_messages}｜参数 {s.n_params}")
        rep.append(f"  位置 kind={s.pos_kind} source={s.pos_source} 告警 {len(win._alerts)} 条"
                   f"（critical {sum(1 for a in win._alerts if a.severity == 'critical')}）"
                   f" 事件 {len(win._events)} 条")
        rep.append(f"  炸机线索: {s.crashes}")
        rep.append(f"  曲线通道: {len(win.plot.lanes())} 道 {win.plot.lanes()}")
        rep.append(f"  跳过原因: {win.skipped_label.text()}")
        # 事件说明知识库：打包版最常错的就是数据文件没带进去，这里既看文件也真跑一次匹配
        try:
            from apmlog.core.knowledge import load_help
            help_ = load_help()
            rep.append(f"  知识库: {help_.stats()}")
            probe = help_.explain_event(kind="ERR", code=12, ecode=1)
            rep.append(f"  知识库试匹配 ERR 12/1 → {probe.title} 命={probe.matched} 建议{len(probe.actions)}条")
        except Exception as exc:                       # noqa: BLE001 只为自检输出
            rep.append(f"  知识库: 不可用（{type(exc).__name__}: {exc}）")
        try:
            infos = win.plugins.plugins()
            rep.append("  插件: " + (", ".join(
                f"{i.id}={i.status}" + (f"（{(i.error or '')[:60]}）" if i.status != "loaded" else "")
                for i in infos) or "（无）"))
            rep.append(f"  插件页: {[win.tabs.tabText(i) for i in range(win.tabs.count())]}")
            split = win.centralWidget().widget(0)
            rep.append(f"  左侧栏宽 {split.sizes()[0]} ｜标签区最小宽 {win.tabs.minimumSizeHint().width()}")
        except Exception as exc:                       # noqa: BLE001 只为自检输出
            rep.append(f"  插件: 读取失败（{type(exc).__name__}: {exc}）")
        rep.append(f"  PNG: {out} {'OK' if ok else 'FAIL'}  轨迹页: {stem}_map{suf} {'OK' if ok_map else 'FAIL'}")
        for line in rep:
            print(line)
        # 窗口版 exe 没有控制台，把同一份报告写盘，便于核对（与 PNG 同目录）
        try:
            (out.parent / f"{stem}_selftest.txt").write_text("\n".join(rep) + "\n",
                                                             encoding="utf-8")
        except OSError:
            pass
        return 0 if (ok and ok_map) else 1
    win.show()          # 必须显式 show：否则带日志启动时只有后台进程，窗口不会出现
    if args.logs:
        win.load(args.logs[0])
        if len(args.logs) > 1:
            win.log_list.scan(str(Path(args.logs[0]).parent), modal=False)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
