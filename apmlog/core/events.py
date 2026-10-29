"""apmlog.core.events —— 报文级事件归集、解码与同刻成组。

约定：
* 时间一律用 ``tc``（连续秒，已消除日志重启造成的时间回退）。
* 解码只使用有出处的码表；未映射一律写 ``（未映射）``，绝不编造含义。
* 炸机证据的正确读法是 **同刻 MSG 原文**，因此本模块提供 :func:`group_by_time`
  把同一毫秒内的 ERR / MSG / ARM / EV 绑成一组给 UI 出卡片。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from .codebooks import Codebook, msg_severity
from .model import EventRec, severity_rank

log = logging.getLogger(__name__)

#: 事件成组的时间容差（秒）。实测炸机日志中 ERR/MSG/ARM/EV 在同一毫秒内。
GROUP_TOL_S = 0.0025

_TEXT_KEYS = ("Message", "Msg", "Text", "MsgText")


def _first_text(d: dict[str, Any]) -> str:
    for k in _TEXT_KEYS:
        v = d.get(k)
        if isinstance(v, (str, bytes)):
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
    return ""


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int,)):
        return int(v)
    if isinstance(v, float):
        return int(v) if v == int(v) else None
    return None


def raw_from_message(kind: str, d: dict[str, Any], tc: float) -> Optional[dict[str, Any]]:
    """把一条报文转成"原始事件"字典（不含任何解释）。返回 None 表示不是事件。"""
    if kind == "ERR":
        return {"t": tc, "kind": "ERR", "code": _as_int(d.get("Subsys")),
                "ecode": _as_int(d.get("ECode") if d.get("ECode") is not None else d.get("ecode")),
                "extra": {k: v for k, v in d.items() if k not in ("TimeUS", "mavpackettype")}}
    if kind == "EV":
        arg = d.get("Arg")
        return {"t": tc, "kind": "EV", "code": _as_int(d.get("Id")),
                "extra": {"Arg": arg} if arg is not None else {}}
    if kind in ("MSG", "MSGB", "MSGT", "MSGS"):
        text = _first_text(d)
        if not text:
            return None
        return {"t": tc, "kind": "MSG", "code": _as_int(d.get("ID")), "text": text,
                "extra": {"Level": d.get("Level")} if d.get("Level") is not None else {}}
    if kind == "ARM":
        return {"t": tc, "kind": "ARM", "code": _as_int(d.get("ArmState")),
                "extra": {"ArmChecks": d.get("ArmChecks"), "Forced": d.get("Forced"),
                          "Method": d.get("Method")}}
    if kind == "MODE":
        num = _as_int(d.get("Mode"))
        if num is None:
            num = _as_int(d.get("ModeNum"))
        return {"t": tc, "kind": "MODE", "code": num,
                "extra": {"Rsn": d.get("Rsn"), "ModeNum": d.get("ModeNum")}}
    if kind == "CMD":
        return {"t": tc, "kind": "CMD", "code": _as_int(d.get("Cmd")),
                "extra": {k: v for k, v in d.items() if k in ("Param", "Result", "Arg")}}
    if kind == "FILE":
        name = d.get("FileName")
        if not name:
            return None
        return {"t": tc, "kind": "FILE", "code": None,
                "text": str(name), "extra": {"Offset": d.get("Offset"), "Length": d.get("Length")}}
    return None


def decode(raws: Sequence[dict[str, Any]], cb: Codebook) -> list[EventRec]:
    """原始事件 → EventRec（带解码文本与严重度）。"""
    evs = [EventRec(t=float(r["t"]), kind=r["kind"], code=r.get("code"), ecode=r.get("ecode"),
                    text=str(r.get("text", "")), extra=dict(r.get("extra", {}))) for r in raws]
    evs.sort(key=lambda e: e.t)

    # 先做同刻分组，让 ERR 能借用同时刻 MSG 的原文与判级
    groups = group_events(evs)
    for g in groups:
        texts = [e.text for e in g if e.kind == "MSG" and e.text]
        if not texts:
            continue
        co = "; ".join(texts)
        co_sev = max((msg_severity(t) for t in texts), key=severity_rank, default="info")
        for e in g:
            if e.kind == "MSG":
                e.severity = msg_severity(e.text) or "info"
            elif e.kind in ("ERR", "ARM", "EV"):
                e.extra["co_text"] = co
                if severity_rank(co_sev) > severity_rank(e.severity):
                    e.severity = co_sev

    for e in evs:
        if e.kind == "ERR":
            txt, sev = cb.err_info(e.code, e.ecode)
            if txt is None:
                e.decoded = ""
                e.text = (f"Subsys={e.code} ECode={e.ecode}（未映射）"
                          if e.ecode is not None else f"Subsys={e.code}（未映射）")
                e.severity = e.severity if severity_rank(e.severity) > severity_rank("info") else "error"
            else:
                e.decoded = txt
                if not e.text:
                    e.text = txt
                if severity_rank(sev) > severity_rank(e.severity):
                    e.severity = sev
        elif e.kind == "EV":
            txt, sev = cb.ev_text(e.code)
            e.decoded = txt or ""
            if txt is None:
                e.text = f"EV Id={e.code}（未映射）"
            elif not e.text:
                e.text = txt
            if severity_rank(sev) > severity_rank(e.severity):
                e.severity = sev
        elif e.kind == "MODE":
            name = cb.mode_name(e.code)
            e.decoded = name or ""
            if not e.text:
                e.text = f"模式切换 → {name}" if name else f"MODE={e.code}（未映射）"
            e.severity = "info"
        elif e.kind == "ARM":
            state = "已解锁" if e.code == 1 else ("已上锁" if e.code == 0 else f"ArmState={e.code}")
            method = cb.arm_method(_as_int(e.extra.get("Method")))
            e.decoded = f"{state}（{method}）" if method else state
            if not e.text:
                e.text = e.decoded
            checks = _as_int(e.extra.get("ArmChecks"))
            if checks:
                names = cb.arming_checks(checks)
                if names:
                    e.extra["arm_check_names"] = names
            if e.code == 0:
                e.severity = e.severity if severity_rank(e.severity) > severity_rank("info") else "hint"
            else:
                e.severity = "info"
        elif e.kind == "MSG":
            if not e.severity:
                e.severity = msg_severity(e.text)
        elif e.kind == "CMD":
            e.severity = "info"
        elif e.kind == "FILE":
            e.severity = "info"
        else:
            e.severity = e.severity or "info"
    return evs


def group_events(evs: Sequence[EventRec], tol: float = GROUP_TOL_S) -> list[list[EventRec]]:
    """把时间差 <= tol 的事件绑成组（用于"炸机证据卡片"）。"""
    groups: list[list[EventRec]] = []
    cur: list[EventRec] = []
    for e in sorted(evs, key=lambda x: x.t):
        if not cur:
            cur = [e]
            continue
        if e.t - cur[-1].t <= tol:
            cur.append(e)
        else:
            groups.append(cur)
            cur = [e]
    if cur:
        groups.append(cur)
    return groups


def find_crash_groups(evs: Sequence[EventRec]) -> list[tuple[float, str]]:
    """返回 [(tc, 描述)]。判据：同刻 MSG 含 crash 关键字（有原文证据，不靠码表）。"""
    out = []
    for g in group_events(evs):
        for e in g:
            if e.kind == "MSG" and "crash" in (e.text or "").lower():
                others = ", ".join(sorted({x.kind for x in g if x is not e}))
                out.append((e.t, f"{e.text}" + (f"［同刻 {others}］" if others else "")))
                break
    return out


def mode_segments(evs: Sequence[EventRec], t_end: float,
                  cb: Optional[Codebook] = None) -> list[tuple[float, float, str]]:
    """由 MODE 事件推 (t0, t1, 模式名)。无名称时用 ``MODE=n``，不猜名字。"""
    modes = sorted([e for e in evs if e.kind == "MODE"], key=lambda e: e.t)
    segs: list[tuple[float, float, str]] = []
    for i, m in enumerate(modes):
        name = m.decoded or (cb.mode_name(m.code) if cb else None) or f"MODE={m.code}"
        t1 = modes[i + 1].t if i + 1 < len(modes) else max(t_end, m.t)
        segs.append((m.t, t1, str(name)))
    return segs


def compress_mode_seq(segs: Sequence[tuple[float, float, str]]) -> list[str]:
    """模式序列文字：连续同模式合并为一次。

    ``mode_segments`` 会按 MODE 事件逐条给出区间（同模式重复进入也各算一段），
    而 ``stats.mode_seq`` 要的是“这次飞行经历史过了哪几个模式”，所以必须压掉连续重复，
    否则会出现 ``ALT_HOLD, ALT_HOLD, LOITER`` 这种看着像两次的误读。
    """
    out: list[str] = []
    for _a, _b, name in segs:
        if not out or out[-1] != name:
            out.append(str(name))
    return out


__all__ = ["raw_from_message", "decode", "group_events", "find_crash_groups",
           "mode_segments", "compress_mode_seq", "GROUP_TOL_S"]
