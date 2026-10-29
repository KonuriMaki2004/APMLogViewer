# -*- coding: utf-8 -*-
"""apmlog.ui.gauges —— 地图左下角的飞行仪表浮层：航向罗盘 + 姿态球。

设计约束（都是踩过的坑）：
* 仪表盘面**固定深色**，像真实航电一样；盘上的字一律用 theme.readable() 按盘面
  底色反算，绝不写死白字——浅色主题下也不会糊。面板边框/说明文字才跟主题走。
* 它是 view.viewport() 的子控件，所以要自己处理“不挡住缩放条、随窗口重定位”。
* 形状填充必须走 setBrush(QBrush(...)) + drawPath：PySide6 6.12 的
  fillPath(path, QColor) 不报错也不画东西，罗盘箭头一度只剩白描边。
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from ._font import ui_font
from .theme import readable

FACE = "#161b21"            # 盘面底色（航电风格，恒定深色）
FACE_EDGE = "#3b454f"
SKY = "#2f6fed"
GROUND = "#8a5a2b"
LADDER = "#e8eef2"
NOSE_RED = "#e03131"
INK_DIM = "#9fb0bd"


def _fmt_deg(v: float, digits: int = 0) -> str:
    return "—" if not math.isfinite(v) else f"{v:.{digits}f}°"


def wrap_deg(a: float) -> float:
    """归一到 (-180, 180]。"""
    if not math.isfinite(a):
        return float("nan")
    x = math.fmod(a, 360.0)
    if x > 180.0:
        x -= 360.0
    elif x <= -180.0:
        x += 360.0
    return x


def compass_ink() -> QtGui.QColor:
    """盘面上可读的通用墨色（按盘面底色反算，不是写死的白）。"""
    return QtGui.QColor(readable("#e8eef2", FACE, 4.5))


# --------------------------------------------------------------------------- #
class CompassDial(QtWidgets.QWidget):
    """航向罗盘：N 在上不动，红色机头箭头指向当前航向（和参考图一致）。"""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 diameter: int = 84) -> None:
        super().__init__(parent)
        self._d = int(diameter)
        self.setFixedSize(self._d, self._d)
        self._yaw = float("nan")
        self._source = ""
        self.setToolTip("航向（偏航角 Yaw）。无 ANG/ATT 时退回地面航迹方位；具体度数在下方读数行")

    # 数据
    def set_value(self, yaw: float, source: str = "") -> None:
        y = float(yaw) if math.isfinite(float(yaw)) else float("nan")
        if y == self._yaw and source == self._source:
            return
        self._yaw, self._source = y, source
        self.update()

    def value(self) -> float:
        return self._yaw

    def clear(self) -> None:
        self.set_value(float("nan"), "")

    # 绘制
    def paintEvent(self, ev: QtGui.QPaintEvent) -> None:      # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = QtCore.QRectF(1, 1, self.width() - 2, self.height() - 2)
        c = r.center()
        rad = min(r.width(), r.height()) / 2.0
        ink = compass_ink()

        p.setBrush(QtGui.QColor(FACE))
        p.setPen(QtGui.QPen(QtGui.QColor(FACE_EDGE), 1.0))
        p.drawEllipse(r)
        # 盘面内容统一裁进圆里：不然姿态球的天/地会溢成方块（真机图上撞过）
        disc = QtGui.QPainterPath()
        disc.addEllipse(r)
        p.setClipPath(disc)

        # 外圈刻度：每 30° 一格长刻线，每 10° 短刻线
        p.setPen(QtGui.QPen(ink, 1.0))
        for a in range(0, 360, 10):
            long = (a % 30 == 0)
            r0 = rad * (0.84 if long else 0.90)
            p2 = _polar(c, rad * 0.96, a)
            p1 = _polar(c, r0, a)
            p.drawLine(p1, p2)

        # 方位字母：放在刻度环内侧、箭头尖端之外，三者不能抢位置
        f = ui_font(7.5, True)
        p.setFont(f)
        fm = QtGui.QFontMetricsF(f)
        for a, txt in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
            pt = _polar(c, rad * 0.72, a)
            box = QtCore.QRectF(pt.x() - fm.horizontalAdvance(txt), pt.y() - fm.height(),
                                fm.horizontalAdvance(txt) * 2.0, fm.height() * 2.0)
            p.setPen(QtGui.QPen(QtGui.QColor(NOSE_RED) if a == 0 else ink, 1.0))
            p.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, txt)

        # 机头箭头
        if math.isfinite(self._yaw):
            p.save()
            p.translate(c)
            p.rotate(self._yaw)
            h = rad * 0.46
            w = rad * 0.19
            path = QtGui.QPainterPath(QtCore.QPointF(0, -h))
            path.lineTo(w, h * 0.55)
            path.lineTo(0, h * 0.28)
            path.lineTo(-w, h * 0.55)
            path.closeSubpath()
            # PySide6 6.12 的 fillPath(path, QColor) 会静默不画（实测 0 个像素）；
            # 必须用 setBrush(QBrush(...)) + drawPath，红填充才真的出来。
            p.setBrush(QtGui.QBrush(QtGui.QColor(NOSE_RED)))
            p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.1))
            p.drawPath(path)
            p.restore()
            # 不在盘心开小窗放数字：实测会把箭头本体盖掉大半，而下方读数行已有“航向 xxx°”
        else:
            p.setFont(ui_font(7.5))
            p.setPen(QtGui.QPen(QtGui.QColor(INK_DIM), 1.0))
            p.drawText(r, QtCore.Qt.AlignmentFlag.AlignCenter, "无\n航向")
        p.end()


def _polar(c: QtCore.QPointF, radius: float, deg: float) -> QtCore.QPointF:
    """屏幕坐标里的极坐标：0°=上，顺时针为正（和罗盘一致）。"""
    a = math.radians(deg - 90.0)
    return QtCore.QPointF(c.x() + radius * math.cos(a), c.y() + radius * math.sin(a))


# --------------------------------------------------------------------------- #
class AttitudeDial(QtWidgets.QWidget):
    """姿态球：上天下地，随横滚旋转、随俯仰平移，中间是固定的机体符号。"""

    PX_PER_DEG = 1.55          # 姿态球刻度密度（84px 盘约 ±27°）

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 diameter: int = 84) -> None:
        super().__init__(parent)
        self._d = int(diameter)
        self.setFixedSize(self._d, self._d)
        self._pitch = float("nan")
        self._roll = float("nan")
        self.setToolTip("姿态：俯仰 Pitch / 横滚 Roll")

    def set_pose(self, pitch: float, roll: float) -> None:
        pi = float(pitch) if math.isfinite(float(pitch)) else float("nan")
        ro = float(roll) if math.isfinite(float(roll)) else float("nan")
        if pi == self._pitch and ro == self._roll:
            return
        self._pitch, self._roll = pi, ro
        self.update()

    def pose(self) -> tuple[float, float]:
        return self._pitch, self._roll

    def clear(self) -> None:
        self.set_pose(float("nan"), float("nan"))

    def paintEvent(self, ev: QtGui.QPaintEvent) -> None:      # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = QtCore.QRectF(1, 1, self.width() - 2, self.height() - 2)
        c = r.center()
        rad = min(r.width(), r.height()) / 2.0
        ink = compass_ink()
        p.setBrush(QtGui.QColor(FACE))
        p.setPen(QtGui.QPen(QtGui.QColor(FACE_EDGE), 1.0))
        p.drawEllipse(r)
        disc = QtGui.QPainterPath()
        disc.addEllipse(r)
        p.setClipPath(disc)

        if not (math.isfinite(self._pitch) or math.isfinite(self._roll)):
            p.setFont(ui_font(7.5))
            p.setPen(QtGui.QPen(QtGui.QColor(INK_DIM), 1.0))
            p.drawText(r, QtCore.Qt.AlignmentFlag.AlignCenter, "无\n姿态")
            p.setClipping(False)
            p.end()
            return

        roll = self._roll if math.isfinite(self._roll) else 0.0
        pitch = self._pitch if math.isfinite(self._pitch) else 0.0
        p.save()
        p.translate(c)
        p.rotate(-roll)                       # 右滚时地平线向左倾
        shift = pitch * self.PX_PER_DEG       # 抬头 → 地平线下移
        big = rad * 3.0
        p.fillRect(QtCore.QRectF(-big, -big + shift, big * 2, big), QtGui.QColor(SKY))
        p.fillRect(QtCore.QRectF(-big, shift, big * 2, big), QtGui.QColor(GROUND))
        p.setPen(QtGui.QPen(QtGui.QColor(LADDER), 1.4))
        p.drawLine(QtCore.QPointF(-big, shift), QtCore.QPointF(big, shift))
        # 俯仰梯：每 10° 一条，短的在 5°
        p.setFont(ui_font(6))
        for deg in (-30, -20, -10, 10, 20, 30):
            y = shift - deg * self.PX_PER_DEG
            if abs(y) > rad - 8:
                continue
            half = 11.0 if deg % 10 == 0 else 6.0
            p.setPen(QtGui.QPen(QtGui.QColor(LADDER), 1.0))
            p.drawLine(QtCore.QPointF(-half, y), QtCore.QPointF(half, y))
            p.setPen(QtGui.QPen(ink, 1.0))
            p.drawText(QtCore.QRectF(half + 2, y - 5, 16, 10),
                       QtCore.Qt.AlignmentFlag.AlignLeft
                       | QtCore.Qt.AlignmentFlag.AlignVCenter, f"{abs(deg)}")
        p.restore()
        p.setClipping(False)

        # 固定机体符号（不参与旋转）：两翼 + 中心点
        p.setPen(QtGui.QPen(QtGui.QColor("#ffd43b"), 2.0))
        p.drawLine(QtCore.QPointF(c.x() - rad * 0.42, c.y()),
                   QtCore.QPointF(c.x() - rad * 0.14, c.y()))
        p.drawLine(QtCore.QPointF(c.x() + rad * 0.14, c.y()),
                   QtCore.QPointF(c.x() + rad * 0.42, c.y()))
        p.setBrush(QtGui.QColor("#ffd43b"))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(c, 1.7, 1.7)

        # 顶部横滚指针
        p.save()
        p.translate(c)
        p.rotate(-roll)
        tri = QtGui.QPainterPath(QtCore.QPointF(0, -rad + 3))
        tri.lineTo(4.2, -rad + 11)
        tri.lineTo(-4.2, -rad + 11)
        tri.closeSubpath()
        p.setBrush(QtGui.QBrush(QtGui.QColor("#ffd43b")))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawPath(tri)
        p.restore()
        p.setPen(QtGui.QPen(ink, 1.4))
        p.drawLine(QtCore.QPointF(c.x(), c.y() - rad + 1),
                   QtCore.QPointF(c.x(), c.y() - rad + 5))
        p.end()


# --------------------------------------------------------------------------- #
def alt_step(v: float) -> int:
    """选主刻度间隔：低空要能读出米数，高空不能糊成一团。"""
    a = abs(float(v))
    if a < 30.0:
        return 5
    if a < 100.0:
        return 10
    if a < 300.0:
        return 20
    if a < 1000.0:
        return 50
    return 100


class AltitudeTape(QtWidgets.QWidget):
    """高度带：竖直滚动刻度 + 中间读数框（和真实 PFD 右侧那条一样）。

    为什么不用圆盘指针：单指针高度表要看“圈数”才能读数，84 px 的盘面上根本看不清；
    高度带直接把数字放在中间，一眼就能读。
    """

    MAJOR_PX = 17.0             # 一个主刻度的像素高度

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 width: int = 56, height: int = 84) -> None:
        super().__init__(parent)
        self._w = int(width)
        self._h = int(height)
        self.setFixedSize(self._w, self._h)
        self._alt = float("nan")
        self._source = ""
        self.setToolTip("高度（相对起飞点）。有航迹时用航迹的位置高度，和地图游标读数同源；\n"
                        "没航迹时退回气压高度。具体数值在中间读数框")

    # 数据
    def set_altitude(self, alt: float, source: str = "") -> None:
        a = float(alt) if math.isfinite(float(alt)) else float("nan")
        if a == self._alt and source == self._source:
            return
        self._alt, self._source = a, source
        self.update()

    def altitude(self) -> float:
        return self._alt

    def source(self) -> str:
        return self._source

    def clear(self) -> None:
        self.set_altitude(float("nan"), "")

    # 绘制
    def paintEvent(self, ev: QtGui.QPaintEvent) -> None:      # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = QtCore.QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QtGui.QPen(QtGui.QColor(FACE_EDGE), 1.0))
        p.setBrush(QtGui.QColor(FACE))
        p.drawRoundedRect(r, 6, 6)
        ink = compass_ink()
        w, h = float(self.width()), float(self.height())
        cy = h / 2.0
        v = self._alt
        if not math.isfinite(v):
            p.setPen(QtGui.QPen(QtGui.QColor(INK_DIM)))
            p.setFont(ui_font(7.5))
            p.drawText(QtCore.QRectF(0, cy - 14, w, 12),
                       QtCore.Qt.AlignmentFlag.AlignHCenter, "无")
            p.drawText(QtCore.QRectF(0, cy, w, 14),
                       QtCore.Qt.AlignmentFlag.AlignHCenter, "高度")
            p.end()
            return

        step = alt_step(v)
        ppm = self.MAJOR_PX / float(step)
        p.setPen(QtGui.QPen(QtGui.QColor(INK_DIM)))
        p.setFont(ui_font(6.0))
        p.drawText(QtCore.QRectF(3, 1, w - 6, 11),
                   QtCore.Qt.AlignmentFlag.AlignLeft, "ALT")

        # 刻度：从右边探进来，数字在刻度左侧；上下各多画一格避免空角
        top, bot = 12.0, h - 2.0
        lo = v - (cy - top) / ppm - step
        hi = v + (bot - cy) / ppm + step
        k = int(math.floor(lo / step))
        while k * step <= hi:
            for half in (False, True):
                val = k * step + (step / 2.0 if half else 0.0)
                y = cy - (val - v) * ppm
                if not (top <= y <= bot):
                    continue
                if half:
                    p.setPen(QtGui.QPen(QtGui.QColor(INK_DIM), 1.0))
                    p.drawLine(QtCore.QPointF(w - 8.0, y), QtCore.QPointF(w - 2.0, y))
                    continue
                p.setPen(QtGui.QPen(ink, 1.2))
                p.drawLine(QtCore.QPointF(w - 14.0, y), QtCore.QPointF(w - 2.0, y))
                p.setFont(ui_font(6.5))
                p.drawText(QtCore.QRectF(1, y - 6.0, w - 17.0, 12.0),
                           QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                           f"{val:.0f}")
            k += 1

        # 中间读数框：浅底深字，盖住后面的刻度（真实高度带就这么干）
        band = QtCore.QRectF(2.0, cy - 8.5, w - 4.0, 17.0)
        p.setPen(QtGui.QPen(QtGui.QColor(FACE_EDGE), 1.0))
        p.setBrush(QtGui.QColor(LADDER))
        p.drawRoundedRect(band, 3, 3)
        digits = 1 if step < 10 else 0
        p.setPen(QtGui.QPen(QtGui.QColor(FACE)))
        p.setFont(ui_font(8.5, bold=True))
        p.drawText(band.adjusted(3, 0, -3, 0),
                   QtCore.Qt.AlignmentFlag.AlignCenter, f"{v:.{digits}f} m")
        # 黄色游标三角，指在读数框右边
        tri = QtGui.QPainterPath()
        tri.moveTo(w - 1.0, cy - 4.5)
        tri.lineTo(w - 7.0, cy)
        tri.lineTo(w - 1.0, cy + 4.5)
        tri.closeSubpath()
        p.setBrush(QtGui.QBrush(QtGui.QColor("#ffd43b")))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawPath(tri)
        p.end()


# --------------------------------------------------------------------------- #
class GaugePanel(QtWidgets.QWidget):
    """罗盘 + 姿态球 + 高度带，浮在地图左下角。"""

    collapseToggled = QtCore.Signal(bool)

    MARGIN = 10

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 collapsed: bool = False) -> None:
        super().__init__(parent)
        self._collapsed = False
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, False)
        self.compass = CompassDial(self)
        self.attitude = AttitudeDial(self)
        self.altitude = AltitudeTape(self)
        self.lbl = QtWidgets.QLabel("姿态", self)
        self.lbl.setFont(ui_font(7.5))
        self.lbl.setContentsMargins(9, 0, 9, 0)
        # 电压/电流单独一行：剩余电量是按电压推的估计值，不能当实测状态量到处标
        # “电量偏低”（用户 2026-10-24），直接把原始量摆出来让人自己判断。
        self.lbl_elec = QtWidgets.QLabel("", self)
        self.lbl_elec.setFont(ui_font(8))
        self.lbl_elec.setContentsMargins(9, 0, 9, 4)   # 最后一行，别贴着圆角边框
        self.lbl_elec.setVisible(False)

        self.btn_fold = QtWidgets.QToolButton(self)
        self.btn_fold.setText("—")
        self.btn_fold.setFixedSize(20, 22)
        self.btn_fold.setToolTip("收起仪表（地图左下角这块地方不大）")
        self.btn_fold.clicked.connect(self._toggle_fold)

        self._row = QtWidgets.QHBoxLayout()
        self._row.setContentsMargins(8, 6, 8, 6)
        self._row.setSpacing(6)
        self._row.addWidget(self.compass)
        self._row.addWidget(self.attitude)
        self._row.addWidget(self.altitude)
        self._row.addStretch(1)
        self._row.addWidget(self.btn_fold)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(self._row)
        lay.addWidget(self.lbl)
        lay.addWidget(self.lbl_elec)
        self.set_collapsed(collapsed)
        self.set_no_data()

    # ---------------- 数据 ----------------
    def set_pose(self, pitch: float, roll: float, yaw: float,
                 source: str = "") -> None:
        self.compass.set_value(yaw, source)
        self.attitude.set_pose(pitch, roll)
        self.lbl.setText(self._readout_text(yaw, pitch, roll, source))
        self.lbl.setStyleSheet("")
        self.lbl.setVisible(not self._collapsed)

    def set_altitude(self, alt: float, source: str = "") -> None:
        """高度单独推：数值就显示在高度带中间的读数框里，不占读数行。"""
        self.altitude.set_altitude(alt, source)

    def set_battery(self, volt: Optional[float] = None,
                    curr: Optional[float] = None) -> None:
        """电池电压与电流（来自 BATT.Volt / BATT.Curr）。

        只显示**原始测量量**，不推断百分比、也不下“电量偏低”的结论：
        ArduPilot 的 RemPct 在没有可靠容量/电流计时本身就是按电压推出来的，
        再拿它去标区域等于把一个估计值当事实。任一项缺就只显示另一项。
        """
        parts: list[str] = []
        v = float(volt) if volt is not None else float("nan")
        a = float(curr) if curr is not None else float("nan")
        if math.isfinite(v):
            parts.append(f"电压 {v:.2f} V")
        if math.isfinite(a):
            parts.append(f"电流 {a:.1f} A")
        self.lbl_elec.setText("　".join(parts))
        self.lbl_elec.setVisible(bool(parts) and not self._collapsed)
        self.adjustSize()
        self.updateGeometry()

    def set_pose_missing(self, note: str = "该日志没有姿态报文") -> None:
        """只清罗盘和姿态球，**不动高度带**。高度往往还能从航迹拿到，
        不能因为没姿态就把整块仪表都报成无数据。"""
        self.compass.clear()
        self.attitude.clear()
        self.lbl.setText(note)
        self.lbl.setStyleSheet("")
        self.lbl.setVisible(not self._collapsed)

    def set_no_data(self, note: str = "该日志没有姿态报文") -> None:
        self.set_pose_missing(note)
        self.altitude.clear()
        self.lbl.setText(note)
        self.lbl_elec.setText("")
        self.lbl_elec.setVisible(False)

    def _readout_text(self, yaw: float, pitch: float, roll: float, source: str) -> str:
        tag = f"（{source}）" if source else ""
        return (f"航向 {_fmt_deg(yaw)}{tag}　俯仰 {_fmt_deg(pitch, 1)}　"
                f"横滚 {_fmt_deg(roll, 1)}")

    # ---------------- 折叠 ----------------
    def _toggle_fold(self) -> None:
        self.set_collapsed(not self._collapsed)
        self.collapseToggled.emit(self._collapsed)

    def set_collapsed(self, on: bool) -> None:
        self._collapsed = bool(on)
        for w in (self.compass, self.attitude, self.altitude, self.lbl, self.lbl_elec):
            w.setVisible(not self._collapsed and (w is not self.lbl_elec
                                                  or bool(self.lbl_elec.text())))
        self.btn_fold.setText("□" if self._collapsed else "—")
        self.btn_fold.setToolTip("展开仪表" if self._collapsed else "收起仪表（地图左下角这块地方不大）")
        self.adjustSize()
        self.updateGeometry()
        self.update()

    def collapsed(self) -> bool:
        return self._collapsed

    # ---------------- 外观 ----------------
    def paintEvent(self, ev: QtGui.QPaintEvent) -> None:      # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QtGui.QPen(QtGui.QColor(FACE_EDGE), 1.0))
        p.setBrush(QtGui.QColor(20, 26, 32, 205))
        p.drawRoundedRect(r, 8, 8)
        p.end()

    def sizeHint(self) -> QtCore.QSize:                        # noqa: N802
        if self._collapsed:
            return QtCore.QSize(40, 34)
        extra = 19 if self.lbl_elec.isVisible() else 0              # 电压/电流那一行 + 底部内边距
        return QtCore.QSize(self.compass.width() + self.attitude.width()
                            + self.altitude.width() + 52,
                            max(self.compass.height(), 40) + 30 + extra)


__all__ = ["CompassDial", "AttitudeDial", "AltitudeTape", "GaugePanel",
           "wrap_deg", "alt_step", "FACE", "SKY", "GROUND"]
