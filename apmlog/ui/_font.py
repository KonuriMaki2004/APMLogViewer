# -*- coding: utf-8 -*-
"""apmlog.ui._font —— UI 覆盖层共用的字体助手。

单独成模块的原因：trackmap 要放仪表浮层，仪表也要用同一套中文字体选择逻辑；
如果 gauges 反过来 from .trackmap import ui_font 就会形成循环导入。
"""
from __future__ import annotations

from typing import Optional

from PySide6 import QtGui

#: 中文标签候选字体。**offscreen 平台插件的 QFontDatabase 是空的**（本机实测
#: families()==0，而真实 windows 平台有 442 个族含 Microsoft YaHei），所以只能
#: “有则用、无则退回默认”；在 offscreen 下中文会渲染成方框，这是平台限制。
_FONT_CANDIDATES = ("Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC",
                    "PingFang SC", "SimHei", "Segoe UI", "Arial Unicode MS")
_font_family_cache: Optional[str] = None


def ui_font_family() -> str:
    """选一个系统里真正存在、且优先含 CJK 字形的字体族。"""
    global _font_family_cache
    if _font_family_cache is None:
        try:
            fams = set(QtGui.QFontDatabase.families())
        except Exception:
            fams = set()
        _font_family_cache = next((f for f in _FONT_CANDIDATES if f in fams), "")
    return _font_family_cache


def ui_font(point_size: float = 8.0, bold: bool = False) -> QtGui.QFont:
    fam = ui_font_family()
    f = QtGui.QFont(fam if fam else "Segoe UI", point_size)
    f.setBold(bold)
    f.setHintingPreference(QtGui.QFont.HintingPreference.PreferNoHinting)
    return f


def panel_qss(base: str) -> str:
    """给覆盖层控件的样式表补上可用的中文字体族。"""
    fam = ui_font_family()
    return base + (f" font-family:'{fam}';" if fam else "")


__all__ = ["ui_font", "ui_font_family", "panel_qss"]
