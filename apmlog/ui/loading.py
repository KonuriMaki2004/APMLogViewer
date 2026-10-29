"""日志加载遮罩：盖在右侧标签区上的「正在读取日志」进度面板。

为什么单独一个文件：它要能被 offscreen 单测独立构造，且不依赖 MainWindow。

行为约定（用户提的）：

* 底色就是**主题色**（走 QPalette，不装全局 QSS，与 theme.py 的分层一致）；
* 上方写「正在读取日志」，中间进度条**跟着解析百分比走**；
* 解析完毕（成功或失败）立刻恢复正常显示；
* **不许闪一下**：命中缓存时解析只要 0.01s，若一调用就显示会白闪一下，
  所以延迟 ``SHOW_DELAY_MS`` 才真正显示，快加载全程不可见。
"""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from apmlog.ui._font import ui_font

#: 标题文案（用户指定的原话）
TITLE = "正在读取日志"
#: 延迟显示，避免缓存命中时的白闪
SHOW_DELAY_MS = 180
#: 进度条占遮罩宽度的比例与下限（用户要求“做长做大一点”，不再是小窄条）
BAR_WIDTH_RATIO = 0.62
BAR_MIN_WIDTH = 320
BAR_EDGE_MARGIN = 48
BAR_HEIGHT = 36
#: 字号：标题 > 百分比 > 阶段说明
TITLE_PT = 15.0
PCT_PT = 14.0
PHASE_PT = 10.0


class LoadingOverlay(QtWidgets.QWidget):
    """覆盖 host 内容区的读取遮罩。

    host 一般是 ``QTabWidget``：遮罩只盖标签页内容区，**标签条仍然露在外面**，
    这样用户能看出自己在哪个页签上，也不会在加载期间误以为程序换了界面。
    """

    def __init__(self, host: QtWidgets.QTabWidget,
                 show_delay_ms: int = SHOW_DELAY_MS) -> None:
        super().__init__(host)
        self._host = host
        # 主题底色：QPalette Window + autoFillBackground，别用硬编码颜色，
        # 否则换深色/自定义主题时遮罩还是白的。
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QtGui.QPalette.ColorRole.Window, pal.color(QtGui.QPalette.ColorRole.Window))
        self.setPalette(pal)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(14)
        lay.addStretch(1)

        self.lbl_title = QtWidgets.QLabel(TITLE, self)
        f = ui_font(TITLE_PT)
        f.setBold(True)
        self.lbl_title.setFont(f)
        self.lbl_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.lbl_title)

        self.bar = QtWidgets.QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.bar.setFormat("%p%")
        # 百分比写在条内居中：字号太小就白瞎了这根粗条。
        pf = ui_font(PCT_PT)
        pf.setBold(True)
        self.bar.setFont(pf)
        self.bar.setFixedHeight(BAR_HEIGHT)
        self._apply_bar_width()
        lay.addWidget(self.bar, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

        self.lbl_phase = QtWidgets.QLabel("", self)
        self.lbl_phase.setFont(ui_font(PHASE_PT))
        self.lbl_phase.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_phase.setWordWrap(True)
        self.lbl_phase.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_phase)
        lay.addStretch(2)

        # 遮罩自己吃掉鼠标，避免加载途中点到底下没画完的图。
        self.setMouseTracking(False)

        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(0, int(show_delay_ms)))
        self._timer.timeout.connect(self._reveal)
        self._active = False
        self._shown_by_timer = False

        host.installEventFilter(self)
        self.setGeometry(self._target_rect())
        self.hide()

    # ------------------------------------------------------------------ 对外
    def begin(self, name: str = "") -> None:
        """开始一次加载：复位进度，延迟显示。"""
        self._active = True
        self._shown_by_timer = False
        self.bar.setValue(0)
        self.lbl_title.setText(TITLE)
        self.lbl_phase.setText(f"文件：{name}" if name else "")
        self._timer.start()

    def set_progress(self, fraction: float, message: str = "") -> None:
        """跟着解析百分比走。fraction 允许 <0 / >1，一律夹到 0..100%。"""
        if not self._active:
            return
        try:
            fr = float(fraction)
        except (TypeError, ValueError):
            return
        if not fr == fr:                      # NaN
            return
        pct = int(max(0.0, min(1.0, fr)) * 100 + 0.5)
        self.bar.setValue(pct)
        if message:
            self.lbl_phase.setText(str(message))
        # 已经过了显示延迟（快加载）就别再跳出来
        if not self._timer.isActive() and not self.isVisible() and pct < 100:
            self._reveal()

    def finish(self, keep_pct: bool = True) -> None:
        """进度到 100% 但界面还没填好：先满条，等 end() 再收。"""
        if self._active and keep_pct:
            self.bar.setValue(100)

    def end(self) -> None:
        """恢复正常显示。成功、失败、异常都要走这里。"""
        self._active = False
        self._shown_by_timer = False
        self._timer.stop()
        self.hide()
        self.bar.setValue(0)
        self.lbl_phase.setText("")

    @property
    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------ 内部
    def _reveal(self) -> None:
        if not self._active or self.isVisible():
            return
        self.setGeometry(self._target_rect())
        self.show()
        self.raise_()
        self._shown_by_timer = True

    def _target_rect(self) -> QtCore.QRect:
        """host 的内容区：标签条下方到边界。"""
        r = self._host.rect()
        bar: Optional[QtCore.QRect] = None
        try:
            tb = self._host.tabBar()
            if tb is not None:
                bar = tb.geometry()
        except Exception:                            # pragma: no cover
            bar = None
        if bar is not None and bar.height() > 0 and bar.top() <= 2:
            top = bar.bottom() + 1
            return QtCore.QRect(0, top, r.width(), max(1, r.height() - top))
        return QtCore.QRect(r)

    def _apply_bar_width(self) -> None:
        """进度条跟着遮罩宽度走：拿内容区的 62%，但不小于 320 px、不贴边。

        用 setFixedWidth 而不是布局 stretch：QHBoxLayout 的 stretch 分的是**多余**
        空间，算不出确定比例，测试也没法钉。宽度在 resizeEvent 里重算。

        坑（不是一句防御式代码）：``QWidget.__init__`` 内部就会触发 resizeEvent，
        那时 ``bar`` / ``_host`` 还没赋值，跑上去就是 AttributeError（PySide6 在
        Qt 回调里报错不抛出，只在 stderr 刷屏，很容易漏看）。trackmap 里早就记过同一条。
        """
        bar = getattr(self, "bar", None)
        if bar is None:
            return
        w = self.width()
        if w <= 1:
            host = getattr(self, "_host", None)
            w = host.width() if host is not None else 0
        if w <= 1:
            return
        want = max(BAR_MIN_WIDTH, int(w * BAR_WIDTH_RATIO))
        want = min(want, max(120, w - BAR_EDGE_MARGIN * 2))
        bar.setFixedWidth(want)

    def resizeEvent(self, ev: QtGui.QResizeEvent) -> None:
        self._apply_bar_width()
        super().resizeEvent(ev)

    def eventFilter(self, obj: QtCore.QObject, ev: QtCore.QEvent) -> bool:
        if getattr(self, "_host", None) is not None and obj is self._host \
                and ev.type() in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show):
            self.setGeometry(self._target_rect())
            self._apply_bar_width()
        return False

    def paintEvent(self, ev: QtGui.QPaintEvent) -> None:
        """先按调色板铺满主题底色，再交给普通控件绘制。

        遮罩必须**完全不透明**：底下是上一份日志没清掉的曲线/地图，
        半透会透出残影，看起来像没加载完。
        """
        p = QtGui.QPainter(self)
        try:
            p.fillRect(self.rect(), self.palette().color(QtGui.QPalette.ColorRole.Window))
        finally:
            p.end()
        super().paintEvent(ev)
