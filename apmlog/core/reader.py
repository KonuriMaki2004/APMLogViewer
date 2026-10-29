"""apmlog.core.reader —— DataFlash ``.BIN`` 解析与列式缓存构建。

实现要点（与 docs/CONTRACT.md 第 1/4 节一致）：

* 解析走 ``pymavlink.DFReader``（FMT 驱动，不硬编码字段表），单遍扫描实测 1.7–3.9 MB/s。
* 每种报文写成一个**不压缩 npz**（``t`` 原始秒、``tc`` 连续秒、``inst``、``seg`` + 各数值列），
  便于 ``np.load(..., mmap_mode='r')`` 懒加载与二分取区间。
* 只收 scalar 数值列；字符串/bytes/数组列单独走事件与文件通道（MSG/FILE/PARM/VER/UNIT）。
* 时间只认 ``TimeUS``。检测到时间回退视为"日志重启/多次上电"，开新段并让 ``tc`` 连续，
  这样 UI 与告警都能用单一单调时间轴。
* 子实例来自 ``I``/``Inst``/``C``/``IMU``（4.6/4.7 已把 IMU2/MAG2 之类合并成多实例）。
  注意 ``VIBE.IMU``、``BAT.Inst`` 是 **1-based**，``IMU.I`` 是 **0-based**：缓存里保留原始值，
  不做归一，标签里显式带原编号，避免把实例号读错。
* 位置轨迹按 ``POS → GPS →(ORGN+XKF1)→ XKF1 → VISP`` 回退；无经纬度时只出本地 ENU。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from . import events as evmod

log = logging.getLogger(__name__)

ProgressFn = Optional[Callable[[float, str], None]]

#: 安全子集：确定表示"子实例号"的列（不用 model.INSTANCE_FIELDS 里的 "ID"，MSG.ID 是报文序号）
INST_FIELDS: tuple[str, ...] = ("I", "Inst", "C", "IMU")

#: 这些列就是实例号本身，不再当数值通道入库（避免 VIBE[2].IMU 这类冗余通道刷屏）
_INST_COLS = frozenset(INST_FIELDS)


def _numeric_scalars(d: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in d.items():
        if k == "mavpackettype" or v is None or k in _INST_COLS:
            continue
        if isinstance(v, bool):
            out[k] = float(v)
        elif isinstance(v, (int, float)) and not isinstance(v, (str, bytes)):
            out[k] = float(v)
    return out

#: 这些类型不进数值表（另行处理或无分析价值）
SPECIAL_TYPES: frozenset[str] = frozenset({"FMT", "FMT2", "FMTU", "UNIT", "PARM", "FILE", "VER"})

#: 事件类型
EVENT_TYPES: tuple[str, ...] = ("ERR", "EV", "MSG", "MSGB", "MSGT", "MSGS", "ARM", "MODE", "CMD", "FILE")

#: 轨迹源优先级（**主轨迹**选取顺序：光标/高度带/里程/状态栏跟着它走）。
TRACK_PRIORITY: tuple[str, ...] = ("POS", "GPS", "XKF1", "VISP")

#: 每个位置报文要读哪些列。顺序也是“非主轨迹”在勾选栏里的展示次序基准。
#: 字段语义与历史一致：POS/GPS 直接有经纬度；XKF1(PN,PE,PD) 与 VISP(PX,PY,PZ)
#: 是本地 NED，必须经 ORGN 才能投成经纬度。
TRACK_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("POS", ("Lat", "Lng", "Alt", "RelHomeAlt")),
    ("GPS", ("Lat", "Lng", "Alt", "NSats", "HDop")),
    ("XKF1", ("PN", "PE", "PD")),
    ("VISP", ("PX", "PY", "PZ")),
)

#: 只有这两类报文按**接收机实例**拆成多条轨迹。XKF1 的 ``C`` 是 EKF 核号、不是接收机
#: 实例（112 份真机日志实测 C 恒为 1），按它拆会得到语义错误的 ``XKF1#1``。
INSTANCE_SPLIT: frozenset[str] = frozenset({"POS", "GPS"})

#: 勾选栏直接显示的中文名。GPS 实例从 1 开始编号（ArduPilot 用户习慣叫 GPS1/GPS2）。
TRACK_LABELS: dict[str, str] = {
    "POS": "EKF 融合位置 (POS)",
    "XKF1": "EKF 状态位置 (XKF1)",
    "VISP": "视觉位置 (VISP)",
}

#: 一个源至少要有这么多有效点才值得单独画一条线（与历史行为同值）。
_MIN_TRACK_POINTS = 5

#: 缓存结构版本。**改动解析/轨迹选取逻辑时必须 +1**，否则旧缓存会被当有效直接复用
#: （v1→2：GPS 无锁定假位置过滤 + 位置跳变剔除，不 bump 就会读到 16,005 km 里程这种脏数据；
#:  v2→3：**新增多轨迹**——POS / GPS#每个接收机 / XKF1 / VISP 各存一份 tracks/<key>.npz
#:  与 tracks/index.json，不 bump 就会让 store.tracks() 读到不存在的子文件而静默少轨迹；
#:  v3→4：**无 GPS / 无 ORGN 的纯视觉日志也要出轨迹**——这类日志只有 XKF1/VISP 的本地
#:  NED 米坐标，旧版直接判 kind="none"（“没有可用位置轨迹”），现版保留为 kind="local"
#:  多条轨迹、由地图在本地 ENU 坐标系里叠画。实际事故：开发中间快照（19:19）也把
#:  schema 写成 3 但内容是“无轨迹”，文件 size/mtime 又没变，is_fresh() 无法区分
#:  → 脏缓存永远读不到轨迹。语义变一点就必须 bump，不许复用同一个号。)
SCHEMA_VERSION = 4

# 位置质量门槛（真机实测出来的，不是拍脑袋）：
#   Copter 4.6.3 无 GNSS 锁定时仍会写 GPS：NSats=0 / HDop=99.99 / Lat=Lng=0
_GPS_MIN_SATS = 3        # 有 NSats 列时：低于此值视为无有效定位
_GPS_BAD_DOP = 99.0      # HDop 到这个量级就是占位值（实测 99.99）
_MAX_STEP_MPS = 200.0    # 单步等效速度超此值→位置跳变，不计入里程与速度

#: 单个 @SYS 文件最多拼多大（防炸内存）
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES_TOTAL = 24 * 1024 * 1024

NAN = float("nan")


class ParseError(RuntimeError):
    """日志打不开或完全无法解析。"""


# --------------------------------------------------------------------------- #
# 缓存目录
# --------------------------------------------------------------------------- #

def cache_root() -> Path:
    """缓存根目录：``APMLOG_CACHE`` 优先，其次 ``%LOCALAPPDATA%\\APMLogViewer\\cache``。

    Git-Bash 等非完整 Windows 环境可能没有 ``LOCALAPPDATA``，这时回退到
    ``~\\AppData\\Local``，保证与 GUI 启动时落在同一个缓存目录。
    """
    env = os.environ.get("APMLOG_CACHE")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA")
    if not base and os.name == "nt":
        guess = Path.home() / "AppData" / "Local"
        if guess.parent.is_dir():
            base = str(guess)
    if not base:
        base = str(Path.home() / ".cache")
    return Path(base) / "APMLogViewer" / "cache"


def _fingerprint(path: Path) -> tuple[int, int, str]:
    st = path.stat()
    raw = f"{os.path.normcase(str(path.resolve()))}|{st.st_size}|{int(st.st_mtime)}"
    return st.st_size, int(st.st_mtime), hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def cache_dir_for(path: str | Path) -> tuple[Path, str]:
    p = Path(path)
    _size, _mtime, log_id = _fingerprint(p)
    return cache_root() / log_id, log_id


def is_fresh(path: str | Path, index: dict[str, Any]) -> bool:
    """缓存有效性：结构版本 + 路径 + 大小 + mtime 全部一致。

    旧版缓存（没有 ``schema`` 字段或版本号不相等）一律当过期重建，因为解析/轨迹
    语义会变而文件大小/时间戳不会变。
    """
    p = Path(path)
    try:
        size, mtime, log_id = _fingerprint(p)
    except OSError:
        return False
    try:
        if int(index.get("schema", 0) or 0) != SCHEMA_VERSION:
            return False
    except (TypeError, ValueError):
        return False
    return (index.get("log_id") == log_id and index.get("size") == size
            and index.get("mtime") == mtime and index.get("abs_path") == os.path.normcase(str(p.resolve())))


# --------------------------------------------------------------------------- #
# 解析缓冲
# --------------------------------------------------------------------------- #

@dataclass
class _Buf:
    """单种报文的列缓冲。"""
    n: int = 0
    t: list[float] = field(default_factory=list)
    tc: list[float] = field(default_factory=list)
    inst: list[int] = field(default_factory=list)
    seg: list[int] = field(default_factory=list)
    cols: dict[str, list[float]] = field(default_factory=dict)
    coltypes: dict[str, str] = field(default_factory=dict)

    def add_row(self, tc: float, t: float, inst: int, seg: int, vals: dict[str, float]) -> None:
        for name, col in self.cols.items():
            col.append(vals.get(name, NAN))
        for name, v in vals.items():
            if name not in self.cols:
                self.cols[name] = [NAN] * self.n + [v]
        self.t.append(t)
        self.tc.append(tc)
        self.inst.append(inst)
        self.seg.append(seg)
        self.n += 1


def _instance_of(d: dict[str, Any]) -> int:
    for f in INST_FIELDS:
        if f in d:
            v = d[f]
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and 0 <= int(v) < 64 and float(v) == int(v):
                return int(v)
    return -1


# --------------------------------------------------------------------------- #
# 主解析
# --------------------------------------------------------------------------- #

def build(path: str | Path, progress: ProgressFn = None, force: bool = False) -> dict[str, Any]:
    """解析 ``path`` 并写出缓存，返回 index 字典。"""
    p = Path(path)
    if not p.exists():
        raise ParseError(f"文件不存在: {p}")
    cdir, log_id = cache_dir_for(p)
    idx_path = cdir / "index.json"
    if idx_path.exists() and not force:
        try:
            old = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        if is_fresh(p, old):
            log.info("缓存命中（未重建）: %s", cdir)
            return old

    size, mtime, _ = _fingerprint(p)
    t0 = time.time()
    from pymavlink import DFReader  # 延迟导入，便于 CLI 快速 --help

    try:
        dfp = DFReader.DFReader_binary(str(p))
    except Exception as exc:                       # pragma: no cover
        raise ParseError(f"DFReader 无法打开: {exc}") from exc
    if dfp is None:
        raise ParseError(f"不是有效的 DataFlash .BIN 或格式不受支持: {p}")

    bufs: dict[str, _Buf] = {}
    raw_events: list[dict[str, Any]] = []
    params: list[dict[str, Any]] = []
    ver: dict[str, Any] = {}
    msg_texts: list[tuple[float, str]] = []
    files: dict[str, dict[int, bytes]] = {}
    file_seen: set[str] = set()
    units: dict[str, str] = {}
    fmt_units: list[dict[str, Any]] = []

    cum = 0.0            # 段累计偏移（秒）
    tc_max = -math.inf
    prev_raw: Optional[int] = None
    seg = 0
    seg_bounds: list[tuple[int, float, float]] = []
    n_msg = 0
    n_bad = 0
    last_prog = 0.0

    while True:
        try:
            m = dfp.recv_msg()
        except Exception as exc:                    # 单条坏消息不能终止整份日志
            n_bad += 1
            log.debug("recv_msg 异常: %s", exc)
            if n_bad > 200:
                break
            continue
        if m is None:
            break
        n_msg += 1
        name = m.get_type()
        try:
            d = m.to_dict()
        except Exception:
            continue
        raw_t = d.get("TimeUS")
        if isinstance(raw_t, (int, float)) and not isinstance(raw_t, bool):
            raw_us = int(raw_t)
            if prev_raw is not None and raw_us < prev_raw - 1_000_000:
                # 时间回退：视为日志重启/多次上电，开新段，tc 保持连续
                seg += 1
                cum = tc_max + 1e-3
                if seg_bounds:
                    seg_bounds[-1] = (seg_bounds[-1][0], seg_bounds[-1][1], tc_max)
            prev_raw = raw_us
            t = raw_us * 1e-6
            tc = cum + t
            if tc > tc_max:
                tc_max = tc
        else:
            t, tc = NAN, (tc_max if math.isfinite(tc_max) else 0.0)

        if name in ("FMT", "FMT2"):
            continue
        if name == "PARM":
            params.append({"name": d.get("Name"), "value": d.get("Value"),
                           "default": d.get("Default")})
            continue
        if name == "VER":
            ver.update({k: v for k, v in d.items() if isinstance(v, (str, int, float))})
            continue
        if name == "UNIT":
            uid = d.get("Id")
            lab = d.get("Label")
            if uid is not None and isinstance(lab, str):
                units[str(int(uid))] = lab
            continue
        if name == "FMTU":
            fmt_units.append({k: v for k, v in d.items() if isinstance(v, (str, int))})
            continue
        if name == "FILE":
            fname = d.get("FileName")
            data = d.get("Data")
            if isinstance(fname, str) and isinstance(data, (bytes, bytearray)) and fname:
                total = sum(len(v) for v in files.get(fname, {}).values())
                if total + len(data) <= MAX_FILE_BYTES and \
                        sum(len(v) for f in files.values() for v in f.values()) + len(data) <= MAX_FILES_TOTAL:
                    files.setdefault(fname, {})[int(d.get("Offset") or 0)] = bytes(data)
                if fname not in file_seen:
                    file_seen.add(fname)
                    r = evmod.raw_from_message("FILE", {"FileName": fname,
                                                        "Offset": d.get("Offset"),
                                                        "Length": d.get("Length")}, tc)
                    if r:
                        raw_events.append(r)
            continue

        if math.isnan(tc):
            continue

        if name not in bufs:
            bufs[name] = _Buf()
        vals = _numeric_scalars(d)
        bufs[name].add_row(tc, t, _instance_of(d), seg, vals)

        if name in EVENT_TYPES:
            r = evmod.raw_from_message(name, d, tc)
            if r is not None:
                raw_events.append(r)
        if name in ("MSG", "MSGB", "MSGT", "MSGS"):
            txt = r.get("text") if r and "text" in r else ""
            if not txt:
                txt = str(d.get("Message") or d.get("Msg") or d.get("Text") or "")
            if txt:
                msg_texts.append((tc, txt))

        off = getattr(dfp, "offset", 0) or 0
        now = time.time()
        if progress and now - last_prog > 0.15:
            last_prog = now
            progress(min(0.97, off / max(size, 1)), f"解析 {name}（{off * 100 // max(size, 1)}%）")

    try:
        dfp.close()
    except Exception:
        pass

    if not bufs:
        raise ParseError(f"未解析到任何报文，文件可能损坏: {p}")

    if seg_bounds:
        seg_bounds[-1] = (seg_bounds[-1][0], seg_bounds[-1][1], tc_max)
    if not seg_bounds:
        seg_bounds = [(0, 0.0, tc_max)]

    # ---------------- 写 npz ---------------- #
    cdir.mkdir(parents=True, exist_ok=True)
    types_meta: dict[str, Any] = {}
    for name, buf in bufs.items():
        if buf.n == 0 or not buf.cols:
            continue
        arrs: dict[str, np.ndarray] = {
            "t": np.asarray(buf.t, dtype=np.float64),
            "tc": np.asarray(buf.tc, dtype=np.float64),
            "inst": np.asarray(buf.inst, dtype=np.int16),
            "seg": np.asarray(buf.seg, dtype=np.int8),
        }
        colstat: dict[str, Any] = {}
        for cname, cvals in buf.cols.items():
            a = np.asarray(cvals, dtype=np.float64)
            arrs[cname] = a
            fin = a[np.isfinite(a)]
            colstat[cname] = {"count": int(fin.size),
                              "min": float(fin.min()) if fin.size else NAN,
                              "max": float(fin.max()) if fin.size else NAN}
        _atomic_npz(cdir / f"{name}.npz", **arrs)
        insts = sorted({int(i) for i in arrs["inst"]})
        iarr = arrs["inst"]
        inst_rows = {str(int(i)): int(np.count_nonzero(iarr == i)) for i in np.unique(iarr) if int(i) >= 0}
        tcs = arrs["tc"]
        types_meta[name] = {
            "rows": int(buf.n),
            "cols": colstat,
            "insts": insts,
            "inst_rows": inst_rows,
            "has_inst": bool([i for i in insts if i >= 0]),
            "t0": float(tcs.min()), "t1": float(tcs.max()),
        }

    # ---------------- 拼 @SYS 文件 ---------------- #
    sysfiles: dict[str, Any] = {}
    if files:
        sdir = cdir / "sysfiles"
        sdir.mkdir(exist_ok=True)
        for fname, chunks in files.items():
            data = bytearray()
            for off in sorted(chunks):
                if len(data) == 0 and off != 0:
                    data.extend(b"\x00" * off)
                elif off > len(data):
                    data.extend(b"\x00" * (off - len(data)))
                data.extend(chunks[off])
            safe = fname.replace("/", "_").replace("@", "AT_").replace("\\", "_")
            (sdir / safe).write_bytes(bytes(data))
            sysfiles[fname] = {"cached_as": safe, "bytes": len(data)}

    # ---------------- 轨迹 ---------------- #
    track_meta = build_track(cdir, types_meta, bufs, params, units)

    # ---------------- 概要 ---------------- #
    stats = summarize(types_meta, bufs, msg_texts, ver, params, track_meta, tc_max)
    bat = bat_semantics(types_meta, params)

    index = {
        "schema": SCHEMA_VERSION,
        "log_id": log_id,
        "abs_path": os.path.normcase(str(p.resolve())),
        "name": p.name,
        "size": size,
        "mtime": mtime,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "parser": "pymavlink.DFReader",
        "seconds": round(time.time() - t0, 2),
        "n_messages": n_msg,
        "n_bad": n_bad,
        "segments": [[int(s), float(a), float(b)] for s, a, b in seg_bounds],
        "t_end": float(tc_max),
        "types": types_meta,
        "track": track_meta,
        "stats": stats,
        "bat_semantics": bat,
        "units": units,
        "sysfiles": sysfiles,
        "n_params": len(params),
        "n_events": len(raw_events),
    }
    _atomic_text(cdir / "params.json", json.dumps(params, ensure_ascii=False))
    _atomic_text(cdir / "events.json", json.dumps(raw_events, ensure_ascii=False))
    _atomic_text(cdir / "index.json", json.dumps(index, ensure_ascii=False, indent=1))

    if progress:
        progress(1.0, f"完成：{n_msg} 条报文 / {len(types_meta)} 种类型")
    log.info("解析完成 %s: %d 报文 %.1fs -> %s", p.name, n_msg, time.time() - t0, cdir)
    return index


# --------------------------------------------------------------------------- #
# 轨迹
# --------------------------------------------------------------------------- #

def _best_instance(buf: _Buf, cand: tuple[str, ...]) -> tuple[np.ndarray, dict[str, np.ndarray], Optional[int]]:
    """返回 (tc, 需要的列, 选中的实例号)。优先字段齐全且行数最多的实例。"""
    have = {c: np.asarray(buf.cols[c], dtype=np.float64) for c in cand if c in buf.cols}
    tc = np.asarray(buf.tc, dtype=np.float64)
    inst = np.asarray(buf.inst, dtype=np.int16)
    if not have:
        return tc, {}, None
    keys = list(have)
    ok = np.isfinite(have[keys[0]])
    for k in keys[1:]:
        ok &= np.isfinite(have[k])
    if not ok.any():
        return tc, {}, None
    if (inst < 0).all():
        return tc, have, None
    best, best_n = None, 0
    for i in sorted({int(v) for v in inst[ok] if v >= 0}):
        n = int(np.count_nonzero(ok & (inst == i)))
        if n > best_n:
            best, best_n = i, n
    if best is None:
        return tc, have, None
    return tc, have, best


def _project_enu(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    """等距圆柱近似（对本工程尺度误差可忽略），返回 (东, 北) 米。"""
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110574.0
    return (lon - lon0) * kx, (lat - lat0) * ky


def _enu_to_latlon(e: np.ndarray, n: np.ndarray, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110574.0
    return lat0 + n / ky, lon0 + e / kx


def _source_valid(t: str, cols: dict[str, np.ndarray]) -> np.ndarray:
    """位置样本有效性掩码。

    真机实测（Copter 4.6.3，无 GNSS 锁定）：GPS 报文**照样以 1Hz+ 写出来**，
    但 ``NSats=0`` / ``HDop=99.99`` / ``Lat=Lng=0``（偶发还有完全不相干的经纬度）。
    把它们当有效点会让里程从 26 m 跳到 16,005 km，所以先过灯再参选：

    * ``Lat``/``Lng`` 必须有限且不同时为 0（null island）
    * ``|Lat|>90`` / ``|Lng|>180`` 排除
    * GPS 额外要求 ``NSats >= 3``（有 ``NSats`` 列时）且 ``HDop < 99``（有 ``HDop`` 列时）
    """
    lat = np.asarray(cols["Lat"], dtype=np.float64)
    lon = np.asarray(cols["Lng"], dtype=np.float64)
    m = np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90.0) & (np.abs(lon) <= 180.0)
    m &= ~((np.abs(lat) < 1e-7) & (np.abs(lon) < 1e-7))
    if t == "GPS":
        ns, hd = cols.get("NSats"), cols.get("HDop")
        if ns is not None:
            m &= np.asarray(ns, dtype=np.float64) >= _GPS_MIN_SATS
        if hd is not None:
            m &= np.asarray(hd, dtype=np.float64) < _GPS_BAD_DOP
    return m


def _track_key(msg: str, inst: Optional[int]) -> str:
    """轨迹唯一键。POS/GPS 带实例时是 ``GPS#0`` 这种形式，其余就是报文名。"""
    if msg in INSTANCE_SPLIT and inst is not None:
        return f"{msg}#{inst}"
    return msg


def _track_label(msg: str, inst: Optional[int]) -> str:
    if msg == "GPS":
        return f"原始 GNSS (GPS{(inst or 0) + 1})"
    if msg in TRACK_LABELS:
        return TRACK_LABELS[msg]
    return msg + (f" #{inst}" if inst is not None else "")


def _track_origin(types_meta: dict[str, Any], bufs: dict[str, _Buf]) -> Optional[tuple[float, float, float]]:
    """ORGN 原点（把本地 NED 投成经纬度用）。没有 ORGN 数据就是 None。"""
    if "ORGN" not in types_meta or "ORGN" not in bufs:
        return None
    ob = bufs["ORGN"]
    _tc_o, cols_o, _ = _best_instance(ob, ("Lat", "Lng", "Alt"))
    if cols_o and np.isfinite(cols_o["Lat"]).any() and np.isfinite(cols_o["Lng"]).any():
        i = int(np.argmax(np.isfinite(cols_o["Lat"])))
        return (float(cols_o["Lat"][i]), float(cols_o["Lng"][i]),
                float(cols_o["Alt"][i]) if "Alt" in cols_o else 0.0)
    return None


def _track_candidates(bufs: dict[str, _Buf]) -> list[tuple[str, Optional[int], int]]:
    """列出值得单独画一条线的 (报文, 实例, 该实例总行数)。

    POS/GPS 按接收机实例逐个枚举（双接收机日志会各出一条）；XKF1/VISP 不拆实例，
    但仍沿用"取主导实例"的过滤，避免把多 EKF 核的点混成锯齿线。
    """
    out: list[tuple[str, Optional[int], int]] = []
    for msg, cols in TRACK_SPECS:
        b = bufs.get(msg)
        if b is None or not cols:
            continue
        iarr = np.asarray(b.inst, dtype=np.int16)
        if msg in INSTANCE_SPLIT and iarr.size and (iarr >= 0).any():
            for i in sorted({int(v) for v in iarr if int(v) >= 0}):
                out.append((msg, i, int(np.count_nonzero(iarr == i))))
            continue
        _tc, have, best = _best_instance(b, cols)
        if not have:
            continue
        n = int(np.count_nonzero(iarr == best)) if best is not None else int(iarr.size)
        out.append((msg, best, n))
    return out


def _one_track(msg: str, inst: Optional[int], b: _Buf,
               origin: Optional[tuple[float, float, float]],
               rows_for_inst: int) -> Optional[dict[str, Any]]:
    """算一条轨迹的全部数组 + 标量元数据（不写盘）。不可用返回 ``{"unusable": 原因}``。

    有效性过滤**每个源各自做**：POS 干净不代表 GPS 干净，GPS 无锁定（NSats=0 /
    HDop=99.99 / 经纬度 0）必须照样剔掉。
    """
    tarr = np.asarray(b.tc, dtype=np.float64)
    iarr = np.asarray(b.inst, dtype=np.int16)
    sel = np.ones(tarr.size, dtype=bool)
    if inst is not None and iarr.size == tarr.size:
        sel = iarr == inst
    if tarr.size == 0:
        return {"unusable": "无该报文数据"}

    if msg in ("POS", "GPS"):
        cand = ("Lat", "Lng", "Alt", "RelHomeAlt", "NSats", "HDop")
        cols = {c: np.asarray(b.cols[c], dtype=np.float64) for c in cand if c in b.cols}
        if "Lat" not in cols or "Lng" not in cols:
            return {"unusable": "缺 Lat/Lng 列"}
        lat_raw, lon_raw = cols["Lat"], cols["Lng"]        # 保持全长，统一由 ok 索引
        alt_src = "RelHomeAlt" if "RelHomeAlt" in cols else "Alt"
        alt_raw = (cols[alt_src] if alt_src in cols
                   else np.full(lat_raw.size, NAN))
        ok = sel & _source_valid(msg, {k: np.asarray(v, dtype=np.float64)
                                       for k, v in cols.items()
                                       if k in ("Lat", "Lng", "NSats", "HDop")})
        if int(np.count_nonzero(ok)) < _MIN_TRACK_POINTS:
            return {"unusable": (f"{msg} 有报文但无有效定位点"
                                 "（NSats/HDop/零经纬度已剔除）")}
        tsel = tarr[ok]
        lat, lon, alt = lat_raw[ok], lon_raw[ok], alt_raw[ok]
        lat0, lon0 = float(lat[0]), float(lon[0])
        e, n = _project_enu(lat, lon, lat0, lon0)
        u = alt - (float(alt[0]) if alt.size and math.isfinite(alt[0]) else 0.0)
        kind = "geodetic"
        note = f"高度基准: {alt_src}(高于 home)" if alt_src == "RelHomeAlt" else "高度基准: Alt(AMSL)"
        proj = "latlon"
    else:
        nn_c, ee_c, dd_c = (("PN", "PE", "PD") if msg == "XKF1" else ("PX", "PY", "PZ"))
        if nn_c not in b.cols or ee_c not in b.cols:
            return {"unusable": f"缺 {nn_c}/{ee_c} 列"}
        nn_raw = np.asarray(b.cols[nn_c], dtype=np.float64)[sel]
        ee_raw = np.asarray(b.cols[ee_c], dtype=np.float64)[sel]
        dd_raw = (np.asarray(b.cols[dd_c], dtype=np.float64)[sel] if dd_c in b.cols
                  else np.full(ee_raw.size, NAN))
        ok_local = np.isfinite(nn_raw) & np.isfinite(ee_raw)
        if int(np.count_nonzero(ok_local)) < _MIN_TRACK_POINTS:
            return {"unusable": f"{msg} 有效本地位置点不足 {_MIN_TRACK_POINTS} 个"}
        tsel = tarr[sel][ok_local]
        e, n = ee_raw[ok_local], nn_raw[ok_local]
        dd = dd_raw[ok_local] if dd_raw.size == nn_raw.size else np.full(e.size, NAN)
        u = -dd
        alt = np.full(e.size, NAN)
        axis = ("NED(PN,PE,PD)" if msg == "XKF1" else "NED(PX,PY,PZ)（假设，见文档）")
        kind, lat, lon = "local", np.full(e.size, NAN), np.full(e.size, NAN)
        note = f"本地 {axis} 转 ENU，无经纬度"
        proj = "local"
        if origin is not None:
            # 有 ORGN 才投影成经纬度；没有就保持 kind="local"（lat/lon 全 NaN）。
            # 红线是**绝不能把 NED 的米数当经纬度填进去**，而不是拒绝这条轨迹——
            # 老行为就是靠 kind="local" 让地图片用 e/n 画出飞行形状。
            lat, lon = _enu_to_latlon(e, n, origin[0], origin[1])
            kind = "geodetic"
            proj = "ned2latlon"
            note = f"由 ORGN 原点 + 本地偏移({axis}) 投影为经纬度（近似）"

    speed = _speed_from(tsel, e, n)
    step = np.hypot(np.diff(e), np.diff(n))
    dt = np.diff(tsel)
    bad = (dt > 0) & (step / np.maximum(dt, 1e-9) > _MAX_STEP_MPS)
    jumps = int(np.count_nonzero(bad))
    dist = float(np.nansum(np.where(bad, NAN, step))) if step.size else NAN
    if speed.size > 1 and jumps:
        speed = speed.copy()
        speed[1:][bad] = NAN
    if jumps:
        note += f"；已剔除 {jumps} 处 >{_MAX_STEP_MPS:.0f}m/s 位置跳变（里程/速度不计入）"
    climb = _climb_from(tsel, u)
    n_valid = int(tsel.size)
    key = _track_key(msg, inst)
    rows = rows_for_inst if rows_for_inst > 0 else n_valid
    return {
        "arrs": {"t": tsel, "lat": lat, "lon": lon, "alt": alt, "e": e, "n": n, "u": u,
                 "speed": speed, "src": np.zeros(n_valid, dtype=np.int8)},
        "meta": {"key": key, "label": _track_label(msg, inst), "source": msg, "inst": inst,
                 "kind": kind, "projection": proj, "n": n_valid,
                 "valid_frac": round(n_valid / rows, 4) if rows else 0.0,
                 "distance": dist,
                 "max_speed": float(np.nanmax(speed)) if np.isfinite(speed).any() else NAN,
                 "jumps": jumps, "climb_min": climb[0], "climb_max": climb[1],
                 "alt_min": float(np.nanmin(u)) if u.size else NAN,
                 "alt_max": float(np.nanmax(u)) if u.size else NAN,
                 "axis_note": note,
                 "src_names": [f"{msg}" + (f"[{inst}]" if inst is not None else "")]},
    }


def build_tracks(cdir: Path, types_meta: dict[str, Any], bufs: dict[str, _Buf],
                 params: list[dict[str, Any]], units: dict[str, str]) -> dict[str, Any]:
    """抽出**所有**可用位置源，每个各写 ``tracks/<key>.npz``，并写主轨迹 ``track.npz``。

    为什么要有这个函数：以前按 ``TRACK_PRIORITY`` 只挑一个源，地图永远只有一条线。
    真机实测（112 份日志）POS（EKF 融合位置，约 2× 采样率）与 GPS（原始 GNSS）逐点
    最大偏差可达 76.5 m，在地图上就是明晃晃两条线——Mission Planner 会同时画出来，
    所以这里必须把每条都留下、交给界面勾选。

    返回的 dict 同时含**主轨迹的顶层字段**（``kind``/``source``/``distance``/``alt_max`` …，
    与历史完全一致，``summarize()`` 和 ``store.track()`` 零改动可用）和新的
    ``tracks`` 列表（勾选栏数据源，主轨迹排第一）。
    """
    meta: dict[str, Any] = {"kind": "none", "source": "", "available": {}, "note": "",
                            "tracks": [], "unusable": {}}
    for t, _cols in TRACK_SPECS:
        if t in types_meta:
            meta["available"][t] = types_meta[t]["rows"]

    origin = _track_origin(types_meta, bufs)
    if origin is not None:
        meta["origin"] = origin

    entries: list[dict[str, Any]] = []
    arrays: dict[str, dict[str, Any]] = {}
    for msg, inst, rows in _track_candidates(bufs):
        b = bufs.get(msg)
        if b is None:
            continue
        got = _one_track(msg, inst, b, origin, rows)
        if got is None:
            continue
        if "unusable" in got:
            meta["unusable"][msg] = got["unusable"]
            continue
        entries.append(got["meta"])
        arrays[got["meta"]["key"]] = got["arrs"]

    # 主轨迹：仍按 POS→GPS#0→XKF1→VISP 取第一个可用的（保持历史语义）
    primary: Optional[dict[str, Any]] = None
    for want in TRACK_PRIORITY:
        for ent in entries:
            if ent["source"] == want and (primary is None):
                primary = ent
                break
        if primary is not None:
            break
    for ent in entries:
        ent["primary"] = (ent is primary)
    ordered = ([primary] if primary is not None else []) + [e for e in entries if e is not primary]

    tdir = cdir / "tracks"
    tdir.mkdir(parents=True, exist_ok=True)
    for ent in ordered:
        ent["origin"] = origin          # 地图对本地源做二次投影/量距时要用它
        _atomic_npz(tdir / f"{ent['key']}.npz", **arrays[ent["key"]])
    # 只留 index，旧的孤儿文件由整目录重建保证（缓存目录名含 log 指纹 + schema）
    _atomic_text(tdir / "index.json", json.dumps(ordered, ensure_ascii=False, indent=1))

    if primary is None:
        meta["note"] = ("该日志没有可用位置轨迹：" +
                        ("；".join(f"{k} — {v}" for k, v in meta["unusable"].items())
                         if meta["unusable"] else "POS/GPS/XKF1/VISP 均无足够有效点") +
                        "（GPS 无锁定时 NSats=0/HDop=99.99/经纬度 0 已剔除）")
        _write_track(cdir, np.empty(0), np.empty(0), np.empty(0), np.empty(0),
                     np.empty(0), np.empty(0), np.empty(0), np.empty(0), np.empty(0, np.int8), [])
        meta["tracks"] = ordered
        return meta

    pa = arrays[primary["key"]]
    _write_track(cdir, pa["t"], pa["lat"], pa["lon"], pa["alt"], pa["e"], pa["n"], pa["u"],
                 pa["speed"], pa["src"], primary["src_names"])
    meta.update({"kind": primary["kind"], "source": primary["source"], "inst": primary["inst"],
                 "key": primary["key"], "label": primary["label"],
                 "rows": primary["n"], "distance": primary["distance"],
                 "max_speed": primary["max_speed"], "jumps": primary["jumps"],
                 "climb_min": primary["climb_min"], "climb_max": primary["climb_max"],
                 "alt_min": primary["alt_min"], "alt_max": primary["alt_max"],
                 "axis_note": primary["axis_note"], "src_names": primary["src_names"],
                 "tracks": ordered})
    if len(ordered) > 1:
        meta["note"] = f"共 {len(ordered)} 条轨迹可选：" + "、".join(e["label"] for e in ordered)
    return meta


def build_track(cdir: Path, types_meta: dict[str, Any], bufs: dict[str, _Buf],
                params: list[dict[str, Any]], units: dict[str, str]) -> dict[str, Any]:
    """历史入口：**只关心主轨迹**的调用方继续用它（内部已走多轨迹）。"""
    return build_tracks(cdir, types_meta, bufs, params, units)


def _tmp_name(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.tmp")


def _atomic_npz(path: Path, **arrs: Any) -> None:
    """np.savez 先写临时文件再 os.replace。

    GUI 与批量扫描（多进程）可能同时解析同一份日志，直接写目标文件会让另一方
    读到半截 npz。同卷 replace 是原子的，读者只会看到旧版或新版。
    """
    tmp = _tmp_name(path)
    try:
        with open(tmp, "wb") as fh:
            np.savez(fh, **arrs)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_text(path: Path, text: str) -> None:
    """json 类文件同样走临时文件 + replace（index.json 是新鲜度门，写一半会被误判）。"""
    tmp = _tmp_name(path)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_track(cdir, t, lat, lon, alt, e, n, u, speed, src, names) -> None:
    _atomic_npz(cdir / "track.npz", t=t, lat=lat, lon=lon, alt=alt, e=e, n=n, u=u,
                speed=speed, src=src)
    _atomic_text(cdir / "track_src.json", json.dumps(names, ensure_ascii=False))


def _speed_from(t: np.ndarray, e: np.ndarray, n: np.ndarray) -> np.ndarray:
    if t.size < 2:
        return np.full(t.size, NAN)
    dt = np.diff(t)
    dt[dt <= 0] = math.nan
    return np.concatenate([[NAN], np.hypot(np.diff(e), np.diff(n)) / dt])


def _climb_from(t: np.ndarray, u: np.ndarray) -> tuple[float, float]:
    if t.size < 2:
        return (NAN, NAN)
    dt = np.diff(t)
    mask = dt > 0.01
    if not mask.any():
        return (NAN, NAN)
    vz = np.diff(u)[mask] / dt[mask]
    vz = vz[np.isfinite(vz)]
    if vz.size == 0:
        return (NAN, NAN)
    return (float(vz.min()), float(vz.max()))


# --------------------------------------------------------------------------- #
# 概要与语义推断
# --------------------------------------------------------------------------- #

_FW_HINTS = ("ardupilot", "arducopter", "chibios", "pixhawk", "frame:", "navio", "cuav", "holybro", "matek")


def summarize(types_meta: dict[str, Any], bufs: dict[str, _Buf],
              msg_texts: list[tuple[float, str]], ver: dict[str, Any],
              params: list[dict[str, Any]], track_meta: dict[str, Any], tc_end: float) -> dict[str, Any]:
    """从已解析数据里凑出概要；取不到的字段一律留空，不猜。"""
    firmware = str(ver.get("FWS") or "")
    board = ""
    frame = ""
    git = ""
    for _tc, txt in msg_texts:
        low = txt.lower()
        if not firmware and any(h in low for h in ("ardupilot", "arducopter")):
            firmware = txt
        if not board and any(h in low for h in ("pixhawk", "cuav", "holybro", "matek", "navio", "durandal")):
            board = txt
        if low.startswith("frame:"):
            frame = txt.split(":", 1)[1].strip()
        if low.startswith("chibios:"):
            git = txt.split(":", 1)[1].strip()
    dur = NAN
    if types_meta:
        d0 = min(v["t0"] for v in types_meta.values())
        d1 = max(v["t1"] for v in types_meta.values())
        dur = d1 - d0
    takeoff = land = None
    thr_type, thr_col = None, None
    for tn, cn in (("CTUN", "ThO"), ("MOTB", "ThrOut")):
        if tn in bufs and cn in bufs[tn].cols:
            thr_type, thr_col = tn, cn
            break
    if thr_type:
        tb = bufs[thr_type]
        tt = np.asarray(tb.tc)
        th = np.asarray(tb.cols[thr_col], dtype=np.float64)
        hi = np.isfinite(th) & (th > 0.12)
        if hi.any():
            takeoff = float(tt[int(np.argmax(hi))])
            low = np.isfinite(th) & (th < 0.05)
            after = low & (tt > (takeoff + 1.0))
            land = float(tt[after][-1]) if after.any() else None
    return {
        "duration": dur, "t_start": min((v["t0"] for v in types_meta.values()), default=NAN),
        "t_end": float(tc_end), "takeoff_t": takeoff, "land_t": land,
        "firmware": firmware, "board": board, "frame_type": frame, "git": git,
        "n_types": len(types_meta),
        "max_alt": track_meta.get("alt_max", NAN), "distance": track_meta.get("distance", NAN),
        "max_speed": track_meta.get("max_speed", NAN),
        "pos_kind": track_meta.get("kind", "none"), "pos_source": track_meta.get("source", ""),
        "n_msg": sum(v["rows"] for v in types_meta.values()),
    }


def bat_semantics(types_meta: dict[str, Any], params: list[dict[str, Any]]) -> dict[str, Any]:
    """判断 ``BAT.Volt`` 是整包还是单体电压。只用同日志数据反推，判不出就写 unknown。"""
    out: dict[str, Any] = {"mode": "unknown", "note": "无 BAT 报文"}
    if "BAT" not in types_meta:
        return out
    cols = types_meta["BAT"]["cols"]
    vmax = cols.get("Volt", {}).get("max")
    vrmax = cols.get("VoltR", {}).get("max")
    pmap = {p["name"]: p.get("value") for p in params if p.get("name")}
    arm = pmap.get("BATT_ARM_VOLT") or pmap.get("BATT2_ARM_VOLT") or 0.0
    out.update({"volt_max": vmax, "voltr_max": vrmax, "batt_arm_voltage": arm,
                "batt_capacity": pmap.get("BATT_CAPACITY"),
                "batt_monitor": pmap.get("BATT_MONITOR"),
                "batt_volt_min": pmap.get("BATT_VOLT_MIN"),
                "batt_amp_per_v": pmap.get("BATT_AMP_PERVLT")})
    try:
        fmax = float(vmax) if vmax is not None else NAN
        farm = float(arm) if arm else 0.0
    except (TypeError, ValueError):
        return out
    if not math.isfinite(fmax):
        return out
    if fmax < 9.0:
        out["mode"] = "cell"
        out["note"] = f"BAT.Volt 最大 {fmax:.2f} < 9V，判定为单体电压"
        if farm > 9.0:
            out["cells_est"] = round(farm / max(fmax, 1e-6))
    elif farm > 0 and abs(fmax - farm) / farm < 0.25:
        out["mode"] = "pack"
        out["note"] = f"BAT.Volt 最大 {fmax:.2f} 与 BATT_ARM_VOLT {farm:.2f} 同量级，判定为整包电压"
    else:
        out["mode"] = "pack" if fmax >= 9.0 else "unknown"
        out["note"] = f"按量级判定为{'整包' if fmax >= 9.0 else '未知'}（Volt max={fmax:.2f}, ARM={farm}）"
    if vrmax is not None and math.isfinite(float(vrmax)) and fmax:
        try:
            out["voltr_over_volt"] = float(vrmax) / fmax
        except ZeroDivisionError:
            pass
    return out


__all__ = ["build", "cache_dir_for", "cache_root", "is_fresh", "ParseError",
           "build_track", "build_tracks", "summarize", "bat_semantics", "INST_FIELDS",
           "TRACK_SPECS", "TRACK_PRIORITY", "TRACK_LABELS", "INSTANCE_SPLIT",
           "_track_key", "_track_label"]
