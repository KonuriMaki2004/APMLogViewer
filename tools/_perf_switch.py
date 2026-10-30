# -*- coding: utf-8 -*-
"""tools/_perf_switch.py —— 大日志下“切页”到底慢在哪。

    .\\.venv\\Scripts\\python.exe -X utf8 tools/_perf_switch.py "日志.bin" [--profile]

只测「切页签」这一件事：`tabs.setCurrentIndex(i)` + 事件循环 + `grab()`（强制真画一遍），
往返各 3 轮，输出每页每次耗时；`--profile` 再对最慢的那次做 cProfile 排序。
"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets                                  # noqa: E402

PAGES = {0: "轨迹地图", 1: "曲线", 2: "告警", 3: "事件", 4: "参数"}


def main() -> int:
    log = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(r"C:\Users\MAKI\Documents\Mission Planner\logs\QUADROTOR\1\2026-10-29 14-18-00.bin")
    profile = "--profile" in sys.argv

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apmlog.ui import settings as st
    tmp = Path(tempfile.mkdtemp(prefix="perf-switch-"))
    st.Settings.set_instance(st.Settings(tmp / "settings.json"))
    st.install_tip_style(app)

    from apmlog.app import MainWindow
    win = MainWindow()
    win._views_path = tmp / "views.json"          # 全局 views.json 必须隔离
    win.resize(1600, 950)
    win.show()
    app.processEvents()

    t0 = time.perf_counter()
    win.load_sync(str(log))
    for _ in range(5):
        app.processEvents()
    print(f"加载（缓存热）：{time.perf_counter() - t0:.2f} s  "
          f"事件 {len(win._events)} 告警 {len(win._alerts)}")

    def switch(i: int) -> float:
        t = time.perf_counter()
        win.tabs.setCurrentIndex(i)
        app.processEvents()
        win.grab()                                  # 强制画面真的画出来
        return time.perf_counter() - t

    # 预热一轮（第一次进某页有懒初始化，单独报）
    for i in PAGES:
        switch(i)
    win.tabs.setCurrentIndex(0)
    app.processEvents()

    rows: dict[tuple[int, int], list[float]] = {}
    for _round in range(3):
        for i in (1, 2, 3, 4):
            rows.setdefault((0, i), []).append(switch(i))
            rows.setdefault((i, 0), []).append(switch(0))
    print()
    print(f"{'切换':<16}{'次数':>4}{'中位(ms)':>10}{'最慢(ms)':>10}")
    worst: tuple[float, tuple[int, int]] = (0.0, (0, 0))
    for key, vals in rows.items():
        med = statistics.median(vals) * 1000
        mx = max(vals) * 1000
        print(f"{PAGES[key[0]] + ' → ' + PAGES[key[1]]:<16}{len(vals):>4}{med:>10.1f}{mx:>10.1f}")
        if mx > worst[0]:
            worst = (mx, key)

    # 逐页首次进入的耗时（懒初始化，用户第一次点的那下）
    win.tabs.setCurrentIndex(0)
    app.processEvents()
    print("\n首次进入（清掉指纹后强制重建）：")
    for i in (1, 2, 3, 4):
        win._applied_fp = None
        t = time.perf_counter()
        win.tabs.setCurrentIndex(i)
        app.processEvents()
        win.grab()
        print(f"  → {PAGES[i]:<8}{1000 * (time.perf_counter() - t):8.1f} ms")
        win.tabs.setCurrentIndex(0)
        app.processEvents()

    if profile:
        win.tabs.setCurrentIndex(0)
        app.processEvents()
        win._applied_fp = None
        pr = cProfile.Profile()
        pr.enable()
        win.tabs.setCurrentIndex(worst[1][1])
        app.processEvents()
        win.grab()
        pr.disable()
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(18)
        print("\n=== cProfile（切到「%s」，未吃到指纹缓存）===" % PAGES[worst[1][1]])
        print("\n".join(s.getvalue().splitlines()[4:34]))

    win.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
