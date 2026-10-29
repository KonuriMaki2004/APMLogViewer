"""一次性诊断脚本：比对 PYZ 里冻结的字节码与磁盘源码编译结果。

用途：构建窗口内如果内核文件被改过（mtime 落在构建开始与产物写出之间），
光看 `-newer exe` 是判不出来的。这个脚本按 dis 输出做规范化摘要，
能直接回答“产物里的这个模块 == 磁盘上的这个模块吗”。

    ./.venv/Scripts/python.exe packaging/_pyi_staleness_check.py
"""
from __future__ import annotations

import contextlib
import dis
import hashlib
import io
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYZ = ROOT / "build" / "pyi" / "apmlogviewer" / "PYZ-00.pyz"

MODULES = [
    "apmlog.app",
    "apmlog.cli",
    "apmlog.core.reader",
    "apmlog.core.alerts",
    "apmlog.core.codebooks",
    "apmlog.ui.seriesplot",
    "apmlog.ui.presets",
    "apmlog.ui.linkpanel",
    "apmlog.ui._tiles",
    "apmlog.link.fakes",
]


def digest(code) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        dis.dis(code, file=buf)
    keep = []
    for ln in buf.getvalue().splitlines():
        s = ln.strip()
        if not s or s.startswith(("File ", "#", "0x")):
            continue
        s = re.sub(r"^>>\s*", "", s)
        s = re.sub(r"^[0-9]+[0-9a-fA-F]*:\s*", "", s)   # 地址前缀
        s = re.sub(r"^\d+\s+", "", s)                    # 行号前缀
        if re.match(r"^(flags|argcount|nlocals|stacksize|firstlineno)\b", s):
            continue
        keep.append(s)
    return hashlib.sha1("\n".join(keep).encode("utf-8")).hexdigest()[:16]


def main() -> int:
    if not PYZ.is_file():
        print(f"找不到 {PYZ}（先跑 packaging/build.py build）")
        return 2
    from PyInstaller.archive.readers import ZlibArchiveReader

    z = ZlibArchiveReader(str(PYZ))
    bad = 0
    for mod in MODULES:
        src = ROOT / (mod.replace(".", "/") + ".py")
        if not src.is_file():
            print(f"{mod:26s} 源码不存在，跳过")
            continue
        try:
            frozen = z.extract(mod)
        except Exception as exc:                                # noqa: BLE001
            print(f"{mod:26s} PYZ 里没有该模块：{exc}")
            bad += 1
            continue
        if not hasattr(frozen, "co_code"):
            print(f"{mod:26s} extract 返回 {type(frozen)}，无法比对")
            bad += 1
            continue
        disk = digest(compile(src.read_text(encoding="utf-8"), str(src), "exec"))
        a, b = digest(frozen), disk
        tag = "一致" if a == b else "不一致 -> 需要重建"
        if a != b:
            bad += 1
        print(f"{mod:26s} 冻结={a} 磁盘={b}  {tag}")
    print("---")
    print("结论：" + ("产物字节码与磁盘源码一致。" if bad == 0 else f"{bad} 个模块不一致。"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
