# -*- coding: utf-8 -*-
"""tools/real_link_tune.py —— 真机链路调优 / 只读审计 / GUI 打真机（可复现）。

需要**真实飞控连着串口**（或任意 tcp/udp 目标）。只做读取：本脚本除
``HEARTBEAT``（仅 udp 需要）/ ``LOG_REQUEST_LIST`` / ``LOG_REQUEST_DATA`` 外不发任何东西，
并且每次结束都会打印 tx 逐 msgid 审计表作为证据。

用法（务必用项目 venv）：
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\real_link_tune.py list  --target COM91
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\real_link_tune.py sweep --target COM91 --log 4
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\real_link_tune.py full  --target COM91 --log 4 --reps 3
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\real_link_tune.py gui   --target COM91 --log 3 --out downloads\\real_fc
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\real_link_tune.py audit --target COM91 --log 4

`full` 是唯一可信的调优口径：实测短样本会因飞控侧 SD/调度抖动出现 35–180 KB/s 双峰，
只有整段下载 + 多轮中位数才能定参数（详见 docs/LOG_DOWNLOAD_NOTES.md 第 6 节）。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apmlog.link.audit import AuditTransport                       # noqa: E402
from apmlog.link.download import default_download_dir, download_log, list_logs  # noqa: E402
from apmlog.link.endpoint import MavLinkSession, open_transport, parse_target  # noqa: E402

SWEEP = [(10, 1), (10, 2), (10, 4), (20, 1), (20, 2), (20, 4), (40, 1), (40, 2), (40, 4),
         (80, 1), (80, 2), (80, 4), (120, 1), (120, 2), (120, 4)]
FULL = [(20, 2), (40, 2), (40, 4), (80, 2), (120, 1), (120, 4)]


def connect(target: str, settle: float = 10.0) -> tuple[MavLinkSession, AuditTransport]:
    ep = parse_target(target)
    audit = AuditTransport(open_transport(ep))
    sess = MavLinkSession(audit, name=f"tune:{ep.describe()}",
                          send_our_heartbeat=ep.kind in ("udp", "udpout"))
    sess.start()
    t0 = time.monotonic()
    while sess.fc_sysid is None and time.monotonic() - t0 < settle:
        time.sleep(0.05)
    if sess.fc_sysid is None:
        sess.close()
        raise SystemExit(f"!! {settle}s 内没收到飞控心跳：{ep.describe()}（检查接线/是否上电）")
    hb = sess.heartbeats.get((sess.fc_sysid, sess.fc_compid))
    print(f"链路 {ep.describe()}  sysid={sess.fc_sysid} compid={sess.fc_compid} "
          f"type={hb.type} base_mode={hb.base_mode} armed={hb.armed}")
    return sess, audit


def pick(logs, want: int):
    if want:
        e = next((x for x in logs if x.log_id == want), None)
        if e is None:
            raise SystemExit(f"!! 飞控上没有 log {want}")
        return e
    return min(logs, key=lambda x: x.size)          # 最小的那条，避免反复整段大日志


def sample(sess, entry, b: int, w: int, limit: int) -> dict:
    d = Path(tempfile.mkdtemp(prefix="apmlog_sweep_"))
    seen = {"done": 0}
    try:
        r = download_log(sess, entry, d, blocks_per_request=b, window=w, resume=False,
                         overwrite=True, progress=lambda dd, t, rr, m: seen.__setitem__("done", dd),
                         cancel=lambda: seen["done"] >= limit)
        return dict(bytes=r.written_bytes, secs=r.elapsed, rate=r.written_bytes / max(r.elapsed, 1e-6),
                    retries=r.retries, crc=r.crc_errors, dup=r.frames_dup)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def cmd_list(a) -> int:
    sess, audit = connect(a.target)
    try:
        diag: dict = {}
        t0 = time.monotonic()
        logs = list_logs(sess, diagnostics=diag)
        for e in logs:
            print(f"  id={e.log_id} size={e.size} blocks={e.blocks} num_logs={e.num_logs} "
                  f"time_utc={e.time_utc} rtc={e.created}")
        print(f"  {len(logs)} 条 complete={diag.get('complete')} 用时 {time.monotonic() - t0:.2f}s")
        print(audit.snapshot().table())
    finally:
        sess.close()
    return 0


def cmd_sweep(a) -> int:
    sess, audit = connect(a.target)
    try:
        entry = pick(list_logs(sess), a.log)
        print(f"短样本扫描：id={entry.log_id} size={entry.size}B，每组合 {a.sample}B 后取消")
        print(f"  {'blocks':>6} {'window':>6} {'B/请求':>7} {'秒':>6} {'KB/s':>7} {'重传':>5} {'重复':>5} {'CRC':>4}")
        for b, w in SWEEP:
            r = sample(sess, entry, b, w, a.sample)
            print(f"  {b:>6} {w:>6} {b * 90:>7} {r['secs']:>6.2f} {r['rate'] / 1024:>7.1f} "
                  f"{r['retries']:>5} {r['dup']:>5} {r['crc']:>4}")
        print("  注意：短样本抖动极大（实测 35–180 KB/s 双峰），仅供排查，不用于定默认值。")
        print(audit.snapshot().table())
    finally:
        sess.close()
    return 0


def cmd_full(a) -> int:
    sess, audit = connect(a.target)
    try:
        entry = pick(list_logs(sess), a.log)
        cfgs = [tuple(int(x) for x in c.split("/")) for c in a.configs.split(",")] if a.configs else FULL
        acc: dict[tuple[int, int], list[dict]] = {c: [] for c in cfgs}
        print(f"整段扫描：id={entry.log_id} size={entry.size}B，{len(cfgs)} 组合 × {a.reps} 轮（交错）")
        print("   " + "   ".join(f"{b:>3}/{w:<2}(s)" for b, w in cfgs))
        for rep in range(a.reps):
            cells = []
            for c in cfgs:
                d = Path(tempfile.mkdtemp(prefix="apmlog_full_"))
                try:
                    r = download_log(sess, entry, d, blocks_per_request=c[0], window=c[1],
                                     resume=False, overwrite=True)
                finally:
                    shutil.rmtree(d, ignore_errors=True)
                acc[c].append(r)
                cells.append(f"{r.elapsed:>6.2f} ")
            print("   " + "   ".join(cells) + f"  <- 第 {rep + 1} 轮")
        print(f"\n   {'blocks/window':>13} {'中位秒':>7} {'最快':>7} {'最慢':>7} {'中位KB/s':>9} "
              f"{'请求':>5} {'重传':>5} {'重复':>5} {'CRC':>4}")
        rows = []
        for c, rs in acc.items():
            secs = [x.elapsed for x in rs]
            med = statistics.median(secs)
            rows.append((c, med))
            print(f"   {c[0]:>6}/{c[1]:<6} {med:>7.2f} {min(secs):>7.2f} {max(secs):>7.2f} "
                  f"{statistics.median([x.bytes_per_sec for x in rs]) / 1024:>9.1f} "
                  f"{sum(x.requests_sent for x in rs) // len(rs):>5} {sum(x.retries for x in rs):>5} "
                  f"{sum(x.frames_dup for x in rs):>5} {sum(x.crc_errors for x in rs):>4}")
        rows.sort(key=lambda x: x[1])
        print(f"\n   最快：{rows[0][0][0]}/{rows[0][0][1]} -> {rows[0][1]:.2f}s")
        print("   选默认时优先看『重传/重复/CRC 全 0 且最慢≈最快』，而不是单看中位最快。")
        print(audit.snapshot().table())
    finally:
        sess.close()
    return 0


def cmd_audit(a) -> int:
    sess, audit = connect(a.target)
    try:
        entry = pick(list_logs(sess), a.log)
        d = Path(tempfile.mkdtemp(prefix="apmlog_audit_"))
        try:
            r = download_log(sess, entry, d, blocks_per_request=a.blocks, window=a.window,
                             resume=False, overwrite=True)
            print(f"  下载 {r.written_bytes}/{r.expected_bytes}B {r.elapsed:.2f}s "
                  f"{r.bytes_per_sec / 1024:.1f} KB/s {r.reason}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
        print(audit.snapshot().table())
    finally:
        sess.close()
    return 0


def cmd_gui(a) -> int:
    """offscreen 下用 LinkPanel 打真机：列表 -> 下载 -> SHA1 -> 断开 -> 重复开关。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from apmlog.ui.linkpanel import LinkPanel

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    states: list[str] = []
    listed: list[list] = []
    got: list[str] = []

    def pump(secs: float = 0.05) -> None:
        end = time.monotonic() + secs
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.005)

    def wait(pred, timeout: float, what):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            app.processEvents()
            r = pred()
            if r:
                return r
            time.sleep(0.02)
        raise AssertionError(f"超时 {timeout}s：{what}")

    w = LinkPanel(default_target=a.target, out_dir=str(out))
    w.connectionChanged.connect(states.append)
    w.logsListed.connect(lambda x: listed.append(list(x)))
    w.logDownloaded.connect(got.append)
    w.show()
    rc = 0
    try:
        w.btn_connect.click()
        wait(lambda: "ready" in states, 20, "ready 状态")
        print(f"  连接 OK：{w.lbl_state.text()!r} 序列={states}")
        w.btn_refresh.click()
        logs = wait(lambda: listed[0] if listed else None, 20, "logsListed")
        print(f"  列表 {len(logs)} 条")
        if a.log:
            row = next(i for i in range(w.table.rowCount())
                       if w.table.item(i, 0).text() == str(a.log))
            w.table.selectRow(row)
            pump(0.1)
            before = len(got)
            w.btn_download.click()
            p = Path(wait(lambda: got[before] if len(got) > before else None, 600, "logDownloaded"))
            h = hashlib.sha1(p.read_bytes()).hexdigest()
            print(f"  下载 {p.name} {p.stat().st_size}B sha1={h[:16]}")
        w.btn_disconnect.click()
        pump(1.0)
        w.shutdown(); w.close(); w.deleteLater(); pump(0.4)
        print(f"  断开+关窗 OK，最终序列={states}")
        for i in range(3):
            st2: list[str] = []
            wi = LinkPanel(default_target=a.target, out_dir=str(out))
            wi.connectionChanged.connect(st2.append)
            wi.show()
            wi.btn_connect.click()
            wait(lambda: "ready" in st2, 20, f"第{i + 1}轮 ready")
            wi.btn_disconnect.click()
            pump(0.6)
            wi.shutdown(); wi.close(); wi.deleteLater(); pump(0.3)
            print(f"  第 {i + 1} 轮开关 OK 序列={st2}")
    except AssertionError as exc:
        print(f"!! {exc}")
        print(w.plain.toPlainText())
        rc = 1
    finally:
        if not a.keep_panel:
            try:
                w.shutdown()
            except Exception:
                pass
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="真机链路调优与只读审计")
    ap.add_argument("mode", choices=("list", "sweep", "full", "audit", "gui"))
    ap.add_argument("--target", default="COM91")
    ap.add_argument("--log", type=int, default=0, help="日志 id；缺省用最小的一条")
    ap.add_argument("--sample", type=int, default=120_000, help="sweep 每组合取样字节数")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--configs", default="", help="full 的组合，如 '120/1,160/1,200/1'")
    ap.add_argument("--blocks", type=int, default=120)
    ap.add_argument("--window", type=int, default=1)
    ap.add_argument("--out", default=str(Path(default_download_dir()) / "real_fc"),
                    help="gui 模式的下载目录（默认系统缓存区，不进工程树）")
    ap.add_argument("--keep-panel", action="store_true")
    a = ap.parse_args(argv)
    return {"list": cmd_list, "sweep": cmd_sweep, "full": cmd_full,
            "audit": cmd_audit, "gui": cmd_gui}[a.mode](a)


if __name__ == "__main__":
    raise SystemExit(main())
