"""APMLogViewer 免安装版（PyInstaller）唯一入口。

这一个 exe 同时承担三种用法（**console=False**，双击不闪黑框）：

    APMLogViewer.exe                          # 图形界面
    APMLogViewer.exe 0000165.BIN               # 图形界面并直接加载日志
    APMLogViewer.exe --selftest X.BIN --preset vibe --png out.png
                                             # 无头自检（stdout 走附加的父控制台）
    APMLogViewer.exe cli stats X.BIN          # 转调 apmlog.cli 的命令行子命令

窗口程序的 stdout 默认是丢弃的，因此：

* 带任何命令行参数启动时，尽力 ``AttachConsole(ATTACH_PARENT_PROCESS)`` 挂回调用者的
  控制台，并把 ``sys.stdout``/``sys.stderr`` 重开为 UTF-8 的 ``CONOUT$``（同时
  ``SetConsoleOutputCP(65001)``，否则 GBK 控制台会把中文和 ``m/s²`` 显示成乱码）。
  挂不上（例如从资源管理器双击启动）就安静地什么都不做。
* ``APMLOG_LOGFILE=<path>`` 把 stdout/stderr 同时 tee 到文件，便于自动化验收取真实输出。
* ``APMLOG_NO_CONSOLE=1`` 禁止附加控制台；``APMLOG_ATTACH_CONSOLE=1`` 强制附加。

本文件只做入口与 IO 适配，不改任何内核/UI 行为。
"""
from __future__ import annotations

import os
import sys


class _NullWriter:
    """无控制台时的安全输出黑洞（保证 logging.basicConfig 不会因为 stream=None 而炸）。"""

    encoding = "utf-8"
    errors = "strict"

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def write(self, s: str) -> int:            # noqa: D102
        return len(s)

    def writelines(self, seq) -> None:          # noqa: D102
        pass

    def flush(self) -> None:                    # noqa: D102
        pass

    def close(self) -> None:                    # noqa: D102
        pass

    def reconfigure(self, **kwargs) -> None:    # noqa: D102
        pass


class _Tee:
    """把写入同时转给原 stream（可能为 None/NullWriter）和一个 UTF-8 文件。"""

    def __init__(self, stream, fp) -> None:
        self._stream = stream
        self._fp = fp

    encoding = "utf-8"
    errors = "replace"

    def isatty(self) -> bool:
        return bool(self._stream is not None and getattr(self._stream, "isatty", lambda: False)())

    def writable(self) -> bool:
        return True

    @property
    def buffer(self):                            # 少数库会探测 .buffer
        return getattr(self._stream, "buffer", None)

    def reconfigure(self, **kwargs) -> None:
        # 把 app.main 的 utf-8 重配置转发给真实 stream，否则管道/重定向里会出 GBK 乱码
        try:
            if self._stream is not None and hasattr(self._stream, "reconfigure"):
                self._stream.reconfigure(**kwargs)
        except Exception:
            pass

    def write(self, s: str) -> int:
        try:
            if self._stream is not None:
                self._stream.write(s)
        except Exception:
            pass
        try:
            self._fp.write(s)
            self._fp.flush()
        except Exception:
            pass
        return len(s)

    def writelines(self, seq) -> None:
        for line in seq:
            self.write(line if isinstance(line, str) else line.decode("utf-8", "replace"))

    def flush(self) -> None:
        for tgt in (self._stream, self._fp):
            try:
                if tgt is not None:
                    tgt.flush()
            except Exception:
                pass

    def close(self) -> None:
        self.flush()


_KEEP_OPEN: list = []          # 防止 CONOUT$ / tee 文件被 GC 关掉


def _has_real_console() -> bool:
    """当前 stdout 是否已经是可用终端（源码模式或 console=True 构建时为真）。"""
    s = sys.stdout
    if s is None:
        return False
    try:
        return bool(s.isatty())
    except Exception:
        return False


def _attach_parent_console() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.AttachConsole.restype = ctypes.c_int
    except Exception:
        return False
    if not k32.AttachConsole(-1):        # ATTACH_PARENT_PROCESS；失败就放弃，绝不 AllocConsole
        return False
    try:
        k32.SetConsoleOutputCP(65001)    # UTF-8，避免 GBK 控制台吃掉中文与上标
        k32.SetConsoleCP(65001)
        out = open("CONOUT$", "w", encoding="utf-8", errors="replace", newline="")
        err = open("CONOUT$", "w", encoding="utf-8", errors="replace", newline="")
    except Exception:
        return False
    _KEEP_OPEN.extend([out, err])
    sys.stdout, sys.stderr = out, err
    return True


def _force_utf8() -> None:
    """Windows 控制台/重定向默认 GBK，中文与 ``m/s²`` 会炸 —— 先把真 stream 换成 UTF-8。"""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None or isinstance(s, (_NullWriter, _Tee)):
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[attr-defined]
        except Exception:
            pass


def _setup_streams() -> None:
    """入口期的 IO 适配：控制台附加 + 可选 tee 到文件 + 兜底 NullWriter。"""
    frozen = bool(getattr(sys, "frozen", False))
    forced = os.environ.get("APMLOG_ATTACH_CONSOLE") == "1"
    banned = os.environ.get("APMLOG_NO_CONSOLE") == "1"
    wants_output = bool(sys.argv[1:]) or forced
    if frozen and wants_output and not banned and not _has_real_console():
        _attach_parent_console()
    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = sys.stdout if isinstance(sys.stdout, _NullWriter) else _NullWriter()
    _force_utf8()
    log_path = os.environ.get("APMLOG_LOGFILE")
    if log_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
            fp = open(log_path, "w", encoding="utf-8", errors="replace", newline="")
            _KEEP_OPEN.append(fp)
            sys.stdout = _Tee(sys.stdout, fp)
            sys.stderr = _Tee(sys.stderr, fp)
        except Exception as exc:                                  # 日志文件写不了也别影响主流程
            print(f"[run_gui] APMLOG_LOGFILE 不可用: {exc!r}", file=sys.stderr)


def _run_cli() -> int:
    """``APMLogViewer.exe cli <apmlog.cli 的参数...>`` —— 同一份产物跑 CLI。"""
    argv = sys.argv[2:]
    from apmlog.cli import main as cli_main
    return int(cli_main(argv) or 0)


def main() -> int:
    _setup_streams()
    if sys.argv[1:2] == ["cli"]:
        return _run_cli()
    from apmlog.app import main as gui_main
    return int(gui_main() or 0)


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        for s in (sys.stdout, sys.stderr):
            try:
                s.flush()
            except Exception:
                pass
    raise SystemExit(code)
