"""apmlog.core.store —— 面向 UI 的只读查询层（docs/CONTRACT.md 第 3 节 API 的唯一实现）。

缓存命中直接 mmap 读取；未命中调用 :mod:`apmlog.core.reader` 构建。
所有返回数组都是新对象或只读 memmap，UI 不得原地修改。
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from . import reader as _rd
from .codebooks import Codebook, load_codebook
from .events import compress_mode_seq, decode as decode_events
from .events import find_crash_groups, mode_segments
from .model import (AlertHit, ChannelInfo, ChannelKey, Envelope, EventRec, LogStats,
                    Series, TrackResult, severity_at_least)

log = logging.getLogger(__name__)

#: 只在有把握的字段上标注单位；不确定就留空，避免把量纲读错
UNIT_HINTS: dict[tuple[str, str], str] = {
    ("ATT", "Roll"): "°", ("ATT", "Pitch"): "°", ("ATT", "Yaw"): "°",
    ("ANG", "Roll"): "°", ("ANG", "Pitch"): "°", ("ANG", "Yaw"): "°",
    ("ANG", "DesRoll"): "°", ("ANG", "DesPitch"): "°", ("ANG", "DesYaw"): "°",
    **{(f"ANG", x): "°/s" for x in ()},
    ("RATE", "RDes"): "°/s", ("RATE", "R"): "°/s", ("RATE", "ROut"): "°/s",
    ("RATE", "PDes"): "°/s", ("RATE", "P"): "°/s", ("RATE", "POut"): "°/s",
    ("RATE", "YDes"): "°/s", ("RATE", "Y"): "°/s", ("RATE", "YOut"): "°/s",
    ("VIBE", "VibeX"): "m/s²", ("VIBE", "VibeY"): "m/s²", ("VIBE", "VibeZ"): "m/s²",
    ("BARO", "Alt"): "m", ("BARO", "AltAMSL"): "m", ("BARO", "Press"): "Pa", ("BARO", "Temp"): "°C",
    ("BAT", "Volt"): "V", ("BAT", "VoltR"): "V", ("BAT", "Curr"): "A",
    ("BAT", "Temp"): "°C", ("BAT", "RemPct"): "%",
    ("POS", "Alt"): "m", ("POS", "RelHomeAlt"): "m", ("POS", "RelOriginAlt"): "m",
    ("GPS", "Alt"): "m", ("GPS", "Spd"): "m/s", ("GPS", "VZ"): "m/s",
    ("XKF1", "VN"): "m/s", ("XKF1", "VE"): "m/s", ("XKF1", "VD"): "m/s",
    ("XKF1", "PN"): "m", ("XKF1", "PE"): "m", ("XKF1", "PD"): "m",
    ("MSG",): "",
}
_NON_COLS = frozenset({"t", "tc", "inst", "seg"})


def _unit_for(type_: str, field_: str) -> str:
    if (type_, field_) in UNIT_HINTS:
        return UNIT_HINTS[(type_, field_)]
    if field_.startswith("C") and field_[1:].isdigit() and type_ in ("RCOU", "RCO2", "RCI2", "RCIN"):
        return "µs"
    return UNIT_HINTS.get((type_, field_), "")


def envelope_from(t: np.ndarray, v: np.ndarray, key: ChannelKey, unit: str = "",
                  t0: Optional[float] = None, t1: Optional[float] = None,
                  buckets: int = 2000) -> Envelope:
    """按时间分桶算 min/max/mean/count。

    抽成模块级函数是为了让**插件合成通道**用同一套分桶算法：之前插件通道走
    ChannelSource 时没有 envelope，build_lanes 直接把它当成“取数异常”整条跳过
    （实测：真日志上 5 条泳道全部被跳）。
    """
    t = np.asarray(t, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    us = unit or _unit_for(key.type, key.field)
    if t.size == 0:
        return Envelope(key, np.empty(0), np.empty(0), np.empty(0), np.empty(0),
                        np.empty(0), us)
    a = t[0] if t0 is None else float(t0)
    b = t[-1] if t1 is None else float(t1)
    if not (math.isfinite(a) and math.isfinite(b)) or b <= a:
        a, b = float(t[0]), float(t[-1])
        if b <= a:
            b = a + 1.0
    edges = np.linspace(a, b, int(buckets) + 1)
    pos = np.searchsorted(t, edges)
    ct = np.maximum(np.diff(pos), 0)
    centers = (edges[:-1] + edges[1:]) / 2.0
    vmin = np.full(centers.size, math.nan)
    vmax = np.full(centers.size, math.nan)
    vmean = np.full(centers.size, math.nan)
    for i in range(centers.size):
        st, en = int(pos[i]), int(pos[i + 1])
        if en <= st:
            continue
        seg = v[st:en]
        fin = seg[np.isfinite(seg)]
        if fin.size == 0:
            ct[i] = 0
            continue
        vmin[i] = float(fin.min())
        vmax[i] = float(fin.max())
        vmean[i] = float(fin.mean())
    return Envelope(key, centers, vmin, vmax, vmean, ct.astype(np.float64), us)


class LogStore:
    """一份日志的只读视图。"""

    def __init__(self, path: str | Path, index: dict[str, Any], cdir: Path,
                 codebook: Optional[Codebook] = None):
        self.path = str(path)
        self._index = index
        self._cdir = Path(cdir)
        self.log_id = index.get("log_id", "")
        self.cb = codebook or load_codebook()
        self._np_cache: dict[str, dict[str, np.ndarray]] = {}
        self._events: Optional[list[EventRec]] = None
        self._params: Optional[dict[str, tuple[float, Optional[float]]]] = None
        self._track: Optional[TrackResult] = None
        self._tracks: Optional[list[TrackResult]] = None
        self.stats = self._build_stats()

    # ---------------------------------------------------------------- 打开
    @classmethod
    def open(cls, path: str | Path, cache_dir: Optional[str | Path] = None,
             progress: _rd.ProgressFn = None, force: bool = False,
             codebook_path: Optional[str | Path] = None) -> "LogStore":
        p = Path(path)
        if cache_dir:
            os.environ["APMLOG_CACHE"] = str(cache_dir)
        if force:
            index = _rd.build(p, progress=progress, force=True)
        else:
            cdir, _ = _rd.cache_dir_for(p)
            ip = cdir / "index.json"
            index = None
            if ip.exists():
                try:
                    loaded = json.loads(ip.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.warning("缓存索引损坏(%s)，重建", exc)
                    loaded = None
                if loaded is not None and _rd.is_fresh(p, loaded):
                    index = loaded
                elif loaded is not None:
                    log.info("缓存过期（结构版本/路径/大小/修改时间不一致），重建")
            if index is None:
                index = _rd.build(p, progress=progress, force=False)
        cdir, _ = _rd.cache_dir_for(p)
        return cls(p, index, cdir, load_codebook(codebook_path))

    # ---------------------------------------------------------------- 元信息
    @property
    def meta(self) -> dict[str, Any]:
        return self._index

    @property
    def name(self) -> str:
        return self._index.get("name", Path(self.path).name)

    @property
    def cache_dir(self) -> Path:
        return self._cdir

    def types(self) -> list[str]:
        return sorted(self._index.get("types", {}).keys())

    # ---------------------------------------------------------------- 通道
    def _type_meta(self, type_: str) -> dict[str, Any]:
        tm = self._index.get("types", {})
        if type_ not in tm:
            raise KeyError(f"日志中没有报文类型 {type_}")
        return tm[type_]

    def channel_infos(self, type_prefix: Optional[str] = None) -> list[ChannelInfo]:
        """列出可用于绘图的数值通道。

        多实例类型的列统计在缓存里是**跳实例合并**的，因此实例级通道的 vmin/vmax
        填 nan（呌人不准的数据呌人不写），需要精确量程用 :meth:`channel_range`。
        """
        out: list[ChannelInfo] = []
        for name, tm in sorted(self._index.get("types", {}).items()):
            if type_prefix and not name.upper().startswith(type_prefix.upper()):
                continue
            insts = [i for i in tm.get("insts", []) if i >= 0] if tm.get("has_inst") else []
            inst_rows = tm.get("inst_rows", {}) or {}
            merged = bool(insts)
            for col, st in sorted(tm.get("cols", {}).items()):
                if col in _NON_COLS or col == "TimeUS":
                    continue
                unit = _unit_for(name, col)
                mn = math.nan if merged else st.get("min", math.nan)
                mx = math.nan if merged else st.get("max", math.nan)
                if insts:
                    for i in insts:
                        cnt = int(inst_rows.get(str(i), st.get("count", 0)))
                        out.append(ChannelInfo(ChannelKey(name, col, i), "f8", cnt,
                                               tm["t0"], tm["t1"], mn, mx, unit, ""))
                else:
                    out.append(ChannelInfo(ChannelKey(name, col, None), "f8", st.get("count", 0),
                                           tm["t0"], tm["t1"], mn, mx, unit, ""))
        return out
    def channel_range(self, key: "ChannelKey | str") -> dict[str, float]:
        """按实例精确计算量程/统计（只在选中该通道时调用）。"""
        return self.series(key).stats() | {"unit": _unit_for(key.type if isinstance(key, ChannelKey) else ChannelKey.parse(key).type,
                                                            key.field if isinstance(key, ChannelKey) else ChannelKey.parse(key).field)}

    def channels_of(self, type_: str) -> list[ChannelKey]:
        return [c.key for c in self.channel_infos(type_) if c.key.type == type_]

    def _npz(self, type_: str) -> dict[str, np.ndarray]:
        """懒加载（mmap）某类型的列。"""
        cached = self._np_cache.get(type_)
        if cached is not None:
            return cached
        fp = self._cdir / f"{type_}.npz"
        if not fp.exists():
            raise KeyError(f"缓存缺少 {type_}.npz")
        with np.load(fp, mmap_mode="r") as z:
            data = {k: np.asarray(z[k]) for k in z.files}
        # 只保留时间轴与常用列的常驻引用其实没必要；这里已拷出（float64 视图按需切片很快）
        self._np_cache[type_] = data
        return data

    def _rows(self, type_: str, inst: Optional[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """返回 (tc 视图, inst 视图, 选择掩码对应的索引数组)。"""
        d = self._npz(type_)
        tc = d["tc"]
        iarr = d.get("inst")
        if iarr is None or inst is not None and int(inst) < 0:
            idx = np.arange(tc.size)
            return tc, (iarr if iarr is not None else np.full(tc.size, -1, np.int16)), idx
        tm = self._type_meta(type_)
        insts = [i for i in tm.get("insts", []) if i >= 0]
        if inst is None:
            if not insts:
                return tc, iarr, np.arange(tc.size)
            # 未指定实例时，明确挑"最小的有数据实例"，并把选择写进日志（不静默混合多实例）
            inst = min(insts)
            log.info("%s 未指定实例，按最小实例号 inst=%s 查询", type_, inst)
        idx = np.nonzero(iarr == int(inst))[0]
        return tc, iarr, idx

    def series(self, key: "ChannelKey | str", t0: Optional[float] = None,
               t1: Optional[float] = None, max_points: Optional[int] = None,
               drop_nan: bool = False) -> Series:
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(key)
        d = self._npz(k.type)
        if k.field not in d:
            raise KeyError(f"{k.type} 没有字段 {k.field}")
        tc_all, _iarr, idx = self._rows(k.type, k.inst)
        v_all = d[k.field]
        lo = -math.inf if t0 is None else float(t0)
        hi = math.inf if t1 is None else float(t1)
        sel = idx[(tc_all[idx] >= lo) & (tc_all[idx] <= hi)]
        t = np.asarray(tc_all[sel], dtype=np.float64)
        v = np.asarray(v_all[sel], dtype=np.float64)
        if drop_nan and v.size:
            m = np.isfinite(v)
            t, v = t[m], v[m]
        if max_points and t.size > max_points:
            t, v = _decimate(t, v, int(max_points))
        return Series(k, t, v, _unit_for(k.type, k.field))

    def series_many(self, keys: Sequence["ChannelKey | str"], t0: Optional[float] = None,
                    t1: Optional[float] = None,
                    max_points: Optional[int] = None) -> dict[ChannelKey, Series]:
        out: dict[ChannelKey, Series] = {}
        for k in keys:
            kk = k if isinstance(k, ChannelKey) else ChannelKey.parse(k)
            try:
                out[kk] = self.series(kk, t0, t1, max_points)
            except KeyError as exc:
                log.warning("通道不可用: %s", exc)
        return out

    def envelope(self, key: "ChannelKey | str", t0: Optional[float] = None,
                 t1: Optional[float] = None, buckets: int = 2000) -> Envelope:
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(key)
        s = self.series(k, t0, t1)
        return envelope_from(s.t, s.v, k, s.unit, t0, t1, buckets)

    # ---------------------------------------------------------------- 事件/参数
    def events(self, kinds: Optional[Iterable[str]] = None, severity_min: Optional[str] = None,
               t0: Optional[float] = None, t1: Optional[float] = None,
               refresh: bool = False) -> list[EventRec]:
        if self._events is None or refresh:
            fp = self._cdir / "events.json"
            raws = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []
            self._events = decode_events(raws, self.cb)
        out = self._events
        if kinds:
            ks = {k.upper() for k in kinds}
            out = [e for e in out if e.kind in ks]
        if severity_min:
            out = [e for e in out if severity_at_least(e.severity, severity_min)]
        if t0 is not None:
            out = [e for e in out if e.t >= t0]
        if t1 is not None:
            out = [e for e in out if e.t <= t1]
        return out

    def params(self) -> dict[str, tuple[float, Optional[float]]]:
        if self._params is None:
            fp = self._cdir / "params.json"
            rows = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []
            out: dict[str, tuple[float, Optional[float]]] = {}
            for r in rows:
                nm = r.get("name")
                if not nm:
                    continue
                val = r.get("value")
                dfv = r.get("default")
                dfv = None if dfv is None or (isinstance(dfv, float) and not math.isfinite(dfv)) else float(dfv)
                out[str(nm)] = (float(val) if isinstance(val, (int, float)) else math.nan, dfv)
            self._params = out
        return self._params

    def param(self, name: str, default: Optional[float] = None) -> Optional[float]:
        got = self.params().get(name)
        if not got:
            return default
        v = got[0]
        return default if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v)

    def modes(self) -> list[tuple[float, float, str]]:
        t_end = float(self._index.get("t_end", 0.0))
        return mode_segments(self.events(), t_end, self.cb)

    # ---------------------------------------------------------------- 轨迹
    def track(self, key: Optional[str] = None) -> TrackResult:
        """取轨迹。``key=None`` 是**主轨迹**（与历史行为一致）。

        具名轨迹（``POS`` / ``GPS#0`` / ``XKF1`` …）从 :meth:`tracks` 的结果里找；
        找不到不抛异常，返回 ``kind="none"`` 并写明原因。
        """
        if key is not None:
            for tr in self.tracks():
                if tr.key == key:
                    return tr
            return TrackResult(kind="none", note=f"没有轨迹 {key}")
        if self._track is not None:
            return self._track
        fp = self._cdir / "track.npz"
        meta = self._index.get("track", {})
        if not fp.exists():
            self._track = TrackResult(kind="none", note=meta.get("note", "无轨迹缓存"))
            return self._track
        self._track = self._read_track(fp, meta, self._cdir / "track_src.json")
        return self._track

    def tracks(self) -> list[TrackResult]:
        """日志里所有可用位置轨迹，**主轨迹排第一**。

        为什么需要：POS（EKF 融合位置）与 GPS（原始 GNSS）在真机日志上逐点最大
        偏差实测 76.5 m，地图上是两条不同的线；只画一条就会把“GPS 飘了”这种关键
        故障信息并掉。无轨迹时返回 ``[]``。

        旧缓存（schema 2，没有 ``tracks`` 段）优雅降级为只含主轨迹的一条，不崩、
        不读脏数据；正常路径下 ``is_fresh()`` 己因 schema 不匹配而重建缓存。
        """
        if self._tracks is not None:
            return self._tracks
        entries = list((self._index.get("track", {}) or {}).get("tracks") or [])
        if not entries:
            primary = self.track()
            if primary.t.size == 0:
                self._tracks = []
            else:
                if not primary.key:                # 旧缓存只有一条，给它个稳定键名
                    primary.key, primary.label = "primary", (primary.label or "主轨迹")
                self._tracks = [primary]
            return self._tracks
        out: list[TrackResult] = []
        tdir = self._cdir / "tracks"
        for ent in entries:
            key = str(ent.get("key") or "")
            fp = tdir / f"{key}.npz"
            if not key or not fp.exists():
                continue                          # 子文件缺失就跳过，不让整条链崩掉
            out.append(self._read_track(fp, ent))
        if not out:                               # index 有但子文件全没 → 退回主轨迹
            primary = self.track()
            out = [] if primary.t.size == 0 else [primary]
        self._tracks = out
        return self._tracks

    @staticmethod
    def _read_track(fp: Path, meta: dict, src_fp: Optional[Path] = None) -> TrackResult:
        with np.load(fp) as z:
            d = {k: np.asarray(z[k], dtype=np.float64 if k != "src" else np.int8)
                 for k in z.files}
        names = meta.get("src_names") or []
        if not names and src_fp is not None and src_fp.exists():
            names = json.loads(src_fp.read_text(encoding="utf-8"))
        return TrackResult(
            kind=meta.get("kind", "local"), t=d.get("t", np.empty(0)),
            lat=d.get("lat", np.empty(0)), lon=d.get("lon", np.empty(0)),
            alt=d.get("alt", np.empty(0)), e=d.get("e", np.empty(0)),
            n=d.get("n", np.empty(0)), u=d.get("u", np.empty(0)),
            speed=d.get("speed", np.empty(0)), src=d.get("src", np.empty(0, np.int8)),
            src_names=names, origin=meta.get("origin"),
            distance=meta.get("distance", math.nan),
            max_speed=meta.get("max_speed", math.nan),
            climb_min=meta.get("climb_min", math.nan),
            climb_max=meta.get("climb_max", math.nan),
            key=str(meta.get("key") or ""), label=str(meta.get("label") or ""),
            note=meta.get("axis_note", "") or meta.get("note", ""))

    # ---------------------------------------------------------------- 告警
    def alerts(self, config_path: Optional[str | Path] = None,
               overrides: Optional[dict[str, dict]] = None) -> list[AlertHit]:
        """计算告警。``overrides`` 是界面阈值覆盖层（见 alerts.evaluate）。"""
        from .alerts import evaluate
        return evaluate(self, config_path, overrides=overrides)

    # ---------------------------------------------------------------- 概要
    def _build_stats(self) -> LogStats:
        s = dict(self._index.get("stats", {}))
        idx = self._index
        raws = []
        fp = self._cdir / "events.json"
        if fp.exists():
            raws = json.loads(fp.read_text(encoding="utf-8"))
        warns: list[str] = []
        for seg in idx.get("segments", []):
            if isinstance(seg, (list, tuple)) and seg[0] not in (0,):
                warns.append(f"检测到 {int(seg[0]) + 1} 段不连续时间（多次上电或日志重启）")
                break
        kind = s.get("pos_kind", "none")
        if kind == "local":
            warns.append("无 GNSS 经纬度，轨迹为相对坐标（真实地图不可用）")
        elif kind == "none":
            warns.append("该日志无位置报文")
        if idx.get("n_bad"):
            warns.append(f"{idx['n_bad']} 条报文解析异常")
        if idx.get("n_params") == 0:
            warns.append("未解析到 PARM 参数块")
        st = LogStats(
            path=self.path, log_id=idx.get("log_id", ""), size_bytes=idx.get("size", 0),
            duration=s.get("duration", math.nan), t_start=s.get("t_start", math.nan),
            t_end=s.get("t_end", math.nan), takeoff_t=s.get("takeoff_t"), land_t=s.get("land_t"),
            firmware=s.get("firmware", ""), board=s.get("board", ""),
            frame_type=s.get("frame_type", ""), git=s.get("git", ""),
            n_messages=idx.get("n_messages", 0), n_types=s.get("n_types", 0),
            n_params=idx.get("n_params", 0), n_events=idx.get("n_events", 0),
            max_alt=s.get("max_alt", math.nan), distance=s.get("distance", math.nan),
            max_speed=s.get("max_speed", math.nan), pos_source=s.get("pos_source", ""),
            pos_kind=s.get("pos_kind", ""),
            segments=[(float(a), float(b)) for _i, a, b in idx.get("segments", [])],
            warnings=warns)
        try:  # 崩溃线索只靠原文，不靠码表
            st.crashes = [f"{t:.2f}s {txt}" for t, txt in find_crash_groups(decode_events(raws, self.cb))]
        except Exception as exc:  # pragma: no cover
            log.debug("crash 提取失败: %s", exc)
        try:
            st.mode_seq = compress_mode_seq(
                mode_segments(decode_events(raws, self.cb),
                              float(idx.get("t_end", 0.0)), self.cb))
        except Exception:
            pass
        return st

    def close(self) -> None:
        self._np_cache.clear()

    def __enter__(self) -> "LogStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LogStore {self.name} types={len(self._index.get('types', {}))} log_id={self.log_id}>"


def _decimate(t: np.ndarray, v: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """min/max 保形抽稀：每桶保留最小值与最大值，保持尖峰不丢。"""
    n = t.size
    if n <= max_points:
        return t, v
    buckets = max(1, max_points // 2)
    edges = np.linspace(0, n, buckets + 1).astype(int)
    ot = np.empty(buckets * 2, dtype=np.float64)
    ov = np.empty(buckets * 2, dtype=np.float64)
    k = 0
    for i in range(buckets):
        st, en = int(edges[i]), int(edges[i + 1])
        if en <= st:
            continue
        seg = v[st:en]
        if not np.isfinite(seg).any():
            ot[k] = t[st]
            ov[k] = math.nan
            k += 1
            continue
        imin = int(np.nanargmin(seg))
        imax = int(np.nanargmax(seg))
        if imin <= imax:
            ot[k], ov[k] = t[st + imin], seg[imin]
            ot[k + 1], ov[k + 1] = t[st + imax], seg[imax]
        else:
            ot[k], ov[k] = t[st + imax], seg[imax]
            ot[k + 1], ov[k + 1] = t[st + imin], seg[imin]
        k += 2
    return ot[:k], ov[:k]


__all__ = ["LogStore"]
