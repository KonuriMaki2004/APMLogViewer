# -*- coding: utf-8 -*-
"""tools/_bench_channels.py —— 同一条真实日志在两个下载通道上各跑一遍。

    .\\.venv\\Scripts\\python.exe -X utf8 tools/_bench_channels.py "路径.bin"

通道：
* ``log``  —— ``LOG_REQUEST_LIST/DATA``（原来的实现，兜底通道）
* ``ftp``  —— MAVFTP ``FILE_TRANSFER_PROTOCOL``(110)，burst 读

注意：这里跑的是 **FakeFC 回环**（TCP 本机），所以数字反映的是“本端处理 + 协议开销”，
不是真机链路带宽；真机未验证。表里同时给出每次请求的字节数，那是与链路无关的
协议效率指标。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os                                                    # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets                                 # noqa: E402

from apmlog.link import ftp as ftpmod                         # noqa: E402
from apmlog.link.download import RemoteLog                    # noqa: E402
from apmlog.link.fakes import FakeFC                          # noqa: E402
from apmlog.ui import linkpanel as lp                         # noqa: E402

LOG_ID = 4                                                    # → /APM/LOGS/00000005.BIN


def run_one(channel: str, data: bytes) -> dict:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    fc = FakeFC([(LOG_ID, data)], heartbeat_period=0.05,
                files={"/APM/LOGS/00000005.BIN": data})
    out = Path(tempfile.mkdtemp(prefix=f"bench-{channel}-"))
    w = lp._LinkWorker()
    w.set_options(out_dir=str(out), blocks_per_request=120, transport=channel)
    done: list = []
    notes: list[str] = []
    w.download_done.connect(done.append)
    w.note.connect(notes.append)
    host, port = fc.serve_tcp("127.0.0.1", 0)
    w._do_connect(target=f"tcp:{host}:{port}", baud=57600, wait=5.0)
    w._known = [RemoteLog(LOG_ID, 1, 1, 0, len(data))]
    t0 = time.perf_counter()
    w._do_download(LOG_ID)
    elapsed = time.perf_counter() - t0
    res = done[-1] if done else None
    verified = bool(res and res.complete and Path(res.path).read_bytes() == data)
    stats = fc.stats
    ftp_calls = sum(1 for op in fc.ftp_ops if op == ftpmod.OP_BURST_READ_FILE)
    row = {
        "channel": channel,
        "bytes": len(data),
        "elapsed_s": round(elapsed, 3),
        "MB_per_s": round(len(data) / elapsed / 1e6, 3),
        "complete": bool(res and res.complete),
        "verified": verified,
        "reason": (res.reason if res else "无结果"),
        "log_data_requests": stats.data_requests,
        "log_data_frames": stats.data_frames,
        "ftp_burst_requests": ftp_calls,
        "ftp_ops": len(fc.ftp_ops),
        "bytes_per_request": round(
            len(data) / max(1, stats.data_requests if channel == "log" else ftp_calls), 1),
    }
    try:
        w.sess.close()
    finally:
        fc.stop()
        w.deleteLater()
        shutil.rmtree(out, ignore_errors=True)
        app.processEvents()
    return row


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else \
        Path(r"C:\Users\MAKI\Documents\Mission Planner\logs\QUADROTOR\1\2026-10-29 14-18-00.bin")
    only = ""
    for a in sys.argv[2:]:
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
    data = path.read_bytes()
    print(f"日志：{path.name}  {len(data) / 1e6:.1f} MB", flush=True)
    rows: list[dict] = []
    dst = ROOT / "tools" / f"_bench_channels_{len(data) / 1e6:.0f}MB.json"
    for ch in (("log", "ftp") if only not in ("log", "ftp") else (only,)):
        print(f"  跑 {ch} …", flush=True)
        row = run_one(ch, data)
        rows.append(row)
        print(f"    {ch}: {row['bytes']} B / {row['elapsed_s']:.2f}s = {row['MB_per_s']:.2f} MB/s "
              f"每次请求 {row['bytes_per_request']} B 校验{'OK' if row['verified'] else 'FAIL'}",
              flush=True)
        dst.write_text(json.dumps({"file": str(path), "size": len(data), "rows": rows},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"{'通道':<6}{'字节':>12}{'耗时(s)':>10}{'MB/s':>8}{'每次请求字节':>14}"
          f"{'请求数':>8}{'校验':>6}")
    for r in rows:
        n_req = r["log_data_requests"] if r["channel"] == "log" else r["ftp_burst_requests"]
        print(f"{r['channel']:<6}{r['bytes']:>12}{r['elapsed_s']:>10.2f}{r['MB_per_s']:>8.2f}"
              f"{r['bytes_per_request']:>14}{n_req:>8}{'OK' if r['verified'] else 'FAIL':>6}")
    log_row = next((r for r in rows if r["channel"] == "log"), None)
    ftp_row = next((r for r in rows if r["channel"] == "ftp"), None)
    if log_row and ftp_row and ftp_row["MB_per_s"] > 0:
        print(f"\n回环上 MAVFTP 是 LOG_* 的 {ftp_row['MB_per_s'] / log_row['MB_per_s']:.1f}×；"
              f"每次请求字节数 {ftp_row['bytes_per_request'] / max(1.0, log_row['bytes_per_request']):.1f}×")
    dst = ROOT / "tools" / f"_bench_channels_{len(data) / 1e6:.0f}MB.json"
    dst.write_text(json.dumps({"file": str(path), "size": len(data), "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {dst}")
    return 0 if all(r["verified"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
