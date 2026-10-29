"""apmlog.core.model —— 全工程共享的数据契约。

UI 组件只依赖本模块与 numpy，不得反向依赖内核实现或主窗。
字段语义见 docs/CONTRACT.md 第 2 节。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import numpy as np

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 事件/告警严重度，从小到大
SEVERITY_LEVELS: tuple[str, ...] = ("info", "hint", "warn", "error", "critical")
_SEV_INDEX = {s: i for i, s in enumerate(SEVERITY_LEVELS)}

#: 用于区分子实例的候选字段（按优先级）。见 CONTRACT 1 节：4.6/4.7 用 I/Inst/C 合并成单类型。
INSTANCE_FIELDS: tuple[str, ...] = ("I", "Inst", "C", "IMU", "ID")

#: 唯一合法时间轴字段（µs，自 boot 起算）
TIME_FIELD: str = "TimeUS"

#: 事件 kind
EVENT_KINDS: tuple[str, ...] = ("ERR", "EV", "MSG", "ARM", "MODE", "CMD", "FILE", "THM", "ALERT")


def severity_rank(sev: str) -> int:
    """严重度转序号，未知按 info 处理。"""
    return _SEV_INDEX.get(sev, 0)


def severity_at_least(sev: str, minimum: Optional[str]) -> bool:
    if minimum is None:
        return True
    return severity_rank(sev) >= severity_rank(minimum)


# --------------------------------------------------------------------------- #
# 通道
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, eq=True)
class ChannelKey:
    """一条数据通道 = 报文类型 + 字段 + 可选子实例号。"""

    type: str
    field: str
    inst: Optional[int] = None

    @property
    def key(self) -> str:
        """机器可读唯一键，例如 ``VIBE#1.VibeX`` / ``VIBE.VibeX``。"""
        if self.inst is None:
            return f"{self.type}.{self.field}"
        return f"{self.type}#{self.inst}.{self.field}"

    @property
    def label(self) -> str:
        """人类可读，例如 ``VIBE[1].VibeX``。"""
        if self.inst is None:
            return f"{self.type}.{self.field}"
        return f"{self.type}[{self.inst}].{self.field}"

    def with_inst(self, inst: Optional[int]) -> "ChannelKey":
        return ChannelKey(self.type, self.field, inst)

    def without_inst(self) -> "ChannelKey":
        return ChannelKey(self.type, self.field, None)

    @staticmethod
    def parse(text: str) -> "ChannelKey":
        """解析 ``key``（VIBE#1.VibeX）/ ``label``（VIBE[1].VibeX）/ ``TYPE.field`` 三种写法。"""
        m = re.fullmatch(r"\s*([A-Za-z0-9_]+)(?:[#\[](\d+)\]?)?\.([A-Za-z0-9_]+)\s*", text)
        if not m:
            raise ValueError(f"无法解析通道键: {text!r}")
        inst = m.group(2)
        return ChannelKey(m.group(1), m.group(3), None if inst is None else int(inst))

    def __str__(self) -> str:  # pragma: no cover - 便于调试
        return self.key


_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_]+)(?:#(\d+))?\.([A-Za-z0-9_]+)\s*$")


def as_key(obj: "ChannelKey | str") -> ChannelKey:
    """把字符串或 ChannelKey 统一成 ChannelKey。"""
    if isinstance(obj, ChannelKey):
        return obj
    return ChannelKey.parse(obj)


@dataclass
class ChannelInfo:
    """通道的元数据（来自缓存 index.json，供通道树与量程自适应使用）。"""

    key: ChannelKey
    dtype: str = "f8"
    count: int = 0
    t0: float = float("nan")
    t1: float = float("nan")
    vmin: float = float("nan")
    vmax: float = float("nan")
    unit: str = ""
    coltype: str = ""       # DataFlash FMT 列类型字符

    @property
    def rate(self) -> float:
        dt = self.t1 - self.t0
        if dt and dt > 0 and self.count > 1:
            return (self.count - 1) / dt
        return 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.key,
            "label": self.key.label,
            "type": self.key.type,
            "field": self.key.field,
            "inst": self.key.inst,
            "dtype": self.dtype,
            "count": self.count,
            "t0": self.t0,
            "t1": self.t1,
            "vmin": self.vmin,
            "vmax": self.vmax,
            "unit": self.unit,
            "coltype": self.coltype,
            "rate": self.rate,
        }


@dataclass
class Series:
    """一条时间序列。t 为秒（自 boot），单调不减。"""

    key: ChannelKey
    t: np.ndarray
    v: np.ndarray
    unit: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        self.t = np.asarray(self.t, dtype=np.float64)
        self.v = np.asarray(self.v, dtype=np.float64)
        if not self.label:
            self.label = self.key.label
        if len(self.t) != len(self.v):
            raise ValueError(f"{self.key.label}: t/v 长度不一致 {len(self.t)}!={len(self.v)}")

    @property
    def n(self) -> int:
        return int(self.t.size)

    @property
    def valid(self) -> np.ndarray:
        return np.isfinite(self.v)

    def span(self) -> tuple[float, float]:
        if self.t.size == 0:
            return (float("nan"), float("nan"))
        return (float(self.t[0]), float(self.t[-1]))

    def slice(self, t0: Optional[float] = None, t1: Optional[float] = None) -> "Series":
        lo = -math.inf if t0 is None else t0
        hi = math.inf if t1 is None else t1
        idx = np.nonzero((self.t >= lo) & (self.t <= hi))[0]
        return Series(self.key, self.t[idx], self.v[idx], self.unit, self.label)

    def stats(self) -> dict[str, float]:
        v = self.v[np.isfinite(self.v)]
        if v.size == 0:
            return {k: float("nan") for k in ("min", "max", "mean", "std", "rms", "p05", "p50", "p95", "n")}
        return {
            "min": float(v.min()),
            "max": float(v.max()),
            "mean": float(v.mean()),
            "std": float(v.std()),
            "rms": float(np.sqrt(np.mean(v * v))),
            "p05": float(np.percentile(v, 5)),
            "p50": float(np.percentile(v, 50)),
            "p95": float(np.percentile(v, 95)),
            "n": float(v.size),
        }


@dataclass
class Envelope:
    """分桶 min/max 包络，用于大数据量绘图。"""

    key: ChannelKey
    t: np.ndarray           # 桶中心（秒）
    vmin: np.ndarray
    vmax: np.ndarray
    vmean: np.ndarray
    count: np.ndarray
    unit: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        for name in ("t", "vmin", "vmax", "vmean", "count"):
            setattr(self, name, np.asarray(getattr(self, name), dtype=np.float64))
        if not self.label:
            self.label = self.key.label


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #


@dataclass
class EventRec:
    """统一事件记录；``decoded`` 为空时 UI 必须退回显示 ``text``。"""

    t: float
    kind: str
    code: Optional[int] = None
    ecode: Optional[int] = None
    text: str = ""
    decoded: str = ""
    severity: str = "info"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        """优先显示解码文本，附带原始码，绝不编造含义。"""
        if self.decoded and self.decoded != self.text:
            return f"{self.decoded}（原文: {self.text}）" if self.text else self.decoded
        return self.text or self.decoded or f"{self.kind} code={self.code}"

    @property
    def rank(self) -> int:
        return severity_rank(self.severity)

    def as_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "kind": self.kind,
            "code": self.code,
            "ecode": self.ecode,
            "text": self.text,
            "decoded": self.decoded,
            "display": self.display,
            "severity": self.severity,
            "extra": dict(self.extra),
        }


# --------------------------------------------------------------------------- #
# 航迹
# --------------------------------------------------------------------------- #


@dataclass
class TrackResult:
    """位置轨迹。

    ``kind='geodetic'`` 时 lat/lon 有效（WGS-84）；``kind='local'`` 时只有 e/n/u。
    两种情况下 e/n/u 都会给出（geodetic 时以首个有效点或 home 为原点的局部 ENU）。
    """

    kind: str = "local"
    t: np.ndarray = field(default_factory=lambda: np.empty(0, np.float64))
    lat: np.ndarray = field(default_factory=lambda: np.empty(0, np.float64))
    lon: np.ndarray = field(default_factory=lambda: np.empty(0, np.float64))
    alt: np.ndarray = field(default_factory=lambda: np.empty(0, np.float64))
    e: np.ndarray = field(default_factory=lambda: np.empty(0, np.float64))
    n: np.ndarray = field(default_factory=lambda: np.empty(0, np.float64))
    u: np.ndarray = field(default_factory=lambda: np.empty(0, np.float64))
    speed: np.ndarray = field(default_factory=lambda: np.empty(0, np.float64))
    src: np.ndarray = field(default_factory=lambda: np.empty(0, np.int8))
    src_names: list[str] = field(default_factory=list)
    origin: Optional[tuple[float, float, float]] = None      # (lat, lon, alt) 或 ENU 原点说明
    home: Optional[tuple[float, float, float]] = None
    distance: float = float("nan")
    max_speed: float = float("nan")
    climb_min: float = float("nan")
    climb_max: float = float("nan")
    note: str = ""
    #: 多轨迹：唯一键（POS / GPS#0 / GPS#1 / XKF1 / VISP）与勾选栏直接显示的中文名。
    #: 旧缓存/旧调用点不传就是空串，所以必须放在最后并给默认值。
    key: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        for name in ("t", "lat", "lon", "alt", "e", "n", "u", "speed", "src"):
            arr = np.asarray(getattr(self, name))
            setattr(self, name, arr.astype(np.float64 if name != "src" else np.int8, copy=False))

    @property
    def n_points(self) -> int:
        return int(self.t.size)

    @property
    def has_geodetic(self) -> bool:
        return self.kind == "geodetic" and bool(np.isfinite(self.lat).any()) and bool(np.isfinite(self.lon).any())

    def alt_range(self) -> tuple[float, float]:
        a = self.alt[np.isfinite(self.alt)] if self.alt.size else np.empty(0)
        if a.size == 0:
            a = self.u[np.isfinite(self.u)] if self.u.size else np.empty(0)
        if a.size == 0:
            return (float("nan"), float("nan"))
        return (float(a.min()), float(a.max()))

    def bounds_enu(self) -> tuple[float, float, float, float]:
        """返回 (e_min, e_max, n_min, n_max)，忽略 nan。"""
        e = self.e[np.isfinite(self.e)] if self.e.size else np.empty(0)
        n = self.n[np.isfinite(self.n)] if self.n.size else np.empty(0)
        if e.size == 0 or n.size == 0:
            return (float("nan"),) * 4
        return (float(e.min()), float(e.max()), float(n.min()), float(n.max()))

    def source_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i, name in enumerate(self.src_names):
            out[name] = int(np.count_nonzero(self.src == i)) if self.src.size else 0
        return out

    def resample(self, max_points: int) -> "TrackResult":
        """等间隔抽稀，保留首末点，用于地图与缩略图。"""
        k = self.n_points
        if k <= max_points or max_points < 2:
            return self
        idx = np.unique(np.linspace(0, k - 1, max_points).round().astype(int))
        return TrackResult(
            kind=self.kind,
            t=self.t[idx], lat=self.lat[idx], lon=self.lon[idx], alt=self.alt[idx],
            e=self.e[idx], n=self.n[idx], u=self.u[idx], speed=self.speed[idx],
            src=self.src[idx], src_names=list(self.src_names), origin=self.origin, home=self.home,
            distance=self.distance, max_speed=self.max_speed,
            climb_min=self.climb_min, climb_max=self.climb_max, note=self.note,
        )


# --------------------------------------------------------------------------- #
# 告警
# --------------------------------------------------------------------------- #


@dataclass
class AlertHit:
    """一条命中的告警。``source`` 标明阈值出处，UI 必须展示。"""

    rule: str
    title: str
    severity: str = "warn"
    t0: float = float("nan")
    t1: float = float("nan")
    value: float = float("nan")
    threshold: float = float("nan")
    unit: str = ""
    channel: Optional[ChannelKey] = None
    message: str = ""
    evidence: list[ChannelKey] = field(default_factory=list)
    source: str = "default-ref"

    @property
    def rank(self) -> int:
        return severity_rank(self.severity)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "title": self.title,
            "severity": self.severity,
            "t0": self.t0,
            "t1": self.t1,
            "value": self.value,
            "threshold": self.threshold,
            "unit": self.unit,
            "channel": self.channel.key if self.channel else None,
            "channel_label": self.channel.label if self.channel else "",
            "message": self.message,
            "evidence": [c.key for c in self.evidence],
            "source": self.source,
        }


# --------------------------------------------------------------------------- #
# 概要
# --------------------------------------------------------------------------- #


@dataclass
class LogStats:
    """单份日志的概要，日志列表与状态栏使用。"""

    path: str = ""
    log_id: str = ""
    size_bytes: int = 0
    duration: float = float("nan")
    t_start: float = float("nan")
    t_end: float = float("nan")
    takeoff_t: Optional[float] = None
    land_t: Optional[float] = None
    firmware: str = ""
    board: str = ""
    frame_type: str = ""
    git: str = ""
    n_messages: int = 0
    n_types: int = 0
    n_params: int = 0
    n_events: int = 0
    mode_seq: list[str] = field(default_factory=list)
    max_alt: float = float("nan")
    distance: float = float("nan")
    max_speed: float = float("nan")
    pos_source: str = ""
    pos_kind: str = ""
    crashes: list[str] = field(default_factory=list)
    segments: list[tuple[float, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def duration_str(self) -> str:
        if not math.isfinite(self.duration):
            return "--:--"
        s = int(round(self.duration))
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"

    def as_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["duration_str"] = self.duration_str
        return d


def fmt_value(v: float, unit: str = "", digits: int = 3) -> str:
    """统一的读数格式化：nan/inf 显示为 '-'。"""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    if abs(v) >= 1e6:
        return f"{v:.4g}{unit}"
    return f"{v:.{digits}f}{unit}".rstrip("0").rstrip(".") + unit


def human_size(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{int(nbytes)} B"
        nbytes /= 1024.0
    return f"{nbytes:.1f} TB"


__all__ = [
    "SEVERITY_LEVELS", "INSTANCE_FIELDS", "TIME_FIELD", "EVENT_KINDS",
    "severity_rank", "severity_at_least",
    "ChannelKey", "as_key", "ChannelInfo", "Series", "Envelope",
    "EventRec", "TrackResult", "AlertHit", "LogStats",
    "fmt_value", "human_size",
]
