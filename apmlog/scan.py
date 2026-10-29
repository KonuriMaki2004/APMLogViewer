# -*- coding: utf-8 -*-
"""apmlog.scan —— 日志目录扫描内核（**零 Qt 依赖、可单测**）。

被 ``apmlog/ui/scandialog.py`` 的 worker 线程调用，因此本模块必须满足：

* **可取消**：``should_cancel()`` 返回 True 时立刻收尾，把**已经拿到的**结果组装返回
  （``cancelled=True``），绝不抛异常、绝不返回空壳；
* **有上限**：``max_files`` / ``max_dirs`` / ``max_entries`` 任一命中即停止，
  ``truncated=True`` 并说明撞了哪个上限。用户把整个磁盘选成目录时，靠这条兜住；
* **绝不跟随链接**：目录符号链接与 Windows junction 一律不进入，防止
  ``C:\\Users\\Default`` 那类别名造成重复/无限遍历；
* **错误容忍**：单个目录没权限、单个文件读一半被删，都只累加 ``errors``，
  任何异常都不允许冒出 `` scan_logs``。

历史 bug：原来 UI 里一行 ``d.rglob("*")`` + 每个文件 ``f.stat()`` 全在 UI 线程同步跑，
选整盘直接"未响应"。所以这里刻意用 ``os.scandir`` 手写栈式遍历：
``Path.rglob`` 会对每个条目 stat、且无法中途取消。

两阶段设计是进度条的依据：阶段一只按名字挑候选（不 stat，总量未知 → 忙碌条 + 计数），
阶段二对候选逐个 stat（总量已知 → 真实百分比）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

__all__ = [
    "SUPPORTED_EXTS",
    "EXT_KINDS",
    "OTHER_KIND",
    "SKIP_DIRNAMES",
    "LogFile",
    "ScanResult",
    "kind_of",
    "is_skipped_dirname",
    "scan_logs",
    "summarize",
]

#: v1 唯一能打开的格式（见 docs/CONTRACT.md）。
SUPPORTED_EXTS: tuple[str, ...] = (".bin",)

#: 扩展名（小写、含点）→ 中文类型名。用于"不同类型日志总数"的归类。
EXT_KINDS: dict[str, str] = {
    ".bin": "DataFlash 日志",
    ".tlog": "遥测日志",
    ".rlog": "旧版遥测日志",
    ".log": "文本日志",
    ".dat": "数据文件",
    ".dflog": "DataFlash 日志",
    ".apj": "APM Planner 工程",
    ".par": "参数文件",
    ".parm": "参数文件",
}

#: 命中扩展名但不在 ``EXT_KINDS`` 里的，归这一类。
OTHER_KIND = "其他日志"

#: 明确无价值的目录名（小写比较）。只跳"确定不会有用户日志"的系统/工具目录：
#: ``$`` 前缀是 NTFS 元数据与回收站；``System Volume Information`` 是卷影副本；
#: ``WinSxS``/``WindowsApps``/``Recovery``/``DumpStack``/``Perflib`` 是系统组件仓库；
#: ``node_modules``/``__pycache__``/``.git``/``.hg``/``.svn`` 是开发工具产物。
#: **故意不跳 AppData**：有人把地面站日志放在 ``AppData`` 下，跳了会扫不到。
SKIP_DIRNAMES: frozenset[str] = frozenset({
    "system volume information",
    "windowsapps",
    "winsxs",
    "recovery",
    "dumpstack",
    "perflib",
    "node_modules",
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
})

_PROGRESS = Callable[[str, int, Optional[int], int], None]
_CANCEL = Callable[[], bool]

#: 阶段二每处理多少个候选回调一次进度（太小会让 UI 刷得太频繁）。
_STAT_CHUNK = 32
#: 阶段一每处理多少个条目检查一次取消（大目录里不至于卡太久）。
_CANCEL_STRIDE = 512


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LogFile:
    """一个候选日志。``size`` 是 ``st_size`` 原始字节数，**不在此处格式化**。"""

    path: str
    name: str
    size: int
    mtime: float
    ext: str
    kind: str


@dataclass(frozen=True)
class ScanResult:
    root: str
    files: tuple[LogFile, ...] = ()
    by_kind: tuple[tuple[str, int], ...] = ()
    total: int = 0
    supported: int = 0
    dirs_scanned: int = 0
    entries_scanned: int = 0
    errors: int = 0
    cancelled: bool = False
    truncated: bool = False
    truncated_reason: str = ""
    elapsed: float = 0.0

    @staticmethod
    def empty(root: str, reason: str = "") -> "ScanResult":
        return ScanResult(root=root, truncated_reason=reason)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _norm_ext(value: str) -> str:
    """``.BIN`` / ``BIN`` / ``a.tlog`` / ``x.y.tar.gz`` → ``.bin`` / ``.tlog`` / ``.gz``。

    先取 basename，避免把路径里的点（``v1.2/log.tlog``）当成扩展名。
    """
    v = os.path.basename(str(value or "").strip().lower())
    if not v:
        return ""
    if "." not in v:
        return "." + v
    return v[v.rindex("."):]


def kind_of(ext_or_name: str) -> str:
    """扩展名或文件名 → 中文类型名；未知扩展名归 ``其他日志``。"""
    ext = _norm_ext(ext_or_name)
    if not ext:
        return OTHER_KIND
    return EXT_KINDS.get(ext, OTHER_KIND)


def is_skipped_dirname(name: str) -> bool:
    """该目录名是否应跳过（大小写不敏感）。"""
    n = str(name or "").strip().lower()
    if not n:
        return False
    if n.startswith("$"):            # $Recycle.Bin / $MFT / $Extend / $Secure…
        return True
    return n in SKIP_DIRNAMES


def _dir_key(path: str) -> str:
    """防环用的目录身份。Windows 上 st_ino 常为 0，所以用 realpath + 大小写归一。"""
    try:
        real = os.path.realpath(path)
    except OSError:
        real = path
    return os.path.normcase(os.path.abspath(real))


def _emit(progress: Optional[_PROGRESS], phase: str, done: int,
          total: Optional[int], found: int) -> None:
    """回调进度。**回调里抛异常不能影响扫描**，一律吞掉。"""
    if progress is None:
        return
    try:
        progress(phase, done, total, found)
    except Exception:                 # noqa: BLE001 - UI 刷新失败不该毁掉一次扫描
        pass


def _cancelled(should_cancel: Optional[_CANCEL]) -> bool:
    if should_cancel is None:
        return False
    try:
        return bool(should_cancel())
    except Exception:                 # noqa: BLE001
        return False


def _wanted(extensions: Optional[Iterable[str]]) -> frozenset[str]:
    if extensions is None:
        return frozenset(EXT_KINDS)
    exts = {e for e in (_norm_ext(x) for x in extensions) if e}
    return frozenset(exts) if exts else frozenset(EXT_KINDS)


# --------------------------------------------------------------------------- #
# 阶段一：只遍历目录树、按名字挑候选
# --------------------------------------------------------------------------- #

def _enumerate(root: str, want: frozenset[str], *, max_files: int, max_dirs: int,
               max_entries: int, progress: Optional[_PROGRESS],
               should_cancel: Optional[_CANCEL]) -> tuple[list[tuple[str, str, str]],
                                                          int, int, int, bool, bool, str]:
    """返回 (候选[(path,name,ext)], dirs, entries, errors, cancelled, truncated, reason)。"""
    candidates: list[tuple[str, str, str]] = []
    stack: list[str] = [root]
    visited: set[str] = {_dir_key(root)}
    dirs = entries = errors = 0
    cancelled = truncated = False
    reason = ""

    while stack:
        current = stack.pop()
        if dirs >= max_dirs:
            truncated, reason = True, f"已遍历目录数达上限 {max_dirs} 个"
            break
        dirs += 1
        stop = False
        try:
            it = os.scandir(current)
        except OSError:               # PermissionError / 目录被删 / 网络盘掉了
            errors += 1
            it = None
        if it is not None:
            try:
                with it as scan:
                    for entry in scan:
                        if entries >= max_entries:
                            truncated, reason, stop = True, \
                                f"已遍历条目数达上限 {max_entries} 个", True
                            break
                        entries += 1
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            if is_dir:
                                # 真目录才进；目录符号链接/junction 的 follow=False
                                # 在 CPython 各版本表现不一致，所以下面再兜一道。
                                if entry.is_symlink():
                                    continue
                                if is_skipped_dirname(entry.name):
                                    continue
                                key = _dir_key(entry.path)
                                if key in visited:
                                    continue
                                visited.add(key)
                                stack.append(entry.path)
                                continue
                            if entry.is_dir(follow_symlinks=True):
                                # 只有链接指向目录时才会走到这里：一律不进入。
                                continue
                            ext = _norm_ext(os.path.splitext(entry.name)[1])
                            if ext in want:
                                candidates.append((entry.path, entry.name, ext))
                                if len(candidates) >= max_files:
                                    truncated, reason, stop = True, \
                                        f"候选日志已达 {max_files} 个上限", True
                                    break
                        except OSError:
                            errors += 1
                        # 单个目录可能有几十万个条目：内层也要能中途取消
                        if entries % _CANCEL_STRIDE == 0 and _cancelled(should_cancel):
                            cancelled, stop = True, True
                            break
            except OSError:
                errors += 1
            if stop:
                break
        _emit(progress, "enumerate", dirs, None, len(candidates))
        if _cancelled(should_cancel):
            cancelled = True
            break
    return candidates, dirs, entries, errors, cancelled, truncated, reason


# --------------------------------------------------------------------------- #
# 阶段二：对候选逐个 stat（这一步总量已知 → 真实百分比）
# --------------------------------------------------------------------------- #

def _stat_all(candidates: Sequence[tuple[str, str, str]], *, progress: Optional[_PROGRESS],
              should_cancel: Optional[_CANCEL]
              ) -> tuple[list[LogFile], int, bool]:
    n = len(candidates)
    out: list[LogFile] = []
    errors = 0
    _emit(progress, "stat", 0, n, n)
    for i, (path, name, ext) in enumerate(candidates):
        try:
            st = os.stat(path)
        except OSError:               # 扫描期间文件被删/移动/无权限
            errors += 1
            st = None
        if st is not None:
            out.append(LogFile(path=path, name=name, size=int(st.st_size),
                               mtime=float(st.st_mtime), ext=ext,
                               kind=EXT_KINDS.get(ext, OTHER_KIND)))
        if (i + 1) % _STAT_CHUNK == 0 or i + 1 == n:
            _emit(progress, "stat", i + 1, n, n)
            if _cancelled(should_cancel):
                return out, errors, True
    return out, errors, False


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #

def scan_logs(root, *, progress: Optional[_PROGRESS] = None,
              should_cancel: Optional[_CANCEL] = None,
              max_files: int = 5000, max_dirs: int = 20000,
              max_entries: int = 2_000_000,
              extensions: Optional[Iterable[str]] = None) -> ScanResult:
    """扫描 ``root`` 下的日志文件。永不抛异常，结果里带状态标记。"""
    t0 = time.time()
    root_s = os.path.abspath(str(root or ""))
    if not root_s or not os.path.isdir(root_s):
        return ScanResult.empty(root_s or str(root or ""),
                                f"目录不存在或不是目录：{root_s or '(空)'}")

    want = _wanted(extensions)
    cands, dirs, entries, enum_err, cancelled, truncated, reason = _enumerate(
        root_s, want, max_files=max(1, int(max_files)), max_dirs=max(1, int(max_dirs)),
        max_entries=max(1, int(max_entries)), progress=progress,
        should_cancel=should_cancel)

    files, stat_err, stat_cancelled = _stat_all(cands, progress=progress,
                                                should_cancel=should_cancel)
    errors = enum_err + stat_err
    if stat_cancelled:
        cancelled = True
    files.sort(key=lambda f: f.mtime, reverse=True)

    counts: dict[str, int] = {}
    for f in files:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    by_kind = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    supported = sum(1 for f in files if f.ext in SUPPORTED_EXTS)

    return ScanResult(
        root=root_s, files=tuple(files), by_kind=by_kind, total=len(files),
        supported=supported, dirs_scanned=dirs, entries_scanned=entries,
        errors=errors, cancelled=cancelled, truncated=truncated,
        truncated_reason=reason, elapsed=time.time() - t0)


def summarize(res: ScanResult) -> str:
    """一句话中文汇总，UI 直接显示。"""
    parts: list[str] = []
    if res.cancelled:
        parts.append("已取消：")
    if not res.files and res.truncated_reason and res.total == 0 and res.entries_scanned == 0:
        return res.truncated_reason
    parts.append(f"共 {res.total} 个日志")
    if res.by_kind:
        parts.append("（" + " · ".join(f"{k} {v}" for k, v in res.by_kind) + "）")
    parts.append(f"，可打开 {res.supported} 个")
    if res.errors:
        parts.append(f"，跳过 {res.errors} 个无权限项")
    if res.truncated:
        parts.append(f"（结果不完整：{res.truncated_reason}）")
    return "".join(parts)
