# -*- coding: utf-8 -*-
"""tests/test_scan.py —— 目录扫描内核 ``apmlog.scan``。

全部用 ``tempfile`` 造树，不依赖真实磁盘、不联网、不读用户日志目录。
守的是那起"选整盘就未响应"的 bug 的四个要点：**有上限、可取消、不跟链接、
错误不外抛**。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_scan.py -q
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apmlog.scan import (EXT_KINDS, OTHER_KIND, SUPPORTED_EXTS, LogFile,  # noqa: E402
                         ScanResult, is_skipped_dirname, kind_of, scan_logs,
                         summarize)


def _touch(path: str, size: int = 1, mtime: float | None = None) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class _Tree(unittest.TestCase):
    """带临时目录树的基类。"""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="apmlog_scan_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def p(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)


# --------------------------------------------------------------------------- #
class TestBasics(_Tree):

    def test_mixed_tree_counts_kinds_and_support(self) -> None:
        _touch(self.p("a.bin"), 10)
        _touch(self.p("b.BIN"), 20)                       # 大写扩展名也要认
        _touch(self.p("sub", "c.tlog"))
        _touch(self.p("sub", "deep", "d.rlog"))
        _touch(self.p("sub", "deep", "notes.txt"))        # 不该进结果
        _touch(self.p("sub", "deep", "noext"))
        r = scan_logs(self.dir)
        self.assertEqual(r.total, 4, "只应收到 4 个已知类型")
        self.assertEqual(r.supported, 2, "可打开的只有两个 .BIN")
        self.assertEqual(r.supported, len([f for f in r.files if f.ext in SUPPORTED_EXTS]))
        self.assertEqual(dict(r.by_kind)["DataFlash 日志"], 2)
        self.assertEqual(dict(r.by_kind)["遥测日志"], 1)
        self.assertNotIn("notes.txt", [f.name for f in r.files])
        self.assertEqual(r.root, os.path.abspath(self.dir))

    def test_size_is_raw_bytes_and_kind_is_chinese(self) -> None:
        _touch(self.p("big.bin"), 4096)
        f = scan_logs(self.dir).files[0]
        self.assertEqual(f.size, 4096, "size 必须是 st_size 原始字节数，不能格式化")
        self.assertEqual(f.kind, "DataFlash 日志")
        self.assertEqual(f.ext, ".bin")
        self.assertTrue(os.path.isabs(f.path))

    def test_sorted_by_mtime_descending(self) -> None:
        _touch(self.p("old.bin"), mtime=1_000)
        _touch(self.p("mid.bin"), mtime=2_000)
        _touch(self.p("new.bin"), mtime=3_000)
        names = [f.name for f in scan_logs(self.dir).files]
        self.assertEqual(names, ["new.bin", "mid.bin", "old.bin"])

    def test_extensions_argument_overrides(self) -> None:
        _touch(self.p("a.bin"))
        _touch(self.p("b.tlog"))
        r = scan_logs(self.dir, extensions=[".tlog"])
        self.assertEqual([f.name for f in r.files], ["b.tlog"])
        self.assertEqual(r.supported, 0)
        r2 = scan_logs(self.dir, extensions=[])          # 空集合退回默认，避免全漏
        self.assertEqual(r2.total, 2)

    def test_empty_dir_returns_empty_not_error(self) -> None:
        r = scan_logs(self.dir)
        self.assertEqual(r.total, 0)
        self.assertFalse(r.cancelled)
        self.assertFalse(r.truncated)
        self.assertEqual(r.errors, 0)


# --------------------------------------------------------------------------- #
class TestCaps(_Tree):

    def _many(self, n: int, folder: str = "") -> None:
        for i in range(n):
            _touch(self.p(folder, f"f{i:03d}.bin") if folder else self.p(f"f{i:03d}.bin"))

    def test_max_files_truncates_but_keeps_results(self) -> None:
        self._many(40)
        r = scan_logs(self.dir, max_files=10)
        self.assertTrue(r.truncated)
        self.assertIn("10", r.truncated_reason)
        self.assertGreater(r.total, 0, "截断也必须把已发现的日志带回来")
        self.assertLessEqual(r.total, 10)
        self.assertFalse(r.cancelled)

    def test_max_dirs_truncates(self) -> None:
        _touch(self.p("root.bin"))
        for i in range(6):
            _touch(self.p(f"d{i}", "x.bin"))
        r = scan_logs(self.dir, max_dirs=2)
        self.assertTrue(r.truncated)
        self.assertIn("2", r.truncated_reason)
        self.assertLessEqual(r.dirs_scanned, 3)
        self.assertGreaterEqual(r.total, 1)

    def test_max_entries_truncates(self) -> None:
        self._many(50)
        r = scan_logs(self.dir, max_entries=12)
        self.assertTrue(r.truncated)
        self.assertIn("12", r.truncated_reason)
        self.assertLessEqual(r.entries_scanned, 12)
        self.assertGreater(r.total, 0)

    def test_default_caps_are_sane_numbers(self) -> None:
        import inspect
        sig = inspect.signature(scan_logs)
        self.assertEqual(sig.parameters["max_files"].default, 5000)
        self.assertEqual(sig.parameters["max_dirs"].default, 20000)
        self.assertEqual(sig.parameters["max_entries"].default, 2_000_000)


# --------------------------------------------------------------------------- #
class TestCancel(_Tree):

    class Once:
        """第 n 次调用起返回 True。"""

        def __init__(self, n: int) -> None:
            self.n, self.calls = n, 0

        def __call__(self) -> bool:
            self.calls += 1
            return self.calls >= self.n

    def test_cancel_during_enumerate_keeps_found_files(self) -> None:
        _touch(self.p("a.bin"))
        for i in range(4):
            _touch(self.p(f"s{i}", "b.bin"))
        r = scan_logs(self.dir, should_cancel=self.Once(2))
        self.assertTrue(r.cancelled)
        self.assertGreater(r.total, 0, "取消后要列出已经扫描到的日志")
        self.assertFalse(r.truncated)

    def test_cancel_during_stat_keeps_stat_files(self) -> None:
        for i in range(100):                              # >32 才能让 stat 阶段中途回调
            _touch(self.p(f"f{i:03d}.bin"))
        # 阶段一只遍历 1 个目录 → 1 次调用；第 2 次调用落在阶段二
        r = scan_logs(self.dir, should_cancel=self.Once(2))
        self.assertTrue(r.cancelled)
        self.assertGreaterEqual(r.total, 32)
        self.assertLess(r.total, 100, "没取消干净")

    def test_cancel_never_raises_and_returns_valid_shape(self) -> None:
        _touch(self.p("a.bin"))
        r = scan_logs(self.dir, should_cancel=lambda: True)
        self.assertIsInstance(r, ScanResult)
        self.assertTrue(r.cancelled)
        self.assertIsInstance(r.files, tuple)
        self.assertEqual(r.total, len(r.files))

    def test_should_cancel_that_raises_is_tolerated(self) -> None:
        _touch(self.p("a.bin"))

        def boom() -> bool:
            raise RuntimeError("UI 没了")

        r = scan_logs(self.dir, should_cancel=boom)
        self.assertEqual(r.total, 1, "取消回调炸掉不该影响扫描")
        self.assertFalse(r.cancelled)


# --------------------------------------------------------------------------- #
class TestLinks(_Tree):

    def test_symlinked_dir_is_not_entered(self) -> None:
        _touch(self.p("real", "a.bin"))
        link = self.p("link")
        try:
            os.symlink(self.p("real"), link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:     # pragma: no cover
            self.skipTest(f"当前 Windows 账户无符号链接权限：{exc}")
        r = scan_logs(self.dir)
        self.assertEqual([f.name for f in r.files], ["a.bin"], "不该透过目录链接重复计数")
        self.assertFalse(r.cancelled)

    def test_symlink_cycle_does_not_hang(self) -> None:
        inner = self.p("real")
        _touch(os.path.join(inner, "a.bin"))
        try:
            os.symlink(inner, os.path.join(inner, "self"), target_is_directory=True)
        except (OSError, NotImplementedError) as exc:     # pragma: no cover
            self.skipTest(f"当前 Windows 账户无符号链接权限：{exc}")
        r = scan_logs(self.dir, max_dirs=1000, max_entries=10000)
        self.assertEqual(r.total, 1)
        self.assertFalse(r.truncated, "防环生效的话不该撞上目录上限")

    def test_visited_dedup_when_two_paths_same_dir(self) -> None:
        """无符号链接权限时也要覆盖 realpath 去重分支：直接测 _dir_key 归一。"""
        from apmlog.scan import _dir_key
        a = _dir_key(self.p("real"))
        b = _dir_key(os.path.join(self.dir, "..", os.path.basename(self.dir), "real"))
        self.assertEqual(a, b, "同一目录的不同写法必须归一到同一个 key")
        self.assertNotEqual(a, _dir_key(self.p("other")))


# --------------------------------------------------------------------------- #
class TestSkipList(_Tree):

    def test_junk_dirs_are_skipped(self) -> None:
        for junk in ("$Recycle.Bin", "System Volume Information", "node_modules",
                     ".git", "__pycache__", "WinSxS"):
            _touch(self.p(junk, "hidden.bin"))
        _touch(self.p("logs", "good.bin"))
        r = scan_logs(self.dir)
        self.assertEqual([f.name for f in r.files], ["good.bin"])

    def test_skip_matching_is_case_insensitive(self) -> None:
        _touch(self.p("NODE_MODULES", "x.bin"))
        _touch(self.p("$RECYCLE.BIN", "y.bin"))
        _touch(self.p("keep", "z.bin"))
        self.assertEqual([f.name for f in scan_logs(self.dir).files], ["z.bin"])

    def test_appdata_is_deliberately_not_skipped(self) -> None:
        """有人把地面站日志放 AppData 下，跳了会扫不到 —— 这是刻意的决定。"""
        _touch(self.p("AppData", "Roaming", "MissionPlanner", "logs", "a.bin"))
        self.assertEqual([f.name for f in scan_logs(self.dir).files], ["a.bin"])

    def test_predicate_table(self) -> None:
        for name in ("$MFT", "$Extend", "system volume information", "Recovery",
                     "DumpStack", "Perflib", "WindowsApps", ".hg", ".svn"):
            self.assertTrue(is_skipped_dirname(name), name)
        for name in ("logs", "AppData", "Documents", "QUADROTOR", "", "  "):
            self.assertFalse(is_skipped_dirname(name), name)


# --------------------------------------------------------------------------- #
class TestErrorTolerance(_Tree):

    def test_permission_error_on_scandir_is_counted_not_raised(self) -> None:
        _touch(self.p("a.bin"))
        with mock.patch("os.scandir", side_effect=PermissionError(13, "denied")):
            r = scan_logs(self.dir)
        self.assertGreaterEqual(r.errors, 1)
        self.assertEqual(r.total, 0)
        self.assertFalse(r.cancelled)

    def test_oserror_on_scandir_is_counted(self) -> None:
        with mock.patch("os.scandir", side_effect=OSError(22, "bad")):
            r = scan_logs(self.dir)
        self.assertGreaterEqual(r.errors, 1)

    def test_file_vanishing_during_stat_is_skipped(self) -> None:
        _touch(self.p("a.bin"))
        _touch(self.p("b.bin"))
        real_stat = os.stat

        def flaky(path, *a, **k):
            if str(path).endswith("b.bin"):
                raise FileNotFoundError(2, "gone")
            return real_stat(path, *a, **k)

        with mock.patch("os.stat", side_effect=flaky):
            r = scan_logs(self.dir)
        self.assertEqual([f.name for f in r.files], ["a.bin"], "读失败的文件要跳过")
        self.assertGreaterEqual(r.errors, 1)

    def test_missing_root_returns_empty_with_reason(self) -> None:
        r = scan_logs(self.p("nope"))
        self.assertEqual(r.total, 0)
        self.assertIn("不存在", r.truncated_reason)
        self.assertFalse(r.cancelled)

    def test_file_as_root_returns_empty_with_reason(self) -> None:
        f = _touch(self.p("a.bin"))
        r = scan_logs(f)
        self.assertEqual(r.total, 0)
        self.assertIn("不是目录", r.truncated_reason)

    def test_progress_callback_that_raises_does_not_break_scan(self) -> None:
        _touch(self.p("a.bin"))

        def boom(*a):
            raise ZeroDivisionError

        r = scan_logs(self.dir, progress=boom)
        self.assertEqual(r.total, 1)

    def test_entry_is_dir_raising_oserror_is_counted(self) -> None:
        """条目属性查询本身失败（网络盘抖动）也不能冒出来。"""
        _touch(self.p("a.bin"))

        class BadEntry:
            name = "weird.bin"
            path = "/tmp/weird.bin"

            def is_dir(self, follow_symlinks=False):
                raise OSError(5, "io")

            def is_symlink(self):
                raise OSError(5, "io")

        class FakeScan:
            def __init__(self, *_a, **_k):
                self.i = iter([BadEntry()])

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return self.i

        with mock.patch("os.scandir", lambda *a, **k: FakeScan()):
            r = scan_logs(self.dir)
        self.assertGreaterEqual(r.errors, 1)
        self.assertIsInstance(r, ScanResult)


# --------------------------------------------------------------------------- #
class TestProgressContract(_Tree):

    def test_two_phase_progress_contract(self) -> None:
        for i in range(40):
            _touch(self.p(f"f{i:02d}.bin"))
        _touch(self.p("sub", "s.bin"))
        ev: list[tuple] = []
        r = scan_logs(self.dir, progress=lambda *a: ev.append(a))
        enum = [e for e in ev if e[0] == "enumerate"]
        stat = [e for e in ev if e[0] == "stat"]
        self.assertTrue(enum, "阶段一必须回调")
        self.assertTrue(all(e[2] is None for e in enum), "阶段一 total 必须是 None")
        self.assertTrue(all(e[1] > 0 for e in enum), "阶段一 done 是已遍历目录数")
        self.assertEqual(stat[0], ("stat", 0, 41, 41), "阶段二要从 0% 开始")
        self.assertEqual(stat[-1], ("stat", 41, 41, 41), "阶段二要以 100% 结束")
        self.assertEqual([e[1] for e in stat], sorted(e[1] for e in stat), "百分比不得倒退")
        self.assertTrue(all(e[3] == 41 for e in stat))
        self.assertEqual(r.total, 41)

    def test_empty_result_still_emits_stat_zero(self) -> None:
        ev: list[tuple] = []
        scan_logs(self.dir, progress=lambda *a: ev.append(a))
        self.assertIn(("stat", 0, 0, 0), ev, "没有候选也要让 UI 能收尾")

    def test_found_counts_monotonic_in_enumerate(self) -> None:
        for i in range(5):
            _touch(self.p(f"d{i}", "x.bin"))
        ev: list[tuple] = []
        scan_logs(self.dir, progress=lambda *a: ev.append(a))
        found = [e[3] for e in ev if e[0] == "enumerate"]
        self.assertEqual(found, sorted(found), "已发现数不得倒退")


# --------------------------------------------------------------------------- #
class TestSummarize(_Tree):

    def test_summary_contains_totals_and_kinds(self) -> None:
        for i in range(3):
            _touch(self.p(f"a{i}.bin"))
        _touch(self.p("b.tlog"))
        _touch(self.p("c.rlog"))
        text = summarize(scan_logs(self.dir))
        self.assertIn("共 5 个日志", text)
        self.assertIn("DataFlash 日志 3", text)
        self.assertIn("遥测日志 1", text)
        self.assertIn("可打开 3 个", text)

    def test_summary_marks_cancel_and_truncation_and_errors(self) -> None:
        for i in range(20):
            _touch(self.p(f"a{i}.bin"))
        self.assertTrue(summarize(scan_logs(self.dir, max_files=5)).startswith("共"))
        trunc = scan_logs(self.dir, max_files=5)
        self.assertIn("结果不完整", summarize(trunc))
        self.assertIn("上限", summarize(trunc))
        cancelled = scan_logs(self.dir, should_cancel=lambda: True)
        self.assertTrue(summarize(cancelled).startswith("已取消："))
        with mock.patch("os.scandir", side_effect=PermissionError(13, "denied")):
            denied = scan_logs(self.dir)
        self.assertIn("无权限", summarize(denied))

    def test_summary_of_unusable_root_is_the_reason(self) -> None:
        text = summarize(scan_logs(self.p("nope")))
        self.assertIn("不存在", text)


# --------------------------------------------------------------------------- #
class TestKindOf(unittest.TestCase):

    def test_known_and_unknown(self) -> None:
        self.assertEqual(kind_of(".bin"), "DataFlash 日志")
        self.assertEqual(kind_of("BIN"), "DataFlash 日志")
        self.assertEqual(kind_of("x.tlog"), "遥测日志")
        self.assertEqual(kind_of("x.rlog"), "旧版遥测日志")
        self.assertEqual(kind_of("x.log"), "文本日志")
        self.assertEqual(kind_of("x.dat"), "数据文件")
        self.assertEqual(kind_of(".xyz"), OTHER_KIND)
        self.assertEqual(kind_of("noext"), OTHER_KIND)
        self.assertEqual(kind_of(""), OTHER_KIND)
        self.assertEqual(kind_of("v1.2/dir/log.tlog"), "遥测日志")

    def test_every_declared_ext_round_trips(self) -> None:
        for ext, name in EXT_KINDS.items():
            self.assertEqual(kind_of(ext), name)
            self.assertEqual(kind_of("file" + ext), name)

    def test_supported_exts_are_a_subset_of_kinds(self) -> None:
        for ext in SUPPORTED_EXTS:
            self.assertIn(ext, EXT_KINDS)


class TestDataclasses(unittest.TestCase):

    def test_logfile_is_frozen(self) -> None:
        f = LogFile("/x", "x.bin", 1, 2.0, ".bin", "DataFlash 日志")
        with self.assertRaises(Exception):
            f.size = 5                                  # type: ignore[misc]

    def test_result_defaults(self) -> None:
        r = ScanResult.empty("/x", "因为某些原因")
        self.assertEqual(r.total, 0)
        self.assertEqual(r.files, ())
        self.assertEqual(r.truncated_reason, "因为某些原因")
        self.assertFalse(r.truncated)
        self.assertEqual(r.by_kind, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
