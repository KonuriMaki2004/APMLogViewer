"""apmlog.link.endpoint —— 连接目标解析 + MAVLink 会话（收/发/心跳/订阅）。

======================== 本机 dialect 实测结论 ========================
（依据 docs/LOG_DOWNLOAD_NOTES.md 第 1 节，勿凭记忆改写）

1. 本机 pip 版 pymavlink 2.4.50 的 ``message_definitions/`` **只有 ``v1.0/``**，
   里面有 ardupilotmega.xml/common.xml 等，但**没有 python.xml、也没有 v2.0/ 目录**。
2. ``mavutil.set_dialect()`` 只有在环境变量 ``MAVLINK20`` 存在时才去 import ``dialects.v20.*``，
   否则 import ``dialects.v10.<dialect>``；import 失败就现场调 mavgen 生成 XML —— 于是：
   * ``set_dialect('ardupilotmega')`` / ``('common')`` → **可用**（预生成模块存在）
   * ``set_dialect('python')`` → **失败** FileNotFoundError: message_definitions\\v1.0\\python.xml
   * ``set_dialect('v20')``     → **失败** ModuleNotFoundError: dialects.v10.v20 → 回落 mavgen → FileNotFoundError
3. 结论：本工程**不走** ``mavutil.mavlink_connection(device, dialect='python')`` 这条路，
   而是直接引用预生成的 v2.0 模块，自己管理传输与收包循环::

       from pymavlink.dialects.v20 import ardupilotmega as D
       mav = D.MAVLink(port, srcSystem=255, srcComponent=190)   # 参数名是 srcSystem/srcComponent

   v2.0 模块生成的解析器同时能解 v1(0xFE) 与 v2(0xFD) 帧，因此不需要 mavutil 的 socket/serial 封装。
4. CRC 算法按 ``MAVLink_message._pack`` 源码实现：``x25crc(msgbuf[1:])`` 之后
   ``accumulate(1 字节 crc_extra)``；**只有 1 字节**，且 v2 会剥离 payload 尾部 0。
   pymavlink 解析器默认不因 CRC 失败而报错，所以本模块**自己严格校验 CRC**，坏帧丢弃并计数。
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time

__all__ = [
    "Endpoint", "parse_target", "open_transport", "FrameReader",
    "HeartbeatInfo", "LinkStats", "MavLinkSession", "MavLinkState",
]

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from pymavlink.dialects.v20 import ardupilotmega as D

from .transport import (
    LinkError, MemoryTransport, SerialTransport, TcpClientTransport,
    TcpServerTransport, Transport, UdpOutTransport, UdpTransport,
)

log = logging.getLogger("apmlog.link.endpoint")

MAVLINK1_MAGIC = 0xFE
MAVLINK2_MAGIC = 0xFD
MAV_MAX_PAYLOAD = 255
IFLAG_SIGNED = 0x20


# --------------------------------------------------------------------------- #
# 目标串语法
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Endpoint:
    """一个连接目标的解析结果。"""

    kind: str                                     # serial | tcp | tcpsrv | udp | udpout | pipe
    device: str = ""                              # 串口号
    baud: int = 57600
    host: str = ""                                # 对端主机（tcp/udpout）
    port: int = 0                                 # 对端端口或本地监听端口
    bind: str = "0.0.0.0"                         # 监听地址
    target: str = ""                              # 原始字符串

    def describe(self) -> str:
        if self.kind == "serial":
            return f"串口 {self.device} @{self.baud}"
        if self.kind == "tcp":
            return f"TCP 客户端 → {self.host}:{self.port}"
        if self.kind == "tcpsrv":
            return f"TCP 服务端 监听 {self.bind}:{self.port}"
        if self.kind == "udp":
            return f"UDP 监听 {self.bind}:{self.port}"
        if self.kind == "udpout":
            return f"UDP 出站 → {self.host}:{self.port}"
        return self.kind

    @property
    def ui_hint(self) -> str:
        return (
            "目标串语法：\n"
            "  COM5                              # 裸串口号（Windows），默认波特率见下拉框\n"
            "  serial:COM5?baud=57600            # 串口，可带 rts/dtr 之外的常用参数\n"
            "  serial:/dev/ttyACM0?baud=115200   # Linux 串口\n"
            "  tcp:192.168.2.1:5760              # TCP 客户端（数传/WiFi 透传）\n"
            "  192.168.2.1:5760                  # 同上（无协议头时默认 TCP 客户端）\n"
            "  tcpsrv:5760 | tcpsrv:0.0.0.0:5760 # TCP 服务端，等飞控/透传箱接入\n"
            "  udp:14550 | udp:0.0.0.0:14550     # UDP 监听，自动学习对端\n"
            "  udpout:192.168.2.1:14550          # UDP 固定目标出站\n"
            "  pipe:自测                           # 内存管道（自测用，不产生真实网络）"
        )


_RE_SERIAL = re.compile(r"^serial:(?P<dev>[A-Za-z]:[^?]*|/[^?]*|COM\d+|\d+)(?:\?(?P<q>.*))?$", re.I)
_RE_BARE_COM = re.compile(r"^(?P<dev>COM\d+)$", re.I)
_RE_SCHEME = re.compile(r"^(?P<kind>tcp|tcpsrv|udp|udpout|pipe):(?P<rest>.+)$", re.I)
_RE_HOSTPORT = re.compile(r"^(?P<host>[A-Za-z0-9_.\-]+)?(?::(?P<port>\d+))?$")


def _baud_from_query(q: Optional[str], default: int) -> int:
    if not q:
        return default
    for part in q.split("&"):
        if part.lower().startswith("baud="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                raise ValueError(f"波特率非法: {part}")
    return default


def parse_target(text: str, *, default_baud: int = 57600) -> Endpoint:
    """把用户输入解析成 :class:`Endpoint`。语法见 :attr:`Endpoint.ui_hint`。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("连接目标不能为空")

    m = _RE_SERIAL.match(raw)
    if m:
        return Endpoint(kind="serial", device=m.group("dev"),
                        baud=_baud_from_query(m.group("q"), default_baud), target=raw)
    m = _RE_BARE_COM.match(raw)
    if m:
        return Endpoint(kind="serial", device=m.group("dev"), baud=default_baud, target=raw)

    m = _RE_SCHEME.match(raw)
    if not m:
        # 无协议头：host:port 视为 TCP 客户端
        if ":" in raw and re.search(r":\d+$", raw):
            host, _, port = raw.rpartition(":")
            return Endpoint(kind="tcp", host=host, port=int(port), target=raw)
        raise ValueError(f"无法识别连接目标: {raw}\n{Endpoint(kind='?', target=raw).ui_hint}")

    kind = m.group("kind").lower()
    rest = m.group("rest").strip()
    if kind == "pipe":
        return Endpoint(kind="pipe", target=raw)

    q = ""
    if "?" in rest:
        rest, q = rest.split("?", 1)
    bind = "0.0.0.0"

    if kind in ("tcp", "udpout"):
        host, sep, port = rest.rpartition(":")
        if not sep or not port.isdigit():
            raise ValueError(f"{kind}: 需要 host:port，收到 {rest}")
        return Endpoint(kind=kind, host=host or "127.0.0.1", port=int(port),
                        baud=_baud_from_query(q, default_baud), target=raw)

    # tcpsrv / udp：可选 bind:port，也允许只写端口
    mp = _RE_HOSTPORT.match(rest)
    if not mp or not (mp.group("port") or rest.isdigit()):
        raise ValueError(f"{kind}: 需要 port 或 bind:port，收到 {rest}")
    if mp.group("host") and ":" in rest:          # 只有显式写了 bind:port 时才改监听地址
        bind = mp.group("host")
    port = int(mp.group("port") or rest)
    return Endpoint(kind=kind, bind=bind, port=port,
                    baud=_baud_from_query(q, default_baud), target=raw)


def open_transport(ep: Endpoint, *, wait: float = 10.0,
                   pair_with: Optional[str] = None) -> Transport:
    """按 :class:`Endpoint` 建立传输。``pipe`` 用于自测（返回互联管道的一端）。"""
    if ep.kind == "serial":
        return SerialTransport(ep.device, ep.baud)
    if ep.kind == "tcp":
        return TcpClientTransport(ep.host, ep.port)
    if ep.kind == "tcpsrv":
        return TcpServerTransport(ep.bind, ep.port, wait=wait)
    if ep.kind == "udp":
        return UdpTransport(ep.bind, ep.port)
    if ep.kind == "udpout":
        return UdpOutTransport(ep.host, ep.port)
    if ep.kind == "pipe":
        a, b = MemoryTransport.create_pair(pair_with or "selftest")
        # 另一端交给调用方（FakeFC）使用；这里返回本地端
        open_transport.last_pair = b               # type: ignore[attr-defined]
        return a
    raise LinkError(f"未知传输类型: {ep.kind}")


# --------------------------------------------------------------------------- #
# 帧读取（严格 CRC）
# --------------------------------------------------------------------------- #


class FrameReader:
    """增量 MAVLink v1/v2 分帧器，严格校验 CRC。

    用法：``for frame_bytes in reader.feed(data): ...``
    每帧同时统计 ``rx_bytes / dropped_bytes / crc_errors / truncated``。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.rx_bytes = 0
        self.rx_frames = 0
        self.dropped_bytes = 0        # 非帧前缀垃圾字节（例如 Mission Planner tlog 的 8B 时间戳）
        self.crc_errors = 0
        self.unknown_msgs = 0

    def reset(self) -> None:
        self._buf.clear()

    @staticmethod
    def _crc(buf: memoryview, extra: int) -> int:
        c = D.x25crc(bytes(buf))
        c.accumulate(bytes([extra & 0xFF]))
        return c.crc & 0xFFFF

    def feed(self, data: bytes) -> Iterable[bytes]:
        """喂入原始字节，产出**完整且 CRC 正确**的帧（含首字节到 CRC 末字节）。"""
        if not data:
            return ()
        self._buf.extend(data)
        out: list[bytes] = []
        buf = self._buf
        i = 0
        n = len(buf)
        while True:
            # 找 magic
            start = -1
            for j in range(i, n):
                if buf[j] in (MAVLINK1_MAGIC, MAVLINK2_MAGIC):
                    start = j
                    break
            if start < 0:
                self.dropped_bytes += n - i
                i = n
                break
            if start > i:
                self.dropped_bytes += start - i
            i = start
            if n - i < 6:
                break
            magic = buf[i]
            ln = buf[i + 1]
            if ln > MAV_MAX_PAYLOAD:
                i += 1
                self.dropped_bytes += 1
                continue
            if magic == MAVLINK2_MAGIC:
                if n - i < 10:
                    break
                mid = buf[i + 7] | (buf[i + 8] << 8) | (buf[i + 9] << 16)
                payload = i + 10
                sig_len = 13 if (buf[i + 2] & IFLAG_SIGNED) else 0
            else:
                # v1 实测字节（pymavlink force_mavlink1 打包 HEARTBEAT）：
                #   fe 09 00 01 01 00 00 00 00 00 02 03 00 04 03 d0 14
                #   = magic len seq sys comp **msgid** <payload> crc2 → 总长 len+8，msgid 在索引 5
                # Mission Planner tlog 首帧同理：fe 06 01 ff be **42** ... = REQUEST_DATA_STREAM(66)
                if n - i < 8:
                    break
                mid = buf[i + 5]
                payload = i + 6
                sig_len = 0
            total = (payload - i) + ln + 2 + sig_len
            if n - i < total:
                break
            frame = bytes(buf[i:i + total])
            off = (payload - i) + ln
            wire = frame[off] | (frame[off + 1] << 8)
            extra = D.mavlink_map[mid].crc_extra if mid in D.mavlink_map else None
            if extra is None:
                self.unknown_msgs += 1
                i += total
                continue
            if self._crc(memoryview(buf)[i + 1:payload + ln], extra) != wire:
                self.crc_errors += 1
                i += 1                       # 疑似伪 magic，逐字节重同步
                continue
            self.rx_frames += 1
            self.rx_bytes += total
            out.append(frame)
            i += total
        if i:
            del buf[:i]
        return out


# --------------------------------------------------------------------------- #
# 会话状态
# --------------------------------------------------------------------------- #


@dataclass
class HeartbeatInfo:
    """一个 (sysid, compid) 的最新心跳快照。"""

    sysid: int = 0
    compid: int = 0
    type: int = 0
    autopilot: int = 0
    base_mode: int = 0
    custom_mode: int = 0
    system_status: int = 0
    mavlink_version: int = 0
    count: int = 0
    last_seen: float = 0.0

    @property
    def armed(self) -> bool:
        return bool(self.base_mode & 0x80)          # MAV_MODE_FLAG_SAFETY_ARMED

    @property
    def custom_mode_main(self) -> int:
        """MAV_MODE_FLAG_DECODE_POSITION_MAIN_MODE（0xE0）——只给原始位段，不猜模式名。"""
        return (self.custom_mode >> 24) & 0x07

    def age(self) -> float:
        return time.monotonic() - self.last_seen if self.last_seen else float("inf")

    def as_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["armed"] = self.armed
        d["age"] = self.age()
        return d


@dataclass
class LinkStats:
    rx_frames: int = 0
    rx_bytes: int = 0
    tx_frames: int = 0
    tx_bytes: int = 0
    crc_errors: int = 0
    dropped_bytes: int = 0
    unknown_msgs: int = 0
    reconnects: int = 0

    def rate_hz(self, window: float, t0: float) -> float:
        return self.rx_frames / window if window > 0 else 0.0

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


MavLinkState = str  # "closed" | "open" | "ready" | "lost" | "error"


class MavLinkSession:
    """一个链路上的 MAVLink 会话。

    * 单读者线程解析帧并把消息分发给 ``listen()`` 注册的队列与回调；
    * 所有发送走同一 ``_tx_lock``，UI 线程与心跳线程可并发调用；
    * ``fc_sysid/fc_compid`` 自动从心跳里选出的飞控地址，用于 LOG_* 请求的目标。
    """

    def __init__(self, transport: Transport, *, our_sysid: int = 255, our_compid: int = 190,
                 expected_sysid: Optional[int] = None, hb_timeout: float = 8.0,
                 send_our_heartbeat: bool = False, name: str = "") -> None:
        self.transport = transport
        self.name = name or transport.name
        self.our_sysid = int(our_sysid)
        self.our_compid = int(our_compid)
        self.expected_sysid = expected_sysid
        self.hb_timeout = float(hb_timeout)
        self.send_our_heartbeat = bool(send_our_heartbeat)

        self._writer = _WriterPort(self)
        self.mav = D.MAVLink(self._writer, self.our_sysid, self.our_compid)
        self._decoder = D.MAVLink(_NullPort(), self.our_sysid, self.our_compid)
        self._reader = FrameReader()

        self._tx_lock = threading.RLock()
        self._rx_thread: Optional[threading.Thread] = None
        self._mon_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._state: MavLinkState = "closed"
        self._state_cbs: list[Callable[[MavLinkState, str], None]] = []
        self._msg_cbs: list[Callable[[Any], None]] = []
        self._listeners: dict[str, list["queue.Queue[Any]"]] = {}
        self._all_queue: "queue.Queue[Any]" = queue.Queue(maxsize=4096)

        self.heartbeats: dict[tuple[int, int], HeartbeatInfo] = {}
        self.fc_sysid: Optional[int] = None
        self.fc_compid: Optional[int] = None
        self.statustexts: list[str] = []
        self.last_error: str = ""
        self.opened_at: float = 0.0
        self.last_rx_at: float = 0.0

    # ------------------------------------------------------------------ 生命周期
    @property
    def state(self) -> MavLinkState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state in ("open", "ready")

    def on_state(self, cb: Callable[[MavLinkState, str], None]) -> None:
        self._state_cbs.append(cb)

    def on_message(self, cb: Callable[[Any], None]) -> None:
        self._msg_cbs.append(cb)

    def _set_state(self, state: MavLinkState, detail: str = "") -> None:
        if state == self._state:
            return
        self._state = state
        log.info("链路状态 %s -> %s (%s) [%s]", self._state, state, detail, self.name)
        for cb in list(self._state_cbs):
            try:
                cb(state, detail)
            except Exception:                            # pragma: no cover
                log.exception("状态回调异常")

    def start(self) -> None:
        if self._rx_thread is not None:
            return
        self._stop.clear()
        self.opened_at = time.monotonic()
        self._set_state("open", "已连接，等待心跳")
        self._rx_thread = threading.Thread(target=self._rx_loop, name=f"mavrx-{self.name}", daemon=True)
        self._mon_thread = threading.Thread(target=self._monitor_loop, name=f"mavmon-{self.name}", daemon=True)
        self._rx_thread.start()
        self._mon_thread.start()
        if self.send_our_heartbeat:
            # 标准 GCS 行为：周期发自身 HEARTBEAT。UDP 场景下这是必需的，
            # 否则飞控侧永远学不到我们的地址（见 docs/LOG_DOWNLOAD_NOTES.md 第 4 节）。
            threading.Thread(target=self._hb_loop, name=f"mavhb-{self.name}", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        try:
            self.transport.close()
        except Exception:                                # pragma: no cover
            pass
        for t in (self._rx_thread, self._mon_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.5)
        self._rx_thread = self._mon_thread = None
        self._set_state("closed", "已断开")

    def __enter__(self) -> "MavLinkSession":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ 接收
    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            if not self.transport.is_open:
                self._set_state("lost", "传输已关闭")
                return
            try:
                data = self.transport.read(4096, timeout=0.1)
            except LinkError as exc:
                self.last_error = str(exc)
                log.warning("链路读错误 %s: %s", self.name, exc)
                self._set_state("lost", self.last_error)
                return
            except Exception as exc:                     # pragma: no cover
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("链路读未知异常")
                self._set_state("error", self.last_error)
                return
            if not data:
                continue
            self.last_rx_at = time.monotonic()
            for frame in self._reader.feed(data):
                try:
                    msgs = self._decoder.parse_buffer(frame) or []
                except Exception:                        # pragma: no cover
                    log.exception("帧解码失败 %s", frame[:16].hex(" "))
                    continue
                for msg in msgs:
                    self._dispatch(msg)

    def _dispatch(self, msg: Any) -> None:
        name = msg.get_type()
        if name == "HEARTBEAT":
            self._note_heartbeat(msg)
        elif name == "STATUSTEXT":
            try:
                text = msg.text.decode("utf-8", "replace") if isinstance(msg.text, (bytes, bytearray)) else str(msg.text)
            except Exception:                            # pragma: no cover
                text = "?"
            self.statustexts.append(text)
            del self.statustexts[:-200]
        for q in self._listeners.get(name, ()):
            try:
                q.put_nowait(msg)
            except queue.Full:                           # pragma: no cover
                pass
        if not self._all_queue.full():
            try:
                self._all_queue.put_nowait(msg)
            except queue.Full:                           # pragma: no cover
                pass
        for cb in list(self._msg_cbs):
            try:
                cb(msg)
            except Exception:                            # pragma: no cover
                log.exception("消息回调异常")

    def _note_heartbeat(self, msg: Any) -> None:
        key = (msg.get_srcSystem(), msg.get_srcComponent())
        info = self.heartbeats.get(key)
        if info is None:
            info = HeartbeatInfo(sysid=key[0], compid=key[1])
            self.heartbeats[key] = info
        info.type = msg.type
        info.autopilot = msg.autopilot
        info.base_mode = msg.base_mode
        info.custom_mode = msg.custom_mode
        info.system_status = msg.system_status
        info.mavlink_version = msg.mavlink_version
        info.count += 1
        info.last_seen = time.monotonic()
        if self.expected_sysid not in (None, key[0]):
            return
        if key[0] == self.our_sysid:
            return
        # 选飞控：优先 ArduPilot autopilot(3) 且 compid==1；否则首个非自身心跳
        if self.fc_sysid is None or (info.autopilot == D.MAV_AUTOPILOT_ARDUPILOTMEGA and key[1] == 1):
            if self.fc_sysid != key[0] or self.fc_compid != key[1]:
                self.fc_sysid, self.fc_compid = key
                log.info("识别到飞控 sysid=%d compid=%d type=%d autopilot=%d", *key, info.type, info.autopilot)
                self._set_state("ready", f"飞控在线 sysid={key[0]}")

    def _monitor_loop(self) -> None:
        while not self._stop.wait(0.5):
            if self._state in ("closed", "error", "lost"):
                return
            if not self.transport.is_open:
                self._set_state("lost", "传输已关闭")
                return
            idle = time.monotonic() - (self.last_rx_at or self.opened_at)
            if idle > self.hb_timeout:
                if self._state == "ready":
                    self._set_state("open", f"心跳静默 {idle:.1f}s，飞控离线")
                self.fc_sysid = self.fc_compid = None

    # ------------------------------------------------------------------ 订阅
    def listen(self, names: Iterable[str], *, maxsize: int = 0) -> "queue.Queue[Any]":
        """注册一个只收指定消息类型的队列；用完必须 :meth:`unlisten`。"""
        q: "queue.Queue[Any]" = queue.Queue(maxsize=maxsize)
        for n in names:
            self._listeners.setdefault(n, []).append(q)
        return q

    def unlisten(self, q: "queue.Queue[Any]") -> None:
        for lst in self._listeners.values():
            if q in lst:
                lst.remove(q)

    def next_message(self, timeout: float = 0.2) -> Any:
        """从全局队列取一条消息（主要用于调试/面板显示）。"""
        try:
            return self._all_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_for(self, name: str, *, pred: Optional[Callable[[Any], bool]] = None,
                 timeout: float = 2.0) -> Any:
        """在超时内等待某类消息；返回消息或 ``None``。"""
        q = self.listen([name])
        deadline = time.monotonic() + timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    msg = q.get(timeout=min(left, 0.25))
                except queue.Empty:
                    continue
                if pred is None or pred(msg):
                    return msg
        finally:
            self.unlisten(q)

    # ------------------------------------------------------------------ 发送
    @property
    def stats(self) -> LinkStats:
        r = self._reader
        return LinkStats(rx_frames=r.rx_frames, rx_bytes=r.rx_bytes,
                         tx_frames=self._writer.frames, tx_bytes=self._writer.bytes,
                         crc_errors=r.crc_errors, dropped_bytes=r.dropped_bytes,
                         unknown_msgs=r.unknown_msgs)

    def target_ids(self) -> tuple[int, int]:
        if self.fc_sysid is None:
            raise LinkError("尚未识别到飞控心跳（HEARTBEAT），无法发送定向请求")
        return int(self.fc_sysid), int(self.fc_compid or 1)

    def _send(self, fn: str, *args: Any) -> None:
        getter = getattr(self.mav, fn, None)
        if getter is None:                               # pragma: no cover
            raise LinkError(f"dialect 不支持 {fn}")
        with self._tx_lock:
            try:
                getter(*args)
            except LinkError:
                raise
            except Exception as exc:
                raise LinkError(f"发送 {fn} 失败: {exc}") from exc

    def send_log_request_list(self, *, start: int = 0, end: int = 0xFFFF) -> None:
        sysid, compid = self.target_ids()
        self._send("log_request_list_send", sysid, compid, start, end)

    def send_log_request_data(self, log_id: int, ofs: int, count: int) -> None:
        sysid, compid = self.target_ids()
        self._send("log_request_data_send", sysid, compid, int(log_id), int(ofs), int(count))

    def send_log_request_end(self) -> None:
        sysid, compid = self.target_ids()
        self._send("log_request_end_send", sysid, compid)

    def ping(self) -> None:
        self._send("ping_send", self.our_sysid, self.our_compid)

    def send_heartbeat_now(self) -> None:
        self._send("heartbeat_send", D.MAV_TYPE_GCS, D.MAV_AUTOPILOT_INVALID, 0, 0, D.MAV_STATE_ACTIVE, 3)

    def _hb_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.send_heartbeat_now()
            except LinkError as exc:
                log.debug("GCS 心跳发送失败（继续）: %s", exc)

    # ------------------------------------------------------------------ 摘要
    def summary(self) -> str:
        st = self.stats
        hb = "无飞控心跳" if self.fc_sysid is None else (
            f"飞控 sys={self.fc_sysid} comp={self.fc_compid} custom_mode="
            f"{self.heartbeats[(self.fc_sysid, self.fc_compid or 1)].custom_mode}")
        return (f"[{self.name}] {self._state} | {hb} | rx {st.rx_frames}帧/{st.rx_bytes}B "
                f"tx {st.tx_frames}帧 | CRC错 {st.crc_errors} | 丢弃 {st.dropped_bytes}B")

    def diagnostics(self) -> dict[str, Any]:
        return {
            "name": self.name, "state": self._state, "connected": self.connected,
            "fc": {"sysid": self.fc_sysid, "compid": self.fc_compid},
            "heartbeats": {f"{k[0]}.{k[1]}": v.as_dict() for k, v in self.heartbeats.items()},
            "stats": self.stats.as_dict(), "last_error": self.last_error,
            "statustext_last": self.statustexts[-5:],
        }


class _NullPort:
    """给解码器用的空端口（解码不需要写出）。"""

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        pass


class _WriterPort:
    """把 pymavlink 打包出的字节写进 Transport，并统计发送量。"""

    def __init__(self, sess: MavLinkSession) -> None:
        self._s = sess
        self.frames = 0
        self.bytes = 0

    def write(self, data: bytes) -> int:
        n = self._s.transport.write(bytes(data))
        self.frames += 1
        self.bytes += n
        return n

    def flush(self) -> None:
        pass
