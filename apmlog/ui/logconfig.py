# -*- coding: utf-8 -*-
"""apmlog.ui.logconfig —— “飞控未开启日志写入”的提示与手动处理方法。

本工程对飞控**严格只读**：只发 HEARTBEAT / LOG_REQUEST_* / PARAM_REQUEST_READ，
绝不发 PARAM_SET、COMMAND_LONG 之类的写/控制指令。所以检测到没开日志时，这里
只做两件事：

1. 把判定依据摊开（哪个参数、当前值、为什么这算“没开”）；
2. 给出**用户自己动手**的开启步骤（Mission Planner / QGC / MAVProxy 三条路 + 卡与
   存储检查），并把建议值标清来源是“你这块飞控自己在用的值”，不是拍脑袋的魔数。

以前这里放过一个“推一下就下发参数”的滑块，已按只读原则整体删除；不要再加回来。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ._font import ui_font
from .theme import readable

if TYPE_CHECKING:                                    # 只为类型提示，运行时不引入依赖
    from apmlog.link.params import LogWriteConfig    # noqa: F401

__all__ = ["LogWriteDialog", "guidance_text"]

_TITLE = "飞控未开启日志写入功能"

#: 手动处理方法。措辞里不含“本工具会替你改”的暗示，命令都是用户在别的地面站里执行。
_GUIDANCE = """本工具对飞控只读，不会替你修改任何参数。请按下面任一方式自行开启：

1. Mission Planner：连接飞控 → 设定(Setup) → 全参数(Full Parameter List) → 等参数下载完
   → 搜 LOG_BACKEND_TYPE 改为 1（写 SD 卡）→ 搜 LOG_BITMASK 改为非 0 → 点 Write Changes
   → 断电重启飞控。
2. QGroundControl：齿轮图标 → 完整参数文档 → 搜同名参数修改 → 重启飞控。
3. MAVProxy：param set LOG_BACKEND_TYPE 1 ；param set LOG_BITMASK 180222 ；然后重启飞控。

还要顺手检查这几项（这些都会导致“参数开了但仍然没有日志”）：
· SD 卡是否插好、是否 FAT32 格式、有没有物理损坏；
· 剩余空间是否低于 LOG_FILE_MB_FREE（默认要求 500 MB）；
· LOG_DISARMED=0 时地面不上锁不写，需要解锁起飞才会产生新日志文件；
· 改完参数后请回到本页点“刷新列表”，或直接重启飞控再连。"""


def guidance_text(cfg: Optional["LogWriteConfig"] = None) -> str:
    """把建议值来源拼到通用步骤后面，形成可整段复制的处理方法。"""
    if cfg is None:
        return _GUIDANCE
    suggest = tuple(getattr(cfg, "suggest", ()) or ())
    if not suggest:
        return _GUIDANCE
    note = str(getattr(cfg, "suggest_note", "") or "").strip()
    lines = ["建议目标值："]
    for name, cur, want in suggest:
        cur_s = "读不到" if cur is None else (f"{cur:g}")
        lines.append(f"· {name}：当前 {cur_s} → 建议 {want:g}")
    if note:
        lines.append(f"（{note}）")
    return _GUIDANCE + "\n\n" + "\n".join(lines)


class LogWriteDialog(QtWidgets.QDialog):
    """只读提示框：说明现状 + 给出手动开启步骤，不提供任何写按钮。"""

    def __init__(self, cfg: "LogWriteConfig", parent: Optional[QtWidgets.QWidget] = None
                 ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self.setWindowTitle("日志写入设置")
        self.setModal(True)
        self.setMinimumSize(520, 460)

        head = QtWidgets.QLabel(_TITLE)
        head.setFont(ui_font(10.5, bold=True))
        head.setWordWrap(True)

        self.detail = QtWidgets.QLabel(str(getattr(cfg, "detail", "") or ""))
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(8)
        outer.addWidget(head)
        outer.addWidget(self.detail)

        box = QtWidgets.QGroupBox("判定依据（本工具只读查询得到）")
        form = QtWidgets.QGridLayout(box)
        form.setContentsMargins(10, 6, 10, 8)
        form.setHorizontalSpacing(14)
        rows = [("LOG_BACKEND_TYPE", getattr(cfg, "backend", None)),
                ("LOG_BITMASK", getattr(cfg, "bitmask", None))]
        y = 0
        for label, pv in rows:
            if pv is None:
                continue
            found = bool(getattr(pv, "found", True))
            val = getattr(pv, "value", None)
            shown = self._fmt(val) if found else f"读不到（{self._reason(getattr(pv, 'reason', ''))}）"
            form.addWidget(QtWidgets.QLabel(label), y, 0)
            v = QtWidgets.QLabel(shown)
            v.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            if not found:
                v.setStyleSheet(f"color:{readable('#8a6d1b', self._bg(), 4.5)};")
            form.addWidget(v, y, 1)
            y += 1
        outer.addWidget(box)

        how = QtWidgets.QGroupBox("处理方法")
        hv = QtWidgets.QVBoxLayout(how)
        hv.setContentsMargins(10, 6, 10, 8)
        hv.setSpacing(6)
        self.txt = QtWidgets.QPlainTextEdit(guidance_text(cfg))
        self.txt.setReadOnly(True)
        self.txt.setFont(ui_font(9.0))
        self.txt.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        hv.addWidget(self.txt)
        outer.addWidget(how, 1)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        self.chk_clip = QtWidgets.QCheckBox("同时复制“处理方法”到剪贴板")
        self.chk_clip.setToolTip("方便直接粘到工单或笔记里")
        row.addWidget(self.chk_clip)
        row.addStretch(1)
        self.btn_copy = QtWidgets.QPushButton("复制处理方法")
        self.btn_copy.clicked.connect(self._copy)
        row.addWidget(self.btn_copy)
        self.btn_close = QtWidgets.QPushButton("知道了")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_close)
        outer.addLayout(row)

        self.btn_close.setDefault(True)           # 回车=关闭；这里本来也没有别的动作

    # ------------------------------------------------------------------ 内部
    def _bg(self) -> str:
        return self.palette().color(QtGui.QPalette.ColorRole.Window).name()

    @staticmethod
    def _fmt(v: Optional[float]) -> str:
        if v is None:
            return "读不到"
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "?"
        return f"{f:g}"

    @staticmethod
    def _reason(r: str) -> str:
        return {"timeout": "飞控未应答", "link_lost": "链路已断开",
                "": "未应答"}.get(str(r or ""), str(r or "未应答"))

    def _copy(self) -> None:
        QtWidgets.QApplication.clipboard().setText(guidance_text(self._cfg))

    def accept(self) -> None:                      # noqa: D102
        if self.chk_clip.isChecked():
            self._copy()
        super().accept()

    @property
    def config(self) -> "LogWriteConfig":
        return self._cfg
