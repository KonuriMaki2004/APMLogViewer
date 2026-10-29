"""apmlog.link.transport —— MAVLink 传输抽象与实现。

约定（全部实现必须遵守）：
* ``read(n, timeout)`` 只由**单一读者线程**调用；返回可能少于 n 字节；``b""`` 表示超时短读，不是错误。
* ``write(data)`` 必须允许多线程并发（内部加锁）。
* 任何底层异常统一包成 :class:`LinkError`，并保留 ``__cause__``。
* 关闭必须幂等。

支持的目标类型（详见 :mod:`apmlog.link.endpoint` 的目标串语法）：
串口 / TCP 客户端 / TCP 服务端 / UDP 本地监听 / UDP 出站 / 内存管道（自测用）。
"""
from __future__ import annotations

import abc
import errno
import logging
import queue
import socket
import threading
import time
from typing import Optional

log = logging.getLogger("apmlog.link.transport")

DEFAULT_READ_TIMEOUT = 0.20       # 单次 read 阻塞上限（秒）

#: 本机回环在高负载下会**瞬时**出现的 Winsock 错误码：
#: 10053 软件造成连接中止 / 10054 远程主机强迫关闭 / 10061 拒绝连接（监听还没完全起来）。
#: 它们不是程序错误：连接侧应有限重试，写侧应按“对端已走”收尾。
TRANSIENT_WINSOCK: tuple[int, ...] = (10053, 10054, 10061)


def is_transient_socket_error(exc: BaseException) -> bool:
    """判断 socket 异常是否属于“瞬时错误 / 对端已断开”（可重试或安静收尾）。"""
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError)):
        return True
    for code in (getattr(exc, "winerror", None), getattr(exc, "errno", None)):
        if code is None:
            continue
        if code in TRANSIENT_WINSOCK or code in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE):
            return True
    return False


def connect_with_retry(host: str, port: int, *, timeout: float = 5.0,
                       retries: int = 3, delay: float = 0.05) -> socket.socket:
    """带超时轮询与有限重试的 TCP 连接（默认 3 次、间隔 50ms）。

    只在 :func:`is_transient_socket_error` 认定的瞬时错误上重试；其它错误直接抛出。
    """
    last: Optional[BaseException] = None
    tries = max(1, int(retries))
    for attempt in range(1, tries + 1):
        try:
            return socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as exc:
            last = exc
            if not is_transient_socket_error(exc) or attempt >= tries:
                break
            log.debug("TCP 连接 %s:%d 第 %d 次失败（%s），%dms 后重试", host, port, attempt, exc,
                      int(delay * 1000))
            time.sleep(delay)
    raise LinkError(f"TCP 连接失败 {host}:{port}（共尝试 {tries} 次）: {last}") from last


class LinkError(RuntimeError):
    """链路层错误（含依赖缺失、连接失败、对端断开）。"""


class Transport(abc.ABC):
    """字节管道抽象。"""

    name: str = "transport"

    def __init__(self) -> None:
        self._closed = False
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    @property
    def is_open(self) -> bool:
        return not self._closed

    def close(self) -> None:                     # pragma: no cover - 抽象
        raise NotImplementedError

    # ------------------------------------------------------------------ IO
    @abc.abstractmethod
    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        ...

    def write(self, data: bytes) -> int:
        with self._write_lock:
            return self._write(data)

    @abc.abstractmethod
    def _write(self, data: bytes) -> int:
        ...

    # ------------------------------------------------------------------ 工具
    def read_at_least(self, n: int, deadline: float) -> bytes:
        """读到 n 字节或超时为止（返回已读到的部分）。"""
        out = bytearray()
        while len(out) < n and time.monotonic() < deadline:
            chunk = self.read(n - len(out), timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            if chunk:
                out += chunk
        return bytes(out)


# --------------------------------------------------------------------------- #
# 串口
# --------------------------------------------------------------------------- #


class SerialTransport(Transport):
    """COM 口。依赖 pyserial —— 本 venv **未安装**，因此是可选依赖。"""

    def __init__(self, port: str, baud: int = 57600, *, read_timeout: float = DEFAULT_READ_TIMEOUT) -> None:
        try:
            import serial  # type: ignore
        except Exception as exc:                      # pragma: no cover
            raise LinkError(
                "串口连接需要 pyserial，但当前 venv 未安装。请执行："
                r'  .\.venv\Scripts\python.exe -m pip install pyserial'
            ) from exc
        super().__init__()
        self._serial = serial
        self.port = port
        self.baud = int(baud)
        self.name = f"serial:{port}@{self.baud}"
        try:
            # Windows 下 COM10+ 需要 \\.\COM10 形式，serial_for_url 已处理
            self._s = serial.serial_for_url(port, baudrate=self.baud, timeout=read_timeout)
        except Exception as exc:
            raise LinkError(f"打开串口失败 {port}@{self.baud}: {exc}") from exc
        log.info("串口已打开 %s", self.name)

    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        if self._closed:
            return b""
        try:
            if timeout is not None:
                self._s.timeout = max(0.001, timeout)
            data = self._s.read(n)
        except Exception as exc:                       # pragma: no cover
            if self._closed:
                return b""
            raise LinkError(f"串口读失败: {exc}") from exc
        return bytes(data) if data else b""

    def _write(self, data: bytes) -> int:
        if self._closed:
            raise LinkError("串口已关闭")
        try:
            return int(self._s.write(data))
        except Exception as exc:                       # pragma: no cover
            raise LinkError(f"串口写失败: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._s.close()
        except Exception:                              # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# TCP
# --------------------------------------------------------------------------- #


class _SocketStreamTransport(Transport):
    """基于流式 socket 的公共实现。"""

    def __init__(self, sock: socket.socket, name: str) -> None:
        super().__init__()
        self._sock = sock
        self.name = name
        self._sock.settimeout(DEFAULT_READ_TIMEOUT)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:                                # pragma: no cover
            pass

    @property
    def peer(self) -> str:
        try:
            return "%s:%d" % self._sock.getpeername()[:2]
        except OSError:
            return "?"

    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        if self._closed:
            return b""
        try:
            if timeout is not None:
                self._sock.settimeout(max(0.001, timeout))
            data = self._sock.recv(max(1, n))
            if not data:
                # 对端正常关闭
                self._closed = True
                raise LinkError(f"对端已断开: {self.name}")
            return data
        except socket.timeout:
            return b""
        except LinkError:
            raise
        except OSError as exc:
            if self._closed or exc.errno in (errno.EBADF, errno.ECONNRESET, errno.ENOTCONN, errno.ESHUTDOWN):
                self._closed = True
                err = LinkError(f"连接已断开: {self.name}")
                err.transient = True                      # type: ignore[attr-defined]
                raise err from exc
            raise LinkError(f"TCP 读失败: {exc}") from exc

    def _write(self, data: bytes) -> int:
        if self._closed:
            raise LinkError("连接已关闭")
        try:
            return int(self._sock.sendall(data) or len(data))
        except OSError as exc:
            self._closed = True
            err = LinkError(f"TCP 写失败: {exc}")
            # 带上标记，让上层（如 FakeFC）能把“客户端已取消而关闭连接”当正常收尾，
            # 而不是当成错误把异常抛出服务线程。
            err.transient = is_transient_socket_error(exc)   # type: ignore[attr-defined]
            raise err from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:                                # pragma: no cover
            pass


class TcpClientTransport(_SocketStreamTransport):
    """主动连接 ``host:port``（数传模块、 mav2tcp、 飞控 WiFi 透传等）。"""

    def __init__(self, host: str, port: int, *, connect_timeout: float = 5.0,
                 retries: int = 3, retry_delay: float = 0.05) -> None:
        try:
            s = connect_with_retry(host, port, timeout=connect_timeout,
                                   retries=retries, delay=retry_delay)
        except LinkError:
            raise
        except OSError as exc:
            raise LinkError(f"TCP 连接失败 {host}:{port}: {exc}") from exc
        super().__init__(s, f"tcp:{host}:{port}")
        log.info("TCP 客户端已连接 %s peer=%s", self.name, self.peer)


class TcpServerTransport(_SocketStreamTransport):
    """监听 ``bind:port``，接受**一个**客户端（用于飞控作 TCP 客户端的反向场景与自测）。

    :param wait: 等待客户端接入的秒数。
    """

    def __init__(self, bind: str = "127.0.0.1", port: int = 0, *, wait: float = 10.0,
                 backlog: int = 1) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((bind, int(port)))
            srv.listen(backlog)
        except OSError as exc:
            raise LinkError(f"TCP 监听失败 {bind}:{port}: {exc}") from exc
        srv.settimeout(min(wait, 5.0))
        sock = None
        deadline = time.monotonic() + wait
        while sock is None:
            left = deadline - time.monotonic()
            if left <= 0:
                srv.close()
                raise LinkError(f"TCP 服务端等待客户端超时（{wait}s） on {bind}:{srv.getsockname()[1]}")
            try:
                srv.settimeout(max(0.05, min(1.0, left)))
                sock, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                # 高负载下 accept 可能瞬时报 10053/10054：只要还没到 deadline 就继续轮询。
                if is_transient_socket_error(exc) and time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                srv.close()
                raise LinkError(f"TCP accept 失败: {exc}") from exc
        local_port = srv.getsockname()[1]
        srv.close()
        super().__init__(sock, f"tcpsrv:{bind}:{local_port}")
        log.info("TCP 服务端已接受连接 %s peer=%s", self.name, self.peer)

    @property
    def bound_port(self) -> int:
        return int(self._sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# UDP
# --------------------------------------------------------------------------- #


class UdpTransport(Transport):
    """本地绑定端口收发 UDP。第一个收到的源自动成为回复目标（MAVProxy 风格）。

    :param reconnect_grace: 对端静默超过该秒数后，允许新的源接管目标地址。
    """

    def __init__(self, bind: str = "0.0.0.0", port: int = 14550, *,
                 peer: Optional[tuple[str, int]] = None, read_timeout: float = DEFAULT_READ_TIMEOUT) -> None:
        super().__init__()
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((bind, int(port)))
            self._sock.settimeout(read_timeout)
        except OSError as exc:
            raise LinkError(f"UDP 绑定失败 {bind}:{port}: {exc}") from exc
        self._peer: Optional[tuple[str, int]] = tuple(peer) if peer else None
        self._last_rx = 0.0
        self.bound_port = int(self._sock.getsockname()[1])
        self.name = f"udp:{bind}:{self.bound_port}"
        log.info("UDP 已监听 %s peer=%s", self.name, self._peer)

    @property
    def peer(self) -> Optional[tuple[str, int]]:
        return self._peer

    def set_peer(self, peer: tuple[str, int]) -> None:
        self._peer = tuple(peer)

    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        if self._closed:
            return b""
        try:
            if timeout is not None:
                self._sock.settimeout(max(0.001, timeout))
            data, addr = self._sock.recvfrom(max(1, n))
        except socket.timeout:
            return b""
        except OSError as exc:
            if self._closed:
                return b""
            raise LinkError(f"UDP 读失败: {exc}") from exc
        now = time.monotonic()
        if self._peer is None or (now - self._last_rx > 30.0 and addr != self._peer):
            self._peer = addr                     # 自动学习对端
        self._last_rx = now
        return data

    def _write(self, data: bytes) -> int:
        if self._closed:
            raise LinkError("UDP 已关闭")
        if self._peer is None:
            raise LinkError("UDP 尚无对端地址（未收到任何包，且未指定 udpout 目标）")
        try:
            self._sock.sendto(data, self._peer)
            return len(data)
        except OSError as exc:                   # pragma: no cover
            raise LinkError(f"UDP 写失败: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:                          # pragma: no cover
            pass


class UdpOutTransport(UdpTransport):
    """固定目标 ``host:port`` 出站（本地端口随机）。"""

    def __init__(self, host: str, port: int, *, bind: str = "0.0.0.0") -> None:
        super().__init__(bind, 0, peer=(host, int(port)))
        self.name = f"udpout:{host}:{port}"


# --------------------------------------------------------------------------- #
# 内存管道（无硬件自测）
# --------------------------------------------------------------------------- #


class MemoryTransport(Transport):
    """线程安全的内存字节管道。``create_pair()`` 返回互联的两端。"""

    def __init__(self, in_q: "queue.Queue[bytes]", out_q: "queue.Queue[bytes]", name: str) -> None:
        super().__init__()
        self._in = in_q
        self._out = out_q
        self._pending = bytearray()
        self.name = name

    @staticmethod
    def create_pair(label: str = "pipe") -> tuple["MemoryTransport", "MemoryTransport"]:
        a2b: "queue.Queue[bytes]" = queue.Queue()
        b2a: "queue.Queue[bytes]" = queue.Queue()
        return (MemoryTransport(b2a, a2b, f"{label}-A"), MemoryTransport(a2b, b2a, f"{label}-B"))

    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        if not self._pending:
            try:
                chunk = self._in.get(timeout=timeout if timeout is not None else DEFAULT_READ_TIMEOUT)
            except queue.Empty:
                return b""
            if chunk is None:                     # 关闭哨兵
                self._closed = True
                return b""
            self._pending.extend(chunk)
        take = min(max(1, n), len(self._pending))
        out = bytes(self._pending[:take])
        del self._pending[:take]
        return out

    def _write(self, data: bytes) -> int:
        if self._closed:
            raise LinkError("管道已关闭")
        self._out.put(bytes(data))
        return len(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._out.put(None)                       # type: ignore[arg-type]


__all__ = [
    "LinkError", "Transport", "SerialTransport",
    "TcpClientTransport", "TcpServerTransport", "UdpTransport", "UdpOutTransport",
    "MemoryTransport", "DEFAULT_READ_TIMEOUT",
]
