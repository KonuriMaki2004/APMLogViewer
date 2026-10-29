# -*- coding: utf-8 -*-
"""apmlog.ui.linkpanel —— MAVLink 连接与飞控日志下载面板（H 工作流 UI）。

对外只暴露信号，不反向依赖主窗、不读全局状态：

    logDownloaded(str)        一条日志下载完成，参数为本地文件绝对路径
    connectionChanged(str)    链路状态变化（closed/open/ready/lost/error）
    logsListed(list)          列表刷新完成，参数为 RemoteLog 列表

网络与 MAVLink 收发全部放在 :class:`_LinkWorker`（QThread）里执行，UI 线程只发命令、收信号。
本文件不导入 apmlog.core.store（只允许用 apmlog.core.model 的展示工具）。
可在 ``QT_QPA_PLATFORM=offscreen`` 下构造并抓图。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from apmlog.link import download as dldl
from apmlog.link import params as lparams
from apmlog.ui import theme as _theme
from apmlog.ui.logconfig import LogWriteDialog
from apmlog.link.download import BLOCK_BYTES, DownloadResult, RemoteLog
from apmlog.link.endpoint import MavLinkSession, parse_target
from apmlog.link.transport import (
    LinkError, SerialTransport, TcpClientTransport, TcpServerTransport,
    Transport, UdpOutTransport, UdpTransport,
)
from apmlog.ui.settings import Settings

BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 921600, 1500000)

#: 连接方式下拉（label, 内部代码）。代码与 parse_target 的协议头一致，manual 例外。
MODES: tuple[tuple[str, str], ...] = (
    ("串口（COM / USB / 数传）", "serial"),
    ("TCP 客户端（连对端数传电台）", "tcp"),
    ("TCP 服务端（等对方连进来）", "tcpsrv"),
    ("UDP 监听（电台广播）", "udp"),
    ("UDP 发送（固定目标）", "udpout"),
    ("手动输入目标（高级语法）", "manual"),
)


def _port_sort_key(dev: str) -> tuple[int, Any]:
    """COM1/COM2/COM10 按数字排，不按字典序。"""
    m = re.match(r"^[A-Za-z]+(\d+)$", dev or "")
    return (0, int(m.group(1))) if m else (1, (dev or "").lower())


#: 常见 USB转串 VID（飞控本体、DAPLink、CH340/CP210x/FTDI 这类）
_FC_VIDS = {"1209": "ArduPilot/PX4 常用 VID", "2da5": "PX4", "0483": "ST 虚拟串口",
            "1fc9": "NXP/DAPLink", "2e88": "Espressif", "10c4": "CP210x", "0403": "FTDI",
            "1a86": "CH340", "067b": "PL2303", "3171": "8BitDo/数传常见"}
#: 几乎不可能是飞控的虚拟口关键字
_VIRTUAL_HINTS = ("bthenum", "蓝牙", "bluetooth", "active management", "wan miniport",
                  "hyper-v", "标准串行", "null", "modem device")


def port_kind(p: Any) -> tuple[int, str]:
    """给一个串口分档：0=像飞控/硬件 UART，1=普通口，2=虚拟口（排到后面并标注）。"""
    text = f"{getattr(p, 'description', '') or ''} {getattr(p, 'hwid', '') or ''}".lower()
    vid = getattr(p, "vid", None)
    if vid and f"{int(vid):04x}" in _FC_VIDS:
        return (0, _FC_VIDS[f"{int(vid):04x}"])
    if any(k in text for k in ("ardupilot", "pixhawk", "px4", "mavlink", "telem", "数传")):
        return (0, "描述里带飞控/数传标识")
    if any(k in text for k in _VIRTUAL_HINTS):
        return (2, "虚拟/蓝牙口，一般不是飞控")
    return (1, "")


def _short_desc(text: str, device: str) -> str:
    """把 'ArduPilot (COM91)' 里的重复口号去掉，并限长，不把整串 hwid 塞进下拉框。"""
    d = re.sub(rf"\(\s*{re.escape(device)}\s*\)", "", str(text or ""), flags=re.I)
    d = re.sub(r"\s+", " ", d).strip()
    return d[:48] + "…" if len(d) > 48 else d


def serial_ports() -> list[tuple[str, str]]:
    """枚举本机串口 [(设备名, 友好标签)]，像飞控的排前面；异常时返回空表不抛错。"""
    try:
        from serial.tools import list_ports
    except Exception:                                   # noqa: BLE001
        log.debug("pyserial 不可用，串口列表为空")
        return []
    out: list[tuple[str, str]] = []
    try:
        ports = list(list_ports.comports())
    except Exception:                                   # noqa: BLE001
        log.debug("枚举串口失败", exc_info=True)
        return []
    rows: list[tuple[int, Any, str, str]] = []
    for p in ports:
        rank, why = port_kind(p)
        rows.append((rank, _port_sort_key(p.device), p.device, ""))
        vid = f"VID {p.vid:04X} / PID {p.pid:04X}" if p.vid and p.pid else ""
        bits = [p.device, _short_desc(p.description, p.device)]
        tail = "　".join(x for x in (vid, why) if x)
        label = "　".join(x for x in bits if x)
        if tail:
            label += f"　[{tail}]"
        out.append((p.device, label))
    order = {dev: (rank, key) for rank, key, dev, _ in rows}
    out.sort(key=lambda item: (order[item[0]][0], order[item[0]][1]))
    return out

log = logging.getLogger("apmlog.ui.linkpanel")

STATE_TEXT = {
    "closed": ("未连接", "#8a8f98"),
    "open": ("已连接，等待飞控心跳", "#c8a24a"),
    "ready": ("飞控在线", "#3aa659"),
    "lost": ("链路中断", "#d1603f"),
    "error": ("链路错误", "#d1603f"),
}


class _LinkWorker(QtCore.QThread):
    """在子线程里持有 Transport/MavLinkSession，UI 只投命令。"""

    state_changed = QtCore.Signal(str, str)
    logs_ready = QtCore.Signal(list)
    progress = QtCore.Signal(int, int, float, str)
    download_done = QtCore.Signal(object)
    note = QtCore.Signal(str)
    #: 日志写入设置检测结果（LogWriteConfig 或 None）。本面板对飞控只读，
    #: 检测结果只用于提示与给出手动处理方法，不存在任何写回通道。
    logconfig_ready = QtCore.Signal(object)

    def __init__(self, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._cmds: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._cancel = threading.Event()
        self.sess: Optional[MavLinkSession] = None
        self.fc = None                                  # FakeFC（自测用）
        self._out_dir = str(dldl.default_download_dir())
        self._blocks = 120
        self._known: list[RemoteLog] = []
        self._logcfg = None                             # 最近一次日志写入检测结果
        self._stop = False

    # -- 命令入口（UI 线程调用，线程安全）
    def post(self, cmd: str, **kw: Any) -> None:
        self._cmds.put((cmd, kw))

    def request_cancel(self) -> None:
        self._cancel.set()

    def set_options(self, *, out_dir: str, blocks_per_request: int) -> None:
        self._out_dir = out_dir
        self._blocks = max(1, int(blocks_per_request))

    def quit(self) -> None:
        self._stop = True
        self._cancel.set()
        self._cmds.put(("quit", {}))

    # -- 线程主体
    def run(self) -> None:                                          # noqa: PLR0912
        """命令循环。

        整层全部包死：任何异常（包括 emit 信号时接收方已销毁）都**不得逃出线程**，
        否则会产生 pytest ``PytestUnhandledThreadExceptionWarning``，并把 traceback
        归给当时正在跑的无关用例。
        """
        try:
            while not self._stop:
                try:
                    cmd, kw = self._cmds.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    if cmd == "connect":
                        self._do_connect(**kw)
                    elif cmd == "disconnect":
                        self._do_disconnect()
                    elif cmd == "list":
                        self._do_list()
                    elif cmd == "download":
                        self._do_download(int(kw["log_id"]))
                    elif cmd == "probe":
                        self._do_probe()
                    elif cmd == "fake":
                        self._do_fake(**kw)
                    elif cmd == "quit":
                        break
                except Exception as exc:                            # 线程里绝不把异常抛给 UI
                    log.debug("链路命令 %s 失败", cmd, exc_info=True)
                    self._emit_safe(self.note, f"错误：{type(exc).__name__}: {exc}")
                    self._emit_safe(self.state_changed, "error", str(exc))
        except Exception:                                           # pragma: no cover
            log.exception("链路 worker 线程异常退出")
        finally:
            try:
                self._do_disconnect(quiet=True)
            except Exception:                                       # pragma: no cover
                log.exception("worker 收尾断开失败")

    @staticmethod
    def _emit_safe(sig, *args: Any) -> None:
        """发信号但不因接收方已销毁而抛异常。"""
        try:
            sig.emit(*args)
        except (RuntimeError, TypeError):                           # pragma: no cover
            log.debug("信号接收方已销毁，丢弃一条消息：%s", args[:1])

    def _do_connect(self, *, target: str, baud: int, wait: float = 15.0,
                    send_our_heartbeat: Optional[bool] = None) -> None:
        ep = parse_target(target, default_baud=baud)
        t: Transport
        if ep.kind == "serial":
            t = SerialTransport(ep.device, ep.baud)
        elif ep.kind == "tcp":
            t = TcpClientTransport(ep.host, ep.port)
        elif ep.kind == "tcpsrv":
            t = TcpServerTransport(ep.bind, ep.port, wait=wait)
        elif ep.kind == "udp":
            t = UdpTransport(ep.bind, ep.port)
        elif ep.kind == "udpout":
            t = UdpOutTransport(ep.host, ep.port)
        else:
            raise LinkError(f"界面不支持的传输类型：{ep.kind}")
        hb = (ep.kind in ("udp", "udpout")) if send_our_heartbeat is None else send_our_heartbeat
        self.sess = MavLinkSession(t, name=ep.describe(), send_our_heartbeat=hb)
        self.sess.on_state(lambda st, detail: self.state_changed.emit(st, detail))
        self.sess.on_message(lambda m: self._relay_message(m))
        self.sess.start()
        self.note.emit(f"已打开链路：{ep.describe()}")
        # 真机心跳约 1Hz：开完链路就等一会儿，让 UI 能立刻区分“口开了但对端不说话”。
        hb_wait = min(max(wait, 1.0), 10.0)
        if not self._wait_for_fc(hb_wait):
            self.state_changed.emit("error", f"链路已打开但 {hb_wait:.0f}s 内无飞控心跳")
            return
        # 连上了就顺手查一下“这台飞控到底会不会写日志”：没开启时列表必定是空的，
        # 让用户对着空表猜不如直接告诉他。探测只读，不会写任何参数。
        self._do_probe()

    def _do_probe(self) -> None:
        """只读检测飞控的日志写入参数（只发 PARAM_REQUEST_READ，绝不写参数）"""
        if self.sess is None:
            self._emit_safe(self.note, "请先连接飞控，再检测日志写入设置")
            self._emit_safe(self.logconfig_ready, None)
            return
        if not self._wait_for_fc(6.0):
            self._emit_safe(self.note, "无飞控心跳，已跳过日志写入检测")
            return
        try:
            cfg = lparams.probe_log_write_config(self.sess)
        except LinkError as exc:
            self._emit_safe(self.note, f"日志写入检测未发起：{exc}")
            return
        except Exception as exc:                            # noqa: BLE001
            log.debug("日志写入检测失败", exc_info=True)
            self._emit_safe(self.note, f"日志写入检测失败：{type(exc).__name__}: {exc}")
            return
        self._logcfg = cfg
        self._emit_safe(self.note, f"日志写入检测：{cfg.state}")
        self._emit_safe(self.logconfig_ready, cfg)

    def _relay_message(self, m: Any) -> None:
        if m.get_type() == "STATUSTEXT":
            try:
                txt = m.text.decode("utf-8", "replace").strip("\x00 ")
            except Exception:
                txt = str(m.text)
            self.note.emit(f"飞控：{txt}")

    def _do_disconnect(self, quiet: bool = False) -> None:
        if self.fc is not None:
            try:
                self.fc.stop()
            except Exception:
                pass
            self.fc = None
        if self.sess is not None:
            try:
                self.sess.close()
            except Exception:
                pass
            self.sess = None
        if not quiet:
            self.note.emit("链路已断开")

    def _wait_for_fc(self, secs: float = 8.0) -> bool:
        """等飞控心跳出现。

        真机实测：USB 串口上飞控约 1Hz 广播心跳，而 ``_do_connect`` 开完链路就返回，
        因此“连接完立刻点刷新”时 ``fc_sysid`` 仍为 None，LOG_* 请求无法定向。
        """
        if self.sess is None:
            return False
        if self.sess.fc_sysid is not None:
            return True
        self.note.emit("等待飞控心跳…")
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs and not self._stop:
            if self.sess.fc_sysid is not None:
                self.note.emit(f"飞控心跳 sysid={self.sess.fc_sysid} compid={self.sess.fc_compid}")
                return True
            time.sleep(0.05)
        return self.sess.fc_sysid is not None

    def _do_list(self) -> None:
        if self.sess is None:
            self.note.emit("请先连接飞控")
            self.logs_ready.emit([])
            return
        if not self._wait_for_fc():
            self.note.emit("等待飞控心跳超时（8s），未发起 LOG_REQUEST_LIST；可检查接线/波特率后重试")
            self.logs_ready.emit([])
            return
        diag: dict = {}
        t0 = time.monotonic()
        logs = dldl.list_logs(self.sess, timeout=10.0, diagnostics=diag)
        self.note.emit(f"列表：收到 {len(logs)} 条（期望 {diag.get('expected')}），"
                       f"{time.monotonic() - t0:.2f}s，{diag.get('reason')}")
        self.logs_ready.emit(logs)

    def _do_download(self, log_id: int) -> None:
        if self.sess is None:
            self.note.emit("请先连接飞控")
            return
        entry = next((x for x in self._known if x.log_id == log_id), None)
        if entry is None:
            self.note.emit(f"日志 {log_id} 不在当前列表里，请先刷新")
            self.download_done.emit(DownloadResult(log_id=log_id, path=None, expected_bytes=0,
                                                  written_bytes=0, complete=False, elapsed=0.0,
                                                  reason="日志不在列表"))
            return
        if self.sess is None:
            self.note.emit("请先连接飞控")
            self.download_done.emit(DownloadResult(log_id=log_id, path=None,
                                                  expected_bytes=entry.size, written_bytes=0,
                                                  complete=False, elapsed=0.0, reason="未连接"))
            return
        if not self._wait_for_fc():
            self.note.emit("等待飞控心跳超时（8s），未发起下载")
            self.download_done.emit(DownloadResult(log_id=log_id, path=None,
                                                  expected_bytes=entry.size, written_bytes=0,
                                                  complete=False, elapsed=0.0, reason="无飞控心跳"))
            return
        self._cancel.clear()
        Path(self._out_dir).mkdir(parents=True, exist_ok=True)
        self.note.emit(f"开始下载 log {log_id}（{entry.size} 字节，每请求 {self._blocks} 块）")
        res: DownloadResult = dldl.download_log(
            self.sess, entry, self._out_dir, blocks_per_request=self._blocks,
            window=1, cancel=self._cancel.is_set,
            progress=lambda d, t, r, m: self.progress.emit(int(d), int(t), float(r), m))
        self.download_done.emit(res)
        if res.complete:
            self.note.emit(res.summary())
        else:
            self.note.emit(f"未完成：{res.reason}（断点已保留，可再次点击继续）")

    def set_known(self, logs: Iterable[RemoteLog]) -> None:
        self._known = list(logs)

    def _do_fake(self, *, src: str, log_id: int = 167) -> None:
        """自测/离线演示：用本机真实 .BIN 起一个 FakeFC 并通过 TCP 回环连上它。"""
        from apmlog.link.fakes import FakeFC, FakeLog

        p = Path(src)
        if not p.exists():
            self.note.emit(f"演示文件不存在：{p}")
            return
        self.fc = FakeFC([FakeLog.from_file(p, log_id)], heartbeat_period=0.15)
        host, port = self.fc.serve_tcp("127.0.0.1", 0)
        self._do_connect(target=f"tcp:{host}:{port}", baud=57600)
        self.note.emit(f"演示飞控（FakeFC）已启动：{host}:{port} ← {p.name}")


class LinkPanel(QtWidgets.QWidget):
    """连接 / 列表 / 下载 三合一面板。"""

    logDownloaded = QtCore.Signal(str)
    batchFinished = QtCore.Signal(list, list)         # (成功的本地路径, 失败/中断的 log_id)
    connectionChanged = QtCore.Signal(str)
    logsListed = QtCore.Signal(list)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None, *,
                 default_target: str = "", out_dir: str = "") -> None:
        super().__init__(parent)
        self.setObjectName("LinkPanel")
        self._out_dir = str(out_dir or dldl.default_download_dir())
        self._busy = False
        self._busy_op = ""
        self._link_up = False
        self._shutdown_done = False
        self._row_state: dict[int, str] = {}
        # 批量下载状态：勾选集合按 log_id 记，刷新列表不丢
        self._checked: set[int] = set()
        self._ids: list[int] = []
        self._sizes: dict[int, int] = {}
        self._batch: list[int] = []
        self._queue: list[int] = []
        self._current: Optional[int] = None
        self._batch_ok: list[str] = []
        self._batch_bad: list[int] = []
        self._logcfg_dialog: Optional[LogWriteDialog] = None   # 当前开着的确认弹窗
        self._logcfg_state: str = ""
        self._item_changing = False

        self.worker = _LinkWorker(self)
        self.worker.state_changed.connect(self._on_state)
        self.worker.logs_ready.connect(self._on_logs)
        self.worker.progress.connect(self._on_progress)
        self.worker.download_done.connect(self._on_done)
        self.worker.note.connect(self._on_note)
        self.worker.logconfig_ready.connect(self._on_logconfig)
        self.worker.start()

        self._build(default_target)
        self._set_buttons()

    # ------------------------------------------------------------------ 布局
    def _build(self, default_target: str) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # --- 连接区：端口/地址靠选，不让人手抄目标串
        conn = QtWidgets.QGroupBox("链路")
        cl = QtWidgets.QGridLayout(conn)
        self.cb_mode = QtWidgets.QComboBox()
        for label, code in MODES:
            self.cb_mode.addItem(label, code)
        self.cb_mode.setToolTip("选一种链路方式，下面只问该方式需要的参数")
        cl.addWidget(QtWidgets.QLabel("连接方式"), 0, 0)
        cl.addWidget(self.cb_mode, 0, 1, 1, 3)

        self.stack = QtWidgets.QStackedWidget()
        pg = QtWidgets.QWidget()
        sf = QtWidgets.QGridLayout(pg)
        sf.setContentsMargins(0, 4, 0, 0)
        self.cb_port = QtWidgets.QComboBox()
        self.cb_port.setMinimumWidth(360)
        self.btn_ports = QtWidgets.QPushButton("刷新端口")
        self.btn_ports.setToolTip("重新枚举本机串口（插拔后点一下）")
        self.lbl_port_info = QtWidgets.QLabel("")
        self.cb_baud = QtWidgets.QComboBox()
        self.cb_baud.setEditable(True)          # 1500000 这类不常见速率也能填
        self.cb_baud.addItems(str(b) for b in BAUDS)
        self.cb_baud.setCurrentText("57600")
        sf.addWidget(QtWidgets.QLabel("端口"), 0, 0)
        sf.addWidget(self.cb_port, 0, 1)
        sf.addWidget(self.btn_ports, 0, 2)
        sf.addWidget(QtWidgets.QLabel("波特率"), 1, 0)
        sf.addWidget(self.cb_baud, 1, 1)
        sf.addWidget(self.lbl_port_info, 2, 0, 1, 3)
        sf.setColumnStretch(1, 1)
        self.page_serial = pg
        self.stack.addWidget(pg)

        self.page_tcp, self.ed_tcp_host, self.sp_tcp_port = self._net_page(
            "主机 / 电台地址", "192.168.2.1", 5760, "本端主动连对端的 MAVLink TCP 端口（数传电台常见）")
        self.stack.addWidget(self.page_tcp)
        self.page_tcpsrv, self.ed_bind, self.sp_srv_port = self._net_page(
            "监听地址", "0.0.0.0", 5760, "等对方（电台/另一台电脑）连进来；监听地址一般留 0.0.0.0")
        self.stack.addWidget(self.page_tcpsrv)
        self.page_udp, self.ed_udp_bind, self.sp_udp_port = self._net_page(
            "监听地址", "0.0.0.0", 14550, "监听 UDP 端口，收电台广播的遥测流")
        self.stack.addWidget(self.page_udp)
        self.page_udpout, self.ed_udpout_host, self.sp_udpout_port = self._net_page(
            "目标主机", "192.168.2.1", 14550, "固定往这个 host:port 发 UDP")
        self.stack.addWidget(self.page_udpout)

        pgm = QtWidgets.QWidget()
        mf = QtWidgets.QGridLayout(pgm)
        mf.setContentsMargins(0, 4, 0, 0)
        self.ed_target = QtWidgets.QLineEdit(default_target)
        self.ed_target.setPlaceholderText("COM5 | serial:COM5?baud=57600 | tcp:192.168.2.1:5760 | "
                                          "tcpsrv:5760 | udp:14550 | udpout:192.168.2.1:14550 | pipe")
        self.ed_target.setToolTip("高级用法：直接写完整目标串（pipe、带 ?baud= 的写法等）")
        mf.addWidget(self.ed_target, 0, 0)
        self.page_manual = pgm
        self.stack.addWidget(pgm)
        cl.addWidget(self.stack, 1, 0, 1, 4)

        self.lbl_target = QtWidgets.QLabel("将连接：—")
        self.lbl_target.setObjectName("linkTargetPreview")
        self.lbl_target.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_connect = QtWidgets.QPushButton("连接")
        self.btn_disconnect = QtWidgets.QPushButton("断开")
        self.lbl_state = QtWidgets.QLabel("未连接")
        self.lbl_state.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.lbl_detail = QtWidgets.QLabel("rx 0 帧 · CRC 0")
        self.lbl_detail.setObjectName("linkDetail")
        cl.addWidget(self.lbl_target, 2, 0, 1, 2)
        cl.addWidget(self.btn_connect, 2, 2)
        cl.addWidget(self.btn_disconnect, 2, 3)
        cl.addWidget(self.lbl_state, 3, 0, 1, 2)
        cl.addWidget(self.lbl_detail, 3, 2, 1, 2)
        cl.setColumnStretch(1, 3)
        cl.setColumnStretch(2, 1)
        cl.setColumnStretch(3, 1)
        root.addWidget(conn)

        # --- 日志列表
        tbl_box = QtWidgets.QGroupBox("飞控上的日志")
        tl = QtWidgets.QVBoxLayout(tbl_box)
        bar = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("刷新列表")
        self.btn_logcfg = QtWidgets.QPushButton("检测日志写入")
        self.btn_logcfg.setToolTip("只读查询 LOG_BACKEND_TYPE / LOG_BITMASK；\n"
                                   "若飞控没开日志写入会弹窗给出手动开启方法（本工具不写参数）")
        self.btn_demo = QtWidgets.QPushButton("用本机日志演示")
        self.btn_demo.setToolTip("启动 FakeFC（假飞控）并用 TCP 回环连接，用于无硬件自检")
        self.lbl_count = QtWidgets.QLabel("0 条")
        self.chk_all = QtWidgets.QCheckBox("全选")
        self.chk_all.setTristate(True)
        self.chk_all.setToolTip("勾选/取消整表；部分勾选时显示为半选")
        self.lbl_checked = QtWidgets.QLabel("已勾选 0 条")
        self.lbl_checked.setStyleSheet(f"color:{_theme.readable_on(self.lbl_checked, '#8a8f98')};")
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_logcfg)
        bar.addWidget(self.btn_demo)
        bar.addWidget(self.chk_all)
        bar.addWidget(self.lbl_checked)
        bar.addStretch(1)
        bar.addWidget(self.lbl_count)
        tl.addLayout(bar)

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["编号", "飞控时间", "大小", "块数", "状态"])
        self.table.setColumnWidth(0, 96)
        hdr0 = self.table.horizontalHeaderItem(0)
        if hdr0 is not None:
            hdr0.setToolTip("勾选＝加入下载队列（可多选，按列表顺序一条接一条下）")
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 80)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._set_buttons)
        self.table.itemChanged.connect(self._on_item_changed)
        tl.addWidget(self.table, 3)

        # --- 下载控制
        dl = QtWidgets.QHBoxLayout()
        dl.addWidget(QtWidgets.QLabel("保存目录"))
        self.ed_dir = QtWidgets.QLineEdit(self._out_dir)
        dl.addWidget(self.ed_dir, 4)
        self.btn_browse = QtWidgets.QPushButton("浏览…")
        dl.addWidget(self.btn_browse)
        dl.addWidget(QtWidgets.QLabel("每请求块数"))
        self.sp_blocks = QtWidgets.QSpinBox()
        self.sp_blocks.setRange(1, 160)
        self.sp_blocks.setValue(int(Settings.instance().get("dl_blocks")))
        self.sp_blocks.setSuffix(" ×90B")
        self.sp_blocks.setToolTip(
            f"每次 LOG_REQUEST_DATA 请求的块数，每块实测 {BLOCK_BYTES} 字节。\n"
            "真机（Copter 4.6.3 / USB CDC）扫参：120 块 = 58 KB/s 且零重传零 CRC；\n"
            "20 块只有 14 KB/s；≥200 块出现重复帧与 CRC 错，因此上限限到 160。")
        dl.addWidget(self.sp_blocks)
        self.btn_download = QtWidgets.QPushButton("下载选中日志")
        self.btn_download.setToolTip("先勾选多条再点：会按列表顺序依次下载，中途断开保留断点")
        dl.addWidget(self.btn_download)
        tl.addLayout(dl)

        self.pb = QtWidgets.QProgressBar()
        self.pb.setRange(0, 100)
        self.pb.setValue(0)
        self.pb.setFormat("空闲")
        tl.addWidget(self.pb)
        self.lbl_rate = QtWidgets.QLabel("")
        self.lbl_rate.setObjectName("linkRate")
        tl.addWidget(self.lbl_rate)
        root.addWidget(tbl_box, 5)

        # --- 事件/STATUSTEXT 输出
        log_box = QtWidgets.QGroupBox("链路输出")
        ll = QtWidgets.QVBoxLayout(log_box)
        self.plain = QtWidgets.QPlainTextEdit()
        self.plain.setReadOnly(True)
        self.plain.setMaximumHeight(120)
        self.plain.setPlaceholderText("HEARTBEAT / STATUSTEXT / 下载进度会出现在这里")
        ll.addWidget(self.plain)
        btns = QtWidgets.QHBoxLayout()
        self.btn_cancel = QtWidgets.QPushButton("取消下载")
        self.btn_diag = QtWidgets.QPushButton("打印诊断")
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_diag)
        btns.addStretch(1)
        ll.addLayout(btns)
        root.addWidget(log_box, 2)

        # --- 信号连接
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        self.btn_refresh.clicked.connect(lambda: self.worker.post("list"))
        self.btn_logcfg.clicked.connect(lambda: self.worker.post("probe"))
        self.btn_download.clicked.connect(self._on_download)
        self.chk_all.stateChanged.connect(self._on_select_all)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_browse.clicked.connect(self._on_browse)
        self.btn_diag.clicked.connect(self._on_diag)
        self.btn_demo.clicked.connect(self._on_demo)
        self.ed_dir.editingFinished.connect(self._sync_options)
        self.sp_blocks.valueChanged.connect(self._sync_options)
        # 块数是“工具偏好”而不是会话参数，存进设置，下次开窗就是它
        self.sp_blocks.valueChanged.connect(lambda v: Settings.instance().set("dl_blocks", int(v)))
        # 链路方式联动：换方式只换表单，目标串始细由控件拼出来
        self.cb_mode.currentIndexChanged.connect(self._mode_changed)
        self.btn_ports.clicked.connect(self._refresh_ports)
        self.cb_port.currentIndexChanged.connect(self._update_preview)
        self.cb_baud.currentTextChanged.connect(self._update_preview)
        self.ed_target.textChanged.connect(self._update_preview)
        for w in (self.ed_tcp_host, self.ed_bind, self.ed_udp_bind, self.ed_udpout_host):
            w.textChanged.connect(self._update_preview)
        for w in (self.sp_tcp_port, self.sp_srv_port, self.sp_udp_port, self.sp_udpout_port):
            w.valueChanged.connect(self._update_preview)
        self._refresh_ports()
        self._apply_default_target(default_target)
        self._sync_options()

    # ------------------------------------------------------ 链路方式与目标拼装
    def _net_page(self, first_label: str, first_default: str, port_default: int, tip: str
                  ) -> tuple[QtWidgets.QWidget, QtWidgets.QLineEdit, QtWidgets.QSpinBox]:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setContentsMargins(0, 4, 0, 0)
        ed = QtWidgets.QLineEdit(first_default)
        ed.setMinimumWidth(240)
        sp = QtWidgets.QSpinBox()
        sp.setRange(1, 65535)
        sp.setValue(int(port_default))
        form.addRow(first_label, ed)
        form.addRow("端口", sp)
        page.setToolTip(tip)
        return page, ed, sp

    def _mode(self) -> str:
        return str(self.cb_mode.currentData() or "serial")

    def _set_mode(self, code: str) -> None:
        i = self.cb_mode.findData(code)
        if i >= 0:
            self.cb_mode.setCurrentIndex(i)

    def _mode_changed(self, *_a: Any) -> None:
        self.stack.setCurrentIndex(max(0, self.cb_mode.currentIndex()))
        self._update_preview()

    def _refresh_ports(self) -> None:
        """重新枚举串口，尽量保留原选择。"""
        self._ports_ts = time.time()
        want = self.cb_port.currentData()
        self.cb_port.blockSignals(True)
        self.cb_port.clear()
        ports = serial_ports()
        for dev, label in ports:
            self.cb_port.addItem(label, dev)
        self.cb_port.blockSignals(False)
        if ports:
            i = self.cb_port.findData(want) if want else -1
            self.cb_port.setCurrentIndex(i if i >= 0 else 0)
            self.lbl_port_info.setText(f"检测到 {len(ports)} 个串口；插拔后点“刷新端口”重列")
        else:
            self.lbl_port_info.setText("没检测到串口：确认驱动已装、线已插，或改用 TCP/UDP/手动输入")
        self._update_preview()

    def _baud(self) -> int:
        raw = str(self.cb_baud.currentText()).strip()
        try:
            b = int(raw)
        except ValueError:
            raise ValueError(f"波特率不是数字：{raw!r}") from None
        if not 1200 <= b <= 5_000_000:
            raise ValueError(f"波特率超出合理范围：{b}")
        return b

    def _compose_target(self) -> str:
        """把当前方式的选择拼成 parse_target 能吃的目标串。"""
        m = self._mode()
        if m == "serial":
            dev = self.cb_port.currentData() or self.cb_port.currentText().strip()
            if not dev:
                raise ValueError("还没有可选串口（先插线并点“刷新端口”，或改用 TCP/UDP）")
            return f"serial:{dev}?baud={self._baud()}"
        if m == "tcp":
            return f"tcp:{self.ed_tcp_host.text().strip() or '127.0.0.1'}:{self.sp_tcp_port.value()}"
        if m == "tcpsrv":
            return f"tcpsrv:{self.ed_bind.text().strip() or '0.0.0.0'}:{self.sp_srv_port.value()}"
        if m == "udp":
            return f"udp:{self.ed_udp_bind.text().strip() or '0.0.0.0'}:{self.sp_udp_port.value()}"
        if m == "udpout":
            return (f"udpout:{self.ed_udpout_host.text().strip() or '127.0.0.1'}"
                    f":{self.sp_udpout_port.value()}")
        return self.ed_target.text().strip()

    def _update_preview(self) -> None:
        try:
            text = self._compose_target()
        except ValueError as exc:
            self.lbl_target.setText(f"将连接：{exc}")
            self.lbl_target.setStyleSheet(f"color:{_theme.readable_on(self.lbl_target, '#d1603f')};")
            return
        self.lbl_target.setText(f"将连接：{text or '（空）'}")
        self.lbl_target.setStyleSheet(f"color:{_theme.readable_on(self.lbl_target, '#8a8f98')};")

    def set_target(self, text: str) -> None:
        """外部直接指定完整目标串（自动切到“手动输入”方式）。"""
        self._set_mode("manual")
        self.ed_target.setText(text or "")
        self._update_preview()

    def _apply_default_target(self, text: str) -> None:
        """把目标串回填到对应方式的控件上；认不出来就退回手动输入。"""
        raw = (text or "").strip() or self._load_link_prefs()
        if not raw:
            self._set_mode("serial")
            self._update_preview()
            return
        try:
            ep = parse_target(raw)
        except ValueError:
            self.set_target(raw)
            return
        kind = str(ep.kind)
        if kind == "serial":
            self._set_mode("serial")
            self.cb_baud.setCurrentText(str(ep.baud))
            i = self.cb_port.findData(ep.device)
            if i >= 0:
                self.cb_port.setCurrentIndex(i)
            else:                       # 上次用的口现在没插：保留名字并说明，别静悄悄丢选择
                self.cb_port.addItem(f"{ep.device}　（当前未检测到，插上后点“刷新端口”）", ep.device)
                self.cb_port.setCurrentIndex(self.cb_port.count() - 1)
        elif kind in ("tcp", "tcpsrv", "udp", "udpout"):
            self._set_mode(kind)
            ed, sp = {"tcp": (self.ed_tcp_host, self.sp_tcp_port),
                      "tcpsrv": (self.ed_bind, self.sp_srv_port),
                      "udp": (self.ed_udp_bind, self.sp_udp_port),
                      "udpout": (self.ed_udpout_host, self.sp_udpout_port)}[kind]
            addr = ep.host if kind in ("tcp", "udpout") else ep.bind
            if addr:
                ed.setText(addr)
            if ep.port:
                sp.setValue(int(ep.port))
        else:
            self.set_target(raw)
        self._update_preview()

    # ------------------------------------------------------------ 上次选择记忆
    def _link_prefs_path(self) -> Path:
        try:
            return Path(dldl.default_download_dir()).parent / "link.json"
        except Exception:                                # noqa: BLE001
            return Path.home() / ".apmlogviewer" / "link.json"

    def _save_link_prefs(self) -> None:
        try:
            p = self._link_prefs_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"mode": self._mode(), "target": self._compose_target()},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:                          # noqa: BLE001
            log.debug("链路偏好保存失败: %s", exc)

    def _load_link_prefs(self) -> str:
        try:
            data = json.loads(self._link_prefs_path().read_text(encoding="utf-8"))
        except Exception:                                # noqa: BLE001
            return ""
        return str(data.get("target") or "") if isinstance(data, dict) else ""

    def showEvent(self, ev: QtGui.QShowEvent) -> None:  # noqa: N802
        """面板打开时自动重枚举一次串口（3s 内不重复）。"""
        super().showEvent(ev)
        if time.time() - float(getattr(self, "_ports_ts", 0.0)) > 3.0:
            self._refresh_ports()

    # ------------------------------------------------------------------ 槽
    def _on_connect(self) -> None:
        try:
            text = self._compose_target()
            baud = self._baud() if self._mode() == "serial" else 57600
        except ValueError as exc:
            self._append(f"目标无法组装：{exc}")
            return
        if not text:
            self._append("目标为空：请选端口/填地址，或在“手动输入目标”里写完整语法")
            return
        try:
            parse_target(text, default_baud=baud)
        except ValueError as exc:
            self._append(f"目标语法错误：{exc}")
            return
        self._sync_options()
        self._save_link_prefs()
        self.worker.post("connect", target=text, baud=baud)
        self._set_busy(True, "正在建立链路…", op="connect")

    def _on_disconnect(self) -> None:
        self.worker.request_cancel()
        self.worker.post("disconnect")
        self._set_busy(False)

    def _on_download(self) -> None:
        ids = self._checked_ids()
        if not ids:
            lid = self.selected_log_id()
            if lid is None:
                self._append("请先勾选要下载的日志（可在“编号”列打勾，也可选一行只下一）")
                return
            ids = [lid]
        self._sync_options()
        self._batch = list(ids)
        self._queue = list(ids[1:])
        self._batch_ok = []
        self._batch_bad = []
        for lid in self._queue:
            self._set_row_state(lid, "排队中")
        self._start_one(ids[0])
        self.pb.setValue(0)
        why = f"下载 log {ids[0]}（1/{len(ids)}）" if len(ids) > 1 else f"下载 log {ids[0]} …"
        self._set_busy(True, why, op="download")
        if len(ids) > 1:
            size = sum(self._sizes.get(l, 0) for l in ids)
            self._append(f"已排队 {len(ids)} 条，共 {self._fmt(size)}，按真机实测 58 KB/s "
                         f"约 {size / (58.4 * 1024) / 60.0:.0f} 分钟；按列表顺序一条接一条")

    def _on_cancel(self) -> None:
        left = len(self._queue)
        self.worker.request_cancel()
        self._queue = []
        if left:
            self._append(f"已请求取消（当前块收完后停止，断点保留），并清空排队中的 {left} 条")
        else:
            self._append("已请求取消（当前块收完后停止，断点保留）")

    def _on_browse(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择保存目录", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)
            self._sync_options()

    def _on_diag(self) -> None:
        sess = self.worker.sess
        if sess is None:
            self._append("当前没有链路")
            return
        self._append(sess.summary())
        for key, hb in sess.heartbeats.items():
            self._append(f"  心跳 {key[0]}.{key[1]}: type={hb.type} autopilot={hb.autopilot} "
                         f"base_mode={hb.base_mode} custom_mode={hb.custom_mode} "
                         f"armed={hb.armed} n={hb.count}")
        st = sess.stats
        self._append(f"  统计 rx={st.rx_frames}帧/{st.rx_bytes}B tx={st.tx_frames}帧/{st.tx_bytes}B "
                     f"crc_err={st.crc_errors} dropped={st.dropped_bytes}B unknown={st.unknown_msgs}")

    def _on_demo(self) -> None:
        cand = Path(r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000167.BIN")
        self.worker.post("fake", src=str(cand), log_id=167)
        self._set_busy(True, "启动演示飞控…", op="demo")

    # ------------------------------------------------------------------ worker 信号
    def _on_logconfig(self, cfg) -> None:
        """检测结果回调。只有明确判定“未开启”才弹窗，unknown 只说一句话。"""
        if cfg is None:
            return
        state = str(getattr(cfg, "state", "") or "")
        self._logcfg_state = state
        if state == "disabled":
            self._show_logconfig_dialog(cfg)
        elif state == "unknown":
            self._append(f"无法判定日志写入设置：{getattr(cfg, 'detail', '')}")
        else:
            self._append(f"日志写入已开启：{getattr(cfg, 'detail', '')}")

    def build_logconfig_dialog(self, cfg) -> LogWriteDialog:
        """只造弹窗不弹。拆开来是为了能用真控件测内容，不卡在模态循环里。"""
        return LogWriteDialog(cfg, self)

    def show_logconfig_dialog(self, dlg: LogWriteDialog) -> int:
        return int(dlg.exec())

    def _show_logconfig_dialog(self, cfg) -> None:
        if self._logcfg_dialog is not None:
            try:
                self._logcfg_dialog.raise_()
                return                              # 已经提示过就不叠第二层
            except RuntimeError:                    # pragma: no cover
                self._logcfg_dialog = None
        dlg = self.build_logconfig_dialog(cfg)
        self._logcfg_dialog = dlg
        try:
            self.show_logconfig_dialog(dlg)
        finally:
            self._logcfg_dialog = None

    def _on_state(self, state: str, detail: str) -> None:
        text, color = STATE_TEXT.get(state, (state, "#8a8f98"))
        self._link_up = state in ("open", "ready")
        self.lbl_state.setText(f"{text}　{detail}".strip())
        self.lbl_state.setStyleSheet(f"color:{_theme.readable_on(self.lbl_state, color)}; font-weight:600;")
        self.connectionChanged.emit(state)
        if state in ("lost", "closed", "error"):
            if self._queue:
                self._append(f"链路 {state}，批量下载中止（剩余 {len(self._queue)} 条未发起）")
                self._finish_batch()
            else:
                self._set_busy(False)
        elif state == "ready" and self._busy_op == "connect":
            # 真机实测：没这个分支时，连接成功后面板会一直接在 busy，
            # 导致“刷新列表/下载”按钮保持禁用（点不动）。
            self._set_busy(False)
        self._set_buttons()
        self._append(f"链路状态：{state} — {detail}")

    def _on_logs(self, logs: list) -> None:
        self.show_logs(logs)
        self.logsListed.emit(logs)
        if self._busy_op != "download":
            self._set_busy(False)      # 列表能回来，说明 connect/demo 的等待已结束

    def _on_progress(self, done: int, total: int, rate: float, msg: str) -> None:
        pct = int(done * 100 / total) if total else 0
        self.pb.setValue(min(100, pct))
        self.pb.setFormat(f"{self._batch_tag()}%p%  {self._fmt(done)} / {self._fmt(total)}")
        self.lbl_rate.setText(f"平均速率 {self._fmt(rate)}/s")
        if total and done >= total:
            self.pb.setValue(100)

    def _batch_tag(self) -> str:
        """批量时的进度前缀“[3/12]”，单下为空。"""
        if len(self._batch) > 1 and self._current in self._batch:
            return f"[{self._batch.index(self._current) + 1}/{len(self._batch)}] "
        return ""

    def _on_done(self, res: DownloadResult) -> None:
        tag = self._batch_tag()
        if res.complete:
            self._set_row_state(res.log_id, "已完成")
            self._append(f"{tag}完成：{res.path}")
            self.logDownloaded.emit(str(res.path))
            if res.path:
                self._batch_ok.append(str(res.path))
            self._checked.discard(int(res.log_id))    # 下完自动取消勾选，失败的留着直接重跑
            self.pb.setValue(100)
            self.pb.setFormat(f"{tag}完成 100%  {self._fmt(res.expected_bytes)}")
            self.lbl_rate.setText(f"用时 {res.elapsed:.1f}s，{self._fmt(res.bytes_per_sec)}/s，"
                                  f"帧 {res.blocks_ok}，重传 {res.retries}，CRC 错 {res.crc_errors}")
        else:
            self._set_row_state(res.log_id, f"未完成：{res.reason}")
            self._batch_bad.append(int(res.log_id))
            self.pb.setFormat(f"{tag}中断 {res.written_bytes}/{res.expected_bytes}B（断点已保存）")
        self._sync_check_ui()
        if res.complete and self._queue:
            nxt = self._queue.pop(0)
            self.pb.setValue(0)
            self._start_one(nxt)
            self._set_busy(True, f"下载 log {nxt}（{self._batch.index(nxt) + 1}/{len(self._batch)}）",
                           op="download")
            return
        self._finish_batch()

    def _finish_batch(self) -> None:
        """收尾：把还停在“排队中”的行标成未下载，发 batchFinished，解锁面板。"""
        self._queue = []
        pending = [r for r in range(self.table.rowCount())
                   if (it := self.table.item(r, 4)) is not None and it.text() == "排队中"]
        for r in pending:
            lid = self._log_id_of_row(r)
            if lid is not None:
                self._set_row_state(lid, "未下载（已停止）")
        if pending:
            self._append(f"已停止：剩余 {len(pending)} 条未发起（仍保持勾选），再点“下载”接着跑")
        batch, ok, bad = list(self._batch), list(self._batch_ok), list(self._batch_bad)
        self._batch = []
        self._batch_ok = []
        self._batch_bad = []
        self._current = None
        self._set_busy(False)
        if len(batch) > 1:
            skipped = len(batch) - len(ok) - len(bad)
            msg = f"批量结束：成功 {len(ok)}/{len(batch)} 条"
            if bad:
                msg += f"，失败/中断 log {', '.join(str(x) for x in bad)}"
            if skipped > 0:
                msg += f"，未发起 {skipped} 条（勾选保留）"
            self._append(msg)
        if batch:
            self.batchFinished.emit(ok, bad)

    def _on_note(self, text: str) -> None:
        self._append(text)

    # ------------------------------------------------------------------ 对外 API
    def show_logs(self, logs: Iterable[RemoteLog]) -> None:
        """填充表格（也供主窗或自测直接调用）；勾选与状态按 log_id 保留，刷新列表不丢。"""
        logs = list(logs)
        self.worker.set_known(logs)
        self._item_changing = True
        self.table.setRowCount(len(logs))
        self._ids = []
        self._sizes = {}
        for i, e in enumerate(logs):
            lid = int(e.log_id)
            self._ids.append(lid)
            self._sizes[lid] = int(e.size)
            tstr = e.created.astimezone().strftime("%Y-%m-%d %H:%M:%S") if e.created else "RTC 未设置"
            cells = [str(e.log_id), tstr, self._fmt(e.size), str(e.blocks),
                     self._row_state.get(lid, "空闲")]
            for c, v in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(v)
                if c == 0:
                    it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    it.setData(QtCore.Qt.ItemDataRole.UserRole, lid)
                    it.setCheckState(QtCore.Qt.CheckState.Checked if lid in self._checked
                                     else QtCore.Qt.CheckState.Unchecked)
                    it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignLeft
                                        | QtCore.Qt.AlignmentFlag.AlignVCenter)
                elif c == 3:
                    it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(i, c, it)
        self._item_changing = False
        self._checked = {lid for lid in self._checked if lid in self._sizes}
        self.lbl_count.setText(f"{len(logs)} 条")
        self._sync_check_ui()

    def selected_log_id(self) -> Optional[int]:
        return self._log_id_of_row(self.table.currentRow())

    # ------------------------------------------------------------ 勾选与队列
    def _log_id_of_row(self, row: int) -> Optional[int]:
        if row < 0 or row >= self.table.rowCount():
            return None
        it = self.table.item(row, 0)
        if it is None:
            return None
        v = it.data(QtCore.Qt.ItemDataRole.UserRole)
        try:
            return int(v if v is not None else it.text())
        except (TypeError, ValueError):
            return None

    def _checked_ids(self) -> list[int]:
        """按表格顺序返回勾选的 log_id（下载顺序＝列表顺序）。"""
        return [lid for lid in self._ids if lid in self._checked]

    def _on_item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        """行内勾选变化：只认第 0 列，填表时由 `_item_changing` 拦住回环。

        注：PySide6 6.12 的 QTableWidgetItem **不暴露 `dataRole`**（C++ Qt6 有），
        所以不能按 role 过滤；列 0 的文本只在 `_item_changing=True` 时写过，足够安全。
        """
        if self._item_changing or item.column() != 0:
            return
        lid = self._log_id_of_row(item.row())
        if lid is None:
            return
        if item.checkState() == QtCore.Qt.CheckState.Checked:
            self._checked.add(lid)
        else:
            self._checked.discard(lid)
        self._sync_check_ui()

    def _on_select_all(self, _state: int = 0) -> None:
        if self._item_changing:
            return
        want = self.chk_all.checkState() != QtCore.Qt.CheckState.Unchecked
        self._item_changing = True
        self._checked = set()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is None:
                continue
            it.setCheckState(QtCore.Qt.CheckState.Checked if want
                             else QtCore.Qt.CheckState.Unchecked)
            if want:
                lid = self._log_id_of_row(r)
                if lid is not None:
                    self._checked.add(lid)
        self._item_changing = False
        self._sync_check_ui()

    def _sync_check_ui(self) -> None:
        """刷新“已勾选 N 条 / 预计耗时 / 按钮文案”。"""
        total = len(self._ids)
        n = len(self._checked)
        size = sum(self._sizes.get(lid, 0) for lid in self._checked)
        txt = f"已勾选 {n} 条"
        if n and size:
            txt += f" · {self._fmt(size)}"
            if n > 1:
                txt += f"，按真机实测 58 KB/s 约 {size / (58.4 * 1024) / 60.0:.0f} 分钟"
        self.lbl_checked.setText(txt)
        self.lbl_checked.setToolTip("下载过程中改勾选只影响下一批；已下完的会自动取消勾选")
        self.chk_all.blockSignals(True)
        if n == 0 or total == 0:
            st = QtCore.Qt.CheckState.Unchecked
        elif n >= total:
            st = QtCore.Qt.CheckState.Checked
        else:
            st = QtCore.Qt.CheckState.PartiallyChecked
        self.chk_all.setCheckState(st)
        self.chk_all.blockSignals(False)
        self.btn_download.setText("下载选中日志" if n == 0 else f"下载勾选的 {n} 条")
        self._set_buttons()

    @property
    def batch_active(self) -> bool:
        """是否还有批量在跑（主窗据此决定要不要每个文件都重载）。"""
        return bool(self._busy and self._busy_op == "download" and len(self._batch) > 1)

    def _start_one(self, lid: int) -> None:
        self._current = int(lid)
        self._set_row_state(lid, "下载中")
        self.worker.post("download", log_id=int(lid))

    def set_state_text(self, state: str, detail: str = "") -> None:
        """供自测/主窗设定显示状态。"""
        self._on_state(state, detail)

    def set_progress(self, done: int, total: int, rate: float = 0.0) -> None:
        self._on_progress(done, total, rate, "")

    def export_png(self, path: str, w: int = 760, h: int = 620) -> bool:
        self.resize(w, h)
        self.show()
        QtWidgets.QApplication.processEvents()
        pix = self.grab()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return bool(pix.save(str(path), "PNG"))

    def shutdown(self) -> None:
        """停掉 worker 线程并解除跨线程信号连接。

        必须在 QApplication 存活期间调用：多个 top-level 面板对象留到解释器退出时才销毁，
        会在 Qt native 层 abort（本机 offscreen 实测）。
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        for sig in (self.worker.state_changed, self.worker.logs_ready, self.worker.progress,
                    self.worker.download_done, self.worker.note):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self.worker.quit()
        self.worker.wait(3000)

    # ------------------------------------------------------------------ 内部小工具
    def _sync_options(self) -> None:
        self._out_dir = self.ed_dir.text().strip() or str(dldl.default_download_dir())
        self.worker.set_options(out_dir=self._out_dir, blocks_per_request=self.sp_blocks.value())

    def _set_row_state(self, log_id: int, text: str) -> None:
        self._row_state[int(log_id)] = text
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.text() == str(log_id):
                self.table.setItem(r, 4, QtWidgets.QTableWidgetItem(text))

    def _set_busy(self, busy: bool, why: str = "", op: str = "") -> None:
        self._busy = busy
        self._busy_op = op if busy else ""
        if busy and why:
            self.pb.setFormat(why)
        self._set_buttons()

    def _set_buttons(self) -> None:
        has_link = self._link_up or self.worker.sess is not None
        sel = self.selected_log_id() is not None
        picked = bool(self._checked_ids())
        self.btn_connect.setEnabled(not self._busy and not has_link)
        self.btn_disconnect.setEnabled(has_link)
        self.btn_refresh.setEnabled(has_link and not self._busy)
        self.btn_logcfg.setEnabled(has_link and not self._busy)
        self.btn_download.setEnabled(has_link and (picked or sel) and not self._busy)
        self.btn_cancel.setEnabled(self._busy)
        self.chk_all.setEnabled(not self._busy)
        for b in (self.ed_target, self.cb_baud, self.ed_dir, self.sp_blocks, self.btn_browse):
            b.setEnabled(not self._busy)
        for b in (self.cb_mode, self.cb_port, self.btn_ports, self.ed_tcp_host, self.sp_tcp_port,
                  self.ed_bind, self.sp_srv_port, self.ed_udp_bind, self.sp_udp_port,
                  self.ed_udpout_host, self.sp_udpout_port):
            b.setEnabled(not self._busy)

    def _append(self, text: str) -> None:
        self.plain.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {text}")
        sb = self.plain.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _fmt(n: float) -> str:
        try:
            from apmlog.core.model import human_size
            return human_size(float(n))
        except Exception:
            v = float(n)
            for u in ("B", "KB", "MB", "GB"):
                if abs(v) < 1024 or u == "GB":
                    return f"{v:.1f}{u}" if u != "B" else f"{int(v)}B"
                v /= 1024
            return f"{v:.1f}GB"

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:      # noqa: N802
        self.shutdown()
        super().closeEvent(ev)


# --------------------------------------------------------------------------- #
# 无头自测：offscreen 构造 + 抓 PNG
# --------------------------------------------------------------------------- #

def _selftest() -> int:                                   # pragma: no cover
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = LinkPanel(default_target="tcp:127.0.0.1:5760")
    logs = [
        RemoteLog(165, 6, 170, 315532800, 3_145_728),
        RemoteLog(166, 6, 170, 315532800, 2_945_024),
        RemoteLog(167, 6, 170, 1_791_359_555, 2_945_024),
        RemoteLog(168, 6, 170, 315532800, 90 * 3 + 56),
    ]
    w.show_logs(logs)
    w.table.setCurrentCell(1, 0)
    w._set_row_state(165, "已完成")
    w._set_row_state(167, "下载中")
    w.lbl_state.setText("飞控在线　sysid=1 compid=1")
    w.lbl_state.setStyleSheet(f"color:{_theme.readable_on(w.lbl_state, '#3aa659')}; font-weight:600;")
    w.lbl_detail.setText("rx 4 213 帧 · CRC 0 · udpout")
    w.set_progress(1_832_040, 2_945_024, 5_120.0)
    w.plain.appendPlainText("[12:41:03] 已打开链路：UDP 出站 → 192.168.2.1:14550")
    w.plain.appendPlainText("[12:41:04] 链路状态：ready — 飞控在线 sysid=1")
    w.plain.appendPlainText("[12:41:05] 飞控：ArduCopter V4.7.0 (1511f271)")
    w.plain.appendPlainText("[12:41:06] 列表：收到 4 条（期望 4），0.11s，complete")
    w.plain.appendPlainText("[12:41:07] 开始下载 log 167（2945024 字节，每请求 20 块）")
    app.processEvents()
    out = Path(__file__).resolve().parents[2] / "selftest" / "linkpanel.png"
    ok = w.export_png(str(out), 760, 640)
    print(f"offscreen grab -> {out} ok={ok} exists={out.exists()} "
          f"size={out.stat().st_size if out.exists() else 0}")
    # offscreen 实测：top-level 面板留到解释器退出时才回收会在 Qt native 层 abort，
    # 因此这里必须趁 QApplication 存活时彻底销毁。
    w.shutdown()
    w.close()
    w.deleteLater()
    del w
    for _ in range(6):
        app.processEvents()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
