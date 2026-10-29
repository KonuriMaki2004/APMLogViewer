"""PyInstaller runtime hook：保证内核在 frozen 下能解析到打包进去的数据文件。

内核用源码相对路径找数据（**本 hook 不改内核**，只把文件摆到内核算出来的位置上）：

* ``apmlog/core/alerts.py``    → ``Path(__file__).resolve().parents[2] / "config" / "alerts.yaml"``
* ``apmlog/core/codebooks.py`` → ``Path(__file__).resolve().parents[2] / "data" / "codebooks" / "copter_4x.json"``

``apmlog.core.alerts`` 在 bundle 里的 ``__file__`` 是 ``<bundle_root>/apmlog/core/alerts.*``，
所以 ``parents[2] == <bundle_root>``（onedir 下 ``sys._MEIPASS`` 就是 ``_internal``）。
spec 已把 datas 放到 ``_internal/config`` 与 ``_internal/data/codebooks``，正常情况直接命中。

本 hook 做两件事：

1. **校验**：按内核同一套算法（``parents[2]``）检查两个文件是否可读；
2. **兜底搬运**：没命中时，在整个 bundle 里按文件名搜一遍并复制到内核期望位置
   （只在安装目录内移动，绝不写用户日志目录）。

诊断输出：``APMLOG_PKG_DEBUG=1`` 时把结论写进 ``APMLOG_LOGFILE``（若有）或 stderr。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DATA_FILES = ("config/alerts.yaml", "data/codebooks/copter_4x.json")


def _log(msg: str) -> None:
    if os.environ.get("APMLOG_PKG_DEBUG") != "1":
        return
    line = f"[apmlog-paths] {msg}"
    try:
        err = sys.stderr
        if err is not None:
            err.write(line + "\n")
            err.flush()
    except Exception:
        pass


def _bundle_root() -> Path:
    """onedir → ``_internal``；onefile → 临时解包目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return Path(sys.executable).resolve().parent


def _kernel_roots() -> list[Path]:
    """内核里那两个模块各自的 ``parents[2]``。

    hook 跑在用户代码之前，``apmlog.core.*`` 还没进 ``sys.modules``，所以这里用
    PyInstaller 给 frozen 模块生成 ``__file__`` 的规则推演：
    ``<bundle_root>/apmlog/core/<mod>.py`` → ``parents[2] == bundle_root``。
    如果模块已经被导入过（例如二次执行 hook），就直接采信真实的 ``__file__``。
    """
    roots: list[Path] = []
    for name in ("alerts", "codebooks"):
        m = sys.modules.get(f"apmlog.core.{name}")
        f = getattr(m, "__file__", None) if m is not None else None
        if f:
            try:
                roots.append(Path(f).resolve().parents[2])
                continue
            except Exception:
                pass
        roots.append(_bundle_root())
    return roots


def _find_in_bundle(name: str) -> Path | None:
    """按文件名在 bundle 里搜（限制深度，避免扫全盘）。"""
    rel = Path(name)
    root = _bundle_root()
    cand = root / rel
    if cand.is_file():
        return cand
    for base in (root, root.parent):
        try:
            for p in base.rglob(rel.name):
                if p.is_file():
                    return p
        except Exception:
            continue
    return None


def _ensure(rel: str) -> None:
    src = _find_in_bundle(rel)
    if src is None:
        _log(f"MISSING  bundle 里找不到 {rel}")
        return
    targets = [r / rel for r in _kernel_roots()] or [_bundle_root() / rel]
    targets.append(_bundle_root() / rel)
    for tgt in targets:
        if tgt.is_file():
            _log(f"OK       {rel} -> {tgt}")
            continue
        try:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_bytes(src.read_bytes())
            _log(f"REPAIRED {rel} -> {tgt} (from {src})")
        except Exception as exc:
            _log(f"FAILED   {rel} -> {tgt}: {exc!r}")


if getattr(sys, "frozen", False):
    _log(f"frozen=True _MEIPASS={getattr(sys, '_MEIPASS', None)} exe={sys.executable}")
    for _rel in _DATA_FILES:
        try:
            _ensure(_rel)
        except Exception as _exc:                                  # hook 绝不导致启动失败
            _log(f"hook error for {_rel}: {_exc!r}")
