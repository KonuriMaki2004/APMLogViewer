# -*- coding: utf-8 -*-
"""apmlog.ui.trackdiffpanel —— 地图右上角「轨迹差异」面板（挂在轨迹勾选栏下方）。

只负责显示，算数在 :mod:`apmlog.core.trackdiff` 里。页面必须自己把**口径与基准**
写出来（用户明确要求），所以面板底部固定一行小字，不能只塞进 tooltip。

用法::

    panel.set_pairs([("POS ↔ GPS#0", "0.02%", "均值 0.79 m / 里程 3.94 km …")],
                    ("总体（1 对）", "0.02%", "…"), note="…")
    panel.set_hint("勾选 2 条及以上轨迹，显示差异预估")
"""
from __future__ import annotations

from typing import Optional, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from ._font import panel_qss, ui_font

__all__ = ["TrackDiffPanel"]

_BG = ("background: rgba(18, 22, 26, 205);"
       "border: 1px solid rgba(255,255,255,64); border-radius: 7px;")

_CALIBER = ("口径：同一时刻的水平偏差均值 ÷ 重叠段两条轨迹里程的平均值。\n"
            "对齐：以采样较稀的一条为时间基准、另一条线性插值，"
            "空档 >2 s 或位置跳变处丢弃该样本。\n"
            "坐标：WGS-84 经纬度在共同原点重投影为 ENU 后取水平距离（不用地图像素坐标，"
            "也不受 GCJ-02 加偏影响）。\n"
            "不含高程：POS/GPS 与 XKF1/VISP 的高度基准不同，相减会留下未知常量偏置。\n"
            "总体：各对的均值偏差之和 ÷ 各对平均里程之和（按里程加权），不是简单平均。")


class TrackDiffPanel(QtWidgets.QWidget):
    """轨迹差异小面板（父控件是地图 viewport，和图例一样是覆盖层）。"""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(panel_qss(_BG + "color:#e8eef2;"))
        self._note = ""

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(9, 6, 9, 7)
        lay.setSpacing(3)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(5)
        title = QtWidgets.QLabel("轨迹差异")
        title.setFont(ui_font(9.5, bold=True))
        title.setToolTip(_CALIBER)
        head.addWidget(title)
        head.addStretch(1)
        self._tag = QtWidgets.QLabel("预估")
        self._tag.setFont(ui_font(7.5))
        self._tag.setStyleSheet(panel_qss("color:#9fb0bd;"))
        self._tag.setToolTip("按现有轨迹数据估算，不是真值误差")
        head.addWidget(self._tag)
        lay.addLayout(head)

        self._body = QtWidgets.QWidget(self)
        self._body.setStyleSheet(panel_qss("background: transparent; border: none;"))
        self._blay = QtWidgets.QVBoxLayout(self._body)
        self._blay.setContentsMargins(0, 0, 0, 0)
        self._blay.setSpacing(2)
        lay.addWidget(self._body)

        self._foot = QtWidgets.QLabel(
            "均值偏差 ÷ 平均里程（同期水平 ENU，不含高程）", self)
        self._foot.setFont(ui_font(7.5))
        self._foot.setStyleSheet(panel_qss("color:#9fb0bd; background: transparent;"))
        self._foot.setWordWrap(True)
        self._foot.setToolTip(_CALIBER)
        lay.addWidget(self._foot)
        self.hide()

    # ------------------------------------------------------------------ 数据
    def set_pairs(self, rows: Sequence[tuple[str, str, str]],
                  total: Optional[tuple[str, str, str]] = None,
                  note: str = "") -> None:
        """rows = [(左文本, 右文本, tooltip), …]；total 同理，加一条分隔线。"""
        self._clear()
        for left, right, tip in rows:
            self._blay.addLayout(self._pair_row(left, right, tip, bold=False))
        if total is not None and rows:
            line = QtWidgets.QFrame(self._body)
            line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            line.setFixedHeight(1)
            line.setStyleSheet(panel_qss("background: rgba(255,255,255,40); border: none;"))
            self._blay.addWidget(line)
        if total is not None:
            left, right, tip = total
            self._blay.addLayout(self._pair_row(left, right, tip, bold=True))
        self._set_note(note)
        self.adjustSize()

    def set_hint(self, text: str, note: str = "") -> None:
        self._clear()
        lab = QtWidgets.QLabel(text, self._body)
        lab.setFont(ui_font(8.0))
        lab.setStyleSheet(panel_qss("color:#c8d4dc; background: transparent;"))
        lab.setWordWrap(True)
        self._blay.addWidget(lab)
        self._set_note(note)
        self.adjustSize()

    # ------------------------------------------------------------------ 内部
    def _pair_row(self, left: str, right: str, tip: str, *, bold: bool) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        a = QtWidgets.QLabel(left, self._body)
        a.setFont(ui_font(8.5, bold=bold))
        a.setStyleSheet(panel_qss("background: transparent;"))
        b = QtWidgets.QLabel(right, self._body)
        b.setFont(ui_font(8.5, bold=True))
        b.setStyleSheet(panel_qss("background: transparent;"
                                  + ("" if bold else "color:#ffd166;")))
        b.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        for w in (a, b):
            w.setToolTip(tip or _CALIBER)
        row.addWidget(a, 1)
        row.addWidget(b, 0)
        return row

    def _set_note(self, note: str) -> None:
        self._note = str(note or "")
        text = "均值偏差 ÷ 平均里程（同期水平 ENU，不含高程）"
        if self._note:
            text += "\n" + self._note
        self._foot.setText(text)

    def _clear(self) -> None:
        while self._blay.count():
            item = self._blay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            sub = item.layout()
            if sub is not None:
                self._drop_layout(sub)

    @staticmethod
    def _drop_layout(lay: QtWidgets.QLayout) -> None:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def sizeHint(self) -> QtCore.QSize:          # noqa: N802
        base = super().sizeHint()
        return QtCore.QSize(max(196, base.width()), base.height())
