"""参数导出：纯逻辑（apmlog/paramexport.py）+ 界面接线（apmlog/app.py）。

纯逻辑部分刻意不依赖 Qt，这里逐字节检查文本；UI 部分只验接线，
所有模态框与文件对话框都被替换掉，测试不会弹任何东西。
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets                                        # noqa: E402

from apmlog import paramexport as pe                                 # noqa: E402
from apmlog.paramexport import ParamRow, fmt_value                   # noqa: E402


def qapp() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(app, n: int = 20) -> None:
    for _ in range(n):
        app.processEvents()


ROWS = [
    ParamRow("ARMING_CHECK", 1.0, 1.0),          # 与默认相同
    ParamRow("ATC_RAT_RLL_P", 0.145, 0.135),     # 不同
    ParamRow("BARO1_DEVID", 1376550.0, 0.0),     # 不同（大整数）
    ParamRow("BATT_AMP_PERVLT", 24.0, None),     # 老日志没写 Default
    ParamRow("FS_CRASH_CHECK", 1.0, 1.0),
    ParamRow("INS_ACCOFFS_X", float("nan"), 0.0),  # 值非有限
]


class TestValueFormat(unittest.TestCase):
    def test_integers_have_no_decimals(self) -> None:
        self.assertEqual(fmt_value(1.0), "1")
        self.assertEqual(fmt_value(-1.0), "-1")
        self.assertEqual(fmt_value(1376550.0), "1376550")
        self.assertEqual(fmt_value(0.0), "0")

    def test_floats_use_six_decimals(self) -> None:
        self.assertEqual(fmt_value(0.145), "0.145000")
        self.assertEqual(fmt_value(0.0065), "0.006500")
        self.assertEqual(fmt_value(-190.5), "-190.500000")

    def test_never_scientific_notation(self) -> None:
        """%g 会写成 1.37655e+06，MP/QGC 的 .param 解析器不认科学计数法。"""
        for v in (1376550.0, 1e-7, 1.23e12, 0.0000005):
            self.assertNotIn("e", fmt_value(v).lower(), f"{v} 被写成了科学计数法")

    def test_non_finite_is_empty(self) -> None:
        self.assertEqual(fmt_value(float("nan")), "")
        self.assertEqual(fmt_value(float("inf")), "")
        self.assertEqual(fmt_value(None), "")


class TestChangedSemantics(unittest.TestCase):
    def test_missing_default_is_not_changed(self) -> None:
        """没写 Default 就判不了，保守当作未改动，不许混进差异导出。"""
        r = ParamRow("X", 24.0, None)
        self.assertFalse(r.has_default)
        self.assertFalse(r.changed)

    def test_within_tolerance_is_same(self) -> None:
        self.assertFalse(ParamRow("X", 1.0, 1.0 + pe.TOL / 2).changed)
        self.assertTrue(ParamRow("X", 1.0, 1.0 + pe.TOL * 10).changed)

    def test_non_finite_value_is_not_changed(self) -> None:
        self.assertFalse(ParamRow("X", float("nan"), 1.0).changed)

    def test_diff_value(self) -> None:
        self.assertAlmostEqual(ParamRow("X", 800.0, 1100.0).diff, -300.0)
        self.assertIsNone(ParamRow("X", 1.0, None).diff)


class TestSelectAndCounts(unittest.TestCase):
    def test_scopes(self) -> None:
        self.assertEqual(len(pe.select(ROWS, pe.SCOPE_ALL)), 6)
        self.assertEqual([r.name for r in pe.select(ROWS, pe.SCOPE_DIFF)],
                         ["ATC_RAT_RLL_P", "BARO1_DEVID"])
        got = pe.select(ROWS, pe.SCOPE_FILTERED, ["FS_CRASH_CHECK", "ATC_RAT_RLL_P"])
        self.assertEqual([r.name for r in got], ["ATC_RAT_RLL_P", "FS_CRASH_CHECK"])

    def test_unknown_scope_raises_instead_of_exporting_everything(self) -> None:
        with self.assertRaises(ValueError):
            pe.select(ROWS, "everything")

    def test_counts(self) -> None:
        c = pe.counts(ROWS)
        self.assertEqual(c["total"], 6)
        self.assertEqual(c["changed"], 2)
        self.assertEqual(c["same"], 2)              # 只算真判定为相同的
        self.assertEqual(c["no_default"], 1)        # BATT 没写 Default
        self.assertEqual(c["no_value"], 1)          # INS 记录值是 NaN
        # 不重不漏：每行恰好属于“改了 / 相同 / 缺默认 / 有默认但值坏了”四类之一
        bad_with_default = sum(1 for r in ROWS if r.has_default and not r.value_ok)
        self.assertEqual(c["changed"] + c["same"] + c["no_default"] + bad_with_default,
                         c["total"], "四类分类必须不重不漏")

    def test_rows_from_mapping_is_sorted_and_typed(self) -> None:
        rows = pe.rows_from_mapping({"B": (2.0, 1.0), "A": (1.0, None), "C": (3.0, 3.0)})
        self.assertEqual([r.name for r in rows], ["A", "B", "C"], "排序保证导出可复现")
        self.assertEqual(rows[1].default, 1.0)
        self.assertIsNone(rows[0].default)

    def test_rows_from_mapping_survives_junk(self) -> None:
        rows = pe.rows_from_mapping({"X": (float("nan"), float("nan")), "Y": ("s", None)})
        self.assertIsNone(rows[0].value)                # 非有限 → None
        self.assertIsNone(rows[1].value)                # 非数值 → None


class TestParamText(unittest.TestCase):
    def test_body_is_name_comma_value(self) -> None:
        txt = pe.to_param_text(pe.select(ROWS, pe.SCOPE_ALL), ["# h"])
        body = [ln for ln in txt.splitlines() if ln and not ln.startswith("#")]
        self.assertIn("ATC_RAT_RLL_P,0.145000", body)
        self.assertIn("BARO1_DEVID,1376550", body)
        self.assertIn("ARMING_CHECK,1", body)

    def test_non_finite_rows_are_dropped(self) -> None:
        """写 NaN 进 .param 会让对方解析失败，宁可少一条。"""
        txt = pe.to_param_text(ROWS, [])
        self.assertNotIn("INS_ACCOFFS_X", txt)

    def test_mission_planner_style_parser_accepts_it(self) -> None:
        """按 MP/QGC 的读法模拟一遍：跳过 # 行、逗号切、float() 能解析。"""
        txt = pe.to_param_text(pe.select(ROWS, pe.SCOPE_ALL), pe.header_lines())
        got = {}
        for ln in txt.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            k, _, v = ln.partition(",")
            got[k.strip()] = float(v)
        self.assertEqual(got["ATC_RAT_RLL_P"], 0.145)
        self.assertEqual(got["BARO1_DEVID"], 1376550.0)
        self.assertEqual(len(got), 5)

    def test_header_states_provenance_and_scope(self) -> None:
        h = "\n".join(pe.header_lines({"log_name": "00000165.BIN", "firmware": "V4.7.0",
                                       "frame": "QUAD/H"},
                                      "仅与出厂默认不同", 2, 6, no_default=2))
        self.assertIn("00000165.BIN", h)
        self.assertIn("V4.7.0", h)
        self.assertIn("仅与出厂默认不同（2 / 6 条）", h)
        self.assertIn("不是从飞控实时读取", h)
        self.assertIn("2 条日志未记录出厂默认", h)
        self.assertTrue(all(ln.startswith("#") for ln in h.splitlines()))

    def test_csv_has_four_columns_and_diff(self) -> None:
        txt = pe.to_csv_text(pe.select(ROWS, pe.SCOPE_ALL), ["# h"])
        lines = [ln for ln in txt.splitlines() if ln]
        self.assertIn("参数名,记录值,出厂默认,与默认差异", txt)
        self.assertIn("ATC_RAT_RLL_P,0.145000,0.135000,+0.01", txt)
        self.assertIn("ARMING_CHECK,1,1,相同", txt)
        self.assertIn("BATT_AMP_PERVLT,24,,", txt)       # 没默认 → 差异留空
        self.assertTrue(lines[0].startswith("#"))


class TestWriteExport(unittest.TestCase):
    def test_param_file_is_utf8_lf_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.param"
            n = pe.write_export(p, pe.select(ROWS, pe.SCOPE_ALL), pe.FMT_PARAM, ["# h"])
            raw = p.read_bytes()
            self.assertEqual(n, 5)
            self.assertNotIn(b"\r\n", raw, ".param 用 LF 最稳")
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), ".param 不要 BOM")

    def test_csv_has_bom_for_excel(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            pe.write_export(p, ROWS, pe.FMT_CSV, ["# h"])
            self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_unknown_format_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                pe.write_export(Path(d) / "x.bin", ROWS, "parm", [])

    def test_suggested_name(self) -> None:
        self.assertEqual(pe.suggested_name("00000165.BIN", pe.SCOPE_DIFF, pe.FMT_PARAM),
                         "00000165_params_diff.param")
        self.assertEqual(pe.suggested_name("a.BIN", pe.SCOPE_ALL, pe.FMT_CSV),
                         "a_params_all.csv")


# --------------------------------------------------------------------------- #
class FakeStats:
    firmware = "ArduCopter V4.7.0 (1511f271)"
    frame_type = "QUAD/H"
    duration_str = "00:01:49"


class FakeStore:
    name = "2026-10-21 11-06-02.bin"
    stats = FakeStats()

    def __init__(self, params):
        # 与真 LogStore.params() 同构：值永远是 float（脏数据是 nan，不是 None）
        self._p = {k: (float(v) if v is not None else math.nan,
                       None if d is None else float(d))
                   for k, (v, d) in params.items()}

    def params(self):
        return self._p

    def close(self) -> None:
        """MainWindow.closeEvent 会调 store.close()；缺了它关窗口就抛 AttributeError。"""
        pass


class TestUIDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.store = FakeStore({n.name: (n.value, n.default) for n in ROWS})
        self.w.param_table.set_params(self.w.store.params())
        self.w._refilter_params()
        self.w.show()
        pump(self.app)

    def tearDown(self) -> None:
        self.w.close()
        pump(self.app)

    def test_button_exists_on_params_page(self) -> None:
        self.assertIsInstance(self.w.btn_param_export, QtWidgets.QPushButton)
        self.assertIn("导出", self.w.btn_param_export.text())
        self.assertTrue(self.w.btn_param_export.toolTip())

    def test_dialog_labels_carry_counts(self) -> None:
        from apmlog.app import _ParamExportDialog
        d = _ParamExportDialog(self.w.param_table.all_param_rows(),
                               self.w.param_table.visible_names(), pe.SCOPE_ALL, self.w)
        self.assertIn("6 条", d.rb_all.text())
        self.assertIn("2 条", d.rb_diff.text())
        self.assertIn("6 条", d.rb_filt.text())
        all_text = " ".join(x.text() for x in d.findChildren(QtWidgets.QLabel))
        self.assertIn("没写 Default", all_text, "有一类判不了的就要说出来")

    def test_dialog_defaults_follow_the_table_checkbox(self) -> None:
        from apmlog.app import _ParamExportDialog
        d1 = _ParamExportDialog(self.w.param_table.all_param_rows(), [], pe.SCOPE_ALL, self.w)
        self.assertEqual(d1.scope, pe.SCOPE_ALL)
        d2 = _ParamExportDialog(self.w.param_table.all_param_rows(), [], pe.SCOPE_DIFF, self.w)
        self.assertEqual(d2.scope, pe.SCOPE_DIFF)
        self.assertTrue(d2.rb_diff.isChecked())

    def test_fmt_property_tracks_radios(self) -> None:
        from apmlog.app import _ParamExportDialog
        d = _ParamExportDialog(self.w.param_table.all_param_rows(), [], pe.SCOPE_ALL, self.w)
        self.assertEqual(d.fmt, pe.FMT_PARAM)
        d.rb_csv.setChecked(True)
        self.assertEqual(d.fmt, pe.FMT_CSV)

    def test_table_and_export_agree_on_changed(self) -> None:
        """同一判定：表格里显示"相同"的，绝不能出现在差异导出里。"""
        diff_names = {r.name for r in pe.select(self.w.param_table.all_param_rows(),
                                                pe.SCOPE_DIFF)}
        for name, (val, dfv) in self.w.store.params().items():
            shown_same = self.w.param_table._is_changed(val, dfv) is False
            in_diff = name in diff_names
            self.assertNotEqual(shown_same and in_diff, True,
                                f"{name} 表格判为相同却进了差异导出")

    def test_filtered_scope_exports_only_visible(self) -> None:
        self.w._param_filter.setText("ATC_")
        self.w._refilter_params()
        vis = self.w.param_table.visible_names()
        self.assertEqual(vis, ["ATC_RAT_RLL_P"])
        out = self._run_export(pe.SCOPE_FILTERED, pe.FMT_PARAM)
        body = [ln for ln in Path(out).read_text(encoding="utf-8").splitlines()
                if ln and not ln.startswith("#")]
        self.assertEqual(body, ["ATC_RAT_RLL_P,0.145000"])

    def _run_export(self, scope: str, fmt: str) -> str:
        """把两个模态框都换掉，只验真实写盘结果。返回落盘路径。"""
        from apmlog import app as appmod
        d = tempfile.mkdtemp()
        target = str(Path(d) / ("out." + pe.FMT_EXT[fmt]))
        with mock.patch.object(appmod._ParamExportDialog, "exec",
                               lambda self: int(QtWidgets.QDialog.DialogCode.Accepted)), \
             mock.patch.object(appmod._ParamExportDialog, "scope",
                               property(lambda self: scope)), \
             mock.patch.object(appmod._ParamExportDialog, "fmt",
                               property(lambda self: fmt)), \
             mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName",
                               lambda *a, **k: (target, "")):
            self.w._export_params()
        self.assertTrue(Path(target).exists(), "没有写出文件")
        return target

    def test_full_param_export_writes_everything(self) -> None:
        out = self._run_export(pe.SCOPE_ALL, pe.FMT_PARAM)
        txt = Path(out).read_text(encoding="utf-8")
        body = [ln for ln in txt.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(body), 5, "6 条里 INS 的 NaN 不该写进去")
        self.assertIn("2026-10-21 11-06-02.bin", txt)
        self.assertIn("完整参数（6 / 6 条）", txt)
        self.assertIn("已导出 5 条参数", self.w.statusBar().currentMessage())

    def test_diff_export_only_changed(self) -> None:
        out = self._run_export(pe.SCOPE_DIFF, pe.FMT_PARAM)
        body = [ln for ln in Path(out).read_text(encoding="utf-8").splitlines()
                if ln and not ln.startswith("#")]
        self.assertEqual(body, ["ATC_RAT_RLL_P,0.145000", "BARO1_DEVID,1376550"])
        self.assertIn("仅与出厂默认不同（2 / 6 条）", Path(out).read_text(encoding="utf-8"))

    def test_csv_export_path_gets_extension(self) -> None:
        """用户手打名字不带后缀时补上，别静默按另一种格式写。"""
        from apmlog import app as appmod
        d = tempfile.mkdtemp()
        target = str(Path(d) / "myparams")
        with mock.patch.object(appmod._ParamExportDialog, "exec",
                               lambda self: int(QtWidgets.QDialog.DialogCode.Accepted)), \
             mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName",
                               lambda *a, **k: (target, "")):
            self.w._export_params()
        self.assertTrue(Path(target + ".param").exists())

    def test_cancel_writes_nothing(self) -> None:
        from apmlog import app as appmod
        with mock.patch.object(appmod._ParamExportDialog, "exec",
                               lambda self: int(QtWidgets.QDialog.DialogCode.Rejected)), \
             mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName",
                               lambda *a, **k: self.fail("取消后不该问保存位置")) as _m:
            self.w._export_params()

    def test_empty_scope_does_not_open_save_dialog(self) -> None:
        from apmlog import app as appmod
        self.w._param_filter.setText("NOSUCHPARAM_")
        self.w._refilter_params()
        info = []
        with mock.patch.object(appmod._ParamExportDialog, "exec",
                               lambda self: int(QtWidgets.QDialog.DialogCode.Accepted)), \
             mock.patch.object(appmod._ParamExportDialog, "scope",
                               property(lambda self: pe.SCOPE_FILTERED)), \
             mock.patch.object(QtWidgets.QFileDialog, "getSaveFileName",
                               lambda *a, **k: self.fail("空结果不该问保存位置")), \
             mock.patch.object(QtWidgets.QMessageBox, "information",
                               lambda *a, **k: info.append(a)):
            self.w._export_params()
        self.assertTrue(info, "要告诉用户为什么什么都没导")
        self.assertIn("0 条", self.w.statusBar().currentMessage())

    def test_no_log_open_says_so(self) -> None:
        from apmlog import app as appmod
        self.w.store = None
        seen = []
        with mock.patch.object(QtWidgets.QMessageBox, "information",
                               lambda *a, **k: seen.append(a)):
            self.w._export_params()
        self.assertTrue(seen)


if __name__ == "__main__":
    unittest.main()
