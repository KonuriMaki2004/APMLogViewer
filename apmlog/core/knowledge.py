"""apmlog.core.knowledge —— 事件/告警的说明与处理建议（纯数据，不依赖 Qt）。

用途：用户在「事件」页双击一条警告/错误、或在「告警」页双击一条告警时，弹出对应的
问题说明 / 可能原因 / 处理建议。

设计口径（与 codebooks.py 一致：**不猜**）
------------------------------------------
1. 文本来自 ``data/knowledge/event_help.json``，是可维护的数据文件，不是散落在 UI 里的字符串；
2. 匹配有明确优先级：``by_code`` > ``by_subsys`` > ``by_msg``/``by_alert_rule`` > ``by_kind`` > fallback；
3. **永远返回非空条目**：数据文件缺失、键未收录、字段写漏，都会落到 ``fallback``（内容写在代码里），
   保证弹窗绝不空白，并明确告诉用户"该类型暂未收录"；
4. 知识库只提供工程经验整理，不冒充 ArduPilot 官方定义；码表里的类型名来自
   ``data/codebooks/copter_4x.json``，弹窗会分开显示。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

#: 知识库位置（相对工程根，与 codebooks 同层）
DEFAULT_HELP_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge" / "event_help.json"

#: 数据文件缺失时也要能弹出有内容的说明，这些是最后的兜底（不依赖外部文件）
_BUILTIN_EVENT_FALLBACK: dict[str, Any] = {
    "title": "该类型暂未收录具体说明",
    "summary": "这条消息的类型没有匹配到专门的说明与处理方案。以下是通用排查步骤。",
    "causes": ["该类型较新或较冷门，知识库尚未覆盖。",
               "该消息可能是固件自定义输出，不在通用码表内。"],
    "actions": ["对齐时间线看该时刻前后 10 s 的姿态/电压/振动曲线。",
                "找同一时刻附近的 MSG/ERR 事件，通常有更具体的原因。",
                "「事件」页右键导出 CSV，连同日志一起归档。",
                "不要仅凭这一条消息就改参数或刷固件。"],
}
_BUILTIN_ALERT_FALLBACK: dict[str, Any] = {
    "title": "该告警暂未收录具体说明",
    "summary": "这条告警没有匹配到专门的说明与处理方案。以下是通用排查步骤。",
    "causes": ["告警来自新规则或插件，知识库尚未覆盖。"],
    "actions": ["看告警标题里的数值与阈值，判断超出多少。",
                "在曲线页打开对应通道，确认是真实异常还是阈值过严。",
                "调整阈值前先确认硬件状态。"],
}


@dataclass(frozen=True)
class HelpEntry:
    """一条可直接渲染的说明。``matched=False`` 表示走的是兜底条目。"""

    title: str
    summary: str
    causes: tuple[str, ...]
    actions: tuple[str, ...]
    scope: str = "fallback"          # code / subsys / kind / msg / alert / resolved / plugin / fallback
    matched: bool = False
    refs: tuple[str, ...] = ()
    note: str = ""

    @property
    def is_fallback(self) -> bool:
        return not self.matched

    def as_text(self) -> str:
        """纯文本形态（复制到剪贴板 / 写报告用）。"""
        lines = [self.title, "", "问题说明：", self.summary, "", "可能原因："]
        lines += [f"{i}. {c}" for i, c in enumerate(self.causes, 1)] or ["（无）"]
        lines += ["", "处理建议："]
        lines += [f"{i}. {a}" for i, a in enumerate(self.actions, 1)] or ["（无）"]
        if self.refs:
            lines += ["", "参考："] + [f"- {r}" for r in self.refs]
        if self.note:
            lines += ["", self.note]
        return "\n".join(lines)


def _entry(data: Mapping[str, Any], scope: str, matched: bool,
           fallback: Mapping[str, Any]) -> HelpEntry:
    """把一条 JSON 字典收敛成 HelpEntry；缺字段用 fallback 补，保证不空白。"""
    title = str(data.get("title") or fallback.get("title") or "未收录类型").strip()
    summary = str(data.get("summary") or fallback.get("summary") or "").strip()
    causes = tuple(str(x).strip() for x in (data.get("causes") or fallback.get("causes") or ())
                   if str(x).strip())
    actions = tuple(str(x).strip() for x in (data.get("actions") or fallback.get("actions") or ())
                    if str(x).strip())
    refs = tuple(str(x).strip() for x in (data.get("refs") or ()) if str(x).strip())
    note = str(data.get("note") or "").strip()
    if not causes:
        causes = ("（未提供可能原因）",)
    if not actions:
        actions = ("按通用步骤排查：对齐时间线、看同刻 MSG、导出事件 CSV 归档。",)
    if not summary:
        summary = "该类型暂无详细说明。"
    return HelpEntry(title=title, summary=summary, causes=causes, actions=actions,
                     scope=scope, matched=matched, refs=refs, note=note)


class EventHelp:
    """知识库：事件与告警 → HelpEntry。"""

    def __init__(self, data: Optional[Mapping[str, Any]] = None,
                 path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self.data: dict[str, Any] = dict(data or {})
        self.loaded = bool(data)

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: str | Path | None = None) -> "EventHelp":
        p = Path(path) if path else DEFAULT_HELP_PATH
        if not p.exists():
            log.info("知识库不存在，使用内置兜底文案: %s", p)
            return cls(None, p)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001 坏文件不能拖垮界面
            log.warning("知识库解析失败(%s)，使用内置兜底文案: %s", exc, p)
            return cls(None, p)
        if not isinstance(data, dict):
            log.warning("知识库顶层不是对象，忽略: %s", p)
            return cls(None, p)
        return cls(data, p)

    # ------------------------------------------------------------------ 表访问
    def _table(self, name: str, kind: type = dict) -> Any:
        v = self.data.get(name)
        return v if isinstance(v, kind) else kind()

    def _rules(self) -> list[dict[str, Any]]:
        out = self._table("by_msg", list)
        return [r for r in out if isinstance(r, dict)]

    def _alert_rules(self) -> list[dict[str, Any]]:
        out = self._table("by_alert_rule", list)
        return [r for r in out if isinstance(r, dict)]

    def _fallback(self, which: str) -> Mapping[str, Any]:
        fb = self._table("fallback")
        got = fb.get(which) if isinstance(fb, dict) else None
        if isinstance(got, dict) and got:
            return got
        return _BUILTIN_EVENT_FALLBACK if which != "alert" else _BUILTIN_ALERT_FALLBACK

    # ------------------------------------------------------------------ 匹配
    def explain_event(self, *, kind: str, code: Optional[int] = None,
                      ecode: Optional[int] = None, text: str = "", decoded: str = "",
                      severity: str = "info", codebook_text: str = "") -> HelpEntry:
        """事件 → 说明。``kind`` 为 ``ERR/MSG/ARM/EV/MODE/CMD/FILE``。

        ERR 的判定顺序：具体错误码 → 已恢复 → 子系统 → 类型兜底 → 全局兜底；
        MSG 用关键字依次匹配 ``by_msg`` 第一条命中的规则。
        """
        k = str(kind or "").strip().upper()
        fb = self._fallback("event")

        if k == "ERR":
            by_code = self._table("by_code")
            key = f"{int(code)}:{int(ecode)}" if code is not None and ecode is not None else ""
            if key and isinstance(by_code.get(key), dict):
                return _entry(by_code[key], "code", True, fb)
            # 错误已恢复（ecode 0 / 码表文本含"已恢复"/RESOLVED）：不该给出"故障处理建议"
            resolved = self._table("resolved")
            if resolved and (ecode == 0 or "已恢复" in (codebook_text or "")
                             or "RESOLVED" in (codebook_text or "").upper()
                             or "已解除" in (codebook_text or "")):
                return _entry(resolved, "resolved", True, fb)
            by_subsys = self._table("by_subsys")
            if code is not None and isinstance(by_subsys.get(str(int(code))), dict):
                ent = _entry(by_subsys[str(int(code))], "subsys", True, fb)
                if codebook_text.strip():
                    ent = HelpEntry(**{**ent.__dict__,
                                       "note": f"码表含义：{codebook_text.strip()}"})
                return ent
            by_kind = self._table("by_kind")
            if isinstance(by_kind.get("ERR"), dict):
                ent = _entry(by_kind["ERR"], "kind", True, fb)
                note = f"未找到子系统 {code} 的专门条目，以下为该类事件的通用说明。"
                return replace(ent, note=f"{note} {ent.note}".strip())
            return _entry(fb, "fallback", False, fb)

        if k == "MSG":
            hay = f"{decoded} {text}".lower()
            for rule in self._rules():
                kws: Iterable[Any] = rule.get("match") or ()
                if any(str(w).lower() in hay for w in kws if str(w).strip()):
                    return _entry(rule, "msg", True, fb)
            by_kind = self._table("by_kind")
            if isinstance(by_kind.get("MSG"), dict):
                ent = _entry(by_kind["MSG"], "kind", True, fb)
                return replace(ent, note="未匹配到专门条目，以下为文本消息的通用说明。")
            return _entry(fb, "fallback", False, fb)

        by_kind = self._table("by_kind")
        if isinstance(by_kind.get(k), dict):
            return _entry(by_kind[k], "kind", True, fb)
        return _entry(fb, "fallback", False, fb)

    def explain_alert(self, *, rule: str, title: str = "", severity: str = "warn",
                      message: str = "") -> HelpEntry:
        """告警（AlertHit）→ 说明。按 ``by_alert_rule`` 的 ``prefix`` 前缀匹配。"""
        fb = self._fallback("alert")
        r = str(rule or "").strip().lower()
        if r.startswith("plugin:"):
            ent = self._table("fallback").get("alert_plugin") if self._table("fallback") else None
            data = ent if isinstance(ent, dict) and ent else {
                "title": "插件定义的告警",
                "summary": "这条告警由插件规则产生，宿主没有它的说明文本。",
                "causes": ["插件按自己的判据上报了这条告警。"],
                "actions": ["查看该插件的文档/源码里的规则说明。",
                            "在「插件」页确认该插件状态与版本。"],
            }
            e = _entry(data, "plugin", False, fb)
            if title:
                e = HelpEntry(**{**e.__dict__, "note": f"告警标题：{title}"})
            return e
        for item in self._alert_rules():
            pref = str(item.get("prefix") or "").strip().lower()
            if pref and (r == pref or r.startswith(pref)):
                e = _entry(item, "alert", True, fb)
                if message:
                    e = HelpEntry(**{**e.__dict__, "note": f"本条告警原文：{message}"})
                return e
        e = _entry(fb, "fallback", False, fb)
        if message:
            e = HelpEntry(**{**e.__dict__, "note": f"本条告警原文：{message}"})
        return e

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict[str, int]:
        return {"by_code": len(self._table("by_code")),
                "by_subsys": len(self._table("by_subsys")),
                "by_msg": len(self._rules()),
                "by_alert_rule": len(self._alert_rules()),
                "by_kind": len(self._table("by_kind"))}


_CACHE: Optional[EventHelp] = None


def load_help(path: str | Path | None = None, *, refresh: bool = False) -> EventHelp:
    """懒加载并缓存知识库（首次弹窗时才读文件，不拖慢启动）。"""
    global _CACHE
    if path is not None:
        return EventHelp.load(path)
    if _CACHE is None or refresh:
        _CACHE = EventHelp.load()
    return _CACHE


__all__ = ["DEFAULT_HELP_PATH", "EventHelp", "HelpEntry", "load_help"]
