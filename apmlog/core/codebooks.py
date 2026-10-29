"""apmlog.core.codebooks —— 数值码→人类可读文本。

设计原则（重要）
----------------
**不猜。** ArduCopter 4.6/4.7 的 ``ERR.Subsys``、``EV.Id``、``ARM.Method``、``MODE`` 编号等
枚举会随版本变化，任何没有出处的映射都可能给出错误结论。因此：

1. 映射表来自外部 JSON：``data/codebooks/copter_4x.json``（由源码核对工作流交付）。
   JSON 不存在或某码未收录时，一律返回 ``None``，由 events.py 生成
   ``"Subsys=12 ECode=1（未映射）"`` 这类**只陈述事实**的文本。
2. 少数映射可以由**日志自身证据**确立（同一毫秒内其它报文的原文），这类会显式标注
   ``provenance='derived-from-log-cooccurrence'``，见 :data:`LOG_DERIVED_EV`。
3. ERR 的可读性主要靠**同刻 MSG 原文**（炸机日志里 ``ERR{Subsys:12,ECode:1}`` 与
   ``MSG 'Crash: Disarming: AngErr=171>30, Accel=0.0<3.0'`` 同刻成组出现），
   不依赖码表也能给出正确信息。

JSON 期望结构（缺失字段容忍）::

    {
      "version": "1",
      "source": {"project": "ArduPilot", "tags": ["Copter-4.6.1", "Copter-4.7.0"],
                 "files": {"mode": "ArduCopter/mode.h", "ev": "libraries/AP_Stats/AP_Stats.h",
                           "err": "ArduCopter/defines.h", "arm": "...", "motb": "..."}},
      "mode_numbers":        {"0": "STABILIZE", "2": "ALTHOLD", ...},
      "error_subsystems":    {"12": {"name": "...", "ecodes": {"1": "..."}, "severity": "critical"}},
      "event_ids":           {"10": "ARMING", ...},
      "arm_methods":         {"0": "...", "12": "..."},
      "arming_checks_bits":  {"3": "...", ...},
      "motb_failflags_bits": {"0": "...", ...},
      "unverified":          ["..."]
    }
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

#: 默认码表位置（相对工程根）
DEFAULT_CODEBOOK_PATH = Path(__file__).resolve().parents[2] / "data" / "codebooks" / "copter_4x.json"

#: 仅由本项目真实日志同刻报文推导出的映射（不来自枚举源码，标注清楚）
LOG_DERIVED: dict[str, dict[str, tuple[str, str]]] = {
    "event_ids": {
        # 165/166：EV Id=10 与 ARM{ArmState:1} 同刻，Id=11 与 ARM{ArmState:0} 同刻
        "10": ("解锁（与 ARM ArmState=1 同刻）", "info"),
        "11": ("上锁（与 ARM ArmState=0 同刻）", "info"),
    },
}

#: MSG 文本关键字 → 严重度。**这是启发式判级，不是 ArduPilot 定义**，故只用于排序与着色，
#: 不用于编造语义；原文始终照实展示。
MSG_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("critical", ("crash", "terminat", "panic", "hardfault", "hard fault")),
    ("error", ("failsafe", "fail-safe", "error", "failure", "failed", "lost", "reject")),
    ("warn", ("warning", "check", "jamming", "interference", "prearm", "arming fail",
              "brownout", "brown-out", "undervolt", "vibration", "reset", "out of", "not armed")),
)


def msg_severity(text: str) -> str:
    """按关键字给 MSG 一个严重度（供排序/着色）。未命中即 info。"""
    low = (text or "").lower()
    for sev, words in MSG_KEYWORDS:
        if any(w in low for w in words):
            return sev
    return "info"


class Codebook:
    """只读码表封装。"""

    def __init__(self, data: Optional[dict[str, Any]] = None, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self.data: dict[str, Any] = data or {}
        self.loaded = bool(data)
        self.source = self.data.get("source", {}) if isinstance(self.data, dict) else {}
        self._merge_derived()

    def _merge_derived(self) -> None:
        """把 LOG_DERIVED 合进表里，但保留外部 JSON 的同键（源码优先）。"""
        for section, mapping in LOG_DERIVED.items():
            cur = self.data.setdefault(section, {})
            if not isinstance(cur, dict):
                continue
            for k, v in mapping.items():
                cur.setdefault(k, {"name": v[0], "severity": v[1], "provenance": "log-cooccurrence"})

    @property
    def provenance(self) -> str:
        tags = self.source.get("tags") if isinstance(self.source, dict) else None
        if not self.loaded:
            return "无码表（仅显示原始码值）"
        if tags:
            return "ArduPilot " + "/".join(str(t) for t in tags)
        return "外部码表（无版本标注）"

    # ------------------------------------------------------------------ #
    def _table(self, section: str) -> dict[str, Any]:
        t = self.data.get(section)
        return t if isinstance(t, dict) else {}

    @staticmethod
    def _as_text(val: Any) -> Optional[tuple[str, str]]:
        """把表项归一化成 (文本, 严重度)。"""
        if val is None:
            return None
        if isinstance(val, str):
            return (val, "")
        if isinstance(val, dict):
            name = val.get("name") or val.get("text") or val.get("desc")
            if not name:
                return None
            return (str(name), str(val.get("severity", "")))
        return None

    def mode_name(self, num: Optional[int]) -> Optional[str]:
        if num is None:
            return None
        got = self._as_text(self._table("mode_numbers").get(str(int(num))))
        return got[0] if got else None

    def err_info(self, subsys: Optional[int], ecode: Optional[int]) -> tuple[Optional[str], str]:
        """返回 (可读文本 或 None, 严重度)。"""
        if subsys is None:
            return (None, "error")
        ent = self._table("error_subsystems").get(str(int(subsys)))
        if not isinstance(ent, dict):
            got = self._as_text(ent)
            if not got:
                return (None, "error")
            return (got[0], got[1] or "error")
        name = ent.get("name") or ent.get("text")
        sev = ent.get("severity") or "error"
        ec_text = None
        ecodes = ent.get("ecodes")
        if isinstance(ecodes, dict) and ecode is not None:
            got = self._as_text(ecodes.get(str(int(ecode))))
            ec_text = got[0] if got else None
        if name is None:
            return (None, sev)
        parts = [str(name)]
        if ec_text:
            parts.append(f"（ECode {ecode}: {ec_text}）")
        elif ecode is not None:
            parts.append(f"（ECode {ecode} 未映射）")
        return (" ".join(parts), sev)

    def ev_text(self, ev_id: Optional[int]) -> tuple[Optional[str], str]:
        if ev_id is None:
            return (None, "info")
        got = self._as_text(self._table("event_ids").get(str(int(ev_id))))
        if not got:
            return (None, "info")
        prov = self._table("event_ids").get(str(int(ev_id)))
        sev = got[1]
        if isinstance(prov, dict):
            sev = prov.get("severity") or "info"
        return (got[0], sev)

    def arm_method(self, method: Optional[int]) -> Optional[str]:
        if method is None:
            return None
        got = self._as_text(self._table("arm_methods").get(str(int(method))))
        return got[0] if got else None

    def arming_checks(self, mask: Optional[int]) -> Optional[list[str]]:
        """位掩码 → 名称列表；表缺失返回 None（调用方原样显示数值）。"""
        table = self._table("arming_checks_bits")
        if not table or mask is None:
            return None
        out = []
        m = int(mask)
        for bit in range(32):
            if m & (1 << bit):
                got = self._as_text(table.get(str(bit)))
                out.append(got[0] if got else f"bit{bit}")
        return out

    def motb_flags(self, bits: Optional[int]) -> Optional[list[str]]:
        table = self._table("motb_failflags_bits")
        if not table or bits is None:
            return None
        out = []
        v = int(bits)
        for bit in range(16):
            if v & (1 << bit):
                got = self._as_text(table.get(str(bit)))
                out.append(got[0] if got else f"bit{bit}")
        return out

    def unmapped_report(self, observed: dict[str, Iterable[int]]) -> dict[str, list[int]]:
        """给定各表观测到的码值集合，返回未映射部分（供验收脚本使用）。"""
        out: dict[str, list[int]] = {}
        for section, values in observed.items():
            table = self._table(section)
            missing = sorted({int(v) for v in values if str(int(v)) not in table})
            if missing:
                out[section] = missing
        return out


def load_codebook(path: Optional[str | Path] = None) -> Codebook:
    """加载码表 JSON；不存在则返回空表（一切照实显示原始码值）。"""
    p = Path(path) if path else DEFAULT_CODEBOOK_PATH
    if not p.exists():
        log.info("码表不存在，使用无映射模式: %s", p)
        return Codebook(None, p)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # JSON 坏掉也不能拖垮主程序
        log.warning("码表解析失败(%s)，使用无映射模式: %s", exc, p)
        return Codebook(None, p)
    if not isinstance(data, dict):
        return Codebook(None, p)
    return Codebook(data, p)


__all__ = ["Codebook", "load_codebook", "msg_severity", "DEFAULT_CODEBOOK_PATH", "LOG_DERIVED"]
