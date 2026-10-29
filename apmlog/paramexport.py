"""参数导出：把日志 `PARM` 块写成 Mission Planner / QGC 能读的 `.param`，
或便于人看/存档的 `.csv`。

**本模块刻意不依赖 Qt**：导出内容是要被单测逐字节检查的东西，混进控件里就没法测了。
UI 侧的对话框在 `apmlog/app.py`。

数据来源与诚实性（写进文件头，别让人误以为这是从飞控实时读的）：

* 记录值和出厂默认值**都来自日志自己的 `PARM` 报文**（`Value` / `Default` 两列）。
  `Default` 是 ArduPilot 较新固件才写的；老日志这一列缺失，那部分参数**无法判断
  是否与默认不同**，一律按"未改动"处理，不会混进 diff 导出。
* 日志里的值是**那次飞行时**的参数，不等于飞控现在的参数（落地后可能又改过）。
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

#: 判定"与默认相同"的容差。表格与导出必须共用这一个值，
#: 否则会出现"表格里显示相同、却被导进差异文件"这种自相矛盾。
TOL = 1e-9

SCOPE_ALL = "all"          # 完整参数
SCOPE_DIFF = "diff"        # 只导与出厂默认不同的
SCOPE_FILTERED = "filtered"  # 只导当前筛选结果

FMT_PARAM = "param"        # NAME,VALUE（Mission Planner / QGC 文本格式）
FMT_CSV = "csv"            # 带默认值与差异列，Excel 直接开

SCOPE_CN = {SCOPE_ALL: "完整参数", SCOPE_DIFF: "仅与出厂默认不同", SCOPE_FILTERED: "当前筛选结果"}
FMT_EXT = {FMT_PARAM: "param", FMT_CSV: "csv"}


@dataclass(frozen=True)
class ParamRow:
    """一个参数的记录值与出厂默认（默认缺失时 ``default is None``）。"""

    name: str
    value: float
    default: Optional[float] = None

    @property
    def value_ok(self) -> bool:
        return isinstance(self.value, (int, float)) and math.isfinite(float(self.value))

    @property
    def has_default(self) -> bool:
        return (self.default is not None and isinstance(self.default, (int, float))
                and math.isfinite(float(self.default)))

    @property
    def changed(self) -> bool:
        """与出厂默认不同。缺默认值或值非有限 → 判为"未改动"（保守，不吓人）。"""
        if not (self.value_ok and self.has_default):
            return False
        return abs(float(self.value) - float(self.default)) >= TOL

    @property
    def diff(self) -> Optional[float]:
        if not (self.value_ok and self.has_default):
            return None
        return float(self.value) - float(self.default)


def rows_from_mapping(params: dict[str, tuple[float, Optional[float]]]) -> list[ParamRow]:
    """`LogStore.params()` → 按参数名排序的行。排序保证导出可 diff、可复现。"""
    out: list[ParamRow] = []
    for name in sorted(params):
        val, dfv = params[name]
        out.append(ParamRow(str(name), _as_float(val), _as_float(dfv)))
    return out


def _as_float(x: object) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)                                  # type: ignore[arg-type]
    return v if math.isfinite(v) else None


def select(rows: Sequence[ParamRow], scope: str = SCOPE_ALL,
           visible_names: Optional[Iterable[str]] = None) -> list[ParamRow]:
    """按范围取行。``filtered`` 用调用方给的名单（表格才知道当前筛选是什么）。"""
    if scope == SCOPE_ALL:
        return list(rows)
    if scope == SCOPE_DIFF:
        return [r for r in rows if r.changed]
    if scope == SCOPE_FILTERED:
        want = {str(n) for n in (visible_names or [])}
        return [r for r in rows if r.name in want]
    raise ValueError(f"未知的导出范围：{scope!r}")


def counts(rows: Sequence[ParamRow]) -> dict[str, int]:
    """给对话框显示“将导出 N 条”用的统计，把“判不了”的两类分开算。

    ``same`` 只算**真的判定为与默认相同**的；缺默认值、或记录值本身不是有限数的，
    归进 ``no_default`` / ``no_value``，不假装它们“没改动”。
    """
    return {
        "total": len(rows),
        "changed": sum(1 for r in rows if r.changed),
        "same": sum(1 for r in rows if r.has_default and r.value_ok and not r.changed),
        "no_default": sum(1 for r in rows if not r.has_default),
        "no_value": sum(1 for r in rows if not r.value_ok),
    }


def fmt_value(v: Optional[float]) -> str:
    """`.param` 里的数值写法：整数就写整数，其余 6 位小数。

    不用 ``%g``：那会把 1376550 写成 ``1.37655e+06``，MP/QGC 的解析器不认科学计数法。
    """
    if v is None or not math.isfinite(float(v)):
        return ""
    f = float(v)
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return f"{f:.6f}"


def header_lines(meta: Optional[dict] = None, scope_text: str = "",
                 n_export: int = 0, n_total: int = 0,
                 no_default: int = 0) -> list[str]:
    """两种格式共用的注释头（`.param` 用 ``#``，CSV 也照写，MP/Excel 都能忽略）。"""
    m = meta or {}
    out = ["# APMLogViewer 参数导出",
           f"# 来源日志：{m.get('log_name', '未知')}",
           f"# 固件：{m.get('firmware', '未知')}　机型：{m.get('frame', '未知')}"]
    if m.get("duration"):
        out.append(f"# 飞行时长：{m['duration']}")
    out.append(f"# 导出范围：{scope_text or '完整参数'}（{n_export} / {n_total} 条）")
    out.append("# 数值与出厂默认均取自日志自身的 PARM 块（Default 列需较新固件才写），"
               "不是从飞控实时读取的")
    if no_default:
        out.append(f"# 其中 {no_default} 条日志未记录出厂默认，无法判断是否相同，"
                   "已按未改动处理（不会进“仅差异”导出）")
    out.append("# 提醒：这是那次飞行时的参数快照，落地后可能又改过；"
               "仅用于对比与存档，不要盲目批量写回飞控")
    return out


def to_param_text(rows: Sequence[ParamRow], header: Sequence[str]) -> str:
    """Mission Planner / QGC 文本格式：每行 ``NAME,VALUE``，注释行以 ``#`` 开头。"""
    lines = list(header)
    lines.append("")
    for r in rows:
        if not r.value_ok:
            continue                    # 非有限值不写：写进去会让对方解析失败
        lines.append(f"{r.name},{fmt_value(r.value)}")
    lines.append("")
    return "\n".join(lines)


def to_csv_text(rows: Sequence[ParamRow], header: Sequence[str]) -> str:
    """四列表格（与界面同列），带 BOM 让 Excel 直接双击能开。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    for line in header:
        w.writerow([line])
    w.writerow(["参数名", "记录值", "出厂默认", "与默认差异"])
    for r in rows:
        d = r.diff
        w.writerow([r.name, fmt_value(r.value),
                    fmt_value(r.default) if r.has_default else "",
                    "" if d is None else ("相同" if abs(d) < TOL else f"{d:+g}")])
    return buf.getvalue()


def write_export(path: str | Path, rows: Sequence[ParamRow], fmt: str,
                 header: Sequence[str]) -> int:
    """落盘并返回真正写出的参数条数。

    CSV 走 ``utf-8-sig`` + ``newline=''``（与界面里既有的表格导出一致）；
    `.param` 用纯 UTF-8 + LF，MP/QGC 读起来最稳。
    """
    p = Path(path)
    if fmt == FMT_CSV:
        text = to_csv_text(rows, header)
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            fh.write(text)
    elif fmt == FMT_PARAM:
        text = to_param_text(rows, header)
        with open(p, "w", newline="\n", encoding="utf-8") as fh:
            fh.write(text)
    else:
        raise ValueError(f"未知的导出格式：{fmt!r}")
    return sum(1 for r in rows if r.value_ok)


def suggested_name(log_name: str, scope: str, fmt: str) -> str:
    """按日志名 + 范围给个不重名的建议文件名。"""
    stem = Path(str(log_name or "params")).stem
    tag = {SCOPE_ALL: "all", SCOPE_DIFF: "diff", SCOPE_FILTERED: "filtered"}.get(scope, "all")
    return f"{stem}_params_{tag}.{FMT_EXT.get(fmt, fmt)}"
