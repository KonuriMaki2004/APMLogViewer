"""核查：真实日志里双击告警/错误 → 弹窗内容与兜底提示（并出截图）。

用法（仓库根目录）：
    .venv/Scripts/python.exe tools/_check_event_help.py <含炸机/错误的日志.bin>
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets                                          # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

tmp = Path(tempfile.mkdtemp(prefix="apmlog_evhelp_check_"))
from apmlog.ui import settings as st                                    # noqa: E402
st.Settings.set_instance(st.Settings(tmp / "settings.json"))

import apmlog.app as appmod                                             # noqa: E402
appmod.MainWindow._views_path = lambda self: tmp / "views.json"

from apmlog.core.model import EventRec                                   # noqa: E402
from apmlog.ui.eventhelp import EventHelpDialog                          # noqa: E402

log_path = sys.argv[1] if len(sys.argv) > 1 else ""
win = appmod.MainWindow()
win.resize(1400, 880)
win.show()
for _ in range(6):
    app.processEvents()

if log_path:
    win.load_sync(log_path)
    for _ in range(8):
        app.processEvents()

print("=== 日志事件概览 ===")
print(f"  事件 {len(win._events)} 条｜告警 {len(win._alerts)} 条")
for e in win._events:
    if e.severity in ("warn", "error", "critical"):
        print(f"  [{e.severity:8s}] {e.kind:4s} t={e.t:8.2f}  {e.display[:70]}")

# ---- 1) 真实错误：双击 —— 只截获说明，验证不弹模态 ----
target = None
for e in win._events:
    if e.kind == "ERR" and e.severity in ("warn", "error", "critical"):
        target = e
        break
if target is None:
    print("这份日志没有 warn/error 级别的事件，改用合成样本")
    target = EventRec(t=12.5, kind="ERR", code=12, ecode=1, text="Subsys=12 ECode=1",
                      decoded="CRASH_CHECK 炸机检测", severity="critical")

cursor_before = getattr(win, "_cursor_t", None)
win_before = getattr(win, "_win", None)
captured: list = []
win._show_help = lambda entry, **kw: captured.append((entry, kw))
win._open_event_help(target)
win._show_help = type(win)._show_help.__get__(win) if hasattr(type(win), "_show_help") else win._show_help
entry, kw = captured[0]
print("\n=== 双击真实错误 ===")
print("  主题:", kw["subject"])
print("  元信息:", kw["meta"])
print("  命中范围:", entry.scope, "｜matched:", entry.matched)
print("  标题:", entry.title)
print("  说明:", entry.summary[:80])
print("  建议条数:", len(entry.actions))
print("  视图未变:", (getattr(win, "_cursor_t", None) == cursor_before
                    and getattr(win, "_win", None) == win_before))

dlg = EventHelpDialog(entry, subject=kw["subject"], meta=kw["meta"], raw=kw["raw"])
dlg.show()
for _ in range(5):
    app.processEvents()
dlg.grab().save(str(ROOT / "selftest" / "event_help_matched.png"))
dlg.close()

# ---- 2) 未收录类型：必须有明确提示 ----
unknown = EventRec(t=3.0, kind="ZZZ", text="未收录的测试类型", decoded="未收录的测试类型",
                   severity="warn")
cap2: list = []
win._show_help = lambda entry, **kw: cap2.append((entry, kw))
win._open_event_help(unknown)
e2, kw2 = cap2[0]
print("\n=== 双击未收录类型 ===")
print("  matched:", e2.matched, "｜scope:", e2.scope)
print("  标题:", e2.title)
print("  提示:", e2.summary[:80])
print("  建议条数:", len(e2.actions))
dlg2 = EventHelpDialog(e2, subject=kw2["subject"], meta=kw2["meta"], raw=kw2["raw"])
dlg2.show()
for _ in range(5):
    app.processEvents()
dlg2.grab().save(str(ROOT / "selftest" / "event_help_fallback.png"))
dlg2.close()

# ---- 3) 真实告警（振动/电池） ----
if win._alerts:
    hit = win._alerts[0]
    cap3: list = []
    win._show_help = lambda entry, **kw: cap3.append((entry, kw))
    win._open_alert_help(hit)
    e3, kw3 = cap3[0]
    print("\n=== 双击真实告警 ===")
    print("  规则:", hit.rule, "｜标题:", hit.title)
    print("  命中:", e3.matched, e3.scope, "｜", e3.title)
    dlg3 = EventHelpDialog(e3, subject=kw3["subject"], meta=kw3["meta"], raw=kw3["raw"])
    dlg3.show()
    for _ in range(5):
        app.processEvents()
    dlg3.grab().save(str(ROOT / "selftest" / "event_help_alert.png"))
    dlg3.close()

print("\n截图: selftest/event_help_matched.png / event_help_fallback.png / event_help_alert.png")
win.close()
print("OK")
