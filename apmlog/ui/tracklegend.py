# -*- coding: utf-8 -*-
"""apmlog.ui.tracklegend —— 地图右侧的半透明轨迹勾选栏。

一份日志里可能同时有多条位置轨迹（EKF 融合位置 POS、原始 GNSS 的 GPS、EKF 状态
XKF1、视觉位置 VISP，双接收机时 GPS 还会分 #0/#1）。真机实测同一份日志里 POS 与 GPS
逐点最大能差 76 m，在地图上就是两条分开的线——所以必须能"全都画出来 + 自己挑看哪几条"。

要点：
* 覆盖在地图上，半透明，不抢鼠标：只在自己的控件上响应，其余区域照常拖动画布；
* 每条轨迹一行：颜色块 + 勾选框 + 中文名 + 点数，主轨迹标"主"；
* 勾选状态变化只发一个 ``visibilityChanged(keys)``，画不画由地图决定；
* 只有一条轨迹时整个栏子隐藏（没人愿意为一根线看一个面板）。
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from ._font import panel_qss, ui_font

__all__ = ["TrackLegendPanel", "TRACK_COLORS"]

#: 轨迹配色。主轨迹用高度渐变（由地图负责），其余按这个顺序取色。
#: 全部是在卫星图和深色底图上都看得清的高饱和色，且互相区分度足够。
TRACK_COLORS: tuple[str, ...] = (
    "#ff3b30",   # 红
    "#ffcc00",   # 黄
    "#00d084",   # 绿
    "#00c7ff",   # 青
    "#ff7ab8",   # 粉
    "#a78bfa",   # 紫
    "#ff9f0a",   # 橙
    "#7dd3fc",   # 浅蓝
)

_BG = ("background: rgba(18, 22, 26, 205);"
       "border: 1px solid rgba(255,255,255,64); border-radius: 7px;")


class TrackLegendPanel(QtWidgets.QWidget):
    """轨迹勾选栏。父控件应是地图 viewport（和仪表盘一样是覆盖层）。"""

    #: 勾选变化 → 当前可见的轨迹 key 列表
    visibilityChanged = QtCore.Signal(list)

    class Row:
        __slots__ = ("key", "label", "color", "n", "primary",
                     "check", "swatch", "cnt", "host")

        def __init__(self) -> None:
            self.key = ""
            self.label = ""
            self.color = "#ffffff"
            self.n = 0
            self.primary = False
            self.check: Optional[QtWidgets.QCheckBox] = None
            self.swatch: Optional[QtWidgets.QLabel] = None
            self.cnt: Optional[QtWidgets.QLabel] = None
            self.host: Optional[QtWidgets.QWidget] = None

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(panel_qss(_BG + "color:#e8eef2;"))
        self._rows: list["TrackLegendPanel.Row"] = []
        self._muted = False                     # 程序性改勾选时不要反弹信号

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(9, 7, 9, 7)
        lay.setSpacing(4)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        title = QtWidgets.QLabel("轨迹")
        title.setFont(ui_font(9.5, bold=True))
        head.addWidget(title)
        head.addStretch(1)
        self.btn_all = QtWidgets.QToolButton()
        self.btn_all.setText("全选")
        self.btn_all.setToolTip("显示全部轨迹")
        self.btn_all.setAutoRaise(True)
        self.btn_all.setFont(ui_font(8.5))
        self.btn_only = QtWidgets.QToolButton()
        self.btn_only.setText("仅主")
        self.btn_only.setToolTip("只显示主轨迹（EKF 融合位置）")
        self.btn_only.setAutoRaise(True)
        self.btn_only.setFont(ui_font(8.5))
        head.addWidget(self.btn_all)
        head.addWidget(self.btn_only)
        lay.addLayout(head)

        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_only.clicked.connect(self._only_primary)

    # ------------------------------------------------------------------ 数据
    def set_tracks(self, specs: Sequence[tuple], visible: Optional[Iterable[str]] = None
                   ) -> None:
        """specs = [(key, label, color, n, primary[, enabled]), ...]，顺序即显示顺序。

        第六项 ``enabled`` 缺省为 True；False 表示这条轨迹在当前模式下画不出来
        （例如主轨迹有经纬度、它只有本地米数），此时勾选框置灰并说明原因，
        绝不能留一个“能勾但什么都不发生”的控件。
        """
        # 记"用户的勾选意图"，不能用 _row_visible：它把"当前画不出来"也算成未勾选，
        # 结果一条轨迹只要经历过一帧不可绘制（例如地图还没定位），勾选状态就被永久清掉——
        # 而主轨迹其实一直在画（实测：图例上 POS 未勾选、地图上 POS 却在），图例在说谎。
        keep = ({r.key: bool(r.check.isChecked()) for r in self._rows if r.check is not None}
                if visible is None else None)
        for r in self._rows:
            if r.host is not None:
                # deleteLater 不会立刻从 findChildren 里消失；必须先脱离父控件，
                # 否则重建时旧勾选框会和新的一起留在面板上。
                r.host.setParent(None)
                r.host.deleteLater()
        self._rows = []
        want = None if visible is None else {str(k) for k in visible}
        lay = self.layout()
        for spec in specs:
            key, label, color, n, primary = spec[:5]
            enabled = bool(spec[5]) if len(spec) > 5 else True
            row = self.Row()
            row.key, row.label, row.color = str(key), str(label), str(color)
            row.n, row.primary = int(n), bool(primary)
            host = QtWidgets.QWidget(self)
            h = QtWidgets.QHBoxLayout(host)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(5)
            sw = QtWidgets.QLabel(host)
            sw.setFixedSize(10, 10)
            sw.setStyleSheet(panel_qss(
                f"background:{row.color};border:1px solid rgba(255,255,255,120);"
                f"border-radius:2px;"))
            sw.setToolTip(f"颜色 {row.color}")
            cb = QtWidgets.QCheckBox(host)
            cb.setText(row.label + ("（主）" if row.primary else ""))
            cb.setFont(ui_font(8.5))
            cb.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            if not enabled:
                cb.setEnabled(False)
                cb.setToolTip("当前底图是真实地图，而这条轨迹只有本地米坐标（无经纬度、"
                              "也无 ORGN 原点），叠上去会把米当度面错位，所以不画。")
            cnt = QtWidgets.QLabel(f"{row.n}", host)
            cnt.setFont(ui_font(8.0))
            cnt.setStyleSheet(panel_qss("color:#9fb0bd;"))
            cnt.setToolTip(f"{row.n} 个有效位置点")
            on = (want is None and (keep or {}).get(row.key, True))
            if want is not None:
                on = row.key in want
            if keep is not None and row.key not in keep:
                on = True                       # 新出现的轨迹默认显示
            if not enabled:
                on = False                      # 画不出来就不装成“已勾选”
            # 先设初值再连信号：否则 set_tracks 还在往面板里加行时就逐个发
            # visibilityChanged（半成品列表），主窗会把它当“用户选择”记住并回灌，
            # 结果把自己的轨迹（含主轨迹）取消勾选，而地图照样在画——图例与画面不一致。
            cb.setChecked(on)
            cb.toggled.connect(lambda _v, k=row.key: self._changed(k))
            h.addWidget(sw)
            h.addWidget(cb, 1)
            h.addWidget(cnt)
            lay.addWidget(host)
            row.check, row.swatch, row.cnt, row.host = cb, sw, cnt, host
            self._rows.append(row)
        self.adjustSize()

    def keys(self) -> list[str]:
        return [r.key for r in self._rows]

    def visible(self) -> list[str]:
        return [r.key for r in self._rows if self._row_visible(r)]

    def checkbox(self, key: str) -> Optional[QtWidgets.QCheckBox]:
        """按轨迹 key 取勾选框（差异面板/测试用，别去猜标签文本）。"""
        for r in self._rows:
            if r.key == str(key):
                return r.check
        return None

    def is_checked(self, key: str) -> bool:
        for r in self._rows:
            if r.key == str(key):
                return self._row_visible(r)
        return False

    def set_visible(self, keys: Iterable[str]) -> None:
        """外部恢复勾选状态（视图记忆）。不发 visibilityChanged。"""
        want = {str(k) for k in keys}
        self._muted = True
        try:
            for r in self._rows:
                r.check.setChecked(r.key in want)
        finally:
            self._muted = False

    def summary(self) -> str:
        return f"{len(self.visible())}/{len(self._rows)} 条轨迹"

    # ------------------------------------------------------------------ 内部
    def _row_visible(self, r: "TrackLegendPanel.Row") -> bool:
        return bool(r.check and r.check.isEnabled() and r.check.isChecked())

    def _changed(self, key: str) -> None:
        if not self._muted:
            self.visibilityChanged.emit(self.visible())

    def _set_all(self, on: bool) -> None:
        self._muted = True
        try:
            for r in self._rows:
                if r.check.isEnabled():          # 画不出来的不要代用户勾上
                    r.check.setChecked(on)
        finally:
            self._muted = False
        self.visibilityChanged.emit(self.visible())

    def _only_primary(self) -> None:
        self._muted = True
        try:
            for r in self._rows:
                if r.check.isEnabled():
                    r.check.setChecked(r.primary)
        finally:
            self._muted = False
        self.visibilityChanged.emit(self.visible())

    def sizeHint(self) -> QtCore.QSize:          # noqa: N802
        base = super().sizeHint()
        return QtCore.QSize(max(168, base.width()), base.height())
