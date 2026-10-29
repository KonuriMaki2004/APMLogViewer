# -*- coding: utf-8 -*-
"""apmlog.link.cli_main —— ``python -m apmlog.cli link ...`` 的子命令实现。

由主 CLI 调用：``from apmlog.link.cli_main import cli_main; rc = cli_main(args)``，
其中 ``args.rest`` 是 ``link`` 之后的原始参数列表。本模块自己再用 argparse 解析，
这样主 CLI 不需要为 link 预置任何参数定义。

子命令：
    targets                         打印目标串语法与本机可用串口
    probe   --target T [...]        连接并打印心跳/统计（在线诊断）
    list    --target T [...]        列出飞控上的日志
    fetch   --target T --log N [...]分块下载一条日志（支持断点续传）
    demo    [--src FILE] [--out D]  无硬件演示：本机 .BIN 充当飞控日志，走 TCP 回环下载并逐字节校验
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path
from typing import Optional

from .download import BLOCK_BYTES, DownloadResult, RemoteLog, default_download_dir, download_log, list_logs
from .endpoint import MavLinkSession, Endpoint, open_transport, parse_target
from .transport import LinkError

DEFAULT_DEMO_SRC = r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000167.BIN"

RC_OK, RC_USAGE, RC_LINK, RC_INCOMPLETE = 0, 2, 3, 4


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _fmt(n: float) -> str:
    v = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if abs(v) < 1024 or u == "GB":
            return f"{v:.1f}{u}" if u != "B" else f"{int(v)}B"
        v /= 1024
    return f"{v:.1f}GB"


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _make_endpoint(ns: argparse.Namespace) -> Endpoint:
    return parse_target(ns.target, default_baud=ns.baud)


def _connect(ep: Endpoint, *, settle: float = 6.0, verbose: bool = True) -> MavLinkSession:
    t = open_transport(ep, wait=20.0)
    sess = MavLinkSession(t, name=ep.describe(), send_our_heartbeat=ep.kind in ("udp", "udpout"))
    sess.start()
    if verbose:
        print(f"链路已打开：{ep.describe()}", flush=True)
    deadline = time.monotonic() + settle
    while sess.fc_sysid is None and time.monotonic() < deadline and sess.connected:
        time.sleep(0.05)
    if sess.fc_sysid is None:
        _eprint("警告：超时内未收到飞控 HEARTBEAT，后续定向请求可能失败")
    else:
        hb = sess.heartbeats.get((sess.fc_sysid, sess.fc_compid or 1))
        if hb is not None and verbose:
            print(f"飞控在线：sysid={hb.sysid} compid={hb.compid} type={hb.type} "
                  f"autopilot={hb.autopilot} base_mode={hb.base_mode} custom_mode={hb.custom_mode} "
                  f"armed={hb.armed}", flush=True)
    return sess


_PROG_STATE = {"pct": -1, "last": 0.0, "tty": None}


def _progress(done: int, total: int, rate: float, _msg: str) -> None:
    """进度输出：TTY 用 \r 原地刷新；**管道/重定向**下改为按阶段行输出。

    （不区分的话，管道里会把几千次刷新写成一行巨长文本 —— 本机实跑已验证。）
    """
    if _PROG_STATE["tty"] is None:
        _PROG_STATE["tty"] = bool(sys.stdout.isatty())
    frac = done / total if total else 0.0
    pct = int(frac * 100)
    if _PROG_STATE["tty"]:
        width = 28
        bar = "#" * int(width * frac)
        bar = (bar + "-" * (width - len(bar)))[:width]
        sys.stdout.write(f"\r[{bar}] {pct:3d}%  {done}/{total}B  {_fmt(rate)}/s")
        sys.stdout.flush()
        return
    now = time.monotonic()
    if pct >= 100:
        if _PROG_STATE["pct"] != 100:
            print(f"进度 100%  {done}/{total}B  {_fmt(rate)}/s", flush=True)
            _PROG_STATE["pct"] = 100
    elif pct >= _PROG_STATE["pct"] + 10 or (now - _PROG_STATE["last"] > 5.0 and pct > _PROG_STATE["pct"]):
        print(f"进度 {pct:3d}%  {done}/{total}B  {_fmt(rate)}/s", flush=True)
        _PROG_STATE["pct"] = pct
        _PROG_STATE["last"] = now


def _progress_reset() -> None:
    _PROG_STATE.update(pct=-1, last=0.0)


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def cmd_targets(_ns: argparse.Namespace) -> int:
    ep = Endpoint(kind="?", target="")
    print(ep.ui_hint)
    print("\n当前解析结果示例：")
    for sample in ("COM5", "serial:COM5?baud=115200", "tcp:192.168.1.10:5760", "192.168.1.10:5760",
                   "tcpsrv:5760", "udp:14550", "udpout:192.168.1.10:14550", "pipe:demo"):
        try:
            e = parse_target(sample)
            print(f"  {sample:34s} -> {e.describe()}")
        except ValueError as exc:
            print(f"  {sample:34s} -> 解析失败: {exc}")
    try:
        import serial.tools.list_ports as lp        # type: ignore
        ports = list(lp.comports())
        print("\n本机串口：")
        if not ports:
            print("  (未发现串口)")
        for p in ports:
            print(f"  {p.device:12s} {p.description}")
    except Exception:
        print("\n本机串口：需要 pyserial（当前 venv 未安装）："
              r".\.venv\Scripts\python.exe -m pip install pyserial")
    return RC_OK


def cmd_probe(ns: argparse.Namespace) -> int:
    sess = _connect(_make_endpoint(ns), settle=ns.settle)
    try:
        time.sleep(max(0.5, ns.settle * 0.3))
        print(sess.summary())
        for key, hb in sorted(sess.heartbeats.items()):
            print(f"  心跳 {key[0]}.{key[1]}: type={hb.type} autopilot={hb.autopilot} "
                  f"base_mode={hb.base_mode} custom_mode={hb.custom_mode} status={hb.system_status} "
                  f"count={hb.count} age={hb.age():.2f}s")
        if sess.statustexts:
            print("  STATUSTEXT（最近）:")
            for line in sess.statustexts[-8:]:
                print(f"    {line}")
        st = sess.stats
        print(f"  统计 rx={st.rx_frames}帧/{st.rx_bytes}B tx={st.tx_frames}帧/{st.tx_bytes}B "
              f"crc_errors={st.crc_errors} dropped_bytes={st.dropped_bytes} unknown={st.unknown_msgs}")
        if st.crc_errors:
            _eprint(f"提示：链路出现 {st.crc_errors} 个 CRC 坏帧（波特率不匹配或干扰的常见症状）")
        return RC_OK
    finally:
        sess.close()


def cmd_list(ns: argparse.Namespace) -> int:
    sess = _connect(_make_endpoint(ns))
    try:
        diag: dict = {}
        logs = list_logs(sess, timeout=ns.timeout, start=ns.start, end=ns.end, diagnostics=diag)
        print(f"\n{'编号':>6}  {'飞控时间':<20} {'字节':>10} {'块数(×90B)':>10}  文件名")
        for e in logs:
            tstr = e.created.astimezone().strftime("%Y-%m-%d %H:%M:%S") if e.created else "RTC未设置"
            print(f"{e.log_id:>6}  {tstr:<20} {e.size:>10} {e.blocks:>10}  {e.name}")
        print(f"\n收到 {len(logs)} 条，期望 {diag.get('expected')}，原因 {diag.get('reason')}，"
              f"用时 {(diag.get('elapsed') or 0):.2f}s")
        return RC_OK if logs else RC_INCOMPLETE
    finally:
        sess.close()


def cmd_fetch(ns: argparse.Namespace) -> int:
    ep = _make_endpoint(ns)
    sess = _connect(ep)
    try:
        logs = list_logs(sess, timeout=ns.timeout)
        entry = next((x for x in logs if x.log_id == ns.log), None)
        if entry is None:
            _eprint(f"飞控上没有日志 {ns.log}（列表：{[x.log_id for x in logs]}）")
            return RC_USAGE
        out = Path(ns.out) if ns.out else default_download_dir()
        _progress_reset()
        res = download_log(sess, entry, out, blocks_per_request=ns.blocks, window=ns.window,
                           resume=not ns.no_resume, send_end=ns.send_end, overwrite=ns.overwrite,
                           progress=None if ns.quiet else _progress)
        if not ns.quiet:
            print()
        print(f"结果：{res.summary()}")
        print(f"路径：{res.path}")
        print(f"细节：帧 {res.blocks_ok} 重复 {res.frames_dup} 外部 {res.frames_foreign} "
              f"重传 {res.retries} CRC {res.crc_errors} 续传起点 {res.resumed_from} 请求 {res.requests_sent}")
        return RC_OK if res.complete else RC_INCOMPLETE
    finally:
        sess.close()


def cmd_demo(ns: argparse.Namespace) -> int:
    """无硬件端到端演示 + 逐字节校验（FakeFC 走本机 TCP 回环）。"""
    from .fakes import FakeFC, FakeLog

    src = Path(ns.src)
    if not src.exists():
        _eprint(f"演示源文件不存在：{src}")
        return RC_USAGE
    data = src.read_bytes()
    log_id = ns.log if ns.log is not None else (int(src.stem) if src.stem.isdigit() else 1)
    fc = FakeFC([FakeLog(log_id, data)], heartbeat_period=0.15)
    host, port = fc.serve_tcp("127.0.0.1", 0)
    print(f"演示飞控已启动：tcp:{host}:{port} 服务 1 条日志 id={log_id} size={len(data)}")
    sess = _connect(parse_target(f"tcp:{host}:{port}"), settle=4.0)
    try:
        logs = list_logs(sess, timeout=6.0)
        for e in logs:
            print("  LOG_ENTRY " + ", ".join(f"{k}={v}" for k, v in sorted(e.raw.items())))
        entry = logs[0]
        t0 = time.monotonic()
        _progress_reset()
        res = download_log(sess, entry, Path(ns.out), blocks_per_request=60, window=4,
                           progress=None if ns.quiet else _progress)
        if not ns.quiet:
            print()
        got = Path(res.path).read_bytes() if Path(res.path).exists() else b""
        same = got == data
        print(f"结果：{res.summary()}  墙钟 {time.monotonic() - t0:.2f}s")
        print(f"飞控侧：{fc.stats}")
        print(f"逐字节一致性：{'一致' if same else '不一致'}  "
              f"源 {len(data)}B sha1={hashlib.sha1(data).hexdigest()[:16]}  "
              f"下载 {len(got)}B sha1={hashlib.sha1(got).hexdigest()[:16] if got else '-'}")
        return RC_OK if (res.complete and same) else RC_INCOMPLETE
    finally:
        sess.close()
        fc.stop()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m apmlog.cli link",
                                description="APMLogViewer 飞控连接与日志下载（只读，不写参数、不执行命令）")
    sub = p.add_subparsers(dest="cmd")

    def common(sp: argparse.ArgumentParser, *, target: bool = True) -> None:
        if target:
            sp.add_argument("--target", "-t", required=True,
                            help="COM5 | serial:COM5?baud=57600 | tcp:host:port | tcpsrv:[bind:]port | "
                                 "udp:[bind:]port | udpout:host:port | pipe:名字")
            sp.add_argument("--baud", "-b", type=int, default=57600)

    sp = sub.add_parser("targets", help="打印目标语法与本机串口")
    sp.set_defaults(func=cmd_targets)

    sp = sub.add_parser("probe", help="连接并诊断心跳/统计")
    common(sp)
    sp.add_argument("--settle", type=float, default=6.0, help="等待飞控心跳的秒数")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("list", help="列出飞控日志")
    common(sp)
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--start", type=int, default=0)
    sp.add_argument("--end", type=int, default=0xFFFF)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("fetch", help="下载一条日志")
    common(sp)
    sp.add_argument("--log", type=int, required=True, help="LOG_ENTRY.id")
    sp.add_argument("--out", "-o", default="", help=f"保存目录，默认 {default_download_dir()}")
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--blocks", type=int, default=120, help=f"每请求块数（每块实测 {BLOCK_BYTES} 字节；真机推荐 120，上限 160）")
    sp.add_argument("--window", type=int, default=1, help="在途请求数（真机实测 >1 反而降吞吐，默认 1）")
    sp.add_argument("--no-resume", action="store_true", help="忽略 .part 断点，从头下载")
    sp.add_argument("--send-end", action="store_true",
                    help="完成后发送 LOG_REQUEST_END（实测 Mission Planner 会话未发送，默认关）")
    sp.add_argument("--overwrite", action="store_true", help="同名同尺寸文件也重新下载")
    sp.add_argument("--quiet", "-q", action="store_true")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("demo", help="无硬件演示：本机 .BIN 当飞控日志走 TCP 回环下载并校验")
    sp.add_argument("--src", default=DEFAULT_DEMO_SRC)
    sp.add_argument("--log", type=int, default=None)
    sp.add_argument("--out", "-o", default=str(default_download_dir() / "demo"))
    sp.add_argument("--quiet", "-q", action="store_true")
    sp.set_defaults(func=cmd_demo)
    return p


def cli_main(args: argparse.Namespace) -> int:
    """主 CLI 入口：``args.rest`` 为 ``link`` 之后的原始参数。"""
    rest = list(getattr(args, "rest", []) or [])
    if not rest or rest[0] in ("-h", "--help", "help"):
        build_parser().print_help()
        return RC_OK if rest else RC_USAGE
    ns = build_parser().parse_args(rest)
    fn = getattr(ns, "func", None)
    if fn is None:
        build_parser().print_help()
        return RC_USAGE
    try:
        return int(fn(ns))
    except LinkError as exc:
        _eprint(f"链路错误：{exc}")
        return RC_LINK
    except KeyboardInterrupt:
        _eprint("已中断")
        return RC_INCOMPLETE
