# -*- coding: utf-8 -*-
"""tools/_perf_switch2.py —— 切成“切页到底把时间花在哪”：不 grab / grab / _apply / _refresh 拆开。

    .\\.venv\\Scripts\\python.exe -X utf8 tools/_perf_switch2.py "日志.bin"
"""
from __future__ import annotations

import os
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
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apmlog.ui import settings as st
    tmp = Path(tempfile.mkdtemp(prefix="perf-switch2-"))
    st.Settings.set_instance(st.Settings(tmp / "settings.json"))
    st.install_tip_style(app)

    from apmlog.app import MainWindow
    win = MainWindow()
    win._views_path = tmp / "views.json"
    win.resize(1600, 950)
    win.show()
    app.processEvents()
    win.load_sync(str(log))
    for _ in range(5):
        app.processEvents()

    stats: dict[str, list[float]] = {}

    def timed(label: str, fn):
        t = time.perf_counter()
        r = fn()
        stats.setdefault(label, []).append(time.perf_counter() - t)
        return r

    orig_apply = win._apply
    win._apply = lambda *a, **k: timed("_apply", lambda: orig_apply(*a, **k))
    orig_refresh = win.plot._refresh
    win.plot._refresh = lambda *a, **k: timed("plot._refresh", lambda: orig_refresh(*a, **k))
    for name in ("_draw_lane", "_draw_ghosts"):
        orig = getattr(win.plot, name)
        setattr(win.plot, name, (lambda o: lambda *a, **k: timed(name, lambda: o(*a, **k)))(orig))

    def switch(i: int, grab: bool) -> float:
        t = time.perf_counter()
        win.tabs.setCurrentIndex(i)
        app.processEvents()
        if grab:
            win.grab()
        return time.perf_counter() - t

    for i in PAGES:                                  # 预热
        switch(i, True)
    for k in stats:
        stats[k].clear()
    win.tabs.setCurrentIndex(0)
    app.processEvents()

    print(f"{'切换':<16}{'不 grab(ms)':>12}{'含 grab(ms)':>12}")
    for target in (1, 2, 3, 4):
        plain = statistics_median([switch(target, False) for _ in range(3)])
        win.tabs.setCurrentIndex(0)
        app.processEvents()
        withgrab = statistics_median([switch(target, True) for _ in range(3)])
        win.tabs.setCurrentIndex(0)
        app.processEvents()
        print(f"{PAGES[0] + ' → ' + PAGES[target]:<16}{plain * 1000:>12.1f}{withgrab * 1000:>12.1f}")

    print("\n期间内部耗时（合计 ms / 次数）：")
    for label, vals in sorted(stats.items(), key=lambda kv: -sum(kv[1])):
        if vals:
            print(f"  {label:<16}{sum(vals) * 1000:9.1f} / {len(vals)}")

    win.close()
    app.processEvents()
    return 0


def statistics_median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2]


if __name__ == "__main__":
    raise SystemExit(main())
