# -*- coding: utf-8 -*-
"""apmlog.ui.theme —— 整个应用的界面主题：跟随系统 / 浅色 / 深色 / 自定义。

为什么只换 QPalette 而不装全局 QSS：

* 本项目的界面全是原生 Qt 控件（工具条、停靠窗、表格、树、对话框），palette 就能整体换肤；
* 全局 QSS 会连带改写 QMenu/QToolTip/QScrollBar 的绘制，和 ``settings.install_tip_style``
  装的 QProxyStyle 抢活，历史上这类混用最容易出“某个控件突然不刷新”的怪问题；
* pyqtgraph 的曲线配色由 :mod:`apmlog.ui.seriesplot` 自己的 ``_THEME`` 管，地图是影像底图，
  两者都不吃 palette —— 所以曲线配色继续由 ``settings["theme"]`` 单独控制。

自定义模式下曲线用浅色还是深色，按“输入区底色”的亮度自动推：暗底配亮线，不会糊成一团。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from PySide6 import QtCore, QtGui, QtWidgets

#: 界面主题取值。system 表示跟随操作系统深浅色。
UI_THEMES: tuple[str, ...] = ("system", "light", "dark", "custom")
UI_THEME_CN: dict[str, str] = {
    "system": "跟随系统",
    "light": "浅色",
    "dark": "深色",
    "custom": "自定义",
}

#: 自定义模式的四个颜色键（直接作为 settings 顶层键存）
CUSTOM_KEYS: tuple[str, ...] = ("c_window", "c_base", "c_text", "c_accent")
CUSTOM_LABELS: dict[str, str] = {
    "c_window": "控件底色",
    "c_base": "输入区底色",
    "c_text": "文字颜色",
    "c_accent": "强调色",
}
CUSTOM_HINTS: dict[str, str] = {
    "c_window": "窗口、工具条、表格底",
    "c_base": "列表、时间线、输入框底",
    "c_text": "正文文字",
    "c_accent": "选中行、游标、按钮高亮",
}

BUILTIN: dict[str, dict[str, str]] = {
    "light": {"window": "#f2f3f5", "base": "#ffffff", "text": "#1f2328", "accent": "#2f6fed"},
    "dark": {"window": "#2b2f36", "base": "#1e2227", "text": "#e8eaed", "accent": "#5b9dff"},
}

#: 自定义的初值取浅色那套：用户想深就自己往深调，不会一上来就黑成一片
CUSTOM_DEFAULTS: dict[str, str] = {
    "c_window": BUILTIN["light"]["window"],
    "c_base": BUILTIN["light"]["base"],
    "c_text": BUILTIN["light"]["text"],
    "c_accent": BUILTIN["light"]["accent"],
}

#: 出任何问题都回到这个内置主题（插件主题失效时的黍底）
DEFAULT_THEME = "system"

#: 插件主题在设置里的存法：``plugin:<插件 id>:<主题 id>``。
#: 存完整值而不是光主题 id，是为了停用/删插件后能一眼看出属于谁。
PLUGIN_PREFIX = "plugin:"

#: 四色角色的别名（插件写 window 或 c_window 都认）
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "window": ("window", "c_window"),
    "base": ("base", "c_base"),
    "text": ("text", "c_text"),
    "accent": ("accent", "c_accent"),
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def is_hex_color(value: Any) -> bool:
    """只接受 ``#rgb`` / ``#rrggbb`` 形式的字符串，别的都算脏数据。"""
    s = str(value or "").strip().lower()
    if not s.startswith("#") or len(s) not in (4, 7):
        return False
    try:
        int(s[1:], 16)
    except ValueError:
        return False
    return QtGui.QColor(s).isValid()


def normalize_hex(value: Any, fallback: str) -> str:
    return str(value).strip().lower() if is_hex_color(value) else fallback


def _qcolor(value: Any) -> QtGui.QColor:
    """收字符串也收 QColor。注意别用 str(QColor)：PySide6 里它不是十六进制，回解会变无效色。"""
    if isinstance(value, QtGui.QColor):
        return QtGui.QColor(value)
    return QtGui.QColor(str(value))


def luminance(color: Any) -> float:
    """0（全黑）~1（全白）的相对亮度，用来判断该配亮字还是暗字。"""
    c = _qcolor(color)
    if not c.isValid():
        c = QtGui.QColor("#808080")
    c = c.toRgb()
    return (0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()) / 255.0


def _lin(v: float) -> float:
    """sRGB 单通道线性化（WCAG 相对亮度算法里的那一步）。"""
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def rel_luminance(color: Any) -> float:
    c = _qcolor(color).toRgb()
    if not c.isValid():
        c = QtGui.QColor("#808080").toRgb()
    return (0.2126 * _lin(c.red() / 255.0) + 0.7152 * _lin(c.green() / 255.0)
            + 0.0722 * _lin(c.blue() / 255.0))


def contrast(fg: Any, bg: Any) -> float:
    """WCAG 对比度：1（完全同色）~21（黑白）。正文建议 ≥ 4.5。"""
    a, b = rel_luminance(fg), rel_luminance(bg)
    hi, lo = (a, b) if a >= b else (b, a)
    return (hi + 0.05) / (lo + 0.05)


def readable(fg: Any, bg: Any, min_ratio: float = 4.5) -> str:
    """保持色相，只把明度往对比度更高的方向推，推到能看清为止。

    写死的辅助色（淡黄注释、灰色提示、模式色带上的文字）在另一套主题下
    很容易“白底白字”，这里统一按实际底色算一遍，而不是靠人记住每个颜色属于哪套主题。
    """
    f, b = _qcolor(fg), _qcolor(bg)
    if not f.isValid() or not b.isValid():
        return str(fg)
    if contrast(f, b) >= min_ratio:
        return f.name()
    h, s, l, a = f.getHsl()
    up = contrast(QtGui.QColor.fromHsl(h, s, 255, a), b) >= contrast(QtGui.QColor.fromHsl(h, s, 0, a), b)
    step = 1 if up else -1
    cur = l
    for _ in range(256):
        cur += step
        if not 0 <= cur <= 255:
            break
        cand = QtGui.QColor.fromHsl(h, s, cur, a)
        if contrast(cand, b) >= min_ratio:
            return cand.name()
    return "#ffffff" if up else "#111318"


def bg_of(widget: Optional[QtWidgets.QWidget]) -> str:
    """控件实际露出来的底色（QLabel 默认透明，看到的是窗口色）。"""
    try:
        return widget.palette().color(QtGui.QPalette.ColorRole.Window).name()
    except Exception:                                # noqa: BLE001
        return "#ffffff"


def readable_on(widget: Optional[QtWidgets.QWidget], fg: Any, min_ratio: float = 4.5) -> str:
    return readable(fg, bg_of(widget), min_ratio)


def _mix(a: str, b: str, fa: float) -> str:
    """按权重 fa 混合两个颜色，用来生成禁用态、占位色、交替底色。"""
    ca, cb = QtGui.QColor(a).toRgb(), QtGui.QColor(b).toRgb()
    fb = 1.0 - fa
    return "#{:02x}{:02x}{:02x}".format(
        int(round(ca.red() * fb + cb.red() * fa)),
        int(round(ca.green() * fb + cb.green() * fa)),
        int(round(ca.blue() * fb + cb.blue() * fa)),
    )


def system_scheme(app: Optional[QtWidgets.QApplication] = None) -> str:
    """返回 "dark"/"light"。任何一步不可用都退回 light，绝不上抛。"""
    app = app or QtWidgets.QApplication.instance()
    try:
        cs = app.styleHints().colorScheme()
        if cs == QtCore.Qt.ColorScheme.Dark:
            return "dark"
        if cs == QtCore.Qt.ColorScheme.Light:
            return "light"
    except Exception:                                # noqa: BLE001 老 Qt / 无头环境
        pass
    try:                                             # 兜底：看当前窗口底色是明是暗
        return "dark" if luminance(app.palette().color(QtGui.QPalette.ColorRole.Window)) < 0.5 else "light"
    except Exception:                                # noqa: BLE001
        return "light"


# --------------------------------------------------------------------------- #
# 规格 → palette
# --------------------------------------------------------------------------- #
def spec_for(mode: str, custom: Optional[Mapping[str, Any]] = None) -> dict[str, str]:
    """把界面主题模式摊平成四个具体颜色；未知模式一律按 system 处理。"""
    m = str(mode or "system")
    if m in ("light", "dark"):
        return dict(BUILTIN[m])
    if m == "custom":
        src = custom or {}
        return {
            "window": normalize_hex(src.get("c_window"), CUSTOM_DEFAULTS["c_window"]),
            "base": normalize_hex(src.get("c_base"), CUSTOM_DEFAULTS["c_base"]),
            "text": normalize_hex(src.get("c_text"), CUSTOM_DEFAULTS["c_text"]),
            "accent": normalize_hex(src.get("c_accent"), CUSTOM_DEFAULTS["c_accent"]),
        }
    return dict(BUILTIN[system_scheme()])


def curve_for(spec: Mapping[str, str]) -> str:
    """曲线配色：输入区底色暗 → 用 dark 调色板（亮线），否则 light。"""
    return "dark" if luminance(spec.get("base", "#ffffff")) < 0.5 else "light"


# --------------------------------------------------------------------------- #
# 插件主题（外部插件提供的四色主题）
# --------------------------------------------------------------------------- #
def theme_value(plugin_id: str, theme_id: str) -> str:
    """插件主题在设置/信号里的规范值。"""
    return f"{PLUGIN_PREFIX}{plugin_id}:{theme_id}"


def is_plugin_theme(value: Any) -> bool:
    return str(value or "").startswith(PLUGIN_PREFIX)


def parse_plugin_theme(value: Any) -> Optional[tuple[str, str]]:
    """``plugin:<pid>:<主题>`` → (pid, 主题)；不是这个形式就返回 None。"""
    s = str(value or "")
    if not s.startswith(PLUGIN_PREFIX):
        return None
    pid, sep, tid = s[len(PLUGIN_PREFIX):].partition(":")
    if not (sep and pid and tid):
        return None
    return pid, tid


def spec_from_theme(theme: Any) -> dict[str, str]:
    """插件主题 → 四色规格。缺角色或颜色非法就抛 ``ValueError``（不猜、不修正）。"""
    colors = theme.get("colors") if isinstance(theme, Mapping) else getattr(theme, "colors", None)
    if not isinstance(colors, Mapping):
        raise ValueError("主题必须给出 colors（window/base/text/accent 四色）")
    out: dict[str, str] = {}
    missing: list[str] = []
    for role, names in ROLE_ALIASES.items():
        raw = None
        for n in names:
            if n in colors:
                raw = colors[n]
                break
        if not is_hex_color(raw):
            missing.append(role)
            continue
        out[role] = str(raw).strip().lower()
    if missing:
        raise ValueError("主题颜色缺失或非法：" + "、".join(missing))
    return out


def apply_spec(app: Optional[QtWidgets.QApplication], spec: Mapping[str, str],
               curve: str = "") -> str:
    """把一份**已校验**的四色规格刷成 palette；返回曲线配色。"""
    if app is None:
        return curve if curve in ("light", "dark") else "light"
    app.setPalette(palette_for(spec))
    return curve if curve in ("light", "dark") else curve_for(spec)


def palette_for(spec: Mapping[str, str]) -> QtGui.QPalette:
    """由四色规格生成完整 QPalette（含禁用态、交替底色、提示框、选中反白）。"""
    win = str(spec.get("window", "#f2f3f5"))
    base = str(spec.get("base", "#ffffff"))
    text = str(spec.get("text", "#1f2328"))
    accent = str(spec.get("accent", "#2f6fed"))

    pal = QtGui.QPalette()
    on_accent = "#ffffff" if luminance(accent) < 0.6 else "#111318"
    disabled = _mix(text, win, 0.55)                 # 禁用文字：往底色靠一点
    placeholder = _mix(text, base, 0.62)
    alternate = _mix(base, win, 0.45)
    tip_bg = _mix(base, "#ffffe1", 0.12)

    roles_active: list[tuple[QtGui.QPalette.ColorRole, str]] = [
        (QtGui.QPalette.ColorRole.Window, win),
        (QtGui.QPalette.ColorRole.WindowText, text),
        (QtGui.QPalette.ColorRole.Base, base),
        (QtGui.QPalette.ColorRole.AlternateBase, alternate),
        (QtGui.QPalette.ColorRole.Text, text),
        (QtGui.QPalette.ColorRole.Button, win),
        (QtGui.QPalette.ColorRole.ButtonText, text),
        (QtGui.QPalette.ColorRole.ToolTipBase, tip_bg),
        (QtGui.QPalette.ColorRole.ToolTipText, text),
        (QtGui.QPalette.ColorRole.PlaceholderText, placeholder),
        (QtGui.QPalette.ColorRole.Link, accent),
        (QtGui.QPalette.ColorRole.Highlight, accent),
        (QtGui.QPalette.ColorRole.HighlightedText, on_accent),
    ]
    groups = (QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorGroup.Inactive)
    for grp in groups:
        for role, col in roles_active:
            pal.setColor(grp, role, QtGui.QColor(col))
    for role, col in roles_active:
        if role in (QtGui.QPalette.ColorRole.Highlight, QtGui.QPalette.ColorRole.HighlightedText,
                    QtGui.QPalette.ColorRole.Window, QtGui.QPalette.ColorRole.Base,
                    QtGui.QPalette.ColorRole.AlternateBase, QtGui.QPalette.ColorRole.ToolTipBase,
                    QtGui.QPalette.ColorRole.Link):
            pal.setColor(QtGui.QPalette.ColorGroup.Disabled, role, QtGui.QColor(col))
        else:
            pal.setColor(QtGui.QPalette.ColorGroup.Disabled, role, QtGui.QColor(disabled))
    return pal


def apply(app: Optional[QtWidgets.QApplication], mode: str,
          custom: Optional[Mapping[str, Any]] = None) -> str:
    """把主题刷到整个应用；返回该主题下推荐的曲线配色。"""
    if app is None:
        return "light"
    spec = spec_for(mode, custom)
    app.setPalette(palette_for(spec))
    return curve_for(spec)


def apply_settings(app: Optional[QtWidgets.QApplication] = None,
                   settings_obj: Any = None) -> str:
    """从 Settings 读 ui_theme + c_*，刷 palette，返回推荐曲线配色。

    延迟导入 settings 是为了让本模块能被单独 import（不触发设置文件读写）。
    """
    if settings_obj is None:
        from apmlog.ui.settings import settings as _s
        settings_obj = _s()
    app = app or QtWidgets.QApplication.instance()
    custom = {k: settings_obj.get(k) for k in CUSTOM_KEYS}
    return apply(app, str(settings_obj.get("ui_theme")), custom)
