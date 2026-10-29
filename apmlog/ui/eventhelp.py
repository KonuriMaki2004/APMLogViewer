"""apmlog.ui.eventhelp —— 「事件/告警说明」弹窗。

用户双击「事件」页里的警告/错误行、或「告警」页的一条告警时弹出：
问题说明 / 可能原因 / 处理建议，全部对应**被双击的那一条具体类型**。

两条硬约束：
1. 弹窗**永远有内容**：知识库没收录时显示明确的"暂未收录"提示 + 通用排查步骤（不空白）。
2. 只读：不改变任何视图状态；关闭后光标仍在原处，原有双击/单击行为不受影响。
"""
from __future__ import annotations

import html
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.knowledge import HelpEntry
from . import theme as ui_theme

SEV_ZH = {"info": "信息", "hint": "提示", "warn": "警告", "error": "错误", "critical": "严重"}
SEV_RGB = {"info": "#4b7bec", "hint": "#7f8fa6", "warn": "#f0a500", "error": "#e05c3a",
           "critical": "#c0392b"}


def _html(entry: HelpEntry) -> str:
    """生成弹窗正文 HTML（所有外部文本都转义）。"""
    def esc(x: str) -> str:
        return html.escape(str(x)).replace("\n", "<br>")

    parts: list[str] = [f"<h2 style='margin:0 0 6px 0;'>{esc(entry.title)}</h2>"]
    if entry.is_fallback:
        parts.append(
            "<p style='margin:0 0 10px 0; padding:6px 8px; "
            "border-left:4px solid #f0a500; background:rgba(240,165,0,0.12);'>"
            "<b>该类型暂未收录具体说明与方案</b>，下面是通用排查步骤。</p>")
    parts.append(f"<h3 style='margin:12px 0 4px 0;'>问题说明</h3><p>{esc(entry.summary)}</p>")
    parts.append("<h3 style='margin:12px 0 4px 0;'>可能原因</h3><ul>")
    parts += [f"<li>{esc(c)}</li>" for c in entry.causes]
    parts.append("</ul>")
    parts.append("<h3 style='margin:12px 0 4px 0;'>处理建议</h3><ol>")
    parts += [f"<li>{esc(a)}</li>" for a in entry.actions]
    parts.append("</ol>")
    if entry.refs:
        parts.append("<h3 style='margin:12px 0 4px 0;'>参考</h3><ul>")
        parts += [f"<li>{esc(r)}</li>" for r in entry.refs]
        parts.append("</ul>")
    if entry.note:
        parts.append(f"<p style='margin-top:10px;'><i>{esc(entry.note)}</i></p>")
    return "".join(parts)


class EventHelpDialog(QtWidgets.QDialog):
    """只读的说明弹窗。"""

    def __init__(self, entry: HelpEntry, *, subject: str = "", meta: str = "",
                 raw: str = "", parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.setWindowTitle("说明与处理建议")
        self.setObjectName("EventHelpDialog")
        self.resize(760, 620)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(8)

        head = QtWidgets.QHBoxLayout()
        self.lbl_subject = QtWidgets.QLabel(subject or entry.title)
        f = self.lbl_subject.font()
        f.setBold(True)
        self.lbl_subject.setFont(f)
        self.lbl_subject.setWordWrap(True)
        head.addWidget(self.lbl_subject, 1)
        self.lbl_scope = QtWidgets.QLabel("具体类型" if entry.matched else "未收录")
        self.lbl_scope.setStyleSheet(
            "padding:1px 8px; border-radius:8px; color:%s; border:1px solid %s;"
            % (("#2e7d32", "#2e7d32") if entry.matched else ("#8a6d1b", "#8a6d1b")))
        head.addWidget(self.lbl_scope, 0)
        root.addLayout(head)

        if meta:
            self.lbl_meta = QtWidgets.QLabel(meta)
            self.lbl_meta.setWordWrap(True)
            self.lbl_meta.setStyleSheet(f"color:{ui_theme.readable_on(self.lbl_meta, '#8a8f98')};")
            root.addWidget(self.lbl_meta)

        if raw:
            self.lbl_raw = QtWidgets.QLabel(f"日志原文：{raw}")
            self.lbl_raw.setWordWrap(True)
            self.lbl_raw.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            self.lbl_raw.setStyleSheet(
                f"color:{ui_theme.readable_on(self.lbl_raw, '#5a6472')};"
                "padding:4px 6px; border-radius:4px; background:rgba(128,128,128,0.10);")
            root.addWidget(self.lbl_raw)

        self.body = QtWidgets.QTextBrowser()
        self.body.setOpenExternalLinks(False)
        self.body.setReadOnly(True)
        self.body.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.body.setHtml(_html(entry))
        root.addWidget(self.body, 1)

        foot = QtWidgets.QHBoxLayout()
        self.lbl_hint = QtWidgets.QLabel("只读说明：不改动日志与视图")
        self.lbl_hint.setStyleSheet(
            f"color:{ui_theme.readable_on(self.lbl_hint, '#8a8f98')};")
        foot.addWidget(self.lbl_hint, 1)
        self.btn_copy = QtWidgets.QPushButton("复制说明")
        self.btn_copy.clicked.connect(self._copy)
        self.btn_close = QtWidgets.QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        foot.addWidget(self.btn_copy)
        foot.addWidget(self.btn_close)
        root.addLayout(foot)
        self.btn_close.setDefault(True)
        self.btn_close.setFocus()

    def plain_text(self) -> str:
        """复制到剪贴板的内容（含上下文，便于贴进工单）。"""
        head = []
        if getattr(self, "lbl_subject", None) is not None:
            head.append(self.lbl_subject.text())
        if getattr(self, "lbl_meta", None) is not None:
            head.append(self.lbl_meta.text())
        if getattr(self, "lbl_raw", None) is not None:
            head.append(self.lbl_raw.text())
        return "\n".join(head) + "\n\n" + self.entry.as_text()

    def _copy(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.plain_text())
        self.lbl_hint.setText("已复制到剪贴板")


__all__ = ["EventHelpDialog", "SEV_ZH", "SEV_RGB"]
