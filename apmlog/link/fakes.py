"""apmlog.link.fakes —— 无硬件的假飞控（FakeFC），按实测线语义应答。

应答语义严格照抄 docs/LOG_DOWNLOAD_NOTES.md 第 3 节从真实 tlog 里解出来的字段：

* ``HEARTBEAT``：周期发送，``type=MAV_TYPE_QUADROTOR(2)``、``autopilot=MAV_AUTOPILOT_ARDUPILOTMEGA(3)``、
  ``compid=1`` —— 这是 :class:`~apmlog.link.endpoint.MavLinkSession` 识别飞控地址的依据。
* ``LOG_REQUEST_LIST(start,end)`` → 对 ``start<=id<=end`` 逐条回
  ``LOG_ENTRY(id, num_logs, last_log_num, time_utc, size)``；
  ``time_utc`` 默认用真实固件里观测到的 **1980 占位值 315532800**（RTC 未设置），
  也可以传入真实时间戳以覆盖另一条分支。
* ``LOG_REQUEST_DATA(id, ofs, count)`` → 从**字节偏移** ``ofs`` 起，回 ``ceil(count/90)`` 帧
  ``LOG_DATA(id, ofs, count, data[90])``，``ofs`` 每帧 **+90**；``count==0xFFFFFFFF`` 表示"直到结尾"；
  最后一帧 ``count`` 是剩余字节（实测尾块 915116 % 90 = 56 即此情形）。
* ``LOG_REQUEST_END``：真实会话里出现 0 次，这里只记录不改变行为。
* ``PARAM_REQUEST_READ``（msgid 20，本机 dialect 实测）→ 按名/按序命中时回
  ``PARAM_VALUE``（msgid 22，``param_type=MAV_PARAM_TYPE_INT32``、16 字节 NUL 填充）；
  **名字不存在就不回** —— 这正是 ArduPilot 的行为，也是 :mod:`apmlog.link.params`
  把"读不到"统一记成超时的原因。
* ``PARAM_SET``（msgid 23）→ **只记账，不回声、不改 ``self.params``**。
  本工程对飞控严格只读，链路层已经不提供写参数入口；这条记账留着，是为了让测试能证明
  “本端一旦发出写指令会被看到”：既能在 ``self.writes`` / ``stats.param_sets`` 上断言，
  也能被 :mod:`apmlog.link.audit` 抓进 ``forbidden``。

故障注入用于测试重传/续传：``drop_prob``（整帧不发）、``corrupt_prob``（改一个字节使 CRC 失效）、
``mute_logs``（该日志不应答下载）、``mute_list`` / ``mute_data``（整类请求不应答 → 触发客户端超时）、
``block_delay``（每帧延迟）。
参数侧的故障注入：``answer_params=False`` 完全不回任何 PARAM_*（测超时/unknown）；
写侧没有故障注入可用（飞控不会被本端写），仅保留收到写指令的记账能力。
"""
from __future__ import annotations

import logging
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from pymavlink.dialects.v20 import ardupilotmega as D

from .download import BLOCK_BYTES
from .endpoint import FrameReader
from .params import _decode_param_id, _encode_param_id
from .transport import (
    LinkError, MemoryTransport, TcpClientTransport, Transport, UdpTransport, _SocketStreamTransport,
)

log = logging.getLogger("apmlog.link.fakes")


class _ClientGone(Exception):
    """内部信号：客户端已断开（取消测试里会直接关掉 socket）。

    绝不允许从服务线程抛出去 —— 否则 :mod:`threading` 会走 excepthook，
    pytest 会报 ``PytestUnhandledThreadExceptionWarning``，而且 traceback 会被归给
    **当时正在跑的其它用例**（实测伪装成 TestFrameReader 里的 WinError 10054）。
    """

__all__ = ["FakeLog", "FakeFC", "DEFAULT_PARAMS"]

UTC_PLACEHOLDER = 315532800

#: 默认参数表："日志已开启"，取值来自用户真机 Pixhawk6X / Copter 4.7 的 PARAM 块实测
DEFAULT_PARAMS: dict[str, float] = {
    "LOG_BACKEND_TYPE": 1.0,
    "LOG_BITMASK": 180222.0,
    "LOG_DISARMED": 0.0,
}


@dataclass
class FakeLog:
    """飞控上的一条日志。"""

    log_id: int
    data: bytes
    time_utc: int = UTC_PLACEHOLDER

    @classmethod
    def from_file(cls, path: Any, log_id: Optional[int] = None) -> "FakeLog":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"假飞控日志源不存在: {p}")
        lid = int(p.stem) if log_id is None else int(log_id)
        return cls(log_id=lid, data=p.read_bytes(), time_utc=UTC_PLACEHOLDER)

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class FakeFCStats:
    heartbeats: int = 0
    list_requests: int = 0
    data_requests: int = 0
    end_requests: int = 0
    data_frames: int = 0
    dropped_frames: int = 0
    corrupted_frames: int = 0
    bytes_sent: int = 0
    param_reads: int = 0
    param_sets: int = 0


class _Port:
    """把 pymavlink 打包字节写进 Transport。"""

    def __init__(self, t: Transport) -> None:
        self.t = t

    def write(self, data: bytes) -> int:
        n = self.t.write(bytes(data))
        return n

    def flush(self) -> None:
        pass


class FakeFC:
    """按实测语义应答的假飞控。可在 TCP 服务端、UDP 端口或内存管道上运行。"""

    def __init__(self, logs: Sequence[FakeLog | tuple[int, bytes] | str | Path], *,
                 sysid: int = 1, compid: int = 1,
                 drop_prob: float = 0.0, corrupt_prob: float = 0.0,
                 block_delay: float = 0.0, heartbeat_period: float = 0.35,
                 seed: int = 0, mute_logs: Iterable[int] = (), max_clients: int = 1,
                 mute_list: bool = False, mute_data: bool = False,
                 statustext: tuple[str, ...] = (),
                 params: Optional[dict[str, float]] = None,
                 answer_params: bool = True,
                 armed: bool = False,
                 msd_supported: bool = True,
                 files: Optional[dict[str, bytes]] = None) -> None:
        self.logs: list[FakeLog] = []
        for item in logs:
            if isinstance(item, FakeLog):
                self.logs.append(item)
            elif isinstance(item, (str, Path)):
                self.logs.append(FakeLog.from_file(item))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                self.logs.append(FakeLog(log_id=int(item[0]), data=bytes(item[1])))
            else:                                        # pragma: no cover
                raise TypeError(f"无法理解的日志项: {type(item)}")
        self.logs.sort(key=lambda x: x.log_id)
        self.sysid = int(sysid)
        self.compid = int(compid)
        self.drop_prob = float(drop_prob)
        self.corrupt_prob = float(corrupt_prob)
        self.block_delay = float(block_delay)
        self.heartbeat_period = float(heartbeat_period)
        self.mute_logs = {int(i) for i in mute_logs}
        self.mute_list = bool(mute_list)          # 对 LOG_REQUEST_LIST 完全不应答
        self.mute_data = bool(mute_data)          # 对 LOG_REQUEST_DATA 完全不应答
        self.max_clients = int(max_clients)
        self.statustext = tuple(statustext)
        # 参数表：键统一大写（MAVLink 侧参数名就是大写 ASCII，小写匹配靠调用方归一）。
        # 默认给一套“日志已开启”的值，保证旧用例里的 FakeFC 行为不变的同时能直接跑参数往返。
        src = DEFAULT_PARAMS if params is None else params
        self.params: dict[str, float] = {str(k).strip().upper(): float(v) for k, v in src.items()}
        self.answer_params = bool(answer_params)     # False → 完全不回 PARAM_*（测超时/unknown）
        # 收到的写指令序列：本端不应产生任何一条，留着只给测试当“越权证据”用。
        self.writes: list[tuple[str, float]] = []    # 收到的参数写请求（不会真的改变 self.params）
        self.reads: list[str] = []                   # 收到的按名读请求（param_index=-1）
        self.rng = random.Random(seed)
        self.stats = FakeFCStats()
        self.request_log: list[tuple[str, Any]] = []
        # ── U 盘模式（MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN param1=5）与 MAVFTP 虚拟机 ──
        self.armed = bool(armed)               # 心跳里的 armed 位
        self.msd_supported = bool(msd_supported)   # False → 回 MAV_RESULT_UNSUPPORTED
        self.msd_entered = False                   # 收到并“接受”后才置 True
        self.commands: list[tuple[int, tuple[float, ...]]] = []   # 收到的 COMMAND_LONG
        self.files: dict[str, bytes] = dict(files or {})          # MAVFTP 虚拟文件系统
        self.ftp_sessions: dict[int, tuple[str, int]] = {}        # session → (path, offset)
        self._next_ftp_session = 1
        self._ftp_seq = 0                 # 应答包序（真机是逐包递增）
        self.ftp_ops: list[int] = []

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._servers: list[socket.socket] = []
        self._transports: list[Transport] = []
        self._lock = threading.Lock()
        self.last_error: str = ""

    # ------------------------------------------------------------------ 便捷构造
    @property
    def num_logs(self) -> int:
        return len(self.logs)

    def log(self, log_id: int) -> Optional[FakeLog]:
        return next((x for x in self.logs if x.log_id == int(log_id)), None)

    def data_of(self, log_id: int) -> bytes:
        e = self.log(log_id)
        return e.data if e else b""

    # ------------------------------------------------------------------ 服务入口
    def serve_tcp(self, host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        """后台线程监听 TCP；返回**实际端口**（port=0 时由系统分配）。

        这里先自行 ``bind/listen`` 以便把端口号立刻交给调用方，
        accept 到的 socket 用同包的 ``_SocketStreamTransport`` 包装（与 TcpServerTransport 同一实现）。
        """
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, int(port)))
        srv.listen(max(1, self.max_clients))
        srv.settimeout(0.2)
        self._servers.append(srv)
        real = srv.getsockname()[1]

        def _run() -> None:
            accepted = 0
            while not self._stop.is_set() and accepted < self.max_clients:
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                accepted += 1
                log.info("FakeFC 接受 TCP 客户端 %s", addr,)
                t = _SocketStreamTransport(conn, f"fakesrv:{real}<-{addr[0]}:{addr[1]}")
                self._transports.append(t)      # stop() 也必须关掉已建立的连接，避免 fd 泄漏
                try:
                    self.serve_transport(t)
                except Exception as exc:                     # 线程顶兼底：绝不逃逸
                    self.last_error = f"serve_transport 异常：{type(exc).__name__}: {exc}"
                    log.warning("FakeFC TCP 服务线程收尾：%s", self.last_error)

            self._stop.set()
            try:
                srv.close()
            except OSError:                              # pragma: no cover
                pass

        th = threading.Thread(target=_run, name="fakefc-tcp", daemon=True)
        self._threads.append(th)
        th.start()
        return (srv.getsockname()[0], real)

    def serve_udp(self, bind: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
        """后台线程在 UDP 端口上服务；返回 (bind, 实际端口)。对端地址自动学习。"""
        t = UdpTransport(bind, port)
        self._transports.append(t)

        def _run() -> None:
            try:
                self.serve_transport(t, close_on_exit=False)
            except Exception as exc:                     # pragma: no cover
                self.last_error = str(exc)
                log.warning("FakeFC UDP 结束: %s", exc)

        th = threading.Thread(target=_run, name="fakefc-udp", daemon=True)
        self._threads.append(th)
        th.start()
        return (bind, t.bound_port)

    def serve_pipe(self, label: str = "fakes") -> Transport:
        """内存管道：返回**客户端侧** transport，另一端由 FakeFC 服务。"""
        a, b = MemoryTransport.create_pair(label)

        def _run() -> None:
            try:
                self.serve_transport(a)
            except Exception as exc:                         # 线程顶兼底
                self.last_error = f"serve_transport 异常：{type(exc).__name__}: {exc}"
                log.warning("FakeFC pipe 服务线程收尾：%s", self.last_error)

        th = threading.Thread(target=_run, name="fakefc-pipe", daemon=True)
        self._threads.append(th)
        th.start()
        return b

    def stop(self) -> None:
        self._stop.set()
        for s in self._servers:
            try:
                s.close()
            except OSError:                              # pragma: no cover
                pass
        for t in self._transports:
            t.close()
        for th in self._threads:
            if th.is_alive():
                th.join(timeout=1.5)
        self._threads.clear()

    # ------------------------------------------------------------------ 核心循环
    def serve_transport(self, transport: Transport, *, close_on_exit: bool = True) -> None:
        """在给定传输上服务（阻塞直到 stop 或链路断开）。

        ``close_on_exit=False`` 用于 UDP：单帧失败不应该拆掉整个服务。
        """
        reader = FrameReader()
        mav = D.MAVLink(_Port(transport), self.sysid, self.compid)
        last_hb = 0.0
        log.info("FakeFC 就绪：logs=%d ids=%s sizes=%s params=%s answer_params=%s",
                 self.num_logs, [x.log_id for x in self.logs], [x.size for x in self.logs],
                 len(self.params), self.answer_params)
        for txt in self.statustext:
            self._send_statustext(mav, txt)
        try:
            while not self._stop.is_set():
                try:
                    data = transport.read(4096, timeout=0.05)
                except LinkError as exc:
                    log.info("FakeFC 链路结束: %s", exc)
                    break
                now = time.monotonic()
                if data:
                    for frame in reader.feed(data):
                        try:
                            self._handle_frame(frame, mav, transport)
                        except _ClientGone:
                            raise
                        except LinkError as exc:        # 回 LOG_ENTRY 时对端走了
                            self.last_error = f"回包失败：{exc}"
                            if getattr(exc, "transient", False):
                                log.info("FakeFC 客户端已断开：%s", self.last_error)
                                return
                            log.warning("FakeFC 回包失败，继续服务：%s", self.last_error)
                            continue
                if now - last_hb >= self.heartbeat_period:
                    last_hb = now
                    try:
                        # base_mode 里的 0x80 = MAV_MODE_FLAG_SAFETY_ARMED，
                        # 与 HeartbeatInfo.armed 的判定保持一致
                        mav.heartbeat_send(D.MAV_TYPE_QUADROTOR, D.MAV_AUTOPILOT_ARDUPILOTMEGA,
                                           0x80 if self.armed else 0, 0, D.MAV_STATE_ACTIVE, 3)
                        self.stats.heartbeats += 1
                    except LinkError as exc:
                        # UDP 还没学到对端地址时属正常现象，等 GCS 先发一个包
                        log.debug("FakeFC 心跳暂不可发: %s", exc)
        except _ClientGone as exc:
            log.info("FakeFC 结束一条连接：%s", exc)
        except LinkError as exc:                         # pragma: no cover
            self.last_error = str(exc)
            log.info("FakeFC 链路错误结束：%s", exc)
        finally:
            if close_on_exit:
                transport.close()

    def _handle_frame(self, frame: bytes, mav: D.MAVLink, transport: Transport) -> None:
        msgs = mav.parse_buffer(frame) or []
        for m in msgs:
            name = m.get_type()
            if name == "LOG_REQUEST_LIST":
                with self._lock:
                    self.stats.list_requests += 1
                    self.request_log.append(("LOG_REQUEST_LIST", (int(m.start), int(m.end))))
                if self.mute_list:
                    return
                last = self.logs[-1].log_id if self.logs else 0
                for lg in self.logs:
                    if not (m.start <= lg.log_id <= m.end):
                        continue
                    mav.log_entry_send(id=lg.log_id, num_logs=self.num_logs,
                                       last_log_num=last, time_utc=lg.time_utc, size=lg.size)
                    if self.block_delay:
                        time.sleep(self.block_delay)
                log.debug("FakeFC 已回 %d 条 LOG_ENTRY", self.num_logs)
            elif name == "LOG_REQUEST_DATA":
                with self._lock:
                    self.stats.data_requests += 1
                    self.request_log.append(("LOG_REQUEST_DATA", (int(m.id), int(m.ofs), int(m.count))))
                self._stream_data(mav, transport, int(m.id), int(m.ofs), int(m.count))
            elif name == "LOG_REQUEST_END":
                with self._lock:
                    self.stats.end_requests += 1
                    self.request_log.append(("LOG_REQUEST_END", ()))
            elif name == "PARAM_REQUEST_READ":
                self._handle_param_read(mav, m)
            elif name == "PARAM_SET":
                self._record_param_write(m)
            elif name == "COMMAND_LONG":
                self._handle_command_long(mav, m)
            elif name == "FILE_TRANSFER_PROTOCOL":
                self._handle_ftp(mav, m)

    # ------------------------------------------------------------------ 指令往返
    def _handle_command_long(self, mav: D.MAVLink, m: Any) -> None:
        """``COMMAND_LONG``：只实现预检重启（246），按 armed / 支持位回 ACK。

        ``param1=5`` = ``REBOOT_TO_MASS_STORAGE``：
        * 已解锁 → ``MAV_RESULT_FAILED``（与上游 ``GCS_Common.cpp`` 一致）
        * 已声明不支持 → ``MAV_RESULT_UNSUPPORTED``
        * 其余 → ``MAV_RESULT_ACCEPTED`` 并置 ``msd_entered``
        """
        from .ftp import (MAV_RESULT_ACCEPTED, MAV_RESULT_FAILED,
                          MAV_RESULT_UNSUPPORTED, REBOOT_SHUTDOWN_ACTION_REBOOT_TO_MASS_STORAGE)
        cmd = int(m.command)
        params = (float(m.param1), float(m.param2), float(m.param3), float(m.param4),
                  float(m.param5), float(m.param6), float(m.param7))
        with self._lock:
            self.commands.append((cmd, params))
            self.request_log.append(("COMMAND_LONG", (cmd, params)))
        if cmd != 246:
            result = MAV_RESULT_UNSUPPORTED
        elif abs(params[0] - float(REBOOT_SHUTDOWN_ACTION_REBOOT_TO_MASS_STORAGE)) < 1e-6:
            if self.armed:
                result = MAV_RESULT_FAILED
            elif not self.msd_supported:
                result = MAV_RESULT_UNSUPPORTED
            else:
                result = MAV_RESULT_ACCEPTED
                self.msd_entered = True
        else:
            result = MAV_RESULT_ACCEPTED
        try:
            mav.command_ack_send(cmd, int(result))
        except LinkError as exc:                       # pragma: no cover
            log.debug("FakeFC COMMAND_ACK 发送失败: %s", exc)

    # ------------------------------------------------------------------ MAVFTP
    def _ftp_reply(self, mav: D.MAVLink, seq: int, session: int, opcode: int,
                   data: bytes = b"", offset: int = 0, burst_complete: bool = False,
                   req_opcode: int = 0) -> None:
        """按上游 ``GCS_FTP`` 的真实格式回包：``<HBBBBBBI`` + 最多 239 字节数据。"""
        from .ftp import DATA_OFF, HDR_FMT, PAYLOAD_LEN
        payload = bytearray(PAYLOAD_LEN)
        struct.pack_into(HDR_FMT, payload, 0, int(seq) & 0xFFFF, int(session) & 0xFF,
                         int(opcode) & 0xFF, len(data), int(req_opcode) & 0xFF,
                         1 if burst_complete else 0, 0, int(offset) & 0xFFFFFFFF)
        payload[DATA_OFF:DATA_OFF + len(data)] = data
        try:
            mav.file_transfer_protocol_send(0, self.sysid, self.compid, bytes(payload))
        except LinkError as exc:                       # pragma: no cover
            log.debug("FakeFC FILE_TRANSFER_PROTOCOL 发送失败: %s", exc)

    def _ftp_nack(self, mav: D.MAVLink, seq: int, session: int, req_opcode: int,
                  code: int) -> None:
        from .ftp import OP_NAK
        self._ftp_reply(mav, seq, session, OP_NAK, bytes([int(code) & 0xFF]),
                        req_opcode=req_opcode)

    def _handle_ftp(self, mav: D.MAVLink, m: Any) -> None:
        """MAVFTP：在内存虚拟文件系统上实现只读的几个操作码（真机同款格式）。"""
        from .ftp import (DATA_OFF, MAX_DATA, OP_ACK, OP_BURST_READ_FILE,
                          OP_LIST_DIRECTORY, OP_OPEN_FILE_RO, OP_READ_FILE,
                          OP_TERMINATE_SESSION, HDR_FMT)
        p = bytes(m.payload)
        if len(p) < DATA_OFF:
            return
        seq, sess, opcode, size, _req, _burst, _pad, offset = struct.unpack_from(HDR_FMT, p, 0)
        data = p[DATA_OFF:DATA_OFF + int(size)]
        with self._lock:
            self.ftp_ops.append(int(opcode))
            # 与真机一致：应答的 seq 逐包递增，不是回填请求的 seq
            base_seq = (self._ftp_seq + 1) & 0xFFFF
            self._ftp_seq = base_seq
        if opcode == OP_OPEN_FILE_RO:
            path = data.split(b"\x00")[0].decode("utf-8", "replace")
            blob = self.files.get(path)
            if blob is None:
                self._ftp_nack(mav, base_seq, sess, OP_OPEN_FILE_RO, 10)   # FileNotFound
                return
            with self._lock:
                sid = self._next_ftp_session
                self._next_ftp_session = (self._next_ftp_session % 254) + 1
                self.ftp_sessions[sid] = (path, 0)
            self._ftp_reply(mav, base_seq, sid, OP_ACK,
                            struct.pack("<I", len(blob)), req_opcode=OP_OPEN_FILE_RO)
            return
        if opcode in (OP_READ_FILE, OP_BURST_READ_FILE):
            with self._lock:
                entry = self.ftp_sessions.get(sess)
            if entry is None:
                self._ftp_nack(mav, base_seq, sess, opcode, 4)              # InvalidSession
                return
            blob = self.files.get(entry[0], b"")
            # 读请求的头 size 字段 = 客户端想要的字节数；真机也是这么算的
            want = max(1, min(MAX_DATA, int(size) or MAX_DATA))
            if opcode == OP_READ_FILE:
                chunk = blob[offset:offset + want]
                self._ftp_reply(mav, base_seq, sess, OP_ACK, chunk, offset=offset,
                                burst_complete=True, req_opcode=OP_READ_FILE)
                return
            pos = int(offset)
            limit = 16 * 1024                       # 模拟真实 burst：一次最多回这么多
            seq_i = base_seq
            while pos < len(blob) and (pos - int(offset)) < limit:
                chunk = blob[pos:pos + want]
                if not chunk:
                    break
                done = pos - int(offset) + len(chunk)
                last = (pos + len(chunk) >= len(blob)) or (done >= limit)
                self._ftp_reply(mav, seq_i, sess, OP_ACK, chunk, offset=pos,
                                burst_complete=last, req_opcode=OP_BURST_READ_FILE)
                pos += len(chunk)
                seq_i = (seq_i + 1) & 0xFFFF
            if seq_i != base_seq:
                with self._lock:
                    self._ftp_seq = (seq_i - 1) & 0xFFFF
            return
        if opcode == OP_TERMINATE_SESSION:
            with self._lock:
                self.ftp_sessions.pop(sess, None)
            self._ftp_reply(mav, base_seq, sess, OP_ACK, req_opcode=OP_TERMINATE_SESSION)
            return
        if opcode == OP_LIST_DIRECTORY:
            path = data.split(b"\x00")[0].decode("utf-8", "replace")
            prefix = path.rstrip("/") + "/"
            entries: list[str] = []
            seen: set[str] = set()
            for fp in sorted(self.files):
                if not fp.startswith(prefix):
                    continue
                rest = fp[len(prefix):]
                if "/" in rest:
                    d = rest.split("/")[0]
                    if d not in seen:
                        seen.add(d)
                        entries.append(f"D{d}\t0")
                else:
                    entries.append(f"F{rest}\t{len(self.files[fp])}")
            blob = b"".join(e.encode("utf-8") + b"\x00" for e in entries)
            page = blob[int(offset):int(offset) + MAX_DATA]
            self._ftp_reply(mav, base_seq, sess, OP_ACK, page, offset=int(offset),
                            req_opcode=OP_LIST_DIRECTORY)
            return
        self._ftp_nack(mav, base_seq, sess, opcode, 7)                       # UnknownCommand

    def _handle_param_read(self, mav: D.MAVLink, m: Any) -> None:
        """``PARAM_REQUEST_READ``：param_index=-1 走按名查，否则走按序查。

        没命中（名字不存在 / index 越界）就**不回**，也不因 ``answer_params=False`` 而记账，
        因为记账目的是让测试能看见“客户端确实发了请求”而与是否应答无关。
        """
        pid = _decode_param_id(m.param_id)
        index = int(m.param_index)
        with self._lock:
            self.stats.param_reads += 1
            self.request_log.append(("PARAM_REQUEST_READ", (pid, index)))
            if index < 0:
                self.reads.append(pid)               # 只记“按名读”；按序读看 request_log
        if not self.answer_params:
            return
        if index >= 0:
            items = list(self.params.items())
            if index >= len(items):
                log.debug("FakeFC 忽略越界参数序号请求 index=%d/%d", index, len(items))
                return
            key, value = items[index]
        else:
            hit = next((k for k in self.params if k == pid), None)
            if hit is None:
                log.debug("FakeFC 无此参数，不应答: %s", pid)
                return
            key, value = hit, self.params[hit]
        self._send_param_value(mav, key, value)

    def _record_param_write(self, m: Any) -> None:
        """收到参数写指令时**只记账**：不回声、不改 ``self.params``。

        留着这条路径而不是直接报错，是为了让测试可以断言“飞控侧确实看到了写请求”，
        从而证明审计护栏有效（见 tests/test_link_params.py 的防回退守卫）。
        本端代码已经没有任何发写指令的入口，正常情况下这两个记账永远为空。
        """
        pid = _decode_param_id(m.param_id)
        with self._lock:
            self.stats.param_sets += 1
            self.request_log.append(("PARAM_SET", (pid, float(m.param_value))))
            self.writes.append((pid, float(m.param_value)))
        log.warning("FakeFC 收到越权的参数写请求 %s=%s：不应答、不存值", pid, float(m.param_value))

    def _send_param_value(self, mav: D.MAVLink, name: str, value: float) -> None:
        """回一条 PARAM_VALUE。param_type 固定 INT32（日志类参数在 ArduPilot 里就是整型）。"""
        items = list(self.params)
        idx = items.index(name) if name in items else 0
        raw = _encode_param_id(name)
        try:
            mav.param_value_send(raw, float(value), int(D.MAV_PARAM_TYPE_INT32),
                                 len(self.params), idx)
        except Exception as exc:                     # pragma: no cover
            self.last_error = f"回 PARAM_VALUE 失败 {name}: {exc}"
            log.warning("FakeFC %s", self.last_error)


    def _stream_data(self, mav: D.MAVLink, transport: Transport, log_id: int, ofs: int, count: int) -> None:
        lg = self.log(log_id)
        if lg is None or log_id in self.mute_logs or self.mute_data:
            log.debug("FakeFC 忽略请求（不存在或静音）log=%d", log_id)
            return
        if count in (0xFFFFFFFF, -1) or count <= 0:
            count = lg.size - ofs                        # 实测 count=4294967295 = 一直到结尾
        pos = ofs
        remaining = max(0, min(count, lg.size - pos))
        while remaining > 0 and not self._stop.is_set():
            n = min(BLOCK_BYTES, remaining)
            chunk = lg.data[pos:pos + n]
            # 实固件行为：LOG_DATA 的 data 永远是 90 字节数组，尾块用 0 填充，
            # 有效长度由 count 给出（pymavlink 的 packer 会直接索引 data[0..89]）。
            padded = list(chunk) + [0] * (BLOCK_BYTES - n)
            try:
                if self.corrupt_prob and self.rng.random() < self.corrupt_prob:
                    self._send_corrupted(mav, transport, log_id, pos, padded, n)
                    self.stats.corrupted_frames += 1
                elif self.drop_prob and self.rng.random() < self.drop_prob:
                    self.stats.dropped_frames += 1       # 模拟线路丢帧：整帧不发
                else:
                    mav.log_data_send(id=log_id, ofs=pos, count=n, data=padded)
                    self.stats.data_frames += 1
                    self.stats.bytes_sent += n
            except Exception as exc:
                self.last_error = f"发送 LOG_DATA 失败 ofs={pos} count={n}: {exc}"
                # 对端已取消/关闭：记一次就停这条连接，**不把异常抛到线程外**。
                # 旧写法是 log + raise，traceback 会逃逸出线程并污染其它用例的 pytest 报告。
                if getattr(exc, "transient", False):
                    log.info("FakeFC 客户端已断开，停止回包：%s", self.last_error)
                    raise _ClientGone(self.last_error) from exc
                log.error("FakeFC %s", self.last_error)
                raise _ClientGone(self.last_error) from exc
            pos += n
            remaining -= n
            if self.block_delay:
                time.sleep(self.block_delay)

    def _send_corrupted(self, mav: D.MAVLink, transport: Transport, log_id: int, ofs: int,
                        padded: list[int], count: int) -> None:
        """打包后翻转 payload 一个字节 —— 客户端应判为 CRC 错并丢弃重传。"""
        msg = mav.log_data_encode(id=log_id, ofs=ofs, count=count, data=padded)
        buf = bytearray(msg.pack(mav))
        idx = len(buf) - 5                               # payload 区内（避开尾部 CRC）
        if 2 <= idx < len(buf) - 2:
            buf[idx] ^= 0xFF
        transport.write(bytes(buf))

    @staticmethod
    def _send_statustext(mav: D.MAVLink, text: str) -> None:
        raw = text.encode("utf-8", "replace")[:50]
        try:
            mav.statustext_send(D.MAV_SEVERITY_INFO, raw + bytes(50 - len(raw)))
        except LinkError as exc:
            log.debug("FakeFC STATUSTEXT 暂不可发: %s", exc)
