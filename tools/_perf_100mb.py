# -*- coding: utf-8 -*-
"""定位 100MB 日志的卡顿：分段计时 + 内存 + GUI 交互耗时。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe tools/_perf_100mb.py <日志.bin> [--cache 缓存目录]
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def mem_mb() -> tuple[float, float]:
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(pmc)
    ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),
                                             ctypes.byref(pmc), pmc.cb)
    return pmc.WorkingSetSize / 1048576, pmc.PeakWorkingSetSize / 1048576


def mark(tag: str, t0: float) -> float:
    ws, peak = mem_mb()
    print(f"  [{tag}] {time.perf_counter() - t0:6.2f}s   内存 {ws:6.0f} MB（峰 {peak:6.0f} MB）", flush=True)
    return time.perf_counter()


def main() -> int:
    log = Path(sys.argv[1])
    cache = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp(prefix="apmlog_perf_"))
    cache.mkdir(parents=True, exist_ok=True)
    print(f"日志 {log.name}  {log.stat().st_size / 1048576:.1f} MB")
    print(f"缓存 {cache}")

    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from apmlog.ui import settings as st
    st.Settings.set_instance(st.Settings(cache / "settings.json"))
    import apmlog.app as appmod
    appmod.MainWindow._views_path = lambda self: cache / "views.json"
    # 让 store 用我们的缓存目录
    import apmlog.core.store as store_mod
    orig = store_mod.LogStore
    print("\n=== 1) 建窗 ===")
    t = time.perf_counter()
    win = appmod.MainWindow()
    win.resize(1600, 950)
    win.show()
    for _ in range(10):
        app.processEvents()
    t = mark("建窗", t)

    print("=== 2) 冷加载（无缓存）===")
    win.load_sync(str(log))
    for _ in range(4):
        app.processEvents()
    t = mark("冷加载", t)
    s = win.store.stats
    print(f"      报文 {s.n_messages:,}｜类型 {s.n_types}｜参数 {s.n_params}｜"
          f"事件 {len(win._events)}｜告警 {len(win._alerts)}｜泳道 {len(win.plot.lanes())}")
    print(f"      炸机线索 {len(s.crashes)}")

    print("=== 3) 切到曲线页 ===")
    t = time.perf_counter()
    win.tabs.setCurrentIndex(1)
    for _ in range(30):
        app.processEvents()
    t = mark("切曲线页", t)

    print("=== 4) 时间轴拖动 20 次（每次 5% 步进）===")
    t0, t1 = win.store.stats.t_start, win.store.stats.t_end
    t = time.perf_counter()
    worst = 0.0
    for i in range(20):
        tk = t0 + (t1 - t0) * (i / 20)
        a = time.perf_counter()
        win._on_time(tk)
        app.processEvents()
        worst = max(worst, time.perf_counter() - a)
    print(f"  [拖动 20 次] 合计 {time.perf_counter() - t:6.2f}s   单次最差 {worst * 1000:6.1f} ms")
    mark("拖动后", t)

    print("=== 5) 曲线缩放 10 次（滚轮等价：直接改视图窗）===")
    t = time.perf_counter()
    worst = 0.0
    span = (t1 - t0)
    for i in range(10):
        w = span / (2 ** i)
        mid = (t0 + t1) / 2
        a = time.perf_counter()
        try:
            win.plot.set_view(mid - w / 2, mid + w / 2)
        except Exception:
            win.plot.set_x_range(mid - w / 2, mid + w / 2)
        app.processEvents()
        worst = max(worst, time.perf_counter() - a)
    print(f"  [缩放 10 次] 合计 {time.perf_counter() - t:6.2f}s   单次最差 {worst * 1000:6.1f} ms")
    mark("缩放后", t)

    print("=== 6) 打开事件/告警详情 ===")
    t = time.perf_counter()
    shown = []
    orig_show = win._show_help
    win._show_help = lambda entry, **kw: shown.append(entry)
    ev = next((e for e in win._events if e.kind == "ERR"), win._events[0] if win._events else None)
    a = time.perf_counter()
    if ev is not None:
        win._open_event_help(ev)
    print(f"  打开事件说明 {1000 * (time.perf_counter() - a):6.1f} ms")
    if win._alerts:
        a = time.perf_counter()
        win._open_alert_help(win._alerts[0])
        print(f"  打开告警说明 {1000 * (time.perf_counter() - a):6.1f} ms")
    win._show_help = orig_show
    mark("弹窗", t)

    print("=== 7) 事件页 / 告警页切换 ===")
    for idx, name in ((3, "事件页"), (2, "告警页"), (4, "参数页")):
        t = time.perf_counter()
        win.tabs.setCurrentIndex(idx)
        for _ in range(6):
            app.processEvents()
        mark(f"切{name}", t)

    print("=== 8) 拖动左侧栏 5 次 ===")
    split = win.centralWidget().widget(0)
    t = time.perf_counter()
    worst = 0.0
    for i in range(5):
        a = time.perf_counter()
        split.setSizes([300 + i * 60, 1200 - i * 60])
        for _ in range(4):
            app.processEvents()
        worst = max(worst, time.perf_counter() - a)
    print(f"  [拖左栏 5 次] 合计 {time.perf_counter() - t:6.2f}s   单次最差 {worst * 1000:6.1f} ms")
    mark("拖左栏后", t)

    print("=== 9) 热加载（有缓存，重新打开同一日志）===")
    t = time.perf_counter()
    win.load_sync(str(log))
    for _ in range(4):
        app.processEvents()
    mark("热加载", t)

    print("=== 10) 建第二个窗（模拟再开一份）===")
    t = time.perf_counter()
    win2 = appmod.MainWindow()
    win2.resize(1200, 800)
    win2.load_sync(str(log))
    for _ in range(4):
        app.processEvents()
    mark("第二窗", t)
    win2.close()
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
