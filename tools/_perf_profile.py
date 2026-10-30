# -*- coding: utf-8 -*-
"""定位冷解析与切曲线页的热点：分阶段计时 + cProfile Top。"""
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
LOG = sys.argv[1]
CACHE = Path(sys.argv[2])


def top(prof: cProfile.Profile, n: int = 18, sort: str = "cumulative") -> None:
    s = io.StringIO()
    pstats.Stats(prof, stream=s).sort_stats(sort).print_stats(n)
    print("\n".join(s.getvalue().splitlines()[4:]), flush=True)


def main() -> int:
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from apmlog.ui import settings as st
    st.Settings.set_instance(st.Settings(CACHE / "settings.json"))
    import apmlog.app as appmod
    appmod.MainWindow._views_path = lambda self: CACHE / "views.json"

    print("=== A) 冷解析（profile）===", flush=True)
    from apmlog.core.store import LogStore
    pr = cProfile.Profile()
    t = time.perf_counter()
    pr.enable()
    store = LogStore.open(LOG, cache_dir=CACHE / "cold")
    pr.disable()
    print(f"  解析+建缓存 {time.perf_counter() - t:.2f}s  报文 {store.stats.n_messages:,}", flush=True)
    top(pr, 20, "cumulative")
    print("\n--- 按自身耗时 ---", flush=True)
    top(pr, 12, "tottime")

    print("\n=== B) 冷加载整窗（含事件/告警/表格/图）===", flush=True)
    win = appmod.MainWindow()
    win.resize(1600, 950)
    win.show()
    for _ in range(8):
        app.processEvents()
    pr2 = cProfile.Profile()
    t = time.perf_counter()
    pr2.enable()
    win.load_sync(LOG)
    pr2.disable()
    print(f"  整窗加载 {time.perf_counter() - t:.2f}s", flush=True)
    top(pr2, 20, "cumulative")

    print("\n=== C) 切曲线页（profile）===", flush=True)
    pr3 = cProfile.Profile()
    t = time.perf_counter()
    pr3.enable()
    win.tabs.setCurrentIndex(1)
    for _ in range(20):
        app.processEvents()
    pr3.disable()
    print(f"  切页 {time.perf_counter() - t:.2f}s", flush=True)
    top(pr3, 20, "cumulative")

    print("\n=== D) 时间轴拖动一次（profile）===", flush=True)
    s = win.store.stats
    pr4 = cProfile.Profile()
    t = time.perf_counter()
    pr4.enable()
    win._on_time((s.t_start + s.t_end) / 2)
    for _ in range(3):
        app.processEvents()
    pr4.disable()
    print(f"  单次跳转 {time.perf_counter() - t:.3f}s", flush=True)
    top(pr4, 12, "cumulative")
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
