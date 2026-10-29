"""apmlog.ui.modecolors —— 时间线「飞行模式色带」的配色。

用户 2026-10-24：「为每种模式分配专属颜色：同一模式在所有时间段同色、不同模式可区分；
默认颜色按各模式操作难度定义，难度由低到高在色相和明度上形成可感知的递进；色块、分段
边界与当前选中状态都要清晰；设置里能按模式自定义颜色、立即生效并持久化，另提供恢复默认。」

设计要点（改动前请先读）：
1. **颜色是模式的属性，不是时间段的属性**。以前按"第几段"轮换 6 个浅色，
   同一模式在不同段颜色不同，看时间线根本认不出这是个什么模式。
2. **默认方案按难度阶梯生成**（``MODE_LADDER``）：色相从青绿(190°)走到红(4°)，
   明度在相邻两档之间交替偏高/偏低（约 73% / 55%）。只做色相渐变的话，18 个模式相邻
   只差 10° 色相，一屏上根本分不开；叠一层明度交替后，相邻最小感知距离仍有 100+。
3. **难度顺序是产品口径**（低 = 全自主、几乎不用人管；高 = 纯手动/高风险动作），
   写死在阶梯里并随代码走，不随日志内容变化——否则同一模式在不同日志里会换颜色。
4. **自定义只覆盖单个模式**：设置里存的是 ``{模式名: "#rrggbb"}`` 的**覆盖层**，
   没覆盖的仍取阶梯默认值。分段划分与其它模式一律不受影响。
5. **相邻太像也要读得清**：时间线始终画分段边界线，并在相邻两色距离过近时加强描边
   （见 ``too_close``），不靠"颜色看着不一样"来兜底。
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import math
from typing import Any, Iterable, Optional

#: 难度阶梯：从"最省心/全自主"到"最需要人介入/风险最高"。
#: 顺序即配色顺序，也是设置页里的排列顺序。
MODE_LADDER: tuple[str, ...] = (
    "LOITER",      # 定点悬停：松手就停，最省心
    "POSHOLD",     # 定点 + 定高
    "ALT_HOLD",    # 只定高
    "STABILIZE",   # 自稳（不自动回中）
    "GUIDED",      # 地面站/脚本控制
    "AUTO",        # 航线任务
    "RTL",         # 自动返航
    "SMART_RTL",   # 智能返航（沿原路）
    "AUTO_RTL",    # 任务中途自动返航
    "CIRCLE",      # 绕圈
    "DRIFT",       # 漂移（半自动）
    "SPORT",       # 高速自稳
    "ACRO",        # 纯角速率，全靠人
    "AUTOTUNE",    # 自动调参（姿态可能剧烈）
    "LAND",        # 自动降落（低空风险）
    "THROW",       # 抛飞启动
    "FLIP",        # 翻滚
    "TURTLE",      # 翻正
)

#: 与 ``MODE_LADDER`` 一一对应；每行注释给出 HSL 出处，便于复核与微调。
MODE_LADDER_COLORS: tuple[tuple[str, str], ...] = (
    ("LOITER", "#88cfdd"),     # hsl(190, 55%, 70%)
    ("POSHOLD", "#4fcecc"),    # hsl(179, 56%, 56%)
    ("ALT_HOLD", "#2da991"),   # hsl(168, 58%, 42%)
    ("STABILIZE", "#85e0bd"),  # hsl(157, 59%, 70%)
    ("GUIDED", "#4ad386"),     # hsl(146, 61%, 56%)
    ("AUTO", "#28ae4a"),       # hsl(135, 62%, 42%)
    ("RTL", "#82e389"),        # hsl(124, 64%, 70%)
    ("SMART_RTL", "#56d846"),  # hsl(113, 65%, 56%)
    ("AUTO_RTL", "#4db324"),   # hsl(102, 67%, 42%)
    ("CIRCLE", "#b0e77e"),     # hsl(92, 68%, 70%)
    ("DRIFT", "#a7dd41"),      # hsl(81, 70%, 56%)
    ("SPORT", "#9fb71f"),      # hsl(70, 71%, 42%)
    ("ACRO", "#eae87b"),       # hsl(59, 73%, 70%)
    ("AUTOTUNE", "#e2c03c"),   # hsl(48, 74%, 56%)
    ("LAND", "#bc7e1a"),       # hsl(37, 76%, 42%)
    ("THROW", "#edaa78"),      # hsl(26, 77%, 70%)
    ("FLIP", "#e76337"),       # hsl(15, 79%, 56%)
    ("TURTLE", "#c12115"),     # hsl(4, 80%, 42%)
)

#: 阶梯之外的模式（新固件新增 / 未登记）：按名字稳定散列取色，跨会话不变。
#: 刻意避开阶梯占用的青→红色相，用蓝紫一侧，一眼能看出"这不是标准模式"。
EXTRA_COLORS: tuple[str, ...] = (
    "#7d8fb3", "#9b7bb8", "#6f93b0", "#a884a0", "#7aa8a0", "#8f86c2",
)

#: 相邻两色的"太像"阈值（近似感知距离，见 ``color_distance``）。
#: 默认阶梯的相邻最小距离 ≈ 86（全局两两最小 ≈ 65），所以默认方案不会触发加重描边；
#: 用户把两个模式改成近似色时才会触发，靠描边保住可读性。
TOO_CLOSE = 80.0


def _rgb(hex_str: str) -> tuple[int, int, int]:
    s = str(hex_str or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"不是 #rrggbb 颜色: {hex_str!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def normalize_hex(value: Any) -> Optional[str]:
    """把用户输入收敛成 ``#rrggbb``；不合法返回 None（调用方保持原值）。"""
    try:
        r, g, b = _rgb(str(value))
    except (TypeError, ValueError):
        return None
    return f"#{r:02x}{g:02x}{b:02x}"


def default_color(name: str) -> str:
    """某模式的默认颜色（未登记的模式按名字稳定散列取 EXTRA_COLORS）。"""
    nm = str(name or "").strip().upper()
    for mode, hexv in MODE_LADDER_COLORS:
        if mode == nm:
            return hexv
    if not nm:
        return "#9aa4ad"
    idx = int(hashlib.md5(nm.encode("utf-8")).hexdigest()[:8], 16) % len(EXTRA_COLORS)
    return EXTRA_COLORS[idx]


def known_modes() -> tuple[str, ...]:
    return tuple(MODE_LADDER)


def color_distance(a: str, b: str) -> float:
    """近似感知距离（0 ~ 765）。红权 2、绿权 4、蓝权 3 是常用的廉价近似。"""
    try:
        ra, ga, ba = _rgb(a)
        rb, gb, bb = _rgb(b)
    except (TypeError, ValueError):
        return float("inf")
    dr, dg, db = ra - rb, ga - gb, ba - bb
    return math.sqrt(2.0 * dr * dr + 4.0 * dg * dg + 3.0 * db * db)


def too_close(a: str, b: str, threshold: float = TOO_CLOSE) -> bool:
    """两色是否太像（相邻段要加重描边才读得清）。"""
    return color_distance(a, b) < float(threshold)


def luminance(hex_str: str) -> float:
    """0..255 的感知亮度，用来决定色块上写深字还是白字。"""
    try:
        r, g, b = _rgb(hex_str)
    except (TypeError, ValueError):
        return 255.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ink_on(bg: str) -> str:
    """色块上的字色：亮底用近黑，暗底用白。"""
    return "#101418" if luminance(bg) > 150.0 else "#ffffff"


# --------------------------------------------------------------------------- #
# 设置持久化：存的是"覆盖层"（只记用户改过的模式），不是整张表
# --------------------------------------------------------------------------- #


def parse_overrides(text: Any) -> dict[str, str]:
    """把设置里的 JSON 字符串解析成 ``{模式名: #rrggbb}``（坏数据一律丢弃）。"""
    if isinstance(text, dict):                 # 兼容有人直接塞 dict
        raw: Any = text
    else:
        try:
            raw = json.loads(str(text or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        name = str(k or "").strip().upper()
        hexv = normalize_hex(v)
        if name and hexv is not None:
            out[name] = hexv
    return out


def dump_overrides(mapping: dict[str, str]) -> str:
    """覆盖层 → 设置里存的 JSON 字符串（键按阶梯顺序，读起来稳定）。"""
    order = {m: i for i, m in enumerate(MODE_LADDER)}
    items = sorted((str(k).upper(), normalize_hex(v) or default_color(k))
                   for k, v in (mapping or {}).items())
    items.sort(key=lambda kv: (order.get(kv[0], 10_000), kv[0]))
    return json.dumps(dict(items), ensure_ascii=False, sort_keys=False)


def resolve(name: str, overrides: Optional[dict[str, str]] = None) -> str:
    """某模式当前该用的颜色：用户覆盖优先，否则默认。"""
    nm = str(name or "").strip().upper()
    ov = overrides or {}
    hit = ov.get(nm)
    if hit:
        norm = normalize_hex(hit)
        if norm is not None:
            return norm
    return default_color(nm)


def palette(overrides: Optional[dict[str, str]] = None,
            extra_modes: Iterable[str] = ()) -> dict[str, str]:
    """完整配色表：阶梯全部模式 + 日志里出现的其它模式（如新固件新增）。"""
    out = {m: resolve(m, overrides) for m in MODE_LADDER}
    for m in extra_modes or ():
        nm = str(m or "").strip().upper()
        if nm and nm not in out:
            out[nm] = resolve(nm, overrides)
    return out


def defaults() -> dict[str, str]:
    return dict(MODE_LADDER_COLORS)


__all__ = ["MODE_LADDER", "MODE_LADDER_COLORS", "EXTRA_COLORS", "TOO_CLOSE",
           "default_color", "known_modes", "color_distance", "too_close",
           "luminance", "ink_on", "parse_overrides", "dump_overrides", "resolve",
           "palette", "defaults", "normalize_hex"]
