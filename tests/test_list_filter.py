"""左侧日志列表的筛选：默认只列能打开的 .BIN。

真目录实测一个 Mission Planner 日志根有 1461 个文件、只有 106 个能打开，
全列出来就是刷屏——所以默认必须筛掉不可打开的，但要留一条随时能取消的路。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtCore, QtWidgets                              # noqa: E402

from apmlog.scan import LogFile, ScanResult                        # noqa: E402
from apmlog.ui import settings as st                               # noqa: E402


def qapp() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(app, n: int = 12) -> None:
    for _ in range(n):
        app.processEvents()


def mk_file(name: str, size: int = 1024) -> LogFile:
    ext = "." + name.rsplit(".", 1)[-1].lower()
    return LogFile(path="C:/logs/" + name, name=name, size=size, mtime=0.0,
                   ext=ext, kind="")


def mk_result(names) -> ScanResult:
    from apmlog.scan import kind_of
    files = tuple(mk_file(n) for n in names)
    sup = sum(1 for f in files if f.ext == ".bin")
    kinds: dict[str, int] = {}
    for f in files:
        kinds[kind_of(f.ext)] = kinds.get(kind_of(f.ext), 0) + 1
    return ScanResult(root="C:/logs", files=files,
                      by_kind=tuple(sorted(kinds.items(), key=lambda kv: -kv[1])),
                      total=len(files), supported=sup, dirs_scanned=1,
                      entries_scanned=len(files))


NAMES = [
    "2026-10-24 09-05-29.rlog", "2026-10-24 09-05-29.tlog",
    "2026-10-23 16-27-23.rlog", "2026-10-23 15-06-02.bin",
    "2026-10-21 11-06-02.bin", "2026-10-20 18-18-57.tlog",
    "readme.log", "2026-10-19 08-00-00.BIN",        # 大写扩展名也要算可打开
]


class ListFilterCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="flt_")
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self._tmp) / "settings.json"))
        from apmlog.app import _LogListDock
        self.d = _LogListDock()
        self.d.resize(320, 500)
        self.d.show()
        pump(self.app)

    def tearDown(self) -> None:
        self.d.close()
        pump(self.app)
        st.Settings.set_instance(self._prev)

    def _texts(self, visible_only: bool = True):
        out = []
        for i in range(self.d.list.count()):
            it = self.d.list.item(i)
            if (not it.isHidden()) == visible_only:
                out.append(it.text())
        return out


class TestDefaults(ListFilterCase):
    def test_only_bin_is_on_by_default(self) -> None:
        self.assertTrue(self.d.flt_bin.isChecked(), "默认就该只列能打开的 .BIN")
        self.assertIsInstance(self.d.flt_name, QtWidgets.QLineEdit)
        self.assertTrue(self.d.flt_name.placeholderText())

    def test_fill_hides_unsupported(self) -> None:
        self.d._fill(mk_result(NAMES))
        self.assertEqual(self.d.list.count(), len(NAMES), "条目还是全建，只是隐藏")
        self.assertEqual(self.d.visible_count(), 3, "只有 3 个 .BIN（含大写 .BIN）")
        for t in self._texts(False):
            name = t.split("  ")[0]                # 行文本是“名字  (大小)  暂不支持”
            self.assertTrue(name.endswith((".rlog", ".tlog", ".log")),
                            f"不该隐藏的：{t}")
        self.assertTrue(all(".bin" in t.lower() for t in self._texts(True)))

    def test_summary_reports_both_totals(self) -> None:
        r = mk_result(NAMES)
        self.d._fill(r)
        txt = self.d.lbl_sum.text()
        self.assertIn("共 8 个日志", txt)
        self.assertIn("当前显示 3 个", txt)
        self.assertIn("已筛选掉 5 个", txt)
        self.assertIn("不可打开的", txt)

    def test_summary_is_plain_when_nothing_filtered(self) -> None:
        self.d.flt_bin.setChecked(False)
        self.d._fill(mk_result(NAMES))
        self.assertNotIn("当前显示", self.d.lbl_sum.text())
        self.assertIn("共 8 个日志", self.d.lbl_sum.text())

    def test_current_row_is_a_visible_one(self) -> None:
        """回归：以前固定选第 0 行，而默认筛选下第 0 行是被隐藏的 .rlog。"""
        self.d._fill(mk_result(NAMES))
        cur = self.d.list.currentItem()
        self.assertIsNotNone(cur)
        self.assertFalse(cur.isHidden(), "光标不能落在看不见的条目上")
        self.assertIn(".bin", cur.text().lower())


class TestToggling(ListFilterCase):
    def test_unchecking_shows_everything_and_keeps_greyed(self) -> None:
        self.d._fill(mk_result(NAMES))
        self.d.flt_bin.setChecked(False)
        self.assertEqual(self.d.visible_count(), len(NAMES))
        for i in range(self.d.list.count()):
            it = self.d.list.item(i)
            if ".bin" in it.text().lower():
                continue
            self.assertFalse(it.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled,
                             "不可打开的必须仍然置灰")
            self.assertIsNone(it.data(QtCore.Qt.ItemDataRole.UserRole))

    def test_rechecking_hides_again(self) -> None:
        self.d._fill(mk_result(NAMES))
        self.d.flt_bin.setChecked(False)
        self.d.flt_bin.setChecked(True)
        self.assertEqual(self.d.visible_count(), 3)

    def test_choice_is_persisted_and_reloaded(self) -> None:
        self.d.flt_bin.setChecked(False)
        self.assertFalse(bool(st.Settings.instance().get("list_only_bin")))
        from apmlog.app import _LogListDock
        d2 = _LogListDock()
        d2.show()
        pump(self.app)
        self.assertFalse(d2.flt_bin.isChecked(), "重启后还是用户上次选的样子")
        d2.close()
        pump(self.app)

    def test_default_comes_from_settings_key(self) -> None:
        st.Settings.instance().set("list_only_bin", False)
        from apmlog.app import _LogListDock
        d2 = _LogListDock()
        d2.show()
        pump(self.app)
        self.assertFalse(d2.flt_bin.isChecked())
        d2.close()
        pump(self.app)


class TestNameFilter(ListFilterCase):
    def test_substring_is_case_insensitive(self) -> None:
        self.d.flt_bin.setChecked(False)
        self.d._fill(mk_result(NAMES))
        self.d.flt_name.setText("15-06-02")
        self.assertEqual([t.split("  ")[0] for t in self._texts(True)],
                         ["2026-10-23 15-06-02.bin"])
        self.d.flt_name.setText("READ")
        self.assertEqual([t.split("  ")[0] for t in self._texts(True)], ["readme.log"])

    def test_filters_combine(self) -> None:
        """两个条件是叠加关系：只看 .BIN + 名字含 11-06。"""
        self.d._fill(mk_result(NAMES))
        self.d.flt_name.setText("11-06")
        self.assertEqual([t.split("  ")[0] for t in self._texts(True)],
                         ["2026-10-21 11-06-02.bin"])
        self.assertIn("名字不匹配的", self.d.lbl_sum.text())

    def test_clearing_restores(self) -> None:
        self.d._fill(mk_result(NAMES))
        self.d.flt_name.setText("zzz")
        self.assertEqual(self.d.visible_count(), 0)
        self.d.flt_name.setText("")
        self.assertEqual(self.d.visible_count(), 3)

    def test_whitespace_only_is_treated_as_empty(self) -> None:
        self.d._fill(mk_result(NAMES))
        self.d.flt_name.setText("   ")
        self.assertEqual(self.d.visible_count(), 3)

    def test_no_match_leaves_no_current_item_crash(self) -> None:
        self.d._fill(mk_result(NAMES))
        self.d.flt_name.setText("nothing-matches-this")
        self.assertEqual(self.d.visible_count(), 0)
        self.d.flt_name.setText("")
        self.assertGreater(self.d.visible_count(), 0)


class TestOpenStillWorks(ListFilterCase):
    def test_double_click_on_visible_bin_opens(self) -> None:
        self.d._fill(mk_result(NAMES))
        got = []
        self.d.refresh_requested.connect(got.append)
        it = next(self.d.list.item(i) for i in range(self.d.list.count())
                  if not self.d.list.item(i).isHidden())
        self.d._activated(it)
        self.assertEqual(got, [it.data(QtCore.Qt.ItemDataRole.UserRole)])

    def test_double_click_on_unsupported_does_nothing(self) -> None:
        self.d.flt_bin.setChecked(False)
        self.d._fill(mk_result(NAMES))
        got = []
        self.d.refresh_requested.connect(got.append)
        bad = next(self.d.list.item(i) for i in range(self.d.list.count())
                   if ".rlog" in self.d.list.item(i).text())
        self.d._activated(bad)
        self.assertEqual(got, [])

    def test_set_current_finds_a_filtered_out_file_without_crashing(self) -> None:
        """从"打开日志"对话框选了个被筛掉的旧文件：不能炸，也不该假装选中。"""
        self.d._fill(mk_result(NAMES))
        self.d.set_current("C:/logs/2026-10-24 09-05-29.rlog")
        self.assertEqual(self.d.dir_edit.text(), "C:\\logs")

    def test_empty_list_and_pre_scan_filter_are_safe(self) -> None:
        self.d._apply_filter()
        self.assertEqual(self.d.visible_count(), 0)
        self.d.flt_bin.setChecked(False)
        self.d.flt_bin.setChecked(True)


class TestScanFailurePath(ListFilterCase):
    def test_failed_scan_resets_summary(self) -> None:
        self.d._fill(mk_result(NAMES))
        self.d._fill(None)
        self.assertEqual(self.d.list.count(), 0)
        self.assertIn("扫描失败", self.d.lbl_sum.text())


if __name__ == "__main__":
    unittest.main()
