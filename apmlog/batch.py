"""apmlog.batch —— 目录批量体检（一次扫完整个日志目录，出风险排序汇总表）。

用法::

    python -m apmlog.batch "C:\\path\\to\\logs" [--out 报表目录] [--cache 缓存根]
                                  [--max-workers 8] [--force] [--no-recursive] [--json]
                                  [--ext .bin] [--limit N] [--top 10] [--no-alerts] [--quiet]

产物（写到 ``--out``，默认当前工作目录）：

* ``apmlog_batch_report.csv``  —— 给 Excel，UTF-8 BOM，一份日志一行，风险降序
* ``apmlog_batch_report.md``   —— 给人读，Top 风险 / 按告警等级分组 / 共性参数差异
* ``apmlog_batch_report.json`` —— 需要 ``--json`` 时才写，含每份日志的完整告警与参数差异

设计约束（详见 docs/BATCH_REPORT.md）：

* **只读用户日志目录**：本模块只 ``stat`` 日志文件，唯一的写入目标是 ``--out``；若 ``--out``
  落在被扫描目录内部，直接拒绝执行。缓存写入完全交给内核默认位置
  （``APMLOG_CACHE`` → ``%LOCALAPPDATA%\\APMLogViewer\\cache``，见 docs/CONTRACT.md 第 4 节）。
* **只用 docs/CONTRACT.md 第 3 节的公开 API**：``LogStore.open/stats/meta/events/alerts/
  params/types/close``。不 import 内核私有函数，不改内核与 UI。
* **并行**：``ProcessPoolExecutor``，worker 是模块级函数 :func:`_worker`（可 pickle，非闭包），
  每个子进程各自 ``LogStore.open()`` 并在 ``finally`` 里 ``close()``，不跨进程共享对象。
* **健壮**：任何单文件异常都收敛成一行 ``status=FAILED`` 的记录，整批继续。
* **管道友好**：进度写 stderr，最终统计写 stdout，报表写文件。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger("apmlog.batch")

__all__ = ["COLUMNS", "find_logs", "summarize_log", "risk_score", "risk_reasons",
           "sort_rows", "scan_dir", "write_csv", "write_markdown", "write_json", "main"]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 报表默认文件名（不含扩展名）
REPORT_BASENAME = "apmlog_batch_report"

#: 告警等级，从最严重开始；汇总表按这个顺序出列
ALERT_SEVERITIES: tuple[str, ...] = ("critical", "error", "warn", "hint", "info")

#: 缓存膨胀率经验值：实测 8 份真日志 npz/index 合计 ≈ 2.5×日志大小（2.48–2.52）
#: 用于扫描**前**的占用估算；扫描后用实际 ``du`` 覆盖这个估算。
CACHE_RATIO_ESTIMATE = 2.5

#: 预估缓存超过该值（GB）时，扫描前打警告并提示用 --cache 换盘
DEFAULT_CACHE_WARN_GB = 5.0

#: 单个 CSV 单元格最多字符数（超长列表截断，Excel 才不至于卡）
MAX_CELL_CHARS = 320

#: --ext 默认值
DEFAULT_EXTS: tuple[str, ...] = (".bin",)

#: 风险分权重（透明可复算，公式见 risk_score 文档）
W_FAILURE = 100_000     # 解析失败永远排最前
W_CRASH = 5_000         # 每条炸机线索
W_ALERT = {"critical": 400, "error": 120, "warn": 25, "hint": 4, "info": 0}
W_VIBE_CRIT = 300.0     # 任一轴峰值 ≥ 30 m/s²
W_VIBE_WARN = 100.0     # 任一轴峰值 ≥ 15 m/s²
W_CLIP_PER = 0.5        # 每个削波计数
W_CLIP_MAX = 200.0      # 削波项封顶
W_LOW_BATT = 150.0      # 最低单芯电压 < 3.1 V（严重）
W_LOW_BATT_30 = 50.0    # 最低单芯电压 < 3.3 V（警告）


# --------------------------------------------------------------------------- #
# 汇总表列定义（CSV 表头 = Excel 列名）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Column:
    """一列：row 里的键 + 中文表头。"""
    key: str
    header: str


COLUMNS: tuple[Column, ...] = (
    Column("path", "文件路径"),
    Column("name", "文件名"),
    Column("status", "解析是否失败"),
    Column("error", "失败原因"),
    Column("size_bytes", "大小(字节)"),
    Column("size_mb", "大小(MB)"),
    Column("mtime", "修改时间"),
    Column("firmware", "固件"),
    Column("board", "板卡"),
    Column("frame", "机型"),
    Column("duration_s", "时长(秒)"),
    Column("duration_str", "时长"),
    Column("takeoff_s", "起飞时刻(秒)"),
    Column("land_s", "落地时刻(秒)"),
    Column("pos_kind", "位置kind"),
    Column("pos_source", "位置源"),
    Column("pos_rows", "位置有效点数"),
    Column("distance_m", "里程(米)"),
    Column("max_alt_m", "最高高度(米)"),
    Column("max_speed_mps", "最大速度(m/s)"),
    Column("pos_jumps", "位置跳变剔除数"),
    Column("bat_mode", "BAT电压语义"),
    Column("bat_volt_min", "BAT最低电压Volt(V)"),
    Column("bat_voltr_min", "BAT最低电压VoltR(V)"),
    Column("bat_cell_min_v", "最低估算单芯电压(V)"),
    Column("vibe_x_max", "峰值震动X(m/s²)"),
    Column("vibe_y_max", "峰值震动Y(m/s²)"),
    Column("vibe_z_max", "峰值震动Z(m/s²)"),
    Column("vibe_axis", "震动主轴"),
    Column("clip_total", "Clip削波总数"),
    Column("clip_row_peak", "Clip单行峰值"),
    Column("n_alert_critical", "告警critical"),
    Column("n_alert_error", "告警error"),
    Column("n_alert_warn", "告警warn"),
    Column("n_alert_hint", "告警hint"),
    Column("n_alert_info", "告警info"),
    Column("n_alerts", "告警合计"),
    Column("crash_n", "炸机线索数"),
    Column("crashes", "炸机线索"),
    Column("err_n", "ERR条数"),
    Column("err_codes", "ERR事件列表(去重)"),
    Column("n_messages", "报文条数"),
    Column("n_types", "FMT类型数"),
    Column("n_params", "参数条数"),
    Column("n_param_diff", "参数与默认不同条数"),
    Column("segments", "时间段数"),
    Column("kernel_warnings", "内核提示"),
    Column("risk_score", "风险分"),
    Column("from_cache", "复用缓存"),
    Column("parse_s", "索引耗时(秒)"),
    Column("elapsed_s", "本文件耗时(秒)"),
    Column("worker_pid", "工作进程PID"),
    Column("peak_rss_mb", "该进程峰值RSS(MB)"),
)

#: CSV 只写这些键；其余（告警明细、参数差异、缓存目录）只进 JSON
CSV_KEYS = frozenset(c.key for c in COLUMNS)

#: status 取值
STATUS_OK = "OK"
STATUS_FAILED = "FAILED"


# --------------------------------------------------------------------------- #
# 小工具：数值/文本格式化（纯函数，单测覆盖）
# --------------------------------------------------------------------------- #

def _num(v: Any, digits: int = 2) -> Optional[float]:
    """把 nan/inf/None 统一成 None，其余转 float 并四舍五入（CSV 留空比写 nan 好）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, digits)


def _text(v: Any, limit: int = MAX_CELL_CHARS) -> str:
    """转字符串并把换行压成空格，超长截断（CSV/Markdown 单元格安全）。"""
    if v is None:
        return ""
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _fmt_duration(seconds: Any) -> str:
    s = _num(seconds)
    if s is None:
        return "--:--"
    i = int(round(s))
    return f"{i // 3600:02d}:{i % 3600 // 60:02d}:{i % 60:02d}" if i >= 3600 else f"{i // 60:02d}:{i % 60:02d}"


def _fmt_mtime(epoch: Any) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return ""


def _fmt_size(nbytes: Any) -> Optional[float]:
    n = _num(nbytes, 0)
    return None if n is None else round(n / 1048576.0, 2)


def join_cell(items: Sequence[str], limit: int = MAX_CELL_CHARS, sep: str = " | ") -> str:
    """把列表拼成一个单元格，并在截断时补上被省掉的条数。"""
    total = len(items)
    out: list[str] = []
    used = 0
    for i, it in enumerate(items):
        piece = _text(it, 200)
        add = len(piece) + (len(sep) if out else 0)
        if used + add > limit - 12 and out:
            out.append(f"…(+{total - i}条)")
            break
        out.append(piece)
        used += add
    return sep.join(out)


def md_cell(v: Any, limit: int = 90) -> str:
    """Markdown 表格单元格：转义竖线、压缩空白。"""
    s = _text(v, limit)
    return s.replace("|", "\\|") or "-"


# --------------------------------------------------------------------------- #
# 文件发现
# --------------------------------------------------------------------------- #

def normalize_exts(exts: Iterable[str]) -> tuple[str, ...]:
    """.bin / bin / '.BIN,.LOG' → ('.bin', '.log')（小写、带点、去重、保序）。"""
    out: list[str] = []
    for raw in exts:
        for part in str(raw).split(","):
            e = part.strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            if e not in out:
                out.append(e)
    return tuple(out) or DEFAULT_EXTS


def find_logs(target: str | Path, recursive: bool = True,
              exts: Sequence[str] = DEFAULT_EXTS, limit: Optional[int] = None) -> list[Path]:
    """列出待体检文件。``target`` 可以是目录，也可以是单个日志文件。

    只按扩展名过滤（默认 ``.bin``），**不读文件内容**，因此 0 字节/非 BIN/截断文件
    也能进到后续流程，由 :func:`summarize_log` 记成失败行。
    """
    es = normalize_exts(exts)
    p = Path(target)
    if p.is_file():
        files = [p]
    elif p.is_dir():
        it = p.rglob("*") if recursive else p.glob("*")
        files = [f for f in it if f.is_file() and f.suffix.lower() in es]
    else:
        raise FileNotFoundError(f"路径不存在: {p}")
    files = sorted({f.resolve() for f in files}, key=lambda f: str(f).lower())
    return files[:limit] if limit else files


# --------------------------------------------------------------------------- #
# 缓存根目录（按 docs/CONTRACT.md 第 4 节的公开规则复算，仅用于容量估算与 du）
# --------------------------------------------------------------------------- #

def cache_root_for(cache_dir: Optional[str | Path] = None) -> Path:
    """推断缓存根目录：显式 ``--cache`` > ``APMLOG_CACHE`` > ``%LOCALAPPDATA%\\APMLogViewer\\cache``。

    与内核 :func:`apmlog.core.reader.cache_root` 的规则一致，但**不 import 私有函数**；
    真正的权威值是 :pyattr:`LogStore.cache_dir`（公开属性），扫描后会用它校验一次。
    Git-Bash 下 ``LOCALAPPDATA`` 常为空，按内核同样方式回退 ``~/AppData/Local``。
    """
    if cache_dir:
        return Path(cache_dir)
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


def dir_bytes(path: str | Path) -> int:
    """递归统计目录字节数（只 stat，不读内容）。目录不存在返回 0。"""
    root = Path(path)
    if not root.is_dir():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            try:
                total += (Path(dirpath) / fn).stat().st_size
            except OSError:
                continue
    return total


def estimate_cache_bytes(total_log_bytes: int, fresh_log_bytes: int = 0,
                         ratio: float = CACHE_RATIO_ESTIMATE) -> dict[str, Any]:
    """扫描**前**的缓存占用估算：已在缓存里的部分按已花掉的算，新日志按 ratio 估。"""
    new = max(0, int(total_log_bytes) - int(fresh_log_bytes))
    est = int(new * ratio) + int(fresh_log_bytes * ratio)
    return {"log_bytes": int(total_log_bytes), "new_log_bytes": new,
            "ratio": ratio, "estimate_bytes": est}


# --------------------------------------------------------------------------- #
# 单文件摘要
# --------------------------------------------------------------------------- #

def dedupe_error_events(events: Sequence[Any]) -> list[str]:
    """ERR 事件去重：同一 ``Subsys/ECode`` 归一条，带首次时刻、次数与解码文本。

    返回形如 ``12/1 CRASH_CHECK 炸机检测 @322.45s ×1`` 的字符串列表，
    按（严重度降序、首次时刻升序）排列。入参是 :class:`apmlog.core.model.EventRec`
    或任何有 ``code/ecode/severity/t/display`` 属性的对象。
    """
    from apmlog.core.model import severity_rank  # 公开数据契约模块

    buckets: dict[tuple[Any, Any], dict[str, Any]] = {}
    order = 0
    for e in events:
        code = getattr(e, "code", None)
        ecode = getattr(e, "ecode", None)
        key = (code, ecode)
        b = buckets.get(key)
        if b is None:
            b = {"code": code, "ecode": ecode, "sev": getattr(e, "severity", "info"),
                 "t0": float(getattr(e, "t", float("nan"))), "n": 0,
                 "disp": _text(getattr(e, "display", "") or "", 160)}
            buckets[key] = b
        b["n"] += 1
        t = float(getattr(e, "t", float("nan")))
        if math.isfinite(t) and (not math.isfinite(b["t0"]) or t < b["t0"]):
            b["t0"] = t
        if severity_rank(getattr(e, "severity", "info")) > severity_rank(b["sev"]):
            b["sev"] = getattr(e, "severity", "info")
        order += 1
    items = sorted(buckets.values(), key=lambda b: (-severity_rank(b["sev"]), b["t0"], order))
    out: list[str] = []
    for b in items:
        head = f"{b['code']}/{b['ecode']}" if b["ecode"] is not None else f"{b['code']}"
        t = f"@{b['t0']:.2f}s" if math.isfinite(b["t0"]) else ""
        times = f" ×{b['n']}" if b["n"] > 1 else ""
        out.append(f"{head} {b['disp']} {t}{times}".strip())
    return out


def collect_param_diffs(params: dict[str, tuple[float, Optional[float]]]) -> dict[str, list[Any]]:
    """挑出"记录值 ≠ 出厂默认"的参数。没有默认值（4.6 老日志）的参数一律不计。"""
    out: dict[str, list[Any]] = {}
    for name, got in (params or {}).items():
        try:
            val, dft = got[0], got[1]
        except (TypeError, IndexError):
            continue
        if dft is None or val is None:
            continue
        if not (math.isfinite(float(val)) and math.isfinite(float(dft))):
            continue
        v, d = float(val), float(dft)
        tol = max(1e-9, abs(d) * 1e-6)
        if abs(v - d) > tol:
            out[str(name)] = [v, d]
    return out


def count_alert_severities(alerts: Sequence[Any]) -> dict[str, int]:
    """告警按等级计数，返回固定 5 个键（缺失补 0），供汇总表列直接展开。"""
    from apmlog.core.model import SEVERITY_LEVELS

    counts = {s: 0 for s in ALERT_SEVERITIES}
    for a in alerts or ():
        sev = getattr(a, "severity", None) or (a.get("severity") if isinstance(a, dict) else "info")
        if sev not in SEVERITY_LEVELS:
            sev = "info"
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def alert_rows(path_name: str, alerts: Sequence[Any]) -> list[dict[str, Any]]:
    """告警明细 → 可 JSON 化的行（Markdown 分组用）。"""
    out = []
    for a in alerts or ():
        d = a.as_dict() if hasattr(a, "as_dict") else dict(a)
        d["file"] = path_name
        out.append(d)
    return out


def _vibe_summary(store, tm: dict[str, Any]) -> dict[str, Any]:
    """峰值震动（跨实例最大瞬时值）+ Clip 削波总数（累计计数器的正增量之和）。

    meta 的列统计是**跨实例合并**的真最大/最小值；内核 envelope 用 min/max 保形抽稀，
    不丢峰值，所以这里等价于"三轴包络最大值"。Clip 按内核 alerts.py 同一口径
    （对每个实例取正增量求和，兼容累计计数器与复位），另存单行最大值以便识别语义。
    """
    from apmlog.core.model import ChannelKey

    res: dict[str, Any] = {"vibe_x_max": None, "vibe_y_max": None, "vibe_z_max": None,
                           "vibe_axis": "", "clip_total": None, "clip_row_peak": None}
    cols = tm.get("cols", {}) if isinstance(tm, dict) else {}
    peaks: dict[str, float] = {}
    for axis, outkey in (("VibeX", "vibe_x_max"), ("VibeY", "vibe_y_max"), ("VibeZ", "vibe_z_max")):
        mx = cols.get(axis, {}).get("max")
        n = _num(mx)
        res[outkey] = n
        if n is not None:
            peaks[axis] = n
    if peaks:
        res["vibe_axis"] = max(peaks, key=lambda k: peaks[k])
    if "Clip" not in cols:
        return res
    insts = [i for i in tm.get("insts", []) if isinstance(i, int) and i >= 0] or [None]
    total = 0.0
    row_peak: Optional[float] = None
    got = False
    for inst in insts:
        try:
            ser = store.series(ChannelKey("VIBE", "Clip", inst))
        except KeyError as exc:
            log.debug("Clip 通道不可用 %s: %s", inst, exc)
            continue
        v = ser.v
        if v.size == 0:
            continue
        v = v[np_isfinite(v)]
        if v.size == 0:
            continue
        got = True
        if v.size > 1:
            d = v[1:] - v[:-1]
            d = d[np_isfinite(d)]
            inc = d[d > 0]
            total += float(inc.sum()) if inc.size else 0.0
        pk = float(v.max())
        row_peak = pk if row_peak is None else max(row_peak, pk)
    if got:
        res["clip_total"] = round(total, 1)
        res["clip_row_peak"] = round(row_peak or 0.0, 1)
    return res


def np_isfinite(arr):  # noqa: ANN001 - 小包装，避免模块级强依赖 numpy 导入顺序
    """numpy.isfinite 的本地别名（延迟 import，保证 --help 冷启动快）。"""
    import numpy as np
    return np.isfinite(arr)


def _empty_row(path: Path, st: Optional[os.stat_result] = None) -> dict[str, Any]:
    """失败行兜底：所有键都存在，数值列为 None，文本列为空。"""
    info = st if st is not None else None
    row: dict[str, Any] = {c.key: None for c in COLUMNS}
    row.update({
        "path": str(path),
        "name": path.name,
        "status": STATUS_FAILED,
        "error": "",
        "size_bytes": int(info.st_size) if info else None,
        "size_mb": _fmt_size(info.st_size) if info else None,
        "mtime": _fmt_mtime(info.st_mtime) if info else "",
        "risk_score": 0,
        "err_codes": "",
        "crashes": "",
        "kernel_warnings": "",
        "vibe_axis": "",
        "alerts": [],
        "param_diffs": {},
        "cache_dir": "",
    })
    for sev in ALERT_SEVERITIES:
        row[f"n_alert_{sev}"] = 0
    row["n_alerts"] = 0
    return row


def summarize_log(path: str | Path, cache_dir: Optional[str | Path] = None,
                  force: bool = False, with_alerts: bool = True,
                  run_started_at: Optional[float] = None) -> dict[str, Any]:
    """体检一份日志，返回一行汇总表记录。**任何异常都收敛成 FAILED 行，绝不抛出**。

    缓存命中与否由内核决定（``force=True`` 强制重建）；本函数只在 ``finally`` 里
    ``close()``，不写日志所在目录。
    """
    p = Path(path)
    t0 = time.perf_counter()
    try:
        st = p.stat()
    except OSError as exc:
        row = _empty_row(p)
        row["error"] = _text(f"{type(exc).__name__}: {exc}", 200)
        row["status"] = STATUS_FAILED
        row["risk_score"] = risk_score(row)
        row["elapsed_s"] = round(time.perf_counter() - t0, 3)
        return row

    row = _empty_row(p, st)
    store = None
    try:
        if st.st_size == 0:
            raise ValueError("文件为 0 字节，没有任何报文")
        from apmlog.core.store import LogStore  # 延迟 import：--help 不加载内核

        store = LogStore.open(p, cache_dir=cache_dir, force=force, progress=None)
        meta = store.meta or {}
        stats = store.stats
        tm = meta.get("types", {}) or {}
        trk = meta.get("track", {}) or {}
        bat_tm_cols = (tm.get("BAT") or {}).get("cols", {})
        bat_mode = (meta.get("bat_semantics") or {}).get("mode", "unknown")

        row.update({
            "status": STATUS_OK,
            "error": "",
            "firmware": _text(stats.firmware, 120),
            "board": _text(stats.board, 120),
            "frame": _text(stats.frame_type, 60),
            "duration_s": _num(stats.duration, 1),
            "duration_str": _fmt_duration(stats.duration),
            "takeoff_s": _num(stats.takeoff_t, 2),
            "land_s": _num(stats.land_t, 2),
            "pos_kind": _text(trk.get("kind", stats.pos_kind), 20),
            "pos_source": _text(trk.get("source", stats.pos_source), 30),
            "pos_rows": int(trk.get("rows", 0) or 0),
            "distance_m": _num(trk.get("distance", stats.distance), 1),
            "max_alt_m": _num(trk.get("alt_max", stats.max_alt), 2),
            "max_speed_mps": _num(trk.get("max_speed", stats.max_speed), 2),
            "pos_jumps": int(trk.get("jumps", 0) or 0),
            "bat_mode": _text(bat_mode, 20),
            "bat_volt_min": _num(bat_tm_cols.get("Volt", {}).get("min")),
            "bat_voltr_min": _num(bat_tm_cols.get("VoltR", {}).get("min")),
            "bat_cell_min_v": _cell_min_v(meta, bat_tm_cols),
            "n_messages": int(stats.n_messages or 0),
            "n_types": len(tm),
            "n_params": int(meta.get("n_params", stats.n_params) or 0),
            "segments": len(meta.get("segments", []) or []),
            "kernel_warnings": join_cell([_text(w, 120) for w in (stats.warnings or [])], 200),
            "crashes": join_cell([_text(c, 160) for c in (stats.crashes or [])], 240),
            "crash_n": len(stats.crashes or []),
            "from_cache": _is_hot(meta, run_started_at),
            "parse_s": _num(meta.get("seconds"), 2),
            "cache_dir": str(store.cache_dir),
        })
        row.update(_vibe_summary(store, tm.get("VIBE") or {}))

        try:
            errs = [e for e in store.events() if getattr(e, "kind", "") == "ERR"]
        except Exception as exc:  # 事件解码坏不该拖垮整行
            log.warning("%s ERR 事件提取失败: %s", p.name, exc)
            errs = []
        row["err_n"] = len(errs)
        row["err_codes"] = join_cell(dedupe_error_events(errs), 400)

        try:
            pd = collect_param_diffs(store.params())
        except Exception as exc:
            log.warning("%s 参数提取失败: %s", p.name, exc)
            pd = {}
        row["n_param_diff"] = len(pd)
        row["param_diffs"] = pd

        hits: list[Any] = []
        if with_alerts:
            try:
                hits = store.alerts()
            except Exception as exc:  # 规则评估失败仍可出其他列
                log.warning("%s 告警评估失败: %s", p.name, exc)
                row["kernel_warnings"] = join_cell(
                    [row["kernel_warnings"], f"告警评估失败: {type(exc).__name__}: {exc}"], 260)
        row.update({f"n_alert_{k}": v for k, v in count_alert_severities(hits).items()})
        row["n_alerts"] = sum(row[f"n_alert_{k}"] for k in ALERT_SEVERITIES)
        row["alerts"] = alert_rows(p.name, hits)
    except Exception as exc:
        row["status"] = STATUS_FAILED
        row["error"] = _text(f"{type(exc).__name__}: {exc}", 300)
        log.info("%s 解析失败: %s", p.name, exc)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # pragma: no cover
                pass
    row["risk_score"] = risk_score(row)
    row["elapsed_s"] = round(time.perf_counter() - t0, 3)
    return row


def _is_hot(meta: dict[str, Any], run_started_at: Optional[float]) -> Optional[bool]:
    """用内核写进 index 的 ``built_at`` 判断这份日志本次是否复用缓存。"""
    if run_started_at is None:
        return None
    built = str(meta.get("built_at", "") or "")
    try:
        ts = datetime.strptime(built, "%Y-%m-%dT%H:%M:%S").timestamp()
    except ValueError:
        return None
    return ts < run_started_at


# --------------------------------------------------------------------------- #
# 风险评分与排序（纯函数）
# --------------------------------------------------------------------------- #

def risk_score(row: dict[str, Any]) -> int:
    """风险分（越大越该先看）。公式全部在这里，不做任何隐藏判断：

    * 解析失败 → ``100000 + `` 其余项（失败一定排最前，但失败之间仍可比较）
    * 每条炸机线索 +5000
    * 告警：critical +400 / error +120 / warn +25 / hint +4 / info 0
    * 任一轴震动峰值 ≥30 m/s² +300；≥15 m/s² +100
    * Clip 削波总数 ×0.5，封顶 +200
    * 最低估算单芯电压 <3.1V +150；<3.3V +50（RemPct 不参与）
    """
    if not row:
        return 0
    s = 0
    if str(row.get("status")) != STATUS_OK:
        s += W_FAILURE
    s += W_CRASH * int(row.get("crash_n") or 0)
    for sev, w in W_ALERT.items():
        s += w * int(row.get(f"n_alert_{sev}") or 0)
    peaks = [row.get(k) for k in ("vibe_x_max", "vibe_y_max", "vibe_z_max")]
    peaks = [float(v) for v in peaks if isinstance(v, (int, float)) and math.isfinite(v)]
    if peaks:
        mx = max(peaks)
        if mx >= 30.0:
            s += W_VIBE_CRIT
        elif mx >= 15.0:
            s += W_VIBE_WARN
    clip = row.get("clip_total")
    if isinstance(clip, (int, float)) and math.isfinite(clip):
        s += min(W_CLIP_MAX, W_CLIP_PER * float(clip))
    cell = row.get("bat_cell_min_v")
    if isinstance(cell, (int, float)) and math.isfinite(cell):
        if cell < 3.1:
            s += W_LOW_BATT
        elif cell < 3.3:
            s += W_LOW_BATT_30
    return int(round(s))


def _cell_min_v(meta: dict, bat_cols: dict) -> float | None:
    """最低估算单芯电压 = BAT.Volt 最低值 ÷ 电芯数（自动识别）。

    RemPct 不参与（用户 2026-10-24）：它是飞控按电压反推的估计值，批处理只报
    原始电压换算出来的量。识别不出串数就留空，不猜。
    """
    vmin = bat_cols.get("Volt", {}).get("min")
    if vmin is None:
        vmin = bat_cols.get("VoltR", {}).get("min")
    if vmin is None:
        return None
    try:
        from apmlog.core.alerts import resolve_cells
        n, _src, _note = resolve_cells({}, meta.get("bat_semantics") or {})
        if not n:
            return None
        return round(float(vmin) / float(n), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def risk_reasons(row: dict[str, Any]) -> list[str]:
    """人读的"为什么排在这里"，只列真触发项。"""
    out: list[str] = []
    if str(row.get("status")) != STATUS_OK:
        out.append(f"解析失败：{row.get('error') or '未知原因'}")
    if row.get("crash_n"):
        out.append(f"炸机线索 {row['crash_n']} 条")
    for sev in ("critical", "error", "warn", "hint"):
        n = int(row.get(f"n_alert_{sev}") or 0)
        if n:
            out.append(f"{sev}×{n}")
    peaks = [(k, row.get(k)) for k in ("vibe_x_max", "vibe_y_max", "vibe_z_max")]
    peaks = [(k, v) for k, v in peaks if isinstance(v, (int, float)) and math.isfinite(v)]
    if peaks:
        k, v = max(peaks, key=lambda kv: kv[1])
        if v >= 15.0:
            out.append(f"{k.split('_')[1].upper()} 峰值 {v:.1f} m/s²")
    clip = row.get("clip_total")
    if isinstance(clip, (int, float)) and math.isfinite(clip) and clip > 0:
        out.append(f"削波 {int(clip)} 次")
    cell = row.get("bat_cell_min_v")
    if isinstance(cell, (int, float)) and math.isfinite(cell) and cell < 3.3:
        out.append(f"单芯电压最低 {cell:.2f} V")
    if int(row.get("err_n") or 0) > 0:
        out.append(f"ERR {row['err_n']} 条")
    if row.get("pos_kind") == "none":
        out.append("无位置报文")
    elif row.get("pos_kind") == "local":
        out.append("无 GNSS 经纬度")
    return out


def sort_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """风险降序；同分按文件名升序，保证多次扫描顺序一致。"""
    return sorted(rows, key=lambda r: (-int(r.get("risk_score") or 0), str(r.get("path") or "")))


def group_alerts_by_severity(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """把各文件的告警明细按等级归组，组内按（文件风险分降序、t0 升序）排。"""
    grouped: dict[str, list[dict[str, Any]]] = {s: [] for s in ALERT_SEVERITIES}
    score_of = {str(r.get("path")): int(r.get("risk_score") or 0) for r in rows}
    for r in rows:
        for a in r.get("alerts") or ():
            sev = a.get("severity") or "info"
            grouped.setdefault(sev, []).append(a)
    for sev, lst in grouped.items():
        lst.sort(key=lambda a: (-score_of.get(str(a.get("_path", "")), 0),
                                a.get("t0") if isinstance(a.get("t0"), float) and math.isfinite(a.get("t0") or 0) else 1e18,
                                str(a.get("file", ""))))
    return grouped


def totals(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """一批日志的合计：成功/失败/炸机/各等级告警总数。"""
    ok = [r for r in rows if str(r.get("status")) == STATUS_OK]
    bad = [r for r in rows if str(r.get("status")) != STATUS_OK]
    out: dict[str, Any] = {
        "files": len(rows), "ok": len(ok), "failed": len(bad),
        "with_crash": sum(1 for r in ok if int(r.get("crash_n") or 0) > 0),
        "crash_rows": sum(int(r.get("crash_n") or 0) for r in rows),
        "with_err": sum(1 for r in ok if int(r.get("err_n") or 0) > 0),
        "geodetic": sum(1 for r in ok if r.get("pos_kind") == "geodetic"),
        "local": sum(1 for r in ok if r.get("pos_kind") == "local"),
        "no_pos": sum(1 for r in ok if r.get("pos_kind") == "none"),
    }
    for sev in ALERT_SEVERITIES:
        out[f"alert_{sev}"] = sum(int(r.get(f"n_alert_{sev}") or 0) for r in rows)
    out["alerts"] = sum(out[f"alert_{s}"] for s in ALERT_SEVERITIES)
    out["log_bytes"] = sum(int(r.get("size_bytes") or 0) for r in rows)
    return out


def aggregate_param_diffs(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """共性参数差异统计：某参数在多少份日志里与出厂默认不同。"""
    stat: dict[str, dict[str, Any]] = {}
    for r in rows:
        for name, got in (r.get("param_diffs") or {}).items():
            d = stat.setdefault(name, {"name": name, "files": 0, "values": {},
                                       "default": None, "examples": []})
            d["files"] += 1
            try:
                v, df = float(got[0]), (None if got[1] is None else float(got[1]))
            except (TypeError, ValueError, IndexError):
                continue
            key = f"{v:g}"
            d["values"][key] = d["values"].get(key, 0) + 1
            if d["default"] is None and df is not None:
                d["default"] = df
            if len(d["examples"]) < 3:
                d["examples"].append(str(r.get("name") or r.get("path")))
    out = []
    for d in stat.values():
        vals = d.pop("values")
        d["distinct_values"] = len(vals)
        d["value_counts"] = dict(sorted(vals.items(), key=lambda kv: -kv[1])[:8])
        out.append(d)
    out.sort(key=lambda d: (-d["files"], d["name"]))
    return out


# --------------------------------------------------------------------------- #
# 报表写出
# --------------------------------------------------------------------------- #

def csv_rows(rows: Sequence[dict[str, Any]]) -> list[list[Any]]:
    """按 COLUMNS 顺序展开成表格行（数值 None → 空串）。"""
    out: list[list[Any]] = [[c.header for c in COLUMNS]]
    for r in rows:
        line = []
        for c in COLUMNS:
            v = r.get(c.key)
            if v is None:
                line.append("")
            elif isinstance(v, float):
                line.append(f"{v:g}" if math.isfinite(v) else "")
            elif isinstance(v, bool):
                line.append("是" if v else "否")
            else:
                line.append(v)
        out.append(line)
    return out


def write_csv(rows: Sequence[dict[str, Any]], path: str | Path) -> Path:
    """写 CSV（UTF-8 BOM，Excel 直接双击不乱码），风险已排序后的顺序落盘。"""
    fp = Path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        for line in csv_rows(rows):
            w.writerow(line)
    return fp


def _sanitize_for_json(obj: Any) -> Any:
    """递归把 nan/inf 转 None、tuple 转 list，保证严格 JSON（Excel/JS 都能读）。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def write_json(rows: Sequence[dict[str, Any]], ctx: dict[str, Any], path: str | Path) -> Path:
    """写 JSON：每行去掉 CSV 已覆盖的大字段之外的全部内容（含告警明细、参数差异）。"""
    fp = Path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "tool": "apmlog.batch",
        "generated_at": ctx.get("generated_at"),
        "target": ctx.get("target"),
        "options": ctx.get("options"),
        "run": ctx.get("run"),
        "cache": ctx.get("cache"),
        "totals": ctx.get("totals"),
        "rows": [{k: v for k, v in r.items() if k not in {"_path"}} for r in rows],
    }
    fp.write_text(json.dumps(_sanitize_for_json(payload), ensure_ascii=False, indent=1),
                  encoding="utf-8")
    return fp


def render_markdown(ctx: dict[str, Any], top: int = 10) -> str:
    """渲染人读报告（纯字符串函数，便于单测）。"""
    rows: list[dict[str, Any]] = ctx["rows"]
    tt: dict[str, Any] = ctx.get("totals") or totals(rows)
    run: dict[str, Any] = ctx.get("run") or {}
    cache: dict[str, Any] = ctx.get("cache") or {}
    ok_rows = [r for r in rows if str(r.get("status")) == STATUS_OK]
    L: list[str] = []
    A = L.append

    A("# APMLogViewer 目录批量体检报告")
    A("")
    A(f"- 目录：`{ctx.get('target', '')}`")
    A(f"- 生成时间：{ctx.get('generated_at', '')}")
    A(f"- 文件：{tt['files']} 份（成功 {tt['ok']} / 失败 {tt['failed']}），"
      f"总大小 {tt['log_bytes'] / 1048576.0:.0f} MB")
    A(f"- 并行：{run.get('workers', '-')} worker，冷/热={run.get('hot_label', '-')}，"
      f"总耗时 {run.get('seconds', '-')} s，单文件中位数 {run.get('median_s', '-')} s，"
      f"峰值 RSS {run.get('peak_rss_mb', '-')} MB")
    A(f"- 缓存根：`{cache.get('root', '-')}`，本次占用 {cache.get('used_after_gb', '-')} GB"
      f"（扫描前估算 {cache.get('estimate_gb', '-')} GB），复用 {run.get('hot', '-')}/{tt['ok']} 份")
    A(f"- 风险分布：炸机线索 {tt['with_crash']} 份 / 含 ERR {tt['with_err']} 份 / "
      f"critical 告警 {tt['alert_critical']} 条 / error {tt['alert_error']} 条 / "
      f"warn {tt['alert_warn']} 条 / hint {tt['alert_hint']} 条")
    A("")
    A("## Top 风险日志")
    A("")
    if not rows:
        A("_没有可分析的文件。_")
    else:
        A("| # | 文件 | 风险分 | critical | error | warn | 炸机 | ERR | 震动峰(m/s²) | 削波 | "
          "最低电压(V) | 最低电量(%) | 时长 | 位置 | 要点 |")
        A("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|")
        for i, r in enumerate(sort_rows(rows)[:top], 1):
            peaks = [r.get(k) for k in ("vibe_x_max", "vibe_y_max", "vibe_z_max")]
            peaks = [p for p in peaks if isinstance(p, (int, float))]
            volt = [v for v in (r.get("bat_volt_min"), r.get("bat_voltr_min")) if v is not None]
            A(f"| {i} | `{md_cell(r.get('name'), 46)}` | {r.get('risk_score', 0)} | "
              f"{r.get('n_alert_critical') or 0} | {r.get('n_alert_error') or 0} | "
              f"{r.get('n_alert_warn') or 0} | {r.get('crash_n') or 0} | {r.get('err_n') or 0} | "
              f"{md_cell(f'{max(peaks):.1f}' if peaks else '-')} | "
              f"{md_cell(r.get('clip_total') if r.get('clip_total') is not None else '-')} | "
              f"{md_cell(f'{min(volt):.2f}' if volt else '-')} | "
              f"{md_cell(f'{cell:.2f}' if (cell := r.get('bat_cell_min_v')) is not None else '-')} | "
              f"{md_cell(r.get('duration_str'), 12)} | "
              f"{md_cell(str(r.get('pos_kind') or '-') + '/' + str(r.get('pos_source') or '-'), 22)} | "
              f"{md_cell('；'.join(risk_reasons(r)), 150)} |")
    A("")

    A("## 按告警等级分组")
    A("")
    A("| 等级 | 条数 | 涉及日志数 |")
    A("|---|---:|---:|")
    for sev in ALERT_SEVERITIES:
        n = int(tt.get(f"alert_{sev}", 0))
        files = sum(1 for r in rows if int(r.get(f"n_alert_{sev}") or 0) > 0)
        A(f"| {sev} | {n} | {files} |")
    A("")
    grouped = group_alerts_by_severity(rows)
    for sev in ("critical", "error"):
        lst = grouped.get(sev) or []
        if not lst:
            continue
        A(f"### {sev} 明细（{len(lst)} 条）")
        A("")
        A("| 文件 | 规则 | 时刻(s) | 实测 | 阈值 | 出处 | 说明 |")
        A("|---|---|---|---:|---:|---|---|")
        for a in lst[:60]:
            A(f"| `{md_cell(a.get('file'), 40)}` | {md_cell(a.get('title'), 40)} | "
              f"{md_cell(_num(a.get('t0'), 1) if a.get('t0') is not None else '-')} | "
              f"{md_cell(_num(a.get('value'))) } | {md_cell(_num(a.get('threshold')))} | "
              f"{md_cell(a.get('source'), 14)} | {md_cell(a.get('message'), 120)} |")
        if len(lst) > 60:
            A(f"")
            A(f"> {sev} 共 {len(lst)} 条，此处只列 60 条，完整见 CSV/JSON。")
        A("")
    warn_rules: dict[str, int] = {}
    for a in grouped.get("warn") or []:
        warn_rules[str(a.get("title"))] = warn_rules.get(str(a.get("title")), 0) + 1
    if warn_rules:
        A("### warn 按规则聚合")
        A("")
        A("| 规则 | 条数 |")
        A("|---|---:|")
        for k, v in sorted(warn_rules.items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
            A(f"| {md_cell(k, 60)} | {v} |")
        A("")

    A("## 共性参数差异统计")
    A("")
    diffs = aggregate_param_diffs(ok_rows)
    if not diffs:
        A("_这些日志的 PARM 都等于出厂默认，或该固件未记录默认值（4.6 及更早无 Default）。_")
    else:
        n_files = len(ok_rows)
        A(f"共 {len(diffs)} 个参数在至少一份日志里与出厂默认不同（成功日志 {n_files} 份）。"
          "下表按「多少份日志不同」排序，前 25 个；完整数据在 JSON 的 `rows[].param_diffs`。")
        A("")
        A("| 参数 | 与默认不同的份数 | 出厂默认 | 出现值(份数) | 示例日志 |")
        A("|---|---:|---:|---|---|")
        for d in diffs[:25]:
            vc = "; ".join(f"{k}×{v}" for k, v in list(d["value_counts"].items())[:4])
            A(f"| `{md_cell(d['name'], 34)}` | {d['files']}/{n_files} | "
              f"{md_cell(_num(d['default'], 4))} | {md_cell(vc, 90)} | {md_cell(', '.join(d['examples']), 70)} |")
        A("")

    A("## 数据质量与覆盖")
    A("")
    A(f"- 位置源：geodetic {tt['geodetic']} 份 / local {tt['local']} 份 / none {tt['no_pos']} 份")
    no_bat = sum(1 for r in ok_rows if r.get("bat_volt_min") is None and r.get("bat_voltr_min") is None)
    no_vibe = sum(1 for r in ok_rows if r.get("vibe_x_max") is None
                  and r.get("vibe_y_max") is None and r.get("vibe_z_max") is None)
    multi_seg = sum(1 for r in ok_rows if int(r.get("segments") or 0) > 1)
    jumps = sum(int(r.get("pos_jumps") or 0) for r in ok_rows)
    A(f"- 无 BAT 报文：{no_bat} 份；无 VIBE 报文：{no_vibe} 份；多时间段（多次上电）：{multi_seg} 份；"
      f"位置跳变剔除合计 {jumps} 处")
    boards = {}
    fws = {}
    frames = {}
    for r in ok_rows:
        boards[r.get("board") or "-"] = boards.get(r.get("board") or "-", 0) + 1
        fws[r.get("firmware") or "-"] = fws.get(r.get("firmware") or "-", 0) + 1
        frames[r.get("frame") or "-"] = frames.get(r.get("frame") or "-", 0) + 1
    A(f"- 固件：{md_cell('; '.join(f'{k}×{v}' for k, v in sorted(fws.items(), key=lambda kv: -kv[1])), 200)}")
    A(f"- 板卡：{md_cell('; '.join(f'{k}×{v}' for k, v in sorted(boards.items(), key=lambda kv: -kv[1])[:6]), 200)}")
    A(f"- 机型：{md_cell('; '.join(f'{k}×{v}' for k, v in sorted(frames.items(), key=lambda kv: -kv[1])), 120)}")
    A("")

    bad = [r for r in rows if str(r.get("status")) != STATUS_OK]
    A("## 失败/异常文件清单")
    A("")
    if not bad:
        A("_无：全部文件都成功解析。_")
    else:
        A("| 文件 | 大小(MB) | 原因 |")
        A("|---|---:|---|")
        for r in bad:
            A(f"| `{md_cell(r.get('name'), 60)}` | {md_cell(r.get('size_mb'))} | {md_cell(r.get('error'), 200)} |")
    A("")

    A("## 全量汇总（前 30 行预览，完整表见 CSV）")
    A("")
    A("| 文件 | 风险分 | 时长 | 里程(m) | 最高(m) | 最大速(m/s) | 位置 | 告警(c/e/w/h) | 状态 |")
    A("|---|---:|---|---:|---:|---:|---|---|---|")
    for r in sort_rows(rows)[:30]:
        A(f"| `{md_cell(r.get('name'), 42)}` | {r.get('risk_score', 0)} | "
          f"{md_cell(r.get('duration_str'), 10)} | {md_cell(r.get('distance_m'))} | "
          f"{md_cell(r.get('max_alt_m'))} | {md_cell(r.get('max_speed_mps'))} | "
          f"{md_cell(f"{r.get('pos_kind') or '-'}/{r.get('pos_source') or '-'}", 20)} | "
          f"{md_cell(f"{r.get('n_alert_critical') or 0}/{r.get('n_alert_error') or 0}/"
                     f"{r.get('n_alert_warn') or 0}/{r.get('n_alert_hint') or 0}", 16)} | "
          f"{md_cell(r.get('status'), 12)} |")
    A("")
    A("> 风险分公式见 `apmlog/batch.py::risk_score`；震动/削波口径与内核 alerts 规则同源，"
      "阈值出处在 `config/alerts.yaml`，本表不做任何阈值以外的推断。")
    A("")
    return "\n".join(L) + "\n"


def write_markdown(ctx: dict[str, Any], path: str | Path, top: int = 10) -> Path:
    fp = Path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(render_markdown(ctx, top=top), encoding="utf-8")
    return fp


# --------------------------------------------------------------------------- #
# 并行扫描
# --------------------------------------------------------------------------- #

def default_max_workers(requested: Optional[int] = None) -> int:
    """默认 ``min(8, cpu_count)``；``--max-workers`` 超限时按 cpu 封顶并提示。"""
    cpus = max(1, (os.cpu_count() or 1))
    cap = min(8, cpus)
    if requested is None:
        return cap
    n = max(1, int(requested))
    return n if n <= cap else cpus if n > cap and n <= cpus else cap


def peak_rss_mb() -> Optional[float]:
    """当前进程的峰值工作集（MB）。Windows 用 GetProcessMemoryInfo，POSIX 用 getrusage。"""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class PMCounter(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = PMCounter()
            counters.cb = ctypes.sizeof(PMCounter)
            hproc = ctypes.windll.kernel32.GetCurrentProcess()
            ok = False
            for dll_name in ("kernel32", "psapi"):
                try:
                    dll = getattr(ctypes.windll, dll_name)
                    if dll.GetProcessMemoryInfo(hproc, ctypes.byref(counters), counters.cb):
                        ok = True
                        break
                except Exception:
                    continue
            if ok:
                return round(counters.PeakWorkingSetSize / 1048576.0, 1)
        except Exception:  # pragma: no cover - 取不到就留空，绝不影响扫描
            return None
        return None
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except Exception:  # pragma: no cover
        return None


def _worker(job: tuple[str, Optional[str], bool, bool, float]) -> dict[str, Any]:
    """进程池 worker（必须是模块级函数才能 pickle）。自己 open/close，绝不共享对象。"""
    path_str, cache_dir, force, with_alerts, started = job
    try:
        row = summarize_log(path_str, cache_dir=cache_dir, force=force,
                            with_alerts=with_alerts, run_started_at=started)
    except BaseException as exc:  # pragma: no cover - summarize_log 已兜底，这里防意外
        row = _empty_row(Path(path_str))
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["risk_score"] = risk_score(row)
    row["worker_pid"] = os.getpid()
    rss = peak_rss_mb()
    row["peak_rss_mb"] = rss
    if row.get("status") == STATUS_FAILED:
        row.setdefault("elapsed_s", 0.0)
    return row


def _median(values: Sequence[float]) -> Optional[float]:
    vs = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not vs:
        return None
    m = len(vs) // 2
    return round(vs[m], 2) if len(vs) % 2 else round((vs[m - 1] + vs[m]) / 2.0, 2)


def _guard_out_dir(out: Path, target: Path) -> None:
    """绝不允许把报表写进被扫描的日志目录（保持日志目录只读）。"""
    try:
        o = os.path.normcase(str(out.resolve()))
        t = os.path.normcase(str(target.resolve()))
    except OSError as exc:  # pragma: no cover
        raise ValueError(f"输出目录不可用: {exc}") from exc
    if o == t or o.startswith(t + os.sep):
        raise ValueError(f"--out 不能在被扫描目录内部（日志目录必须保持只读）：{out} ⊂ {target}")


def scan_dir(target: str | Path, *, out_dir: Optional[str | Path] = None,
             cache_dir: Optional[str | Path] = None, force: bool = False,
             max_workers: Optional[int] = None, recursive: bool = True,
             exts: Sequence[str] = DEFAULT_EXTS, limit: Optional[int] = None,
             with_alerts: bool = True, write_json_out: bool = False,
             progress: bool = True, cache_warn_gb: float = DEFAULT_CACHE_WARN_GB,
             top: int = 10, quiet: bool = False) -> dict[str, Any]:
    """扫完整个目录，返回上下文 dict（rows/run/cache/totals/报表路径）。"""
    tp = Path(target)
    started = time.time()
    files = find_logs(tp, recursive=recursive, exts=exts, limit=limit)
    if not files:
        raise FileNotFoundError(f"{tp} 下没有找到扩展名 {','.join(normalize_exts(exts))} 的文件")
    out = Path(out_dir) if out_dir else Path.cwd()
    guard_target = tp if tp.is_dir() else tp.parent
    _guard_out_dir(out, guard_target)
    out.mkdir(parents=True, exist_ok=True)

    workers = default_max_workers(max_workers)
    total_log_bytes = sum(f.stat().st_size for f in files if f.exists())
    root = cache_root_for(cache_dir)
    cache_before = dir_bytes(root)
    est = estimate_cache_bytes(total_log_bytes)
    if not quiet:
        print(f"[cache] 根目录 {root}（当前 {cache_before / 2**30:.2f} GB）；"
              f"待扫 {len(files)} 份 / {total_log_bytes / 2**20:.0f} MB，"
              f"预计新增缓存 ≈ {est['estimate_bytes'] / 2**30:.2f} GB（{CACHE_RATIO_ESTIMATE}×实测膨胀率）",
              file=sys.stderr, flush=True)
        if est["estimate_bytes"] / 2**30 > cache_warn_gb:
            print(f"[warn] 预计缓存 {est['estimate_bytes'] / 2**30:.1f} GB 超过阈值 "
                  f"{cache_warn_gb} GB；建议用 --cache D:\\other\\apmlog_cache 指到空间充足的盘，"
                  "或降低 --max-workers 以减小并发写入。", file=sys.stderr, flush=True)
        if force:
            print("[info] --force：所有日志重新索引，缓存会被覆盖重建", file=sys.stderr, flush=True)

    jobs = [(str(f), str(cache_dir) if cache_dir else None, bool(force),
             bool(with_alerts), started) for f in files]
    rows: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    interrupted = False
    if workers == 1 or len(jobs) == 1:
        for i, job in enumerate(jobs, 1):
            row = _worker(job)
            rows.append(row)
            if progress:
                _progress(i, len(jobs), row, file=sys.stderr)
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_worker, job): job for job in jobs}
            try:
                for i, fut in enumerate(as_completed(futs), 1):
                    try:
                        row = fut.result()
                    except BaseException as exc:  # pragma: no cover - worker 不抛
                        row = _empty_row(Path(futs[fut][0]))
                        row["error"] = f"{type(exc).__name__}: {exc}"
                        row["risk_score"] = risk_score(row)
                        row["elapsed_s"] = 0.0
                    rows.append(row)
                    if progress:
                        _progress(i, len(jobs), row, file=sys.stderr)
            except KeyboardInterrupt:
                interrupted = True
                print(f"\n[中断] 已完成 {len(rows)}/{len(jobs)}，正在写出部分报表…",
                      file=sys.stderr, flush=True)
                ex.shutdown(wait=False, cancel_futures=True)

    seconds = round(time.perf_counter() - t0, 2)
    ranked = sort_rows(rows)
    pids = sorted({int(r["worker_pid"]) for r in rows if r.get("worker_pid")})
    rss_vals = [r.get("peak_rss_mb") for r in rows if isinstance(r.get("peak_rss_mb"), (int, float))]
    cache_after = dir_bytes(root)
    used_dirs = {str(r.get("cache_dir")) for r in rows if r.get("cache_dir")}
    used_bytes = sum(dir_bytes(d) for d in used_dirs if d)
    tt = totals(ranked)
    hot = sum(1 for r in rows if r.get("from_cache") is True)
    cold = sum(1 for r in rows if r.get("from_cache") is False)
    ctx: dict[str, Any] = {
        "tool": "apmlog.batch",
        "target": str(tp),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "options": {"out": str(out), "cache": str(cache_dir) if cache_dir else None,
                    "force": bool(force), "recursive": bool(recursive),
                    "exts": list(normalize_exts(exts)), "with_alerts": bool(with_alerts),
                    "requested_workers": max_workers},
        "run": {"workers": workers, "worker_pids": len(pids), "seconds": seconds,
                "files": len(ranked), "ok": tt["ok"], "failed": tt["failed"],
                "hot": hot, "cold": cold,
                "hot_label": f"复用 {hot} / 重建 {cold}" if force is False else f"全部重建 {cold}",
                "median_s": _median([r.get("elapsed_s") for r in rows]),
                "max_s": round(max([r.get("elapsed_s") or 0 for r in rows] or [0]), 2),
                "peak_rss_mb": round(max(rss_vals), 1) if rss_vals else None,
                "interrupted": interrupted},
        "cache": {"root": str(root), "before_bytes": cache_before, "after_bytes": cache_after,
                  "before_gb": round(cache_before / 2**30, 3),
                  "after_gb": round(cache_after / 2**30, 3),
                  "involved_dirs": len(used_dirs), "used_bytes": used_bytes,
                  "used_after_gb": round(used_bytes / 2**30, 3),
                  "estimate_bytes": est["estimate_bytes"], "estimate_gb": round(est["estimate_bytes"] / 2**30, 2),
                  "ratio_measured": round(used_bytes / total_log_bytes, 2) if total_log_bytes else None,
                  "warn_gb": cache_warn_gb},
        "totals": tt,
        "rows": ranked,
    }
    csv_path = write_csv(ranked, out / f"{REPORT_BASENAME}.csv")
    md_path = write_markdown(ctx, out / f"{REPORT_BASENAME}.md", top=top)
    ctx["reports"] = {"csv": str(csv_path), "markdown": str(md_path)}
    if write_json_out:
        json_path = write_json(ranked, ctx, out / f"{REPORT_BASENAME}.json")
        ctx["reports"]["json"] = str(json_path)
    ctx["run"]["report_seconds"] = round(time.perf_counter() - t0 - seconds, 2)
    return ctx


def _progress(done: int, total: int, row: dict[str, Any], file) -> None:
    """进度只写 stderr：``[12/82] 00000013.BIN  OK 2.1s``。"""
    status = row.get("status") or STATUS_FAILED
    extra = "" if status == STATUS_OK else f" {str(row.get('error') or '')[:48]}"
    print(f"[{done}/{total}] {str(row.get('name') or row.get('path')):<34} "
          f"{status}{extra} {row.get('elapsed_s', 0):.1f}s", file=file, flush=True)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="apmlog.batch",
        description="APMLogViewer 目录批量体检：一次扫完整个 .BIN 目录，出风险排序汇总表。")
    ap.add_argument("target", help="日志目录（或单个 .BIN 文件）")
    ap.add_argument("--out", default=None, help="报表输出目录，默认当前工作目录（不得在被扫描目录内）")
    ap.add_argument("--cache", default=None, help="缓存根目录，覆盖内核默认 %LOCALAPPDATA%\\APMLogViewer\\cache")
    ap.add_argument("--max-workers", type=int, default=None,
                    help="并行进程数，默认 min(8, cpu_count)")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新索引（--refresh 同义）")
    ap.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-recursive", action="store_true", help="只扫目录第一层")
    ap.add_argument("--ext", default=",".join(DEFAULT_EXTS), help="逗号分隔扩展名，默认 .bin")
    ap.add_argument("--limit", type=int, default=None, help="最多扫 N 份（调试用）")
    ap.add_argument("--no-alerts", action="store_true", help="跳过告警评估（只看结构字段）")
    ap.add_argument("--json", action="store_true", help="额外写 apmlog_batch_report.json")
    ap.add_argument("--top", type=int, default=10, help="Markdown Top 风险条数，默认 10")
    ap.add_argument("--cache-warn-gb", type=float, default=DEFAULT_CACHE_WARN_GB,
                    help="缓存预估超过该 GB 数时告警，默认 5")
    ap.add_argument("--quiet", action="store_true", help="不打印逐文件进度")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows GBK 控制台：统一强制 UTF-8，管道/重定向时不乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.ERROR,
                        format="%(levelname)s %(name)s: %(message)s")
    force = bool(args.force or args.refresh)
    try:
        ctx = scan_dir(
            args.target, out_dir=args.out, cache_dir=args.cache, force=force,
            max_workers=args.max_workers, recursive=not args.no_recursive,
            exts=normalize_exts([args.ext]), limit=args.limit,
            with_alerts=not args.no_alerts, write_json_out=bool(args.json),
            progress=not args.quiet, cache_warn_gb=args.cache_warn_gb, top=args.top,
            quiet=args.quiet)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断", file=sys.stderr)
        return 130

    run, tt, cache = ctx["run"], ctx["totals"], ctx["cache"]
    out_lines = [
        f"目录: {ctx['target']}",
        f"文件: {tt['files']} 份（成功 {tt['ok']} / 失败 {tt['failed']}），"
        f"{tt['log_bytes'] / 2**20:.0f} MB",
        f"耗时: {run['seconds']} s，worker {run['workers']}（实际进程 {run['worker_pids']}），"
        f"单文件中位 {run['median_s']} s，峰值 RSS {run['peak_rss_mb']} MB",
        f"缓存: 复用 {run['hot']} / 重建 {run['cold']}，根 {cache['root']}，"
        f"本次涉及 {cache['involved_dirs']} 个目录共 {cache['used_after_gb']} GB"
        f"（扫描前估算 {cache['estimate_gb']} GB，实测膨胀率 {cache['ratio_measured']}×）",
        f"风险: 炸机线索 {tt['with_crash']} 份 / critical {tt['alert_critical']} / "
        f"error {tt['alert_error']} / warn {tt['alert_warn']} / hint {tt['alert_hint']} / "
        f"位置 none {tt['no_pos']} 份",
    ]
    worst = sort_rows(ctx["rows"])[:1]
    if worst:
        r = worst[0]
        out_lines.append(f"最该看: {r.get('name')} 风险分 {r.get('risk_score')}"
                         f"（{'；'.join(risk_reasons(r))}）")
    out_lines.append("报表: " + " | ".join(f"{k}={v}" for k, v in ctx["reports"].items()))
    print("\n".join(out_lines))
    if tt["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
