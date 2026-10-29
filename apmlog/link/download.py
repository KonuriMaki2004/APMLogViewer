"""apmlog.link.download —— 飞控日志列表与分块下载（.BIN）。

线语义完全来自本机真实 tlog 的 CRC 严格解析（见 docs/LOG_DOWNLOAD_NOTES.md 第 3 节）：

* ``LOG_REQUEST_LIST(target_system, target_component, start, end)``，实测 ``start=0, end=65535``；
* ``LOG_ENTRY(id, num_logs, last_log_num, time_utc, size)``，本机 dialect 打包格式 ``<IIHHH``
  （线序 time_utc,size,id,num_logs,last_log_num）；实测 131 条：
  ``{'id':1,'num_logs':131,'last_log_num':131,'time_utc':315532800,'size':819200}`` 等，
  **只有这 5 个字段**，``time_utc=315532800``(1980) 是 RTC 未设置的占位值；
* ``LOG_REQUEST_DATA(target_system, target_component, id, ofs, count)``，实测
  ``{'id':125,'ofs':1207800,'count':180}``、``{'ofs':2017710,'count':270}``、``{'ofs':0,'count':4294967295}``；
* ``LOG_DATA(id, ofs, count, data[90])``，打包格式 ``<IHBB`` + 90B payload，实测 **1 290 836** 帧，
  每帧 ``data`` 长度恒为 90、``ofs`` 从 0 起严格 +90 递增、最大 ofs=30244860。

由此确定的两条硬结论（本模块的实现依据）：

1. **``ofs`` 是字节偏移，不是块号**；``count`` 是字节数（90 的倍数）。
   若 ofs 是块号，2017710 块 ≈ 181MB，而同一日志实测最大字节偏移只有 30244860B，矛盾。
2. **完整性只以 ``LOG_ENTRY.size`` 为唯一目标**（915116 % 90 = 56 → 尾块是半帧，按 size 截断），
   不使用任何 ``num_blocks``/``address_offset``/``length`` 字段（本机 dialect 里根本没有这些字段）。

断点续传：``<name>.part`` + 旁车 ``<name>.part.json``（记录 log_id/size/chunk/已完成字节区间/统计）。
完成落盘命名：``%08d.BIN``（ArduPilot 的 ``NNNNNNNN.BIN`` 习惯，编号取 ``LOG_ENTRY.id``）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from pymavlink.dialects.v20 import ardupilotmega as D

from .endpoint import MavLinkSession
from .transport import LinkError

log = logging.getLogger("apmlog.link.download")

__all__ = [
    "BLOCK_BYTES", "RemoteLog", "list_logs", "DownloadResult", "download_log",
    "default_download_dir", "final_name",
]

BLOCK_BYTES = 90                       # 实测每帧 LOG_DATA 恒定 90 字节
UTC_PLACEHOLDER = 315532800            # 1980-01-01，实测 RTC 未设置时的占位值
PROGRESS = Optional[Callable[[int, int, float, str], None]]   # (done, total, bytes_per_sec, msg)
CANCEL = Optional[Callable[[], bool]]


# --------------------------------------------------------------------------- #
# 目录与命名
# --------------------------------------------------------------------------- #


def default_download_dir() -> Path:
    """默认下载目录（绝不写入用户的日志目录）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APMLOG_CACHE")
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    return root / "APMLogViewer" / "downloads"


def final_name(log_id: int, source_name: str = "") -> str:
    """ArduPilot 风格 ``NNNNNNNN.BIN``。"""
    return f"{int(log_id):08d}.BIN"


# --------------------------------------------------------------------------- #
# LOG_ENTRY
# --------------------------------------------------------------------------- #


@dataclass
class RemoteLog:
    """一条 LOG_ENTRY。"""

    log_id: int
    num_logs: int
    last_log_num: int
    time_utc: int
    size: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_rtc(self) -> bool:
        """飞控 RTC 是否可信（实测大量日志为 1980 占位）。"""
        return self.time_utc > UTC_PLACEHOLDER + 86400

    @property
    def created(self) -> Optional[datetime]:
        if not self.has_rtc:
            return None
        try:
            return datetime.fromtimestamp(self.time_utc, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    @property
    def blocks(self) -> int:
        return math.ceil(self.size / BLOCK_BYTES) if self.size else 0

    @property
    def tail_bytes(self) -> int:
        return self.size % BLOCK_BYTES

    @property
    def name(self) -> str:
        return final_name(self.log_id)

    def label(self) -> str:
        d = self.created.astimezone().strftime("%Y-%m-%d %H:%M:%S") if self.created else "RTC未设置"
        return f"#{self.log_id}  {d}  {self.size}B ({self.blocks}块)"

    @staticmethod
    def from_message(m: Any) -> "RemoteLog":
        return RemoteLog(log_id=int(m.id), num_logs=int(m.num_logs), last_log_num=int(m.last_log_num),
                         time_utc=int(m.time_utc), size=int(m.size),
                         raw={k: getattr(m, k, None) for k in ("id", "num_logs", "last_log_num", "time_utc", "size")})


def _fmt_size(n: float) -> str:
    try:
        from apmlog.core.model import human_size
        return human_size(n)
    except Exception:
        units = ["B", "KB", "MB", "GB"]
        v = float(n)
        for u in units:
            if v < 1024 or u == units[-1]:
                return f"{v:.1f}{u}" if u != "B" else f"{int(v)}B"
            v /= 1024
        return f"{v:.1f}GB"


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #


def list_logs(session: MavLinkSession, *, timeout: float = 10.0,
              start: int = 0, end: int = 0xFFFF,
              quiet: bool = False, diagnostics: Optional[dict] = None) -> list[RemoteLog]:
    """发送 ``LOG_REQUEST_LIST`` 并聚合 ``LOG_ENTRY``。

    容忍不完整的列表：超时后返回已收到的条目（``diagnostics`` 里给出期望/实收数量与原因）。
    重复 id 以最后一条为准，并按 id 排序。
    """
    q = session.listen(["LOG_ENTRY", "STATUSTEXT"])
    got: dict[int, RemoteLog] = {}
    expected: Optional[int] = None
    reason = "timeout"
    t0 = time.monotonic()
    try:
        try:
            session.send_log_request_list(start=start, end=end)
        except LinkError as exc:
            reason = f"发送 LOG_REQUEST_LIST 失败: {exc}"
            if diagnostics is not None:
                diagnostics["error"] = reason
            raise LinkError(reason) from exc

        deadline = t0 + timeout
        while time.monotonic() < deadline:
            try:
                m = q.get(timeout=0.2)
            except Exception:
                if not session.connected:
                    reason = "link_lost"
                    break
                continue
            if m.get_type() == "STATUSTEXT":
                continue
            e = RemoteLog.from_message(m)
            got[e.log_id] = e
            if expected is None and e.num_logs:
                expected = e.num_logs
            if expected is not None and len(got) >= expected:
                reason = "complete"
                break
    finally:
        session.unlisten(q)

    logs = sorted(got.values(), key=lambda x: x.log_id)
    if diagnostics is not None:
        diagnostics.update({
            "expected": expected, "received": len(logs), "elapsed": time.monotonic() - t0,
            "reason": reason, "ids": [x.log_id for x in logs],
            "link_stats": session.stats.as_dict(),
            "complete": (expected is not None and len(logs) >= expected),
        })
    if not logs:
        log.warning("日志列表为空：%s (%.1fs)", reason, time.monotonic() - t0)
    elif reason != "complete":
        log.warning("日志列表不完整：%s，期望 %s 收到 %d", reason, expected, len(logs))
    return logs


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #


@dataclass
class DownloadResult:
    """一次下载（或续传尝试）的结果。"""

    log_id: int
    path: Optional[str]                       # 完成时的最终路径；未完成为 .part 路径
    expected_bytes: int
    written_bytes: int
    complete: bool
    elapsed: float
    blocks_ok: int = 0                        # 本次会话收到的帧数
    frames_dup: int = 0                       # 落在已覆盖区间的重复帧
    frames_foreign: int = 0                   # id 不匹配或 ofs 越界的帧
    crc_errors: int = 0                       # 本次会话链路上新增的 CRC 坏帧
    requests_sent: int = 0
    retries: int = 0
    resumed_from: int = 0                     # 续传起点字节
    bytes_per_sec: float = 0.0
    reason: str = ""

    @property
    def rate_text(self) -> str:
        return f"{_fmt_size(self.bytes_per_sec)}/s"

    def summary(self) -> str:
        return (f"log {self.log_id}: {self.written_bytes}/{self.expected_bytes}B "
                f"{'完成' if self.complete else '未完成'} 用时 {self.elapsed:.2f}s "
                f"{self.rate_text} 帧{self.blocks_ok} 重试{self.retries} CRC{self.crc_errors} "
                f"({self.reason or '-'})")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Coverage:
    """字节覆盖区间集合（有序、不重叠、已合并）。"""

    total: int
    ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(b - a for a, b in self.ranges)

    def add(self, start: int, end: int) -> bool:
        """加入 [start,end)；返回是否**新增了字节**（重复/邻接已覆盖区则返回 False）。

        注意：相邻区间（b == start）必须合并而不是当重复，否则顺序流会被全部误判为重复。
        """
        start, end = max(0, start), min(self.total, end)
        if end <= start:
            return False
        before = self.done
        new = [(start, end)] + list(self.ranges)
        new.sort()
        acc = [new[0]]
        for a, b in new[1:]:
            pa, pb = acc[-1]
            if a <= pb:                       # 重叠或紧邻 → 合并
                acc[-1] = (pa, max(pb, b))
            else:
                acc.append((a, b))
        self.ranges = acc
        return self.done > before

    def first_missing(self, from_ofs: int = 0) -> Optional[int]:
        pos = from_ofs
        for a, b in self.ranges:
            if b <= pos:
                continue
            if a > pos:
                return pos
            pos = b
        return pos if pos < self.total else None

    def covered(self, start: int, end: int) -> bool:
        return any(a <= start and end <= b for a, b in self.ranges)

    def first_gap(self, exclude: Iterable[tuple[int, int]] = ()) -> Optional[int]:
        """第一个既未覆盖、也不在任何在途请求区间内的字节偏移（块边界对齐）。"""
        busy = sorted((max(0, a), min(self.total, b)) for a, b in exclude)
        pos = 0
        while pos < self.total:
            if self.covered(pos, min(self.total, pos + BLOCK_BYTES)):
                pos += BLOCK_BYTES
                continue
            if any(a <= pos < b for a, b in busy):
                pos = max(b for a, b in busy if a <= pos < b)
                continue
            return (pos // BLOCK_BYTES) * BLOCK_BYTES
        return None

    def as_list(self) -> list[list[int]]:
        return [[a, b] for a, b in self.ranges]

    @staticmethod
    def from_list(total: int, lst: Iterable[Iterable[int]]) -> "_Coverage":
        c = _Coverage(total=total)
        for pair in lst:
            a, b = pair
            c.add(int(a), int(b))
        return c

    @property
    def complete(self) -> bool:
        return self.total > 0 and len(self.ranges) == 1 and self.ranges[0] == (0, self.total)


def _sidecar(part_path: Path) -> Path:
    return part_path.with_name(part_path.name + ".json")


def _save_state(part_path: Path, cov: _Coverage, entry: RemoteLog, blocks_per_request: int,
                stat: "DownloadResult", tag: str = "") -> None:
    payload = {
        "version": 1,
        "log_id": entry.log_id,
        "size": entry.size,
        "num_logs": entry.num_logs,
        "time_utc": entry.time_utc,
        "chunk_blocks": blocks_per_request,
        "block_bytes": BLOCK_BYTES,
        "ranges": cov.as_list(),
        "done_bytes": cov.done,
        "crc_errors": stat.crc_errors,
        "retries": stat.retries,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tag": tag,
    }
    tmp = part_path.with_name(part_path.name + ".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _sidecar(part_path))


def _load_state(part_path: Path, entry: RemoteLog, blocks_per_request: int) -> Optional[dict]:
    sc = _sidecar(part_path)
    if not sc.exists() or not part_path.exists():
        return None
    try:
        st = json.loads(sc.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("续传旁车不可读，忽略：%s", exc)
        return None
    if int(st.get("log_id", -1)) != entry.log_id or int(st.get("size", -1)) != entry.size:
        log.warning("续传旁车与当前 LOG_ENTRY 不匹配（log_id/size 变了），重新下载")
        return None
    if int(st.get("block_bytes", BLOCK_BYTES)) != BLOCK_BYTES:
        return None
    if int(st.get("chunk_blocks", blocks_per_request)) != blocks_per_request:
        log.info("续传：chunk_blocks 由 %s 改为 %d，已覆盖区间仍复用", st.get("chunk_blocks"), blocks_per_request)
    return st


def download_log(session: MavLinkSession, entry: RemoteLog, out_dir: os.PathLike | str = "", *,
                 blocks_per_request: int = 120, timeout_chunk: float = 2.5, timeout_stall: float = 15.0,
                 max_retries_per_chunk: int = 12, max_retries_total: int = 20000,
                 window: int = 1, progress: PROGRESS = None, cancel: CANCEL = None,
                 resume: bool = True, send_end: bool = False, overwrite: bool = False,
                 verify_size: bool = True) -> DownloadResult:
    """分块下载一条日志。

    :param blocks_per_request: 每次 ``LOG_REQUEST_DATA`` 请求的 90B 块数（``count`` 一律换算成**字节**）。
        默认 120 块 = 10800B：真机 COM91（ArduCopter 4.6.3，USB CDC）整段扫参得到的推荐值——
        同一条 446464B 日志中位 **7.51s / 58.0 KB/s，重传 0、重复帧 0、CRC 错 0**（两轮 7.51/7.52s）；
        旧的 20 块要 30.84s / 14.1 KB/s。≥200 块开始出现重复帧与 CRC 错（200/1：152 重复、5 CRC），
        建议没有理由超过 160。证据表见 docs/LOG_DOWNLOAD_NOTES.md 第 6 节。
    :param window: 允许在途的请求数。**真机实测 >1 反而降吞吐**：120/1=7.51s、120/2=7.92s、
        120/4=12.82s（且 18 次重传），故默认 1；只有 FakeFC 本地回环下 window>1 才占优。
    :param timeout_chunk: 单个请求的等待上限，超时则对**未覆盖字节**重发（计入 retries）。
    :param max_retries_per_chunk / max_retries_total: 重试上限，超限即中止并保留 ``.part``。
    :param send_end: 实测 Mission Planner 会话中 ``LOG_REQUEST_END`` 出现 0 次，故默认关闭。
    :raises LinkError: 尚未识别飞控地址、或写入失败等无法继续的错误。
    """
    out_dir = Path(out_dir) if out_dir else default_download_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / final_name(entry.log_id)
    part = out_dir / (final.name + ".part")

    if entry.size <= 0:
        raise LinkError(f"LOG_ENTRY.size={entry.size} 非法，无法下载")
    if final.exists() and not overwrite:
        if final.stat().st_size == entry.size:
            return DownloadResult(log_id=entry.log_id, path=str(final), expected_bytes=entry.size,
                                  written_bytes=entry.size, complete=True, elapsed=0.0,
                                  reason="already-present")
        log.warning("同名文件大小 %d != 期望 %d，重新下载", final.stat().st_size, entry.size)

    cov = _Coverage(total=entry.size)
    stat = DownloadResult(log_id=entry.log_id, path=str(part), expected_bytes=entry.size,
                          written_bytes=0, complete=False, elapsed=0.0)
    if resume:
        st = _load_state(part, entry, blocks_per_request)
        if st:
            cov = _Coverage.from_list(entry.size, st.get("ranges", []))
            stat.resumed_from = cov.done
            stat.crc_errors = int(st.get("crc_errors", 0))
            stat.retries = int(st.get("retries", 0))
            log.info("续传 log %d：已覆盖 %d/%d 字节", entry.log_id, cov.done, entry.size)

    if not resume and part.exists():            # 不续传时彻底丢弃旧断点，避免半截数据被当成已完成
        part.unlink(missing_ok=True)
        _sidecar(part).unlink(missing_ok=True)
    f = open(part, "r+b" if part.exists() else "w+b")
    crc_base = session.stats.crc_errors
    t_start = time.monotonic()
    last_progress_at = t_start
    q = session.listen(["LOG_DATA", "LOG_ENTRY"])
    inflight: dict[int, tuple[int, float, int]] = {}     # req_ofs -> (bytes, sent_at, retries)
    last_gap_req: dict[int, float] = {}                   # req_ofs -> 上次因缺口重发的时刻（节流）
    aborted = ""
    try:
        f.seek(0, os.SEEK_END)
        if f.tell() < entry.size:                         # 预分配到目标长度，支持任意 ofs 写入
            f.truncate(entry.size)
        cov.add(0, 0)

        def request_upto() -> None:
            """把在途请求补到 window 个；按**字节 ofs** 请求（实测固件 ofs 为字节偏移）。"""
            nonlocal aborted
            while len(inflight) < max(1, int(window)):
                block_start = cov.first_gap([(k, k + v[0]) for k, v in inflight.items()])
                if block_start is None:
                    return
                count = min(blocks_per_request * BLOCK_BYTES, entry.size - block_start)
                try:
                    session.send_log_request_data(entry.log_id, block_start, count)
                except LinkError as exc:
                    aborted = f"发送 LOG_REQUEST_DATA 失败: {exc}"
                    return
                inflight[block_start] = (count, time.monotonic(), 0)
                stat.requests_sent += 1

        try:
            request_upto()
        except Exception as exc:                          # pragma: no cover
            aborted = str(exc)

        while not cov.complete:
            if cancel is not None and cancel():
                aborted = "cancelled"
                break
            if aborted:
                break
            if not inflight:
                request_upto()
                if not inflight and not cov.complete:
                    aborted = "no-inflight-stuck"
                    break

            try:
                m = q.get(timeout=0.2)
            except Exception:
                now = time.monotonic()
                # 请求超时 -> 重发未覆盖部分
                expired = [k for k, (_, sent, _n) in inflight.items() if now - sent > timeout_chunk]
                for k in expired:
                    _cnt, _sent, n = inflight.pop(k)
                    nxt = cov.first_missing(k)
                    if nxt is None:
                        continue
                    if n >= max_retries_per_chunk or stat.retries >= max_retries_total:
                        aborted = f"重试上限（块重试 {n}，总重试 {stat.retries}）"
                        break
                    align = (nxt // BLOCK_BYTES) * BLOCK_BYTES
                    count = min(blocks_per_request * BLOCK_BYTES, entry.size - align)
                    try:
                        session.send_log_request_data(entry.log_id, align, count)
                    except LinkError as exc:
                        aborted = f"重发失败: {exc}"
                        break
                    inflight[align] = (count, now, n + 1)
                    stat.retries += 1
                    log.debug("重试 log %d ofs=%d count=%d (第 %d 次)", entry.log_id, align, count, n + 1)
                if aborted:
                    break
                if time.monotonic() - last_progress_at > timeout_stall:
                    aborted = f"stalled {time.monotonic() - last_progress_at:.1f}s 无新数据"
                    break
                if not session.connected:
                    aborted = "link_lost"
                    break
                continue

            if m.get_type() == "LOG_ENTRY":       # 迟到的列表条目，忽略
                continue
            if int(m.id) != entry.log_id:
                stat.frames_foreign += 1
                continue
            ofs = int(m.ofs)
            payload = bytes(m.data[:int(m.count)]) if m.count else b""
            if not payload:
                payload = bytes(m.data)
            if ofs + len(payload) > entry.size:   # 尾帧可能带填充，按 size 截断
                payload = payload[: max(0, entry.size - ofs)]
                if not payload:
                    stat.frames_foreign += 1
                    continue
            f.seek(ofs)
            f.write(payload)
            fresh = cov.add(ofs, ofs + len(payload))
            if fresh:
                stat.blocks_ok += 1
                last_progress_at = time.monotonic()
            else:
                stat.frames_dup += 1
            # 飞控是**顺序流**（实测 ofs 严格 +90 递增）：收到的块一旦越过某个仍未覆盖的字节，
            # 说明那块真的丢了（不会自己再出现）。此时立即为**该缺口所属的请求**重发，
            # 而不是干等 timeout_chunk（否则少量丢块会把耗时放大到分钟级）。
            # 关键约束：一个缺口只在其归属请求上重发，并按 align 节流；
            # 否则重复到达的帧会对同一个洞狂重发（本机实测曾出现 0.01s 内 12 次重试）。
            for k in [k for k, (cnt, _s, _n) in inflight.items()
                      if cov.covered(k, min(entry.size, k + cnt))]:
                inflight.pop(k)
            gap = cov.first_missing(0)
            if gap is not None and gap < ofs:
                align = (gap // BLOCK_BYTES) * BLOCK_BYTES
                owner = next((k for k, (cnt, _s, _n) in inflight.items() if k <= gap < k + cnt), None)
                if align in inflight:
                    pass                                   # 已有同起点请求在途，等它
                elif owner is None:
                    pass                                   # 交给 request_upto() 统一调度
                else:
                    n = inflight.pop(owner)[2]
                    if n + 1 > max_retries_per_chunk or stat.retries >= max_retries_total:
                        aborted = f"重试上限（ofs={align} 第 {n + 1} 次，累计 {stat.retries}）"
                    elif time.monotonic() - last_gap_req.get(align, 0.0) < 0.05:
                        pass                                   # 节流：50ms 内不重复请求同一缺口
                    else:
                        count2 = min(blocks_per_request * BLOCK_BYTES, entry.size - align)
                        try:
                            session.send_log_request_data(entry.log_id, align, count2)
                            last_gap_req[align] = time.monotonic()
                            inflight[align] = (count2, time.monotonic(), n + 1)
                            stat.retries += 1
                        except LinkError as exc:
                            aborted = f"丢块重传发送失败: {exc}"
            if aborted:
                break
            if len(inflight) < window:
                request_upto()
            if progress is not None and (cov.complete or cov.done - stat.written_bytes >= 4096):
                stat.written_bytes = cov.done
                rate = cov.done / max(1e-6, time.monotonic() - t_start)
                progress(cov.done, entry.size, rate, f"log {entry.log_id} {cov.done}/{entry.size}")
        stat.written_bytes = cov.done
    finally:
        session.unlisten(q)
        try:
            f.flush()
            os.fsync(f.fileno())
        except OSError:                            # pragma: no cover
            pass
        f.close()

    elapsed = time.monotonic() - t_start
    stat.elapsed = elapsed
    stat.crc_errors = max(stat.crc_errors, session.stats.crc_errors - crc_base)
    stat.bytes_per_sec = cov.done / elapsed if elapsed > 0 else 0.0

    if cov.complete:
        if verify_size:
            got = part.stat().st_size
            if got != entry.size:
                stat.reason = f"落盘大小 {got} != 期望 {entry.size}"
                stat.complete = False
                return stat
        os.replace(part, final)
        _sidecar(part).unlink(missing_ok=True)
        stat.path = str(final)
        stat.complete = True
        stat.reason = stat.reason or "complete"
        if send_end:
            try:
                session.send_log_request_end()
            except LinkError as exc:                # pragma: no cover
                log.warning("LOG_REQUEST_END 发送失败（不影响已完成的文件）：%s", exc)
        log.info("下载完成 %s", final)
    else:
        stat.path = str(part)
        stat.reason = aborted or "incomplete"
        _save_state(part, cov, entry, blocks_per_request, stat, tag=stat.reason)
        log.warning("下载未完成（%s），保留断点：%s", stat.reason, part)
    return stat
