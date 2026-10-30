# -*- coding: utf-8 -*-
"""tools/_perf_apply.py —— 拆开一次完整 `_apply()`（也就是第一次切到某页时的重建）。

    .\\.venv\\Scripts\\python.exe -X utf8 tools/_perf_apply.py "日志.bin"

做法：把 `_apply()` 里调到的每个下游方法包一层计时，按总耗时排序。不猜，只量。
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


def main() -> int:
    log = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(r"C:\Users\MAKI\Documents\Mission Planner\logs\QUADROTOR\1\2026-10-29 14-18-00.bin")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apmlog.ui import settings as st
    tmp = Path(tempfile.mkdtemp(prefix="perf-apply-"))
    st.Settings.set_instance(st.Settings(tmp / "settings.json"))
    st.install_tip_style(app)

    from apmlog import app as A
    win = A.MainWindow()
    win._views_path = tmp / "views.json"
    win.resize(1600, 950)
    win.show()
    app.processEvents()
    win.load_sync(str(log))
    for _ in range(5):
        app.processEvents()
    print(f"事件 {len(win._events)} 告警 {len(win._alerts)}")

    times: dict[str, list[float]] = {}

    def wrap(obj, name: str, label: str) -> None:
        orig = getattr(obj, name)

        def inner(*a, **k):
            t = time.perf_counter()
            try:
                return orig(*a, **k)
            finally:
                times.setdefault(label, []).append(time.perf_counter() - t)

        setattr(obj, name, inner)

    wrap(A, "build_lanes", "build_lanes（取数+建泳道）")
    for name, label in (("set_lanes", "plot.set_lanes"),
                        ("set_events", "plot.set_events"),
                        ("set_regions", "plot.set_regions")):
        wrap(win.plot, name, label)
    for name, label in (("set_tracks", "map.set_tracks"),
                        ("set_track", "map.set_track"),
                        ("set_events", "map.set_events*"),
                        ("set_regions", "map.set_regions*")):
        wrap(win.map, name, label)
    for name, label in (("set_span", "timeline.set_span"),
                        ("set_modes", "timeline.set_modes"),
                        ("set_events", "timeline.set_events*"),
                        ("set_regions", "timeline.set_regions*")):
        wrap(win.timeline, name, label)

    print("\n== 一次完整 _apply（keep_window=True）==")
    total = 0.0
    for i in range(3):
        win._applied_fp = None
        t = time.perf_counter()
        win._apply(keep_window=True)
        dt = time.perf_counter() - t
        total += dt
        print(f"  第 {i + 1} 次：{dt * 1000:.0f} ms")
    print(f"  平均：{total / 3 * 1000:.0f} ms")

    print("\n== 分项（三次合计）==")
    acc: dict[str, float] = {}
    for label, vals in times.items():
        acc[label] = sum(vals)
    for label, sec in sorted(acc.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<28}{sec * 1000:9.0f} ms  (调用 {len(times[label])} 次)")
    covered = sum(acc.values())
    print(f"  {'(以上合计)':<28}{covered * 1000:9.0f} ms / 总 {total * 1000:.0f} ms")

    win.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

