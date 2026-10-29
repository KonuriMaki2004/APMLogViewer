# -*- coding: utf-8 -*-
"""apmlog.link.params —— MAVLink 参数**只读**读取层（检测飞控是否在写 DataFlash）。

======================== 本模块对飞控严格只读 ========================
本模块**只发 ``PARAM_REQUEST_READ``（msgid 20，按名读）**，只接收 ``PARAM_VALUE``（msgid 22）。
它不提供、不引用、也不将来"预留"任何参数写入入口：整个工程对飞控保持只读，
日志下载流程只发 HEARTBEAT / PING / LOG_REQUEST_* 与本模块的按名读参数，
其余写/控制/固件类消息一律由 :mod:`apmlog.link.audit` 判为禁发并让只读校验失败。

所以这里的行为契约是：**检测到了未开启，也只给出手动处理方法**。
:class:`LogWriteConfig` 里的 :attr:`~LogWriteConfig.suggest` 是"建议用户在 Mission Planner /
QGroundControl / MAVProxy 里自己改成的值"，不是本端将要执行的写操作；本端一个字节也不写。
这个决定来自用户与同事的最终意见：宁可多一步人工操作，也不让分析工具碰飞控配置。

取值依据（**不是猜的**）：从用户真机 Pixhawk6X / ArduCopter 4.7 的日志 PARAM 块读到
``LOG_BACKEND_TYPE = 1.0``、``LOG_BITMASK = 180222.0``、``LOG_DISARMED = 0.0``。
据此可知 ``LOG_BACKEND_TYPE`` 的 bit0=1 表示 SD 卡文件后端、``LOG_BITMASK`` 非 0 表示确有内容可写；
下面两个 ``DEFAULT_*`` 常量就是这组实测值，仅作为**建议用户手动设为**的参考值。

请求/应答聚合的写法沿用 :func:`apmlog.link.download.list_logs`：
``listen() → 发请求 → 带 deadline 轮询队列 → finally: unlisten()``。
链路上可能同时有别的 GCS 在拉全量参数（哗哗刷 PARAM_VALUE），因此读取**严格按名字过滤**，
绝不"取第一条"。
"""
from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .endpoint import LinkError, MavLinkSession

__all__ = [
    "LOG_BACKEND_TYPE", "LOG_BITMASK", "BACKEND_FILE_BIT",
    "DEFAULT_LOG_BACKEND_TYPE", "DEFAULT_LOG_BITMASK",
    "ParamValue", "LogWriteConfig",
    "read_param", "read_params", "classify", "probe_log_write_config",
]

log = logging.getLogger("apmlog.link.params")

# --------------------------------------------------------------------------- #
# 已知的"日志写入"参数与实测建议值（**只作为手动设置的参考，本端不写**）
# --------------------------------------------------------------------------- #

LOG_BACKEND_TYPE = "LOG_BACKEND_TYPE"
LOG_BITMASK = "LOG_BITMASK"
BACKEND_FILE_BIT = 1                    # LOG_BACKEND_TYPE bit0 = SD 卡文件后端
#: 建议在 Mission Planner / QGC / MAVProxy 里手动设为该值（依据：用户飞控 4.7 实测）
DEFAULT_LOG_BACKEND_TYPE = 1.0
#: 同上：建议手动设为该值（依据：用户飞控 4.7 日志 PARAM 块实测）
DEFAULT_LOG_BITMASK = 180222.0

#: PARAM_ID 在线上是定长 16 字节、NUL 填充（dialect 实测）
PARAM_ID_LEN = 16


@dataclass(frozen=True)
class ParamValue:
    """一次按名读参数的结果。``found=False`` 时 ``value`` 必为 None，``reason`` 说明为什么。"""

    name: str
    found: bool
    value: Optional[float]
    param_type: int = 0                 # MAV_PARAM_TYPE_*，由飞控在 PARAM_VALUE 里报回来
    reason: str = ""


@dataclass(frozen=True)
class LogWriteConfig:
    """飞控"是否在写日志"的判定结论，可直接喂给 UI 显示。

    :attr:`suggest` 是**给用户的手动处理建议**，三元组 ``（参数名, 当前值或 None, 建议值）``，
    顺序固定 backend 在前、bitmask 在后；本端不会执行其中任何一项。
    """

    state: str                          # "enabled" | "disabled" | "unknown"
    backend: ParamValue
    bitmask: ParamValue
    detail: str                         # 中文一句话，可直接显示给用户
    suggest: tuple[tuple[str, Optional[float], float], ...] = ()
    suggest_note: str = ""              # 建议值来源与手动操作入口说明


# --------------------------------------------------------------------------- #
# 线上字段的小工具
# --------------------------------------------------------------------------- #


def _encode_param_id(name: str) -> bytes:
    """参数名 → 16 字节 NUL 填充。超长直接截断（飞控侧本来也只认 16 字节）。"""
    raw = name.strip().upper().encode("ascii", "replace")[:PARAM_ID_LEN]
    return raw + bytes(PARAM_ID_LEN - len(raw))


def _decode_param_id(raw: Any) -> str:
    """PARAM_VALUE.param_id → 去 NUL 填充的大写字符串（bytes/str 两种方言实现都兼容）。"""
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).split(b"\x00", 1)[0].decode("ascii", "replace")
    elif isinstance(raw, str):
        text = raw.split("\x00", 1)[0]
    elif isinstance(raw, (list, tuple)):                  # pragma: no cover - 老 pymavlink 数组形态
        text = "".join(chr(int(b)) for b in raw if int(b))
    else:                                                  # pragma: no cover
        text = str(raw)
    return text.strip().upper()


def _matches_target(msg: Any, sysid: int, compid: int) -> bool:
    """只接受来自目标飞控的应答；链路上可能有别的组件（companion / 另一台机）。"""
    try:
        return int(msg.get_srcSystem()) == int(sysid) and int(msg.get_srcComponent()) == int(compid)
    except Exception:                                      # pragma: no cover
        return False


def _param_value_from(msg: Any) -> tuple[str, float, int]:
    return _decode_param_id(msg.param_id), float(msg.param_value), int(msg.param_type)


def _norm(name: str) -> str:
    """参数名在线上是 NUL 填充的大写 ASCII；比较与字典键都用这个归一形式。"""
    return _encode_param_id(name).split(b"\x00", 1)[0].decode()


def _fmt(v: Optional[float]) -> str:
    """参数值显示：整数值不写小数点（180222.0 → 180222），非整数保留最多 3 位。"""
    if v is None:
        return "无"
    if float(v).is_integer():
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _miss(name: str, reason: str) -> ParamValue:
    return ParamValue(name=name, found=False, value=None, param_type=0, reason=reason)


# --------------------------------------------------------------------------- #
# 读（本模块唯一的链路动作）
# --------------------------------------------------------------------------- #


def read_param(sess: MavLinkSession, name: str, *, timeout: float = 1.5) -> ParamValue:
    """按名字读一个参数（``PARAM_REQUEST_READ`` 的 ``param_index=-1`` 分支）。

    ``LinkError``（还没识别到飞控心跳）原样抛出，UI 要据此区分"没连上"；
    其余一切失败都收进 :attr:`ParamValue.reason`，**不向调用方抛异常**。
    """
    return read_params(sess, (name,), timeout=timeout)[name]


def read_params(sess: MavLinkSession,
                names: Sequence[str] = (LOG_BACKEND_TYPE, LOG_BITMASK),
                *, timeout: float = 1.5) -> dict[str, ParamValue]:
    """一次读多个参数，**共用同一个应答窗口**（不是串行等两次超时）。

    返回的 dict 一定包含 ``names`` 里的每个键；没读到的值为 ``found=False``。
    """
    ordered = list(dict.fromkeys(str(n) for n in names))          # 去重保序
    if not ordered:
        return {}
    # 先拿飞控地址：这里可能抛 LinkError（还没识别到飞控心跳），按约定原样抛给调用方。
    sysid, compid = sess.target_ids()

    want: dict[str, str] = {_norm(n): n for n in ordered}
    remaining = set(want)
    found: dict[str, ParamValue] = {}
    reason = "timeout"
    q = sess.listen(["PARAM_VALUE"])
    t0 = time.monotonic()
    try:
        try:
            for n in ordered:
                sess._send("param_request_read_send", sysid, compid, _encode_param_id(n), -1)
        except LinkError as exc:
            reason = "link_lost" if not sess.connected else f"send_failed: {exc}"
            log.warning("参数读取发送失败 %s: %s", ordered, exc)
        else:
            deadline = t0 + max(0.05, float(timeout))
            while remaining and time.monotonic() < deadline:
                try:
                    m = q.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
                except queue.Empty:
                    if not sess.connected:
                        reason = "link_lost"
                        break
                    continue
                if m.get_type() != "PARAM_VALUE" or not _matches_target(m, sysid, compid):
                    continue
                pid, value, ptype = _param_value_from(m)
                orig = want.get(pid)
                if orig is None or pid not in remaining:
                    continue                                    # 无关参数的噪声
                found[orig] = ParamValue(name=orig, found=True, value=value,
                                         param_type=ptype, reason="")
                remaining.discard(pid)
                log.debug("读到参数 %s=%s (type=%d)", orig, _fmt(value), ptype)
    finally:
        sess.unlisten(q)

    out: dict[str, ParamValue] = {}
    for n in ordered:
        out[n] = found.get(n) or _miss(n, reason)
    if remaining:
        log.debug("参数读取结束：%d/%d 命中，原因 %s，耗时 %.2fs",
                  len(found), len(ordered), reason, time.monotonic() - t0)
    return out


# --------------------------------------------------------------------------- #
# 判定（纯函数，无 I/O）
# --------------------------------------------------------------------------- #

_SUGGEST_NOTE = (
    f"建议值取自用户真机（Pixhawk6X / ArduCopter 4.7）日志 PARAM 块实测："
    f"{LOG_BACKEND_TYPE}={_fmt(DEFAULT_LOG_BACKEND_TYPE)}、"
    f"{LOG_BITMASK}={_fmt(DEFAULT_LOG_BITMASK)}。"
    "本工具对飞控严格只读，不会代你改参数；请在 Mission Planner 的 Full Parameter List"
    "（或 QGC 参数页、MAVProxy 的 ``set`` 命令）里手动设为建议值，改完重连再复查。"
)


def classify(backend: ParamValue, bitmask: ParamValue) -> LogWriteConfig:
    """把两个参数的读取结果判成"日志写入已开启/未开启/无法判断"。**纯函数，无 I/O**。

    判定原则（按"读不到的参数不参与判定"这条显式分支实现）：
      * 两个都读到：bit0=1 且 bitmask≠0 → enabled；否则 disabled，
        ``suggest`` 只列真正需要用户手动改的那几项（哪一项坏就只列哪一项）。
      * 只读到一个：只有在**读到的那项正面支持开启**（bitmask≠0）时才判 enabled，
        并在 detail 里注明后端类型未知。
      * 一个都没读到：unknown，``suggest=()``，**不给任何写入建议**。

    不变式：``suggest`` 只在 ``state="disabled"`` 时非空；enabled/unknown 恒为空。
    """
    b_ok: Optional[bool] = None
    m_ok: Optional[bool] = None
    if backend.found and backend.value is not None:
        b_ok = bool(int(backend.value) & BACKEND_FILE_BIT)
    if bitmask.found and bitmask.value is not None:
        m_ok = float(bitmask.value) != 0.0

    missing = [p.name for p in (backend, bitmask) if not (p.found and p.value is not None)]

    # --- 显式分支 1：两项都有正面证据 → 已开启
    if b_ok is True and m_ok is True:
        return LogWriteConfig(
            state="enabled", backend=backend, bitmask=bitmask,
            detail=(f"日志写入已开启：{LOG_BACKEND_TYPE}={_fmt(backend.value)}（含 SD 卡文件后端），"
                    f"{LOG_BITMASK}={_fmt(bitmask.value)}（非 0，有内容可写）"),
            suggest=(), suggest_note="")
    # --- 显式分支 2：只有 LOG_BITMASK 读到且非 0 → 认为在写，但后端类型未知
    if b_ok is None and m_ok is True:
        return LogWriteConfig(
            state="enabled", backend=backend, bitmask=bitmask,
            detail=(f"{LOG_BITMASK}={_fmt(bitmask.value)}（非 0，有内容可写），"
                    f"但未读到 {LOG_BACKEND_TYPE}，日志后端类型未知"
                    f"（旧固件可能用 SDCARD_OPTIONS 表达同一含义）"),
            suggest=(), suggest_note="")
    # --- 显式分支 3：有任一明确为"关" → 未开启，只列出坏的那几项供用户手动改
    if b_ok is False or m_ok is False:
        items: list[tuple[str, Optional[float], float]] = []
        why: list[str] = []
        if b_ok is False:
            items.append((LOG_BACKEND_TYPE, backend.value, DEFAULT_LOG_BACKEND_TYPE))
            why.append(f"{LOG_BACKEND_TYPE}={_fmt(backend.value)} 未选 SD 卡文件后端（bit0=0），"
                       f"建议手动设为 {_fmt(DEFAULT_LOG_BACKEND_TYPE)}")
        if m_ok is False:
            items.append((LOG_BITMASK, bitmask.value, DEFAULT_LOG_BITMASK))
            why.append(f"{LOG_BITMASK}=0 表示不记录任何日志内容，"
                       f"建议手动设为 {_fmt(DEFAULT_LOG_BITMASK)}")
        detail = "日志写入未开启：" + "；".join(why)
        if missing:
            detail += f"（{ '、'.join(missing) } 未读到，不参与判定）"
        detail += "。本工具不会代你改参数，请按上面的建议手动设置后重连复查。"
        return LogWriteConfig(state="disabled", backend=backend, bitmask=bitmask,
                              detail=detail, suggest=tuple(items), suggest_note=_SUGGEST_NOTE)
    # --- 显式分支 4：没有任何可读证据，或只有"证明不了开启"的半截证据
    if b_ok is None and m_ok is None:
        detail = (f"无法判断日志写入状态：{LOG_BACKEND_TYPE} 与 {LOG_BITMASK} 都没读到"
                  f"（{_read_reason(backend)} / {_read_reason(bitmask)}）。"
                  "可能是飞控正被其它地面站占用或链路太慢，先重试读取。")
    else:
        got = backend if b_ok is not None else bitmask
        other = bitmask if got is backend else backend
        detail = (f"无法确认日志写入状态：只读到 {got.name}={_fmt(got.value)}，"
                  f"{other.name} 没读到（{_read_reason(other)}），证据不完整，"
                  "先重试读取；若仍读不到，请在地面站参数页里人工确认这两个参数。")
    return LogWriteConfig(state="unknown", backend=backend, bitmask=bitmask,
                          detail=detail, suggest=(), suggest_note="")


def _read_reason(p: ParamValue) -> str:
    """把 ParamValue.reason 翻成面向用户的话，避免出现"未知错误"这种废话。"""
    r = (p.reason or "").strip()
    if not r:
        return "无失败原因"
    if r == "timeout":
        return "读取超时，飞控没应答（参数可能不存在）"
    if r == "link_lost":
        return "读取过程中链路断开"
    if r.startswith("send_failed"):
        return f"读取请求没发出去（{r}）"
    return r


def probe_log_write_config(sess: MavLinkSession, *, timeout: float = 1.5) -> LogWriteConfig:
    """**只读**探测：按名读两个日志参数并判定。本函数不发任何写指令。"""
    got = read_params(sess, (LOG_BACKEND_TYPE, LOG_BITMASK), timeout=timeout)
    cfg = classify(got[LOG_BACKEND_TYPE], got[LOG_BITMASK])
    log.info("日志写入探测：%s | %s", cfg.state, cfg.detail)
    return cfg
