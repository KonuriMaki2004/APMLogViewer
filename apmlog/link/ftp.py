# -*- coding: utf-8 -*-
"""MAVFTP 只读客户端（``FILE_TRANSFER_PROTOCOL`` / msg 110）。

为什么要它：``LOG_REQUEST_DATA`` 一次只回 90 字节一帧、每请求都要等飞控回一轮；
MAVFTP 一帧数据段可到 239 字节，且支持一次请求连续突发（burst）。

**报文格式以 pymavlink 的权威实现为准**（``pymavlink/mavftp.py`` 的 ``FTP_OP``）：

    偏移 0..1   seq            uint16 LE（每条请求 +1；burst 应答也会递增）
    偏移 2      session        uint8
    偏移 3      opcode         uint8（128=ACK，129=NAK，其余为操作码）
    偏移 4      size           uint8（本包数据字节数）
    偏移 5      req_opcode     uint8（应答里回填被应答的请求操作码）
    偏移 6      burst_complete uint8
    偏移 7      padding
    偏移 8..11  offset         uint32 LE
    偏移 12..   data           最多 239 字节

> 早期版本按“payload[3]=seq、数据从第 14 字节起”实现（把 3 字节目标地址前缀算进了
> payload），在 FakeFC 上自洽，**真机完全不认**（实测发出去没有任何应答）。
> 2026-10-30 用真实飞控（Copter/APM，COM94）对照上游格式改正。

写操作（Create/Write/Remove/Rename/...）**故意不实现**：本工程对飞控只读。
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

#: MAVFTP 操作码（与上游 GCS_FTP.cpp 的 FTP_OP 一致）
OP_NONE = 0
OP_TERMINATE_SESSION = 1
OP_RESET_SESSIONS = 2
OP_LIST_DIRECTORY = 3
OP_OPEN_FILE_RO = 4
OP_READ_FILE = 5
OP_CREATE_FILE = 6
OP_WRITE_FILE = 7
OP_REMOVE_FILE = 8
OP_CREATE_DIRECTORY = 9
OP_REMOVE_DIRECTORY = 10
OP_OPEN_FILE_WO = 11
OP_TRUNCATE_FILE = 12
OP_RENAME = 13
OP_CALC_CRC32 = 14
OP_BURST_READ_FILE = 15
OP_ACK = 128
OP_NAK = 129

PAYLOAD_LEN = 251
HDR_FMT = "<HBBBBBBI"
HDR_LEN = struct.calcsize(HDR_FMT)          # 12
DATA_OFF = HDR_LEN
MAX_DATA = PAYLOAD_LEN - DATA_OFF           # 239

#: 服务端错误码（FtpError）
ERR_ZH: dict[int, str] = {
    1: "失败",
    2: "失败（附带 errno）",
    3: "数据长度非法",
    4: "会话号非法/已失效",
    5: "没有可用会话",
    6: "已到文件尾",
    7: "未知命令",
    8: "文件已存在",
    9: "文件受保护",
    10: "文件不存在",
}

#: MAV_RESULT（与 MAVLink common.xml 一致）
MAV_RESULT_ACCEPTED = 0
MAV_RESULT_TEMPORARILY_REJECTED = 1
MAV_RESULT_DENIED = 2
MAV_RESULT_UNSUPPORTED = 3
MAV_RESULT_FAILED = 4
MAV_RESULT_IN_PROGRESS = 5
MAV_RESULT_CANCELLED = 6

RESULT_ZH: dict[int, str] = {
    MAV_RESULT_ACCEPTED: "已接受",
    MAV_RESULT_TEMPORARILY_REJECTED: "暂时拒绝",
    MAV_RESULT_DENIED: "拒绝",
    MAV_RESULT_UNSUPPORTED: "不支持（固件没编进这个功能）",
    MAV_RESULT_FAILED: "失败",
    MAV_RESULT_IN_PROGRESS: "进行中",
    MAV_RESULT_CANCELLED: "已取消",
}

#: 常用指令号 / 参数取值
MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN = 246
REBOOT_SHUTDOWN_ACTION_REBOOT = 1
REBOOT_SHUTDOWN_ACTION_REBOOT_TO_BOOTLOADER = 3
REBOOT_SHUTDOWN_ACTION_REBOOT_TO_MASS_STORAGE = 5

#: 日志在 SD 卡上的默认位置（ArduPilot 默认日志目录）
DEFAULT_LOG_DIR = "/APM/LOGS"


class MavFtpError(RuntimeError):
    """MAVFTP 交互失败（超时 / 服务端报错 / 协议不符）。

    ``code`` 是服务端 NAK 的错误码（无 NAK 时为 None），用来区分“会话卡住”与“文件不存在”。
    """

    def __init__(self, msg: str, code: Optional[int] = None) -> None:
        super().__init__(msg)
        self.code = code


class MavFtp:
    """极简只读 MAVFTP 客户端。

    只做三件事：打开文件、连续读、关会话。写操作**故意不实现**：本工程飞控链路
    是只读的（读日志不改日志、连飞控只读）。
    """

    def __init__(self, session: Any, *, timeout: float = 3.0) -> None:
        self.sess = session
        self.timeout = float(timeout)
        self._seq = 0
        self._lock = threading.Lock()
        self._resync_needed = False      # burst 流错位 → 需要 ResetSessions 重开

    # ---------------------------------------------------------------- 底层
    def _next_seq(self) -> int:
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFF
            return self._seq

    @staticmethod
    def _parse(p: Any) -> dict[str, Any]:
        raw = bytes(p)                      # pymavlink 可能给 list，统一成 bytes
        if len(raw) < PAYLOAD_LEN:
            raise MavFtpError(f"FILE_TRANSFER_PROTOCOL payload 只有 {len(raw)} 字节")
        seq, session, opcode, size, req_opcode, burst, _pad, offset = \
            struct.unpack_from(HDR_FMT, raw, 0)
        return {
            "seq": int(seq),
            "session": int(session),
            "opcode": int(opcode),
            "size": int(size),
            "req_opcode": int(req_opcode),
            "burst_complete": bool(burst),
            "offset": int(offset),
            "data": raw[DATA_OFF:DATA_OFF + int(size)],
        }

    def _send(self, opcode: int, *, session: int = 0, offset: int = 0,
              data: bytes = b"", size: Optional[int] = None) -> int:
        """发一条请求。

        ``size`` 是报文头里的“长度”字段：普通请求（路径/参数）= 数据字节数；
        **读请求（ReadFile/BurstReadFile）里它是“我想要多少字节”**，此时 ``data`` 为空
        （上游 pymavlink：``FTP_OP(seq, session, OP_ReadFile, length, 0, 0, offset, None)``）。
        """
        send = getattr(self.sess, "send_file_transfer_protocol", None)
        if send is None:                                     # pragma: no cover
            raise MavFtpError("当前链路不支持 FILE_TRANSFER_PROTOCOL")
        if len(data) > MAX_DATA:
            raise MavFtpError(f"单包数据 {len(data)} 字节超过上限 {MAX_DATA}")
        n = len(data) if size is None else int(size)
        seq = self._next_seq()
        payload = bytearray(PAYLOAD_LEN)
        struct.pack_into(HDR_FMT, payload, 0, seq, int(session) & 0xFF,
                         int(opcode) & 0xFF, n & 0xFF, 0, 0, 0,
                         int(offset) & 0xFFFFFFFF)
        payload[DATA_OFF:DATA_OFF + len(data)] = data
        send(bytes(payload))
        return seq

    def _await(self, opcode: int, *, session: int = 0, offset: int = 0,
               data: bytes = b"", size: Optional[int] = None,
               timeout: Optional[float] = None) -> dict[str, Any]:
        """发一条请求并等第一条匹配的应答（数据包/ACK；NAK 抛异常）。"""
        limit = self.timeout if timeout is None else float(timeout)
        q = self.sess.listen(["FILE_TRANSFER_PROTOCOL"])
        try:
            self._send(opcode, session=session, offset=offset, data=data, size=size)
            deadline = time.monotonic() + limit
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise MavFtpError(f"MAVFTP 请求超时（opcode={opcode}, offset={offset}）")
                try:
                    m = q.get(timeout=min(left, 0.2))
                except Exception:                            # queue.Empty
                    continue
                d = self._parse(getattr(m, "payload", b""))
                if d["req_opcode"] not in (0, opcode):
                    continue                                  # 上一轮迟到的包
                if session and d["session"] not in (0, session):
                    continue
                if d["opcode"] == OP_NAK:
                    raise MavFtpError(self._nack_text(d, opcode),
                                      int(d["data"][0]) if d["data"] else None)
                if d["opcode"] not in (OP_ACK, opcode):
                    continue
                return d
        finally:
            self.sess.unlisten(q)

    @staticmethod
    def _nack_text(d: dict[str, Any], opcode: int) -> str:
        code = int(d["data"][0]) if d["data"] else 0
        return f"MAVFTP 服务端拒绝 opcode={opcode}（错误码 {code}：{ERR_ZH.get(code, '未知')}）"

    def _drain(self, q: Any, session: int, *, quiet: float = 0.4,
               hard: float = 2.0) -> int:
        """把这一轮没消费完的 burst 包读掉。

        不为取数据，只为了把链路清干净：ArduPilot 的 burst 会连续突一整轮，
        中途丢下不管会让下一包请求排在它后面（真机实测直接导致 OpenFileRO 超时）。
        返回丢掉的包数。
        """
        dropped = 0
        end = time.monotonic() + hard
        while time.monotonic() < end:
            try:
                m = q.get(timeout=quiet)
            except Exception:                            # queue.Empty → 安静了，收工
                break
            try:
                d = self._parse(getattr(m, "payload", b""))
            except MavFtpError:
                continue
            if d["session"] not in (0, session):
                continue
            dropped += 1
            if d["burst_complete"]:
                break
        return dropped

    def _burst(self, session: int, offset: int, size: int,
               timeout: Optional[float] = None) -> list[dict[str, Any]]:
        """BurstReadFile：一次请求收多包，直到服务端置 burst_complete 或安静下来。

        实测（COM94 + Copter/APM，2026-10-30）：应答的 ``seq`` 逐包递增，不能用
        ``seq == 请求seq`` 过滤；而且会对一个正在运行的流**继续往后突**（新请求的
        offset 不一定马上生效）。所以这里不做连续性断言，只把“这一轮能收到的包”按
        offset 去重后原样交给上层，由 ``fetch`` 按 offset 就位、缺哪儿再要哪儿。
        """
        limit = self.timeout if timeout is None else float(timeout)
        q = self.sess.listen(["FILE_TRANSFER_PROTOCOL"])
        try:
            self._send(OP_BURST_READ_FILE, session=session, offset=offset,
                       data=b"", size=int(size))
            packets: list[dict[str, Any]] = []
            seen: set[int] = set()
            deadline = time.monotonic() + limit
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    m = q.get(timeout=min(left, 0.2))
                except Exception:                            # queue.Empty
                    if packets:                              # 已经收到东西，收尾即可
                        break
                    continue
                d = self._parse(getattr(m, "payload", b""))
                if d["session"] not in (0, session):
                    continue
                # 真机实测：应答把 req_opcode 回填为 15（burst）；用这个把别类应答挡掉
                if d["req_opcode"] not in (0, OP_BURST_READ_FILE):
                    continue
                if d["opcode"] == OP_NAK:
                    raise MavFtpError(self._nack_text(d, OP_BURST_READ_FILE),
                                      int(d["data"][0]) if d["data"] else None)
                if d["opcode"] != OP_ACK or not d["data"]:
                    continue
                if d["offset"] in seen:
                    continue
                seen.add(d["offset"])
                packets.append(d)
                deadline = time.monotonic() + limit         # 有进展就续期
                if d["burst_complete"]:
                    break
            self._drain(q, session)
            return packets
        finally:
            self.sess.unlisten(q)

    # ---------------------------------------------------------------- 会话
    def open_ro(self, path: str) -> tuple[int, int]:
        """打开只读文件，返回 ``(session, size)``。"""
        d = self._await(OP_OPEN_FILE_RO, data=path.encode("utf-8") + b"\x00")
        if len(d["data"]) < 4:
            raise MavFtpError(f"OpenFileRO 应答没有文件长度（path={path}）")
        size = int(struct.unpack_from("<I", d["data"], 0)[0])
        # 实测：ArduPilot 的会话号从 0 开始，0 是合法会话，不能当成“没给会话”
        return int(d["session"]), size

    def read(self, session: int, offset: int, size: int = MAX_DATA) -> dict[str, Any]:
        """单包读取：头部 ``size`` 字段就是“要多少字节”（上限 239）。"""
        return self._await(OP_READ_FILE, session=session, offset=offset,
                           size=min(int(size), MAX_DATA))

    def burst_read(self, session: int, offset: int,
                   size: int = MAX_DATA) -> list[dict[str, Any]]:
        """BurstReadFile：一次请求收多包（服务端在最后一包置 burst_complete）。"""
        return self._burst(session, offset, min(int(size), MAX_DATA))

    def _burst_with_retry(self, session: int, offset: int,
                          retries: int = 3) -> list[dict[str, Any]]:
        """失败重试一次 burst（真机上偶发丢包/超时不应该整个下载失败）。"""
        last: Optional[MavFtpError] = None
        for _attempt in range(max(1, int(retries))):
            try:
                return self.burst_read(session, offset)
            except MavFtpError as exc:
                last = exc
                log.debug("burst 重试（offset=%s）：%s", offset, exc)
        assert last is not None
        raise last

    def reset_sessions(self) -> None:
        """清掉服务端所有会话。

        真机实测（COM94，2026-10-30）：上一次没关干净的会话会把新连接卡在
        ``NAK 错误码 1``；``ResetSessions`` 一下就恢复。只在打开文件报
        1/4/5 类错误时用，不当成常规步骤（它会影响同链路上的其它客户端）。
        """
        try:
            self._await(OP_RESET_SESSIONS, timeout=min(self.timeout, 2.0))
        except MavFtpError:
            log.debug("ResetSessions 没有应答，忽略")

    def terminate(self, session: int) -> None:
        """关掉会话；服务端不回应也算成功（会话会自己超时回收）。"""
        try:
            self._await(OP_TERMINATE_SESSION, session=session, timeout=min(self.timeout, 1.5))
        except MavFtpError:
            log.debug("TerminateSession 没有应答（session=%s），忽略", session)

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        """列目录（不递归）：``[{'name','dir','size'}]``。

        服务端按页返回，条目格式 ``<D|F><名称>\\t<字节数>\\x00``；
        一页返回不完整时用累计字节数作为 offset 继续要。
        """
        out: list[dict[str, Any]] = []
        offset = 0
        for _page in range(64):
            d = self._await(OP_LIST_DIRECTORY, offset=offset,
                            data=path.encode("utf-8") + b"\x00")
            chunk = d["data"]
            if not chunk:
                break
            for text in chunk.split(b"\x00"):
                if not text:
                    continue
                s = text.decode("utf-8", "replace")
                if len(s) < 2:
                    continue
                kind = s[0].upper()
                name, _, size_s = s[1:].partition("\t")
                try:
                    size = int(size_s)
                except ValueError:
                    size = 0
                out.append({"name": name, "dir": kind == "D", "size": size})
            offset += len(chunk)
            if len(chunk) < MAX_DATA:
                break
        return out

    # ---------------------------------------------------------------- 整文件
    def fetch(self, remote: str, local: str | Path, *,
              progress: Optional[Callable[[int, int, float], None]] = None,
              cancel: Optional[Callable[[], bool]] = None,
              retries: int = 3) -> dict[str, Any]:
        """把 ``remote`` 整个读到 ``local``（先写 ``.part``，完成再改名）。

        返回 ``{'path','bytes','elapsed','rate'}``（rate 单位 B/s）；失败抛 :class:`MavFtpError`。
        """
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        part = local.with_name(local.name + ".part")
        session = 0
        opened = False
        written = 0
        total = 0
        buf: Optional[bytearray] = None
        got = 0
        t0 = time.perf_counter()
        try:
            try:
                session, total = self.open_ro(remote)
            except MavFtpError as exc:
                # 上一次没关干净的会话会让新连接直接 NAK 1/4/5；清一下就活
                if exc.code not in (1, 4, 5):
                    raise
                self.reset_sessions()
                session, total = self.open_ro(remote)
            opened = True
            if total <= 0:
                raise MavFtpError(f"{remote} 长度为 0")
            part.write_bytes(b"")               # 先占位：中途取消也能看到 .part 断点
            buf = bytearray(total)
            have = bytearray(total)              # 每个字节“收到了没”
            got = 0
            def place(packets: list[dict[str, Any]]) -> int:
                """把收到的包按 offset 就位，返回本轮最远到达的位置。"""
                far = -1
                nonlocal got
                for p in packets:
                    off = int(p["offset"])
                    data = p["data"]
                    if off >= total or not data:
                        continue                 # 越过文件尾 / 空包
                    end = min(total, off + len(data))
                    buf[off:end] = data[:end - off]
                    have[off:end] = b"\x01" * (end - off)
                    far = max(far, end)
                got = have.count(1)
                return far

            def report() -> None:
                nonlocal written
                written = got
                if progress is not None:
                    dt = max(1e-6, time.perf_counter() - t0)
                    progress(written, total, written / dt)

            # 阶段一：顺着服务端的流往前推。
            # 真机实测：服务端会对**正在跑的流**继续往后突，新请求的 offset 不一定生效，
            # 所以这里不能“从第一个缺口要”，而要跟着它跑，跑到文件尾。
            next_off = 0
            stalled = 0
            for _round in range(4096):
                if cancel is not None and cancel():
                    raise MavFtpError("已取消")
                if next_off >= total:
                    break
                packets = self._burst_with_retry(session, next_off, retries)
                far = place(packets)
                report()
                if far > next_off:
                    next_off = far
                    stalled = 0
                else:
                    stalled += 1
                    if stalled >= 3:
                        break

            # 阶段二：补洞。流已经停了，这时新请求的 offset 才会被采纳。
            for _round in range(512):
                if cancel is not None and cancel():
                    raise MavFtpError("已取消")
                gap = have.find(0)
                if gap < 0:
                    break
                packets = self._burst_with_retry(session, gap, retries)
                if place(packets) < 0:
                    stalled += 1
                    if stalled >= 3:
                        break
                report()

            if written != total:
                raise MavFtpError(f"长度不符：期望 {total}，实际 {written}")
            part.write_bytes(bytes(buf))
            part.replace(local)
        finally:
            ok = bool(opened and total > 0 and got >= total)
            if opened:
                try:
                    self.terminate(session)
                except Exception:                            # pragma: no cover
                    log.debug("关会话失败", exc_info=True)
            if opened and not ok:
                if buf is not None and got:
                    try:
                        part.write_bytes(bytes(buf))         # 留个断点（下次可以看进度）
                    except Exception:                        # pragma: no cover
                        log.debug("写 .part 失败", exc_info=True)
                # 半途失败时飞控可能还在突旧流，必须清干净，否则下一次请求直接超时
                self.reset_sessions()
        elapsed = max(1e-6, time.perf_counter() - t0)
        return {"path": str(local), "bytes": written, "elapsed": elapsed,
                "rate": written / elapsed}
