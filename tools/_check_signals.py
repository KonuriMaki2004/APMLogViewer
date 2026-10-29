"""核查文档里写到的三个信号时机：log_closed / alerts_updated / selection_changed。

用两份真日志跑一遍主窗，打印实际收到的信号序列。

用法（仓库根目录）：
    .venv/Scripts/python.exe tools/_check_signals.py <日志A.bin> <日志B.bin>
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets                                          # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

tmp = Path(tempfile.mkdtemp(prefix="apmlog_sig_"))
from apmlog.ui import settings as st                                    # noqa: E402
st.Settings.set_instance(st.Settings(tmp / "settings.json"))

import apmlog.app as appmod                                             # noqa: E402
appmod.MainWindow._views_path = lambda self: tmp / "views.json"

win = appmod.MainWindow()
win.show()

events: list[str] = []
s = win.plugins.signals
s.log_loaded.connect(lambda info: events.append(f"log_loaded({info['name']}, "
                                                f"alerts={info['n_alerts']})"))
s.log_closed.connect(lambda: events.append("log_closed"))
s.alerts_updated.connect(lambda hits: events.append(f"alerts_updated({len(hits)})"))
s.selection_changed.connect(lambda d: events.append(
    f"selection_changed(t0={d['t0']:.1f}, t1={d['t1']:.1f}, whole={d['whole_log']})"))
s.mode_changed.connect(lambda m: events.append(f"mode_changed({m})"))

for _ in range(4):
    app.processEvents()

a, b = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("", "")
if not a or not b:
    raise SystemExit("需要两个日志路径")

print("=== 加载第一份 ===")
win.load_sync(a)
for _ in range(6):
    app.processEvents()

print("=== 加载第二份（应先 log_closed 再 log_loaded） ===")
win.load_sync(b)
for _ in range(6):
    app.processEvents()

print("=== 框选后复位 ===")
win._on_region(30.0, 40.0)
win._reset_window()
for _ in range(4):
    app.processEvents()

for e in events:
    print("  ", e)

need = ["log_loaded", "log_closed", "alerts_updated", "selection_changed"]
missing = [n for n in need if not any(x.startswith(n) for x in events)]
print("缺失:", missing or "无")
win.close()
