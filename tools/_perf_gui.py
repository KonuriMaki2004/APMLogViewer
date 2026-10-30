# -*- coding: utf-8 -*-
"""只测已缓存日志的 GUI 交互耗时（不重跑冷解析）。"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LOG, CACHE = sys.argv[1], Path(sys.argv[2])


def top(pr, n=14, sort="cumulative"):
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats(sort).print_stats(n)
    print("\n".join(s.getvalue().splitlines()[4:]), flush=True)


def main() -> int:
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apmlog.ui import settings as st
    st.Settings.set_instance(st.Settings(CACHE / "settings.json"))
    import apmlog.app as appmod
    appmod.MainWindow._views_path = lambda self: CACHE / "views.json"

    win = appmod.MainWindow()
    win.resize(1600, 950)
    win.show()
    for _ in range(8):
        app.processEvents()
    t = time.perf_counter()
    win.load_sync(LOG)
    for _ in range(4):
        app.processEvents()
    print(f"[1] 热加载（缓存）{time.perf_counter() - t:.2f}s", flush=True)
    print(f"    绘图项总数 {sum(1 for it in win.plot._graphics.ci.items if True) if hasattr(win.plot, '_graphics') else '?'}", flush=True)

    for idx, name in ((1, "曲线页"), (0, "轨迹页"), (3, "事件页"), (2, "告警页")):
        t = time.perf_counter()
        win.tabs.setCurrentIndex(idx)
        for _ in range(20):
            app.processEvents()
        print(f"[2] 切{name} {time.perf_counter() - t:.2f}s", flush=True)

    print("[3] 切曲线页详情 profile", flush=True)
    win.tabs.setCurrentIndex(0)
    for _ in range(6):
        app.processEvents()
    pr = cProfile.Profile()
    t = time.perf_counter()
    pr.enable()
    win.tabs.setCurrentIndex(1)
    for _ in range(20):
        app.processEvents()
    pr.disable()
    print(f"    切页 {time.perf_counter() - t:.2f}s", flush=True)
    top(pr, 16)

    s = win.store.stats
    print("[4] 时间轴拖动 20 次", flush=True)
    worst = 0.0
    t = time.perf_counter()
    for i in range(20):
        tk = s.t_start + (s.t_end - s.t_start) * i / 20
        a = time.perf_counter()
        win._on_time(tk)
        for _ in range(2):
            app.processEvents()
        worst = max(worst, time.perf_counter() - a)
    print(f"    合计 {time.perf_counter() - t:.2f}s 最差 {worst * 1000:.0f} ms", flush=True)

    print("[5] 缩放 8 次", flush=True)
    span = s.t_end - s.t_start
    mid = (s.t_start + s.t_end) / 2
    worst = 0.0
    t = time.perf_counter()
    for i in range(8):
        w = span / (2 ** i)
        a = time.perf_counter()
        win.plot.zoom_to(mid - w / 2, mid + w / 2)
        for _ in range(2):
            app.processEvents()
        worst = max(worst, time.perf_counter() - a)
    print(f"    合计 {time.perf_counter() - t:.2f}s 最差 {worst * 1000:.0f} ms", flush=True)

    print("[6] 读内存/拓扑", flush=True)
    print(f"    泳道 {len(win.plot._lanes)}｜事件画笔 {len(win.plot._badges)}｜"
          f"GraphicsItems {len(win.plot._graphics.ci.items)}", flush=True)
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
