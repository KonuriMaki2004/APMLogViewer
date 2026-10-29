# -*- coding: utf-8 -*-
"""整盘扫描压力冒烟：证明"选整个磁盘当目录"不再卡死/崩溃。只读，不写不删。"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")
from apmlog.scan import scan_logs, summarize

print("=== 1) 整盘 C:\ 带上限 ===")
t = time.time()
r = scan_logs("C:\\", max_files=300, max_dirs=4000, max_entries=200000,
              progress=lambda *a: None)
print(f"耗时 {time.time() - t:.2f}s  正常返回 {type(r).__name__}")
print(f"total={r.total} supported={r.supported} dirs={r.dirs_scanned} "
      f"entries={r.entries_scanned} errors={r.errors}")
print(f"truncated={r.truncated} cancelled={r.cancelled} reason={r.truncated_reason!r}")
print("summarize:", summarize(r))
print("by_kind:", r.by_kind[:4])

print("\n=== 2) 整盘取消（第 50 次进度回调后取消）===")
state = {"n": 0}
def prog(*a):
    state["n"] += 1
def cancel():
    return state["n"] >= 50
t = time.time()
r2 = scan_logs("C:\\", progress=prog, should_cancel=cancel,
               max_files=5000, max_dirs=20000, max_entries=2000000)
el = time.time() - t
print(f"耗时 {el:.2f}s  cancelled={r2.cancelled} total={r2.total} "
      f"progress回调={state['n']} 结构合法={isinstance(r2.files, tuple)}")
print("summarize:", summarize(r2))
assert el < 3.0, f"取消太慢：{el:.2f}s"
assert r2.cancelled, "没取消成功"

print("\n=== 3) 无上限跑用户日志目录（模拟真实使用）===")
t = time.time()
r3 = scan_logs(r"C:\Users\MAKI\Documents\Mission Planner\logs")
print(f"耗时 {time.time() - t:.2f}s |", summarize(r3))
