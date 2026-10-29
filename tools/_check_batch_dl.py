# -*- coding: utf-8 -*-
"""真字节批量下载核验：FakeFC(TCP 回环) 挂 3 条日志 → 面板勾选 3 条 → 一次下完。

跑法：
    QT_QPA_PLATFORM=offscreen ./.venv/Scripts/python.exe tools/_check_batch_dl.py
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets                                    # noqa: E402

from apmlog.link.fakes import FakeFC, FakeLog                            # noqa: E402
from apmlog.ui.linkpanel import LinkPanel                                # noqa: E402

SRC = Path(r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000168.BIN")
IDS = [201, 202, 203]


def pump(app, n: int = 3) -> None:
    for _ in range(n):
        app.processEvents()


def wait_for(app, cond, secs: float, why: str) -> bool:
    t0 = time.time()
    while time.time() - t0 < secs:
        pump(app, 2)
        time.sleep(0.02)
        if cond():
            return True
    print(f"  !! 超时 {secs}s：{why}")
    return False


def main() -> int:
    logging.basicConfig(level=logging.DEBUG if "-v" in sys.argv else logging.INFO,
                        format="%(levelname).1s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)])
    if not SRC.exists():
        print(f"找不到样本日志 {SRC}")
        return 2
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    out = Path(tempfile.mkdtemp(prefix="apmlog_batch_"))
    want_sha = hashlib.sha1(SRC.read_bytes()).hexdigest()
    fc = FakeFC([FakeLog.from_file(SRC, i) for i in IDS], heartbeat_period=0.15)
    host, port = fc.serve_tcp("127.0.0.1", 0)
    print(f"FakeFC 已启动 {host}:{port}，3 条日志，每条 {SRC.stat().st_size} B")

    panel = LinkPanel(default_target="", out_dir=str(out))
    panel.show()
    pump(app)
    ok = True

    panel.set_target(f"tcp:{host}:{port}")
    print(f"  手动目标 → 方式 {panel._mode()}，预览 {panel.lbl_target.text()}")
    panel.btn_connect.click()
    # 注意：“open” 就已经把 _link_up 置 True，但此时面板还在 busy（ready 才解锁），
    # 在 busy 里点“刷新列表”是空操作——等“就绪且空闲”才能继续。
    if not wait_for(app, lambda: panel._link_up and not panel._busy, 25.0, "链路未就绪或未解锁"):
        ok = False
    print(f"  链路就绪：{panel.lbl_state.text()}｜刷新按钮可用={panel.btn_refresh.isEnabled()}")
    panel.btn_refresh.click()
    if not wait_for(app, lambda: panel.table.rowCount() == 3, 20.0, "列表没回来"):
        ok = False
        print("  ---- 面板链路输出 ----")
        print("\n".join("  | " + x for x in panel.plain.toPlainText().splitlines()[-14:]))
        print("  ---- FakeFC ----")
        print("  last_error =", repr(getattr(fc, "last_error", None)))
        print("  stats =", getattr(panel.worker.sess, "stats", None))
    print(f"  列表 {panel.table.rowCount()} 条，状态列：",
          [panel.table.item(r, 4).text() for r in range(panel.table.rowCount())])

    # 勾选 3 条（走行内 checkbox 的真实交互路径）
    for r in range(panel.table.rowCount()):
        panel.table.item(r, 0).setCheckState(QtCore.Qt.CheckState.Checked)
    pump(app)
    print(f"  勾选后：{panel._checked_ids()}｜按钮「{panel.btn_download.text()}」｜{panel.lbl_checked.text()}")

    got: list[tuple[list, list]] = []
    panel.batchFinished.connect(lambda ok_, bad_: got.append((list(ok_), list(bad_))))
    t0 = time.time()
    panel.btn_download.click()
    pump(app)
    if not wait_for(app, lambda: panel._current == IDS[0] and panel.table.item(0, 4).text() == "下载中",
                    10.0, "第一条没开始"):
        print("  （提示：若 worker.post 正常，这里应立即进入下载中）")
    if not wait_for(app, lambda: bool(got), 60.0, "批量没在 60s 内结束"):
        ok = False
    dt = time.time() - t0
    paths, bad = (got[0] if got else ([], []))
    print(f"  批量结束：{len(paths)} 成功 / {len(bad)} 失败，用时 {dt:.1f}s "
          f"≈ {sum(p.stat().st_size for p in out.glob('*.BIN') if p.is_file()) / 1024 / max(dt, 0.1):.0f} KB/s")
    print(f"  进度条文案：{panel.pb.format()}｜速率行：{panel.lbl_rate.text()}")
    print(f"  行状态：", [panel.table.item(r, 4).text() for r in range(panel.table.rowCount())])
    print(f"  结束后剩余勾选：{panel._checked_ids()}（应为空）")

    for lid in IDS:
        f = out / f"0000000{lid}.BIN" if False else out / f"{lid:08d}.BIN"
        if not f.exists():
            cands = sorted(out.glob(f"*{lid}*.BIN"))
            f = cands[0] if cands else f
        if not f.exists():
            print(f"  !! 缺文件 log {lid}：{f}")
            ok = False
            continue
        h = hashlib.sha1(f.read_bytes()).hexdigest()
        same = h == want_sha
        ok = ok and same
        print(f"  log {lid}: {f.name} {f.stat().st_size}B sha1 {h[:16]} {'== 源' if same else '!= 源 差异!'}")
    if panel._busy:
        print("  !! 批量结束后面板仍处于 busy，按钮锁死")
        ok = False
    panel.shutdown()
    panel.close()
    panel.deleteLater()
    pump(app, 6)
    fc.stop() if hasattr(fc, "stop") else None
    print("结论:", "PASS" if ok else "FAIL", " 输出目录", out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
