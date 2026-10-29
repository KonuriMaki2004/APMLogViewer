# -*- coding: utf-8 -*-
"""tools/selftest_seriesplot.py —— C 工作流（多泳道时序图）自测。

用法（必须用工程 venv）：
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\selftest_seriesplot.py            # 两个都跑
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\selftest_seriesplot.py synthetic   # 只跑合成数据
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\selftest_seriesplot.py real        # 只跑真实日志

输出：selftest/seriesplot.png、selftest/seriesplot_real.png + stdout 实测数字。
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                              # noqa: E402
from PySide6 import QtWidgets                                   # noqa: E402

from apmlog.ui.seriesplot import PALETTES, LaneSpec, SeriesPlot  # noqa: E402
from PySide6 import QtCore, QtGui                                # noqa: E402


def _lane_point(w: SeriesPlot, idx: int, fx: float, fy: float) -> QtCore.QPoint:
    """泳道内相对位置 -> GraphicsLayoutWidget 的 viewport 坐标。"""
    vb = w._lanes[idx].plot.getViewBox()
    xr, yr = vb.viewRange()
    xv = float(xr[0]) + (float(xr[1]) - float(xr[0])) * fx
    yv = float(yr[0]) + (float(yr[1]) - float(yr[0])) * fy
    return w._graphics.mapFromScene(vb.mapViewToScene(QtCore.QPointF(xv, yv)))


def _send(w: SeriesPlot, kind: QtCore.QEvent.Type, pos: QtCore.QPoint,
          button: QtCore.Qt.MouseButton, mods: QtCore.Qt.KeyboardModifier,
          held: QtCore.Qt.MouseButton | None = None) -> None:
    """派发合成鼠标事件。held = 当前按住的键，拖动时必须传，否则 Qt 当 hover 处理。"""
    vp = w._graphics.viewport()
    lp = QtCore.QPointF(pos)
    gp = vp.mapToGlobal(pos)
    if kind == QtCore.QEvent.MouseMove:
        buttons = held if held is not None else QtCore.Qt.MouseButton.NoButton
    elif kind == QtCore.QEvent.MouseButtonRelease:
        buttons = QtCore.Qt.MouseButton.NoButton
    else:
        buttons = button
    QtWidgets.QApplication.sendEvent(vp, QtGui.QMouseEvent(kind, lp, gp, button, buttons, mods))


def _wheel(w: SeriesPlot, pos: QtCore.QPoint, delta: int = 120) -> None:
    """派发滚轮事件（delta>0 向上，pyqtgraph 会缩小时间窗）。"""
    vp = w._graphics.viewport()
    ev = QtGui.QWheelEvent(QtCore.QPointF(pos), QtCore.QPointF(vp.mapToGlobal(pos)),
                           QtCore.QPoint(0, 0), QtCore.QPoint(0, delta),
                           QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
                           QtCore.Qt.ScrollPhase.ScrollUpdate, False)
    QtWidgets.QApplication.sendEvent(vp, ev)


def _xrange(w: SeriesPlot, idx: int = 0) -> tuple[float, float]:
    xr = w._lanes[idx].plot.getViewBox().viewRange()[0]
    return (float(xr[0]), float(xr[1]))

SELFTEST = ROOT / "selftest"
SELFTEST.mkdir(exist_ok=True)


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# 合成数据
# --------------------------------------------------------------------------- #


def make_synthetic_lanes():
    """4 条泳道：2M 点振动 / PWM / 带 nan 缺口的电压 / 全 nan。"""
    pal = PALETTES["light"]

    # 道 1：2_000_000 点 1kHz 振动，其中 15 秒超标（模拟 VIBE 超标段）
    fs = 1000.0
    n = 2_000_000
    t = np.arange(n, dtype=np.float64) / fs                     # 0..2000s
    rng = np.random.default_rng(7)
    y = (0.02 + 0.01 * rng.standard_normal(n)) * np.sin(2 * np.pi * 30.0 * t)
    mask = (t > 600) & (t < 615)
    y[mask] += 0.9 * np.sin(2 * np.pi * 120.0 * t[mask])        # 超标段，幅值 ~0.9
    lane_vibe = LaneSpec(label="VIBE[0].VibeX", color=pal[0], unit=" m/s²", t=t, y=y,
                         threshold=0.3, band=(0.0, 0.15), note="VIBE#0.VibeX")

    # 道 2：RCOU 类 PWM，1000–2000，固定量程 + step
    n2 = 60_000
    t2 = np.linspace(0.0, t[-1], n2)
    pwm = 1500 + 300 * np.sign(np.sin(2 * np.pi * t2 / 240.0)) + rng.standard_normal(n2) * 6
    pwm = np.clip(pwm, 1000, 2000)
    lane_pwm = LaneSpec(label="RCOU[0].C1", color=pal[1], unit=" us", t=t2, y=pwm,
                        y_min=1000.0, y_max=2000.0, style="step", note="RCOU#0.C1")

    # 道 3：BAT 电压，插入 200 个 nan 缺口
    n3 = 20_000
    t3 = np.linspace(0.0, t[-1], n3)
    v3 = 12.6 - 2.0 * (t3 / t[-1]) + 0.05 * rng.standard_normal(n3)
    gap_idx = rng.choice(n3, size=200, replace=False)
    v3[gap_idx] = np.nan
    lane_bat = LaneSpec(label="BAT[0].Volt", color=pal[3], unit=" V", t=t3, y=v3, note="BAT#0.Volt")

    # 道 4：全 nan（验证占位）
    lane_empty = LaneSpec(label="BAT[0].Temp(全nan)", color=pal[4], unit=" degC",
                          t=np.linspace(0, t[-1], 100), y=np.full(100, np.nan), note="BAT#0.Temp")

    return [lane_vibe, lane_pwm, lane_bat, lane_empty], t[-1]


def run_synthetic(app: QtWidgets.QApplication) -> dict[str, float]:
    lanes, t_end = make_synthetic_lanes()
    total = sum(int(l.t.size) for l in lanes)
    w = SeriesPlot(theme="light")
    w.resize(1780, 1000)
    w.show()
    app.processEvents()

    t0 = time.perf_counter()
    w.set_lanes(lanes, t_span=(0.0, t_end))
    app.processEvents()
    w._refresh()
    pm = w.grab()
    first_render = time.perf_counter() - t0
    tower_ms = max((float(getattr(l, "tower_ms", 0.0) or 0.0) for l in w._lanes), default=0.0)

    # 缩放扫描：从全览一路放大到原始点，记录每档耗时与渲染模式
    sweep: list[tuple[str, float, str, int]] = []
    spans = [(0.0, t_end, "全览"), (0.0, t_end / 10.0, "1/10"), (0.0, t_end / 100.0, "1/100"),
             (0.0, t_end / 1000.0, "1/1000"), (600.0, 615.0, "15s 窗口"), (600.0, 601.0, "1s 窗口")]
    for a, b, name in spans:
        t0 = time.perf_counter()
        w.zoom_to(a, b)
        w._refresh()                      # 同步重绘，测的是真实重绘成本
        dt = (time.perf_counter() - t0) * 1000.0
        n_view = int(w._lanes[0].visible_counts(a, b))
        sweep.append((name, dt, w._lanes[0].mode, n_view))
    zoom_once = max(s[1] for s in sweep)
    mode_zoomed = sweep[-1][2] if sweep else "?"      # 最深一档应已切到 raw
    w.zoom_to(0.0, t_end)
    w._refresh()
    mode_full = w._lanes[0].mode

    # 游标移动 1000 次
    t0 = time.perf_counter()
    for i in range(1000):
        w.set_time(600.0 + i * 0.015)
    app.processEvents()
    cursor_avg = (time.perf_counter() - t0) / 1000.0 * 1000.0     # ms/次

    # 区域与事件
    w.set_events([{"t": 607.0, "severity": "critical", "display": "ERR Subsys=12 Crash: Disarming"},
                  {"t": 120.0, "severity": "warn", "display": "VIBE clip"}])
    w.set_regions([{"t0": 600.0, "t1": 615.0, "severity": "critical", "title": "振动超标"}])
    w.zoom_to(598.0, 618.0)
    app.processEvents()
    png = str(SELFTEST / "seriesplot.png")
    ok = w.export_png(png, 1780, 1000)

    # 全量 2M 点导 CSV 只为了验证行数：写到系统临时目录，量完就删，不在工程里堆 100MB
    import tempfile
    tmp_csv = os.path.join(tempfile.gettempdir(), "apmlog_seriesplot_full.csv")
    t0 = time.perf_counter()
    rows = w.export_csv(tmp_csv)
    csv_ms = (time.perf_counter() - t0) * 1000.0
    csv_mb = os.path.getsize(tmp_csv) / 1024 / 1024 if os.path.exists(tmp_csv) else 0.0
    os.remove(tmp_csv)
    # 工程里只留一份"用户真正会导"的区间 CSV
    csv_path = str(SELFTEST / "seriesplot_synthetic_region.csv")
    rows_region = w.export_csv(csv_path, 600.0, 615.0)
    sel = w.get_region()

    sig: dict[str, list] = {}
    w.timeChanged.connect(lambda t: sig.setdefault("t", []).append(t))
    w.regionChanged.connect(lambda a, b: sig.setdefault("r", []).append((a, b)))
    w.laneVisibilityChanged.connect(lambda s, on: sig.setdefault("v", []).append((s, on)))

    # 用真实鼠标事件驱动：单击设游标 / Ctrl+拖拽框选 / 点标题切换可见性
    w.zoom_to(0.0, t_end)
    w._refresh()
    p = _lane_point(w, 0, 0.6, 0.5)
    _send(w, QtCore.QEvent.MouseButtonPress, p, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    _send(w, QtCore.QEvent.MouseButtonRelease, p, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    app.processEvents()
    p0 = _lane_point(w, 0, 0.40, 0.5)
    p1 = _lane_point(w, 0, 0.55, 0.5)
    ctrl = QtCore.Qt.ControlModifier
    _send(w, QtCore.QEvent.MouseButtonPress, p0, QtCore.Qt.LeftButton, ctrl)
    _send(w, QtCore.QEvent.MouseMove, p1, QtCore.Qt.NoButton, ctrl)
    _send(w, QtCore.QEvent.MouseButtonRelease, p1, QtCore.Qt.LeftButton, ctrl)
    app.processEvents()
    w._toggle_lane(2, False, from_user=True)
    w._toggle_lane(2, True, from_user=True)
    app.processEvents()

    stats = lanes[0].y if lanes[0].y is not None else None
    res = {
        "total_points": total,
        "first_render_ms": first_render * 1000.0,
        "tower_build_ms": tower_ms,
        "zoom_worst_ms": zoom_once,
        "zoom_avg_ms": float(np.mean([s[1] for s in sweep])),
        "cursor_avg_ms": cursor_avg,
        "csv_rows": rows,
        "csv_ms": csv_ms,
        "png_bytes": os.path.getsize(png) if ok else 0,
        "grab_px": pm.width() * pm.height(),
    }
    log("—— 合成数据自测 ——")
    log(f"  泳道数={len(lanes)} 总点数={total:,} (道1={lanes[0].t.size:,})")
    log(f"  首帧渲染(含建包络塔 {tower_ms:.1f}ms)={first_render*1000:.1f} ms")
    for name, dt, mode, nv in sweep:
        log(f"  缩放[{name:>7}]: 重绘={dt:7.2f} ms 模式={mode:>3} 可见点={nv:,}")
    log(f"  缩放最差={zoom_once:.2f} ms 平均={np.mean([s[1] for s in sweep]):.2f} ms")
    log(f"  游标移动 1000 次平均={cursor_avg:.4f} ms/次")
    log(f"  模式切换: 全览={mode_full} → 深放大={mode_zoomed} (期望 env → raw)")
    log(f"  export_csv 全量={rows:,} 行 / {csv_mb:.1f} MB / {csv_ms:.0f} ms（临时文件已删）")
    log(f"  export_csv 区间[600,615]={rows_region:,} 行 -> {csv_path}")
    log(f"  get_region={sel}")
    log(f"  timeChanged 捕获={len(sig.get('t', []))} 次, 末值={sig.get('t', [])[-1:]}")
    log(f"  PNG={png} ok={ok} size={res['png_bytes']:,} B  grab={pm.width()}x{pm.height()}")
    log(f"  读数条文本={w._readout.text()[:140]}")
    checks = {
        "env→raw 切换": mode_full == "env" and mode_zoomed == "raw",
        "缩放最差<64ms": zoom_once < 64.0,
        "游标<1ms": cursor_avg < 1.0,
        "timeChanged 有发出": len(sig.get("t", [])) > 0,
        "regionChanged 有发出": len(sig.get("r", [])) > 0,
        "laneVisibilityChanged 有发出": len(sig.get("v", [])) > 0,
        "CSV 行数≈点数": abs(rows - total) < 5000,
    }
    for k, v in checks.items():
        log(f"  检查[{k}] = {'PASS' if v else 'FAIL'}")
    res["all_checks"] = all(checks.values())
    return res


# --------------------------------------------------------------------------- #
# 交互回归：派发真实 Qt 事件，逐项统计信号数量与视图变化
# --------------------------------------------------------------------------- #

FALLTHROUGH_EVENTS = ("HoverEnter", "HoverMove", "HoverLeave", "Enter", "Leave",
                      "FocusIn", "FocusOut", "UpdateRequest", "EnabledChange",
                      "WindowActivate", "StatusTip", "ToolTip", "WhatsThis", "Resize")


def run_interaction(app: QtWidgets.QApplication) -> dict[str, float]:
    """滚轮缩放 / 左键平移 / 单击 / Ctrl+框选 / 双击复位 / fallthrough 的可见断言。"""
    import logging

    lanes_all, t_end = make_synthetic_lanes()
    lanes = lanes_all[:3]
    w = SeriesPlot(theme="light")
    w.resize(1500, 760)
    w.show()
    app.processEvents()
    w.set_lanes(lanes, t_span=(0.0, t_end))
    app.processEvents()
    w._refresh()

    counts = {"timeChanged": 0, "regionChanged": 0, "laneVisibilityChanged": 0}
    w.timeChanged.connect(lambda t: counts.__setitem__("timeChanged", counts["timeChanged"] + 1))
    w.regionChanged.connect(lambda a, b: counts.__setitem__("regionChanged", counts["regionChanged"] + 1))
    w.laneVisibilityChanged.connect(lambda s_, on: counts.__setitem__(
        "laneVisibilityChanged", counts["laneVisibilityChanged"] + 1))
    errs: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                errs.append(record.getMessage())

    lg = logging.getLogger("apmlog.ui.seriesplot")
    lg.addHandler(_Cap())
    lg.setLevel(logging.ERROR)

    def pump(n: int = 5) -> None:
        for _ in range(n):
            app.processEvents()
            time.sleep(0.004)

    log("—— 交互回归（派发真实 Qt 事件）——")
    pump()
    checks: dict[str, bool] = {}

    # 1) 滚轮缩放：窗口变窄，三个信号都不该发
    w.zoom_to(0.0, t_end)
    pump()
    b0, c0 = _xrange(w), dict(counts)
    _wheel(w, _lane_point(w, 0, 0.8, 0.5))
    pump()
    a0 = _xrange(w)
    checks["滚轮缩放"] = (a0[1] - a0[0]) < (b0[1] - b0[0]) and counts == c0
    log(f"  滚轮缩放: 窗口 {b0[1]-b0[0]:.1f}s -> {a0[1]-a0[0]:.1f}s  信号增量="
        f"{ {k: counts[k] - c0[k] for k in counts} }  {'PASS' if checks['滚轮缩放'] else 'FAIL'}")

    # 2) 左键拖拽平移：窗口平移且宽度不变，不发信号，_view 必须跟上真实视图
    b1, c1 = _xrange(w), dict(counts)
    held = QtCore.Qt.LeftButton
    _send(w, QtCore.QEvent.MouseButtonPress, _lane_point(w, 0, 0.75, 0.5), held, QtCore.Qt.NoModifier)
    for fx in (0.62, 0.5, 0.38):
        _send(w, QtCore.QEvent.MouseMove, _lane_point(w, 0, fx, 0.5), QtCore.Qt.NoButton,
              QtCore.Qt.NoModifier, held=held)
    _send(w, QtCore.QEvent.MouseButtonRelease, _lane_point(w, 0, 0.38, 0.5), held, QtCore.Qt.NoModifier)
    pump()
    a1 = _xrange(w)
    checks["左键平移"] = (a1[0] > b1[0] and abs((a1[1] - a1[0]) - (b1[1] - b1[0])) < 1.0
                      and counts == c1 and abs(w._view[0] - a1[0]) < 1.0)
    log(f"  左键平移: {b1[0]:.1f}~{b1[1]:.1f} -> {a1[0]:.1f}~{a1[1]:.1f}  "
        f"_view={w._view[0]:.1f}~{w._view[1]:.1f}  信号增量={ {k: counts[k] - c1[k] for k in counts} }"
        f"  {'PASS' if checks['左键平移'] else 'FAIL'}")

    # 3) 单击设游标：timeChanged 恰好 +1
    c2 = dict(counts)
    p = _lane_point(w, 1, 0.55, 0.5)
    _send(w, QtCore.QEvent.MouseButtonPress, p, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    _send(w, QtCore.QEvent.MouseButtonRelease, p, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    pump()
    checks["单击游标"] = counts["timeChanged"] - c2["timeChanged"] == 1
    log(f"  单击游标: t={w._cursor_t:.2f}s  timeChanged +"
        f"{counts['timeChanged'] - c2['timeChanged']}  {'PASS' if checks['单击游标'] else 'FAIL'}")

    # 4) Ctrl+左键框选：regionChanged 恰好 +1，get_region 与之一致
    c3 = dict(counts)
    ctrl = QtCore.Qt.ControlModifier
    p0, p1 = _lane_point(w, 1, 0.30, 0.5), _lane_point(w, 1, 0.60, 0.5)
    _send(w, QtCore.QEvent.MouseButtonPress, p0, QtCore.Qt.LeftButton, ctrl)
    _send(w, QtCore.QEvent.MouseMove, _lane_point(w, 1, 0.45, 0.5), QtCore.Qt.NoButton, ctrl)
    _send(w, QtCore.QEvent.MouseButtonRelease, p1, QtCore.Qt.LeftButton, ctrl)
    pump()
    ra, rb = w.get_region()
    checks["Ctrl框选"] = counts["regionChanged"] - c3["regionChanged"] == 1 and rb > ra
    log(f"  Ctrl+框选: region=({ra:.2f}, {rb:.2f})  regionChanged +"
        f"{counts['regionChanged'] - c3['regionChanged']}  {'PASS' if checks['Ctrl框选'] else 'FAIL'}")

    # 5) 泳道显隐：laneVisibilityChanged 计数 +2
    c4 = dict(counts)
    w._toggle_lane(2, False, from_user=True)
    w._toggle_lane(2, True, from_user=True)
    pump()
    checks["泳道显隐"] = counts["laneVisibilityChanged"] - c4["laneVisibilityChanged"] == 2
    log(f"  泳道显隐: laneVisibilityChanged +"
        f"{counts['laneVisibilityChanged'] - c4['laneVisibilityChanged']}  "
        f"{'PASS' if checks['泳道显隐'] else 'FAIL'}")

    # 6) 双击复位
    w.zoom_to(t_end * 0.10, t_end * 0.12)
    pump()
    _send(w, QtCore.QEvent.MouseButtonDblClick, _lane_point(w, 0, 0.5, 0.5),
          QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
    pump()
    s6 = _xrange(w)
    checks["双击复位"] = (s6[1] - s6[0]) > t_end * 0.5
    log(f"  双击复位: 窗口宽度 {s6[1]-s6[0]:.1f}s (全览 {t_end:.1f}s)  "
        f"{'PASS' if checks['双击复位'] else 'FAIL'}")

    # 7) fallthrough 非鼠标事件 + 悬停移动：必须安静
    n_err = len(errs)
    vp = w._graphics.viewport()
    for name in FALLTHROUGH_EVENTS:
        QtWidgets.QApplication.sendEvent(vp, QtCore.QEvent(QtCore.QEvent.Type[name]))
    pump()
    for fx in (0.2, 0.4, 0.6):
        _send(w, QtCore.QEvent.MouseMove, _lane_point(w, 0, fx, 0.5), QtCore.Qt.NoButton,
              QtCore.Qt.NoModifier)
    pump()
    checks["fallthrough安静"] = len(errs) == n_err
    log(f"  fallthrough {len(FALLTHROUGH_EVENTS)} 类事件 + 悬停移动: 新增 ERROR 日志 "
        f"{len(errs) - n_err} 条  {'PASS' if checks['fallthrough安静'] else 'FAIL'}")
    for e in errs[n_err:]:
        log(f"    !! {e}")

    # 交互之后再出一张图，证明状态仍可正常渲染
    w.set_events([{"t": t_end * 0.62, "severity": "critical", "display": "ERR Subsys=12 Crash"}])
    w.set_regions([{"t0": t_end * 0.60, "t1": t_end * 0.66, "severity": "error", "title": "振动超标"}])
    w.zoom_to(t_end * 0.55, t_end * 0.72)
    w.set_time(t_end * 0.62)
    pump()
    png = str(SELFTEST / "seriesplot_interaction.png")
    ok_png = w.export_png(png, 1500, 760)
    size = os.path.getsize(png) if ok_png else 0
    checks["交互后PNG"] = bool(ok_png and size > 5000)
    log(f"  交互后 PNG={png} ok={ok_png} size={size:,} B")
    log("  信号累计: " + ", ".join(f"{k}={v}" for k, v in counts.items())
        + f", ERROR 日志={len(errs)} 条")
    bad = [k for k, v in checks.items() if not v]
    log("  交互结论: " + ("PASS" if not bad else "FAIL " + ", ".join(bad)))
    res: dict[str, float] = {f"interact_{k}": 1.0 if v else 0.0 for k, v in checks.items()}
    res["interact_timeChanged"] = float(counts["timeChanged"])
    res["interact_regionChanged"] = float(counts["regionChanged"])
    res["interact_laneVisibilityChanged"] = float(counts["laneVisibilityChanged"])
    res["interact_error_logs"] = float(len(errs))
    res["interact_all"] = 1.0 if not bad else 0.0
    w.deleteLater()
    return res


# --------------------------------------------------------------------------- #
# 真实日志
# --------------------------------------------------------------------------- #


def read_real_channels(path: str) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """直接用 pymavlink DFReader 取 VIBE / BAT 通道（内核未交付，故自己解析）。

    返回 {label: (t, inst, value)}
    """
    from pymavlink import DFReader

    log_ = DFReader.DFReader_binary(path)
    if log_ is None:
        raise RuntimeError(f"DFReader 打不开: {path}")
    want = {"VIBE": ("IMU", ("VibeX", "VibeY", "VibeZ", "Clip")),
            "BAT": ("Inst", ("Volt", "Curr", "RemPct"))}
    acc: dict[str, list] = {}
    n_msg = 0
    while True:
        m = log_.recv_msg()
        if m is None:
            break
        typ = m.get_type()
        if typ not in want:
            continue
        inst_field, fields = want[typ]
        d = m.to_dict()
        tu = d.get("TimeUS")
        if tu is None:
            continue
        try:
            inst = int(d.get(inst_field, 0) or 0)
        except (TypeError, ValueError):
            inst = 0
        n_msg += 1
        for f in fields:
            v = d.get(f)
            if v is None:
                continue
            acc.setdefault(f"{typ}#{inst}.{f}", []).append((float(tu) * 1e-6, float(v)))

    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for key, rows in acc.items():
        rows.sort(key=lambda r: r[0])
        arr = np.asarray(rows, dtype=np.float64)
        t = np.unique(arr[:, 0], return_index=True)[1]
        out[key] = (arr[t, 0], arr[t, 1], np.zeros(arr[t, 0].size))
    return out


def run_real(app: QtWidgets.QApplication) -> dict[str, float]:
    path = r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000165.BIN"
    if not os.path.exists(path):
        log(f"  !! 真实日志不存在，跳过: {path}")
        return {}
    size_mb = os.path.getsize(path) / 1e6
    t0 = time.perf_counter()
    chans = read_real_channels(path)
    read_dt = time.perf_counter() - t0
    if not chans:
        log("  !! 真实日志没读到 VIBE/BAT 通道")
        return {}

    pal = PALETTES["light"]
    order = ["VIBE#0.VibeX", "VIBE#0.VibeY", "VIBE#0.VibeZ", "VIBE#0.Clip",
             "BAT#0.Volt", "BAT#0.Curr", "BAT#0.RemPct"]
    keys = [k for k in order if k in chans] + [k for k in chans if k not in order]
    lanes: list[LaneSpec] = []
    for i, k in enumerate(keys[:8]):
        t, v, _ = chans[k]
        typ, _, fld = k.partition("#")
        unit = {"VibeX": " m/s²", "VibeY": " m/s²", "VibeZ": " m/s²", "Clip": "",
                "Volt": " V", "Curr": " A", "RemPct": " %",
                "VoltR": " V", "Temp": " degC"}.get(fld, "")
        thr = 0.15 if fld in ("VibeX", "VibeY", "VibeZ") else None
        inst = k.split("#")[1].split(".")[0] if "#" in k else "-"
        lanes.append(LaneSpec(label=k.replace("#", "[").replace(".", "].", 1) if inst != "-" else k,
                              color=pal[i % 16], unit=unit, t=t, y=v, note=k, threshold=thr))
    tspan = (min(float(np.min(l.t[~np.isnan(l.t)] if l.t.size else np.nan)) for l in lanes),
             max(float(np.max(l.t)) for l in lanes))
    w = SeriesPlot(theme="light")
    w.resize(1780, 1000)
    w.show()
    app.processEvents()
    t0 = time.perf_counter()
    w.set_lanes(lanes, t_span=tspan)
    app.processEvents()
    w._refresh()
    render_ms = (time.perf_counter() - t0) * 1000.0
    png = str(SELFTEST / "seriesplot_real.png")
    ok = w.export_png(png, 1780, 1000)
    rows = w.export_csv(str(SELFTEST / "seriesplot_real.csv"))

    log("—— 真实日志自测 (00000165.BIN, 炸机日志) ——")
    log(f"  文件 {size_mb:.1f} MB, DFReader 读取 {read_dt:.2f} s, 消息 {sum(v[0].size for v in chans.values()):,} 样本")
    log(f"  时间跨度 t=[{tspan[0]:.2f}, {tspan[1]:.2f}] s (boot 基准)")
    for lane, spec in zip(w._lanes, lanes):
        ymin, ymax, ymean = lane.y[np.isfinite(lane.y)].min() if np.isfinite(lane.y).any() else math.nan, \
            lane.y[np.isfinite(lane.y)].max() if np.isfinite(lane.y).any() else math.nan, \
            lane.y[np.isfinite(lane.y)].mean() if np.isfinite(lane.y).any() else math.nan
        log(f"  {spec.label:<26} n={lane.t.size:>7,} 量程=[{ymin:.4f}, {ymax:.4f}] mean={ymean:.4f} "
            f"模式={getattr(lane, 'mode', '?')} 渲染={'有数据' if lane.has_data else '占位'}")
    log(f"  组件首帧渲染={render_ms:.1f} ms  CSV 行数={rows:,}")
    log(f"  PNG={png} ok={ok}")
    # 放大到炸机瞬间附近（165 的 crash 在 322.45s）
    w.zoom_to(318.0, 330.0)
    app.processEvents()
    w.set_events([{"t": 322.45, "severity": "critical", "display": "Crash: Disarming AngErr=171"}])
    app.processEvents()
    w._refresh()
    w.export_png(str(SELFTEST / "seriesplot_real_crash.png"), 1780, 1000)
    log(f"  炸机段放大图模式={[getattr(l, 'mode', '?') for l in w._lanes]}")
    log(f"  PNG={SELFTEST / 'seriesplot_real_crash.png'}")
    return {"real_render_ms": render_ms}


# --------------------------------------------------------------------------- #


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    res: dict[str, float] = {}
    if which in ("all", "synthetic"):
        res.update(run_synthetic(app))
    if which in ("all", "real"):
        res.update(run_real(app))
    if which in ("all", "interaction"):
        res.update(run_interaction(app))
    pngs = ["seriesplot.png", "seriesplot_real.png", "seriesplot_real_crash.png",
            "seriesplot_interaction.png"]
    missing = [f for f in pngs if not (SELFTEST / f).exists() or (SELFTEST / f).stat().st_size < 5000]
    log("  交付 PNG: " + ", ".join(
        f"{f}={(SELFTEST / f).stat().st_size if (SELFTEST / f).exists() else 0}B" for f in pngs))
    if missing:
        log(f"  缺失/过小 PNG: {missing}")
    ok = bool(res) and not missing and res.get("all_checks", 1) and res.get("interact_all", 1)
    log(f"结论: {'PASS' if ok else 'FAIL'}  {res}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
