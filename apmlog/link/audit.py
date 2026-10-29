"""apmlog.link.audit —— 本端发送内容的只读审计（真机安全护栏）。

用途：把 GCS 侧**实际发到链路上的字节**逐帧反解，统计每种 msgid 的发送次数与字节数，
并按"写操作/控制/固件"名单判定这次会话是否**严格只读**。

实现要点：
* 不修改任何行为，只是在 :class:`~apmlog.link.transport.Transport` 外面套一层 tee；
* 复用 :class:`~apmlog.link.endpoint.FrameReader`（同一套严格 CRC 分帧），因此统计的是
  真上线的字节，而不是"打算发送的调用"；
* 禁发名单按 **dialect 里的消息名**动态解析，避免硬编码 msgid 记错。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Optional

from pymavlink.dialects.v20 import ardupilotmega as D

from .endpoint import FrameReader
from .transport import Transport

__all__ = ["FORBIDDEN_PATTERNS", "ALLOWED_PATTERNS", "TxAudit", "AuditTransport", "audit_summary"]

#: 任何一条命中即视为"非只读"，真机日志下载绝不该出现
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "PARAM_SET", "PARAM_WRITE_*", "FTPARAM_*",
    "COMMAND_LONG", "COMMAND_INT", "COMMAND_ACK", "COMMAND_CANCEL",
    "SET_MODE", "COMPONENT_ARM_DISARM", "MAV_RUN_NAV_COMMANDS",
    "MISSION_*", "NAV_CONTROLLER_OUTPUT_REQUEST",
    "FTP_PACKET", "FILE_TRANSFER_PROTOCOL", "OPREPORT_*",
    "FLASH_*", "MAV_INSTALL_*", "REBOOT", "SAFETY_SET_ALLOWED_AREA",
    "ROI_*", "GUIDED_*", "DO_*", "BUTTON_*", "CONTROL_*",
    "SET_GPS_GLOBAL_ORIGIN", "MOUNT_*", "CAMERA_*", "GIMBAL_*",
    "IMAGE_START_CAPTURE", "VIDEO_*", "LOG_ERASE", "LOG_REQUEST_LIST_ACK",
)

#: 本工作流允许出现的发送（白名单，用于报告里区分"预期内"与"意外"）
ALLOWED_PATTERNS: tuple[str, ...] = (
    "HEARTBEAT", "PING", "LOG_REQUEST_LIST", "LOG_REQUEST_DATA", "LOG_REQUEST_END",
    "LOG_REQUEST_PARTIAL_LIST", "AUTOPILOT_VERSION_REQUEST", "STATUSTEXT_LOCAL_STORAGE",
    "PROTOCOL_VERSION", "SCA_DEPRESSION",  # 仅用于统计，不参与判定
    # 日志写入检测的只读探测：它不写任何东西，归在“预期内”而不是“意外”，
    # 否则每次连飞控的报告里都挂一条噪声，真正的意外就被淹掉了。
    "PARAM_REQUEST_READ",
)


def _id_to_name() -> dict[int, str]:
    out: dict[int, str] = {}
    for mid, cls in D.mavlink_map.items():
        out[int(mid)] = str(getattr(cls, "msgname", "") or cls.__name__)
    return out


_ID2NAME = _id_to_name()


def _match(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


@dataclass
class TxAudit:
    """一次会话的发送统计快照。"""

    counts: dict[str, int] = field(default_factory=dict)          # msgname -> 帧数
    bytes_: dict[str, int] = field(default_factory=dict)          # msgname -> 字节数
    unknown: dict[int, int] = field(default_factory=dict)         # 无法命名的 msgid -> 帧数
    crc_errors: int = 0
    total_frames: int = 0
    total_bytes: int = 0

    @property
    def forbidden(self) -> dict[str, int]:
        return {n: c for n, c in self.counts.items() if _match(n, FORBIDDEN_PATTERNS)}

    @property
    def unexpected(self) -> dict[str, int]:
        """既不在白名单也不在禁发名单里的发送（需要人工看一眼）。"""
        return {n: c for n, c in self.counts.items()
                if not _match(n, ALLOWED_PATTERNS) and not _match(n, FORBIDDEN_PATTERNS)}

    def is_read_only(self) -> bool:
        return not self.forbidden and not self.unknown and self.crc_errors == 0

    def table(self) -> str:
        if not self.counts:
            return "(本端未发送任何可解析的 MAVLink 帧)"
        lines = [f"{'msgid':>6}  {'消息名':<28} {'帧数':>8} {'字节':>10}  归类"]
        for name, cnt in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            mid = next((str(i) for i, n in _ID2NAME.items() if n == name), "?")
            tag = ("允许(只读)" if _match(name, ALLOWED_PATTERNS) else
                   "!! 禁发" if _match(name, FORBIDDEN_PATTERNS) else "意外")
            lines.append(f"{mid:>6}  {name:<28} {cnt:>8} {self.bytes_.get(name, 0):>10}  {tag}")
        for mid, cnt in self.unknown.items():
            lines.append(f"{mid:>6}  {'<dialect 未知>':<28} {cnt:>8} {'-':>10}  !! 未知")
        lines.append(f"{'':>6}  {'合计':<28} {self.total_frames:>8} {self.total_bytes:>10}"
                     f"  CRC 错帧 {self.crc_errors}")
        lines.append("只读判定：" + ("通过（无 PARAM_SET / COMMAND_* / FTP / 固件类发送）"
                                    if self.is_read_only() else "**不通过**"))
        return "\n".join(lines)


class AuditTransport(Transport):
    """包住任意 Transport，统计**写出去**的字节里的每一帧。"""

    def __init__(self, inner: Transport) -> None:
        super().__init__()
        self.inner = inner
        self.name = f"audit+{inner.name}"
        self._reader = FrameReader()
        self._counts: dict[str, int] = {}
        self._bytes: dict[str, int] = {}
        self._unknown: dict[int, int] = {}
        self._rx_reader = FrameReader()          # 顺便统计链路上收到的帧类型
        self._rx_counts: dict[str, int] = {}

    # -- 读侧透传（不改行为），同时统计收到的消息类型
    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        data = self.inner.read(n, timeout)
        if data:
            for fr in self._rx_reader.feed(data):
                mid = self._msgid_of(fr)
                name = _ID2NAME.get(mid, f"?{mid}")
                self._rx_counts[name] = self._rx_counts.get(name, 0) + 1
        return data

    def _write(self, data: bytes) -> int:
        n = self.inner.write(data)          # 走内层自己的写锁，不改变行为
        for fr in self._reader.feed(bytes(data)):
            mid = self._msgid_of(fr)
            name = _ID2NAME.get(mid)
            if name is None:
                self._unknown[mid] = self._unknown.get(mid, 0) + 1
                continue
            self._counts[name] = self._counts.get(name, 0) + 1
            self._bytes[name] = self._bytes.get(name, 0) + len(fr)
        return n

    @staticmethod
    def _msgid_of(frame: bytes) -> int:
        if frame[0] == 0xFD:
            return frame[7] | (frame[8] << 8) | (frame[9] << 16)
        return frame[5]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.inner.close()

    # -- 结果
    def snapshot(self) -> TxAudit:
        r = self._reader
        return TxAudit(counts=dict(self._counts), bytes_=dict(self._bytes), unknown=dict(self._unknown),
                       crc_errors=r.crc_errors, total_frames=r.rx_frames, total_bytes=r.rx_bytes)

    def rx_summary(self) -> dict[str, int]:
        return dict(sorted(self._rx_counts.items(), key=lambda kv: -kv[1]))


def audit_summary(t: AuditTransport) -> str:
    return t.snapshot().table()
