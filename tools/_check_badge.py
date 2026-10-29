"""临时验证脚本：感叹号徽标 + 悬停提示（命中/不命中/普通读数）。"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from PySide6 import QtCore, QtWidgets  # noqa: E402

app = QtWidgets.QApplication([])
from apmlog.app import MainWindow, _pump  # noqa: E402

w = MainWindow()
w.resize(1500, 950)
w.show()
w.tabs.setCurrentIndex(1)   # 曲线页要成为当前页，否则 pyqtgraph 拿不到真实布局
_pump(app, 250)
w.load_sync(r"C:/Users/MAKI/Desktop/工作相关/ZHT/测试/测试3/日志/00000165.BIN")
_pump(app, 600)

p = w.plot
lane0 = p._lanes[0]
vb = lane0.plot.getViewBox()
vbr = vb.sceneBoundingRect()
print("ViewBox 场景矩形:", [round(v, 1) for v in (vbr.x(), vbr.y(), vbr.width(), vbr.height())])
xr = vb.viewRange()[0]
scale = vbr.width() / (xr[1] - xr[0])


def pos_at(t=None, dx=0.0, dy=6.0):
    x = vbr.left() + ((t - xr[0]) * scale if t is not None else vbr.width() * 0.5) + dx
    return p._graphics.mapFromScene(QtCore.QPointF(x, vbr.top() + dy))


print("徽标数:", len(p._badges))
for b in p._badges:
    tip = p._badge_tip(lane0, b["t"], pos_at(b["t"]))
    head = (tip or "").splitlines()[:1]
    print(f"  {b['sev']:8s} n={b['n']} t={b['t']:8.2f} 命中={'Y' if tip else 'N'} 首行={head}")

b0 = p._badges[-1]
print("右移 40px 处:", p._badge_tip(lane0, b0["t"], pos_at(b0["t"], dx=40.0)))
mid_h = p._graphics.mapFromScene(QtCore.QPointF(vbr.left() + (b0["t"] - xr[0]) * scale,
                                                vbr.top() + vbr.height() * 0.6))
print("泳道中部（应不抢提示）:", p._badge_tip(lane0, b0["t"], mid_h))

p._update_hover(lane0, pos_at(b0["t"]))
print("悬停徽标 QToolTip 首行:", QtWidgets.QToolTip.text().splitlines()[:1])
p._update_hover(lane0, pos_at())
print("普通位置 QToolTip:", repr(QtWidgets.QToolTip.text()[:80]))
w.grab().save("selftest/badge_chart.png")
print("截图已存 selftest/badge_chart.png")
w.close()
_pump(app, 400)
print("关窗 OK")
