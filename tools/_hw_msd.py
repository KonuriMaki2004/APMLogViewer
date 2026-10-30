# -*- coding: utf-8 -*-
"""tools/_hw_msd.py —— U 盘模式的真机“护栏”验证（不触发重启）。

做三件事，全部**不发**那条 246 命令：

1. armed 拦截：把 ``is_armed`` 置真（真实链路、真实 worker），确认 ``msd_result`` 报失败、
   且链路上**一个字节都没多发**（tx_frames 不变）——证明“armed 时直接拦截”。
2. 二次确认文案：用真实 ``LinkPanel._confirm_msd()`` 的对话框（不 exec），抓文本 + 截图。
3. ACK 结果映射：打印 ``RESULT_ZH`` 覆盖的分支，确认 UNSUPPORTED / FAILED 都有中文说明。

    .\\.venv\\Scripts\\python.exe -X utf8 tools/_hw_msd.py COM94
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

from PySide6 import QtWidgets                                    # noqa: E402

from apmlog.link import ftp as ftpmod                            # noqa: E402
from apmlog.ui import linkpanel as lp                            # noqa: E402
from apmlog.ui.settings import Settings                          # noqa: E402


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM94"
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="hw-msd-"))
    Settings.set_instance(Settings(tmp / "settings.json"))

    w = lp._LinkWorker()
    results: list[tuple[bool, str]] = []
    notes: list[str] = []
    w.msd_result.connect(lambda ok, msg: results.append((ok, msg)))
    w.note.connect(notes.append)
    w.set_options(out_dir=str(tmp), blocks_per_request=120, transport="auto")
    w._do_connect(target=port, baud=115200, wait=8.0)
    if w.sess is None:
        print("连不上", port)
        return 2
    sess = w.sess
    print(f"链路：{sess.summary()}")
    print(f"真机 armed 状态：{sess.is_armed()}")

    # ---- 1) armed 拦截（真实链路，只把 armed 判成真，不发命令）
    tx_before = sess.stats.tx_frames
    sess.is_armed = lambda: True                 # 模拟解锁：只影响拦截判断
    t0 = time.perf_counter()
    w._do_mass_storage()
    dt = time.perf_counter() - t0
    time.sleep(0.4)
    tx_after = sess.stats.tx_frames
    ok, msg = results[-1]
    print(f"\n[1] armed 拦截：ok={ok} 用时 {dt * 1000:.0f} ms")
    print(f"    提示：{msg}")
    print(f"    tx 帧 {tx_before} → {tx_after}（差 {tx_after - tx_before}，"
          f"应为 0：一个包都没发）")
    print(f"    链接日志：{notes[-2:] if notes else []}")
    blocked = (not ok) and "解锁" in msg and tx_after == tx_before

    # ---- 2) 二次确认对话框（真构造，不 exec）
    panel = lp.LinkPanel(default_target=port)
    panel._link_up = True
    shown: dict[str, str] = {}
    box_text: list[str] = []
    real_exec = QtWidgets.QMessageBox.exec

    def fake_exec(self) -> int:                  # noqa: ANN001
        box_text.append(self.text())
        box_text.append(self.informativeText())
        box_text.append("；".join(b.text() for b in self.buttons()))
        png = ROOT / "selftest" / "hw_msd_confirm.png"
        png.parent.mkdir(exist_ok=True)
        self.grab().save(str(png))
        shown["png"] = str(png)
        return 0                                  # 返回 0：相当于点了“取消”

    QtWidgets.QMessageBox.exec = fake_exec
    try:
        confirmed = panel._confirm_msd()
    finally:
        QtWidgets.QMessageBox.exec = real_exec
    text = "\n".join(box_text)
    print(f"\n[2] 二次确认：返回 {confirmed}（False=取消，说明默认不通过）")
    for line in text.splitlines():
        print("    " + line)
    need = ["重启", "U 盘", "APM/LOGS", "安全弹出", "断电", "不能飞行", "取消"]
    missing = [k for k in need if k not in text]
    print(f"    必备要素齐全：{not missing}" + (f"（缺 {missing}）" if missing else ""))
    print(f"    截图：{shown.get('png')}")
    panel.shutdown()
    panel.deleteLater()
    app.processEvents()

    # ---- 3) ACK 映射
    print("\n[3] COMMAND_ACK 结果映射：")
    for code in (0, 1, 2, 3, 4, 5, 6):
        print(f"    {code} → {ftpmod.RESULT_ZH.get(code, '（未知）')}")
    print(f"    246/param1=5 常量：cmd={ftpmod.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN} "
          f"action={ftpmod.REBOOT_SHUTDOWN_ACTION_REBOOT_TO_MASS_STORAGE}")

    sess.close()
    w.deleteLater()
    app.processEvents()
    good = blocked and not missing
    print(f"\n结论：{'通过' if good else '不通过'}（未发送任何重启命令）")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
