# -*- coding: utf-8 -*-
"""tools/_hw_channels.py —— 真机：同一份日志在 MAVFTP / LOG_* / 自动回退三条路上各跑一遍。

    .\\.venv\\Scripts\\python.exe -X utf8 tools/_hw_channels.py COM94 [log_id]

做的事（全部只读：只发 LOG_REQUEST_* 与 FILE_TRANSFER_PROTOCOL 的读操作）：

1. ``ftp``：MAVFTP 直接下载，记耗时/吞吐/sha256；
2. ``auto``（人为让 MAVFTP 打不开：把路径改成不存在的文件）→ 验证自动回退到 ``LOG_*``，
   并比较落盘内容；
3. ``log``：``LOG_REQUEST_DATA`` 兜底通道，同样记耗时/吞吐/sha256。

三条都下同一份日志，所以 sha256 应该完全一致——这是“通道只影响速度、不影响内容”的硬证据。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets                                    # noqa: E402

from apmlog.link.download import RemoteLog                       # noqa: E402
from apmlog.ui import linkpanel as lp                            # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_channel(app, port: str, entry: RemoteLog, channel: str,
                *, break_ftp: bool = False) -> dict:
    out = Path(tempfile.mkdtemp(prefix=f"hw-{channel}-"))
    w = lp._LinkWorker()
    w.set_options(out_dir=str(out), blocks_per_request=120, transport=channel)
    done: list = []
    notes: list[str] = []
    w.download_done.connect(done.append)
    w.note.connect(notes.append)
    if break_ftp:
        # 让 MAVFTP 打不开：改成飞控上不存在的路径（链路级失败，不是 mock 结果）
        w._ftp_remote_path = lambda log_id: f"/APM/LOGS/__no_such__{int(log_id) + 1:08d}.BIN"
    t_conn = time.perf_counter()
    w._do_connect(target=port, baud=115200, wait=8.0)
    conn_s = time.perf_counter() - t_conn
    if w.sess is None:
        raise SystemExit(f"连不上 {port}")
    w._known = [entry]
    t0 = time.perf_counter()
    w._do_download(entry.log_id)
    elapsed = time.perf_counter() - t0
    res = done[-1] if done else None
    row = {
        "channel": channel,
        "connect_s": round(conn_s, 2),
        "ok": bool(res and res.complete),
        "reason": (res.reason if res else "无结果"),
        "bytes": (res.written_bytes if res else 0),
        "elapsed_s": round(elapsed, 3),
        "KB_per_s": round((res.written_bytes / elapsed / 1024.0) if res and elapsed else 0.0, 2),
        "sha256": "",
        "fell_back": any("回退" in n for n in notes),
        "notes": notes[-4:],
    }
    p = Path(res.path) if res and res.path and Path(res.path).exists() else None
    if p is not None:
        row["sha256"] = sha256(p)
        row["path"] = str(p)
    try:
        w.sess.close()
    finally:
        w.deleteLater()
        app.processEvents()
        if p is None:
            shutil.rmtree(out, ignore_errors=True)
    return row


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM94"
    want_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    # 先列日志（独立一次连接），挑最大的一条
    probe = lp._LinkWorker()
    logs: list[RemoteLog] = []
    probe.logs_ready.connect(lambda rows: logs.extend(rows))
    probe._do_connect(target=port, baud=115200, wait=8.0)
    probe._do_list()
    time.sleep(1.0)
    app.processEvents()
    if probe.sess is not None:
        probe.sess.close()
    probe.deleteLater()
    if not logs:
        print("没拿到日志列表")
        return 2
    entry = next((x for x in logs if x.log_id == want_id), None) if want_id is not None \
        else max(logs, key=lambda x: x.size)
    assert entry is not None
    print(f"目标日志：id={entry.log_id} size={entry.size} B "
          f"（飞控共 {len(logs)} 条）", flush=True)

    rows = []
    for channel, break_ftp, label in (("ftp", False, "MAVFTP"),
                                      ("auto", True, "自动回退（MAVFTP 故意打不开）"),
                                      ("log", False, "LOG_*")):
        print(f"  跑 {label} …", flush=True)
        row = run_channel(app, port, entry, channel, break_ftp=break_ftp)
        row["label"] = label
        rows.append(row)
        print(f"    {label}: {row['bytes']} B / {row['elapsed_s']:.2f}s = "
              f"{row['KB_per_s']:.1f} KB/s ok={row['ok']} 回退={row['fell_back']} "
              f"sha={row['sha256'][:16]}…", flush=True)
        dst = ROOT / "tools" / f"_hw_channels_{entry.size}B.json"
        dst.write_text(json.dumps({"port": port, "log_id": entry.log_id,
                                   "size": entry.size, "rows": rows},
                                  ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n通道      字节      耗时(s)    KB/s    回退   sha256")
    for r in rows:
        print(f"{r['label']:<10}{r['bytes']:>10}{r['elapsed_s']:>10.2f}{r['KB_per_s']:>9.1f}"
              f"   {'是' if r['fell_back'] else '否':<4}{r['sha256'][:16]}")
    shas = {r["sha256"] for r in rows if r["sha256"]}
    print(f"\n三条通道内容一致：{'是' if len(shas) == 1 else '否 ' + str(shas)}")
    return 0 if len(shas) == 1 and all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
