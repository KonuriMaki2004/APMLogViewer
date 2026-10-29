"""示例主题插件 —— 两套配色，外加「插件自己切换主题」的演示。

安装：拷到 ``%LOCALAPPDATA%\\APMLogViewer\\plugins\\``，再在「插件」页点
「重新扫描并重载」。之后在 设置 → 界面主题 的下拉里就能选到「插件：午夜蓝 / 暖纸」，
也可以在「插件」页的「插件主题」里点「应用主题」。

主题只有四个颜色角色，全部必须是 ``#rgb`` / ``#rrggbb``：
``window`` 窗口/工具条底、``base`` 列表/输入区底、``text`` 正文、``accent`` 选中与游标。
少给一个角色、或颜色写错（例如写成 ``red``），宿主会**拒绝注册**并在插件页记错误——
不会猜、也不会半套生效。``curve`` 留空 = 按 base 亮度自动选亮线/暗线。
"""
from __future__ import annotations

PLUGIN_NAME = "示例：两套配色主题"
__version__ = "1.0"

from PySide6 import QtWidgets

from apmlog.plugins import ThemeSpec

MIDNIGHT = ThemeSpec(id="midnight", name="午夜蓝", colors={
    "window": "#131a26", "base": "#0d1320", "text": "#e4ecff", "accent": "#4d9dff"})

SEPIA = ThemeSpec(id="sepia", name="暖纸", curve="light", colors={
    "window": "#f2ead9", "base": "#fbf6ec", "text": "#3a3226", "accent": "#a8541c"})


def register(ctx) -> None:
    ctx.register_theme(MIDNIGHT)
    ctx.register_theme(SEPIA)

    # 「应用主题」能力：插件可以自己切换（失败会回退内置默认主题，本插件被停用）
    page = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(page)
    lay.addWidget(QtWidgets.QLabel("点下面的按钮让宿主立刻换成这套配色："))
    row = QtWidgets.QHBoxLayout()
    for spec in (MIDNIGHT, SEPIA):
        b = QtWidgets.QPushButton(spec.name)
        b.clicked.connect(lambda _c=False, s=spec: ctx.apply_theme(s.id))
        row.addWidget(b)
    b2 = QtWidgets.QPushButton("回到跟随系统")
    b2.clicked.connect(lambda: ctx.apply_theme("system"))
    row.addWidget(b2)
    row.addStretch(1)
    lay.addLayout(row)
    lay.addStretch(1)
    ctx.register_tab("主题示例", page)

    # 收主题变化事件：别的插件/用户在设置里换主题也能知道
    def on_theme(value: str) -> None:
        ctx.log.info("当前主题变了：%s", value)

    ctx.signals.theme_changed.connect(on_theme)
