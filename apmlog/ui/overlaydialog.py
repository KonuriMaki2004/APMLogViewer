"""apmlog.ui.overlaydialog —— 「叠加绘制」对话框。

用户 2026-10-24：「在曲线位置加个"叠加绘制"功能」。

两种用法：
* **新建一条泳道**：所选通道合成一条新泳道，第一条是主曲线，其余是同轴叠加（虚线），
  共用一根 Y 轴，各有胶囊；
* **叠加到已有泳道**：所选通道作为那条泳道的叠加曲线追加进去
  （比如把 `BARO.Alt` 叠到 `轨迹 alt` 上直接比高度）。
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from PySide6 import QtCore, QtWidgets

from apmlog.core.model import ChannelKey

log = logging.getLogger(__name__)

#: 目标下拉里"新建一条泳道"的数据值（泳道标签不会为空，所以用它做哨兵）
NEW_LANE = ""

#: 新建泳道至少两条通道（第一条主曲线 + 至少一条叠加）
MIN_NEW_CHANNELS = 2


class OverlayDialog(QtWidgets.QDialog):
    """选通道 + 选目标 → 返回 (目标, 通道键列表)。"""

    def __init__(self, channels: Sequence[tuple[str, str]],
                 lane_labels: Sequence[str] = (),
                 preselected: Sequence[ChannelKey] = (),
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("叠加绘制")
        self.resize(560, 560)
        self._items: list[tuple[QtWidgets.QListWidgetItem, str]] = []
        self._pre = {k.key for k in preselected}

        lay = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("目标"))
        self.cb_target = QtWidgets.QComboBox()
        self.cb_target.addItem(f"新建一条泳道（第一条为主曲线，其余同轴叠加）", NEW_LANE)
        for lbl in lane_labels:
            self.cb_target.addItem(f"叠加到已有泳道：{lbl}", str(lbl))
        self.cb_target.setToolTip("新建：所选通道各占一条曲线，共用一根 Y 轴；\n"
                                 "叠加到已有：把所选通道画成那条道的虚线对比曲线")
        top.addWidget(self.cb_target, 1)
        lay.addLayout(top)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("过滤报文/字段，如 ALT、Roll、VIBE")
        lay.addWidget(self.search)

        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        lay.addWidget(self.list, 1)
        for key_str, text in channels:
            it = QtWidgets.QListWidgetItem(text)
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.CheckState.Checked if key_str in self._pre
                             else QtCore.Qt.CheckState.Unchecked)
            it.setData(QtCore.Qt.ItemDataRole.UserRole, key_str)
            self.list.addItem(it)
            self._items.append((it, key_str))

        self.lbl_count = QtWidgets.QLabel()
        self.lbl_count.setWordWrap(True)
        lay.addWidget(self.lbl_count)

        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                         | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

        self.search.textChanged.connect(self._filter)
        for it, _k in self._items:
            it.setHidden(False)
        self.list.itemChanged.connect(lambda _i: self._refresh_count())
        self.cb_target.currentIndexChanged.connect(lambda _i: self._refresh_count())
        self._refresh_count()

    # ---- 查询 ---------------------------------------------------------- #

    def checked_keys(self) -> list[str]:
        """勾选的通道键（按列表顺序，也就是通道类型/字段顺序）。"""
        out = []
        for it, key_str in self._items:
            if it.isHidden():
                continue
            if it.checkState() == QtCore.Qt.CheckState.Checked:
                out.append(key_str)
        return out

    def target(self) -> str:
        return str(self.cb_target.currentData() or NEW_LANE)

    def is_new_lane(self) -> bool:
        return self.target() == NEW_LANE

    # ---- 交互 ---------------------------------------------------------- #

    def _filter(self, text: str) -> None:
        t = (text or "").strip().upper()
        for it, _k in self._items:
            it.setHidden(bool(t) and t not in it.text().upper())
        self._refresh_count()

    def _refresh_count(self) -> None:
        n = len(self.checked_keys())
        if self.is_new_lane():
            ok = n >= MIN_NEW_CHANNELS
            hint = (f"已选 {n} 条：第 1 条为主曲线，其余 {max(0, n - 1)} 条同轴叠加"
                    if ok else f"已选 {n} 条；新建泳道至少要选 {MIN_NEW_CHANNELS} 条")
        else:
            ok = n >= 1
            hint = (f"已选 {n} 条，将叠加到「{self.target()}」" if ok
                    else "至少要选 1 条通道")
        self.lbl_count.setText(hint)
        self._set_ok_enabled(ok)

    def _ok_button(self) -> Optional[QtWidgets.QPushButton]:
        box = self.findChild(QtWidgets.QDialogButtonBox)
        if box is None:
            return None
        return box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)

    def _set_ok_enabled(self, on: bool) -> None:
        okb = self._ok_button()
        if okb is not None:
            okb.setEnabled(bool(on))

    def _ok_enabled(self) -> bool:
        okb = self._ok_button()
        return True if okb is None else bool(okb.isEnabled())

    def accept(self) -> None:                      # noqa: D102
        if self.is_new_lane() and len(self.checked_keys()) < MIN_NEW_CHANNELS:
            return                                  # 按钮已置灰，这里只是双保险
        if not self.checked_keys():
            return
        super().accept()


__all__ = ["OverlayDialog", "NEW_LANE", "MIN_NEW_CHANNELS"]
