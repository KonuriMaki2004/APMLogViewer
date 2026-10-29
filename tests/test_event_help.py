# -*- coding: utf-8 -*-
"""tests/test_event_help.py —— 双击事件/告警弹出该类型的说明与处理建议。

用户 2026-10-28：日志里的警告/错误类型消息，双击后要弹出**对应那条类型**的详细说明
（问题说明 / 可能原因 / 处理建议，方案要具体可操作）；没匹配到也要明确提示，不许空白窗口；
不得影响现有日志查看与双击行为。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets                                     # noqa: E402

from apmlog.core import knowledge as kb                           # noqa: E402
from apmlog.core.model import AlertHit, EventRec                  # noqa: E402
from apmlog.plugins import manager as mgr_mod                     # noqa: E402
from apmlog.ui import settings as st                              # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(times: int = 4) -> None:
    app = qapp()
    for _ in range(times):
        app.processEvents()


ROOT = Path(__file__).resolve().parent.parent
HELP_PATH = ROOT / "data" / "knowledge" / "event_help.json"


# --------------------------------------------------------------------------- #
# 1. 数据文件本身
# --------------------------------------------------------------------------- #
class TestKnowledgeData(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads(HELP_PATH.read_text(encoding="utf-8"))
        self.help = kb.EventHelp.load()

    def test_file_loads(self) -> None:
        self.assertTrue(self.help.loaded)
        stats = self.help.stats()
        self.assertGreaterEqual(stats["by_subsys"], 31)
        self.assertGreaterEqual(stats["by_code"], 5)
        self.assertGreaterEqual(stats["by_msg"], 8)
        self.assertGreaterEqual(stats["by_alert_rule"], 6)

    def test_every_codebook_subsystem_is_covered(self) -> None:
        cb = json.loads((ROOT / "data" / "codebooks" / "copter_4x.json")
                        .read_text(encoding="utf-8"))
        missing = sorted(set(cb["error_subsystems"]) - set(self.data["by_subsys"]),
                         key=int)
        self.assertEqual(missing, [], f"码本里的子系统没写说明: {missing}")

    def test_every_entry_has_required_sections(self) -> None:
        def check(name: str, e: dict) -> None:
            self.assertTrue(str(e.get("title", "")).strip(), f"{name} 缺 title")
            self.assertTrue(str(e.get("summary", "")).strip(), f"{name} 缺 summary")
            self.assertGreaterEqual(len(e.get("causes") or ()), 2, f"{name} 原因太少")
            self.assertGreaterEqual(len(e.get("actions") or ()), 2, f"{name} 建议太少")

        for k, e in self.data["by_subsys"].items():
            check(f"by_subsys.{k}", e)
        for k, e in self.data["by_code"].items():
            self.assertRegex(k, r"^\d+:\d+$", "by_code 键必须是 subsys:ecode")
            check(f"by_code.{k}", e)
        for i, e in enumerate(self.data["by_msg"]):
            check(f"by_msg[{i}]", e)
            self.assertTrue(e.get("match"), "by_msg 规则必须有 match 关键字")
        for i, e in enumerate(self.data["by_alert_rule"]):
            check(f"by_alert_rule[{i}]", e)
            self.assertTrue(str(e.get("prefix", "")).strip(), "告警规则必须有 prefix")
        for k in ("ERR", "MSG", "ARM", "EV", "MODE"):
            check(f"by_kind.{k}", self.data["by_kind"][k])
        check("resolved", self.data["resolved"])
        check("fallback.event", self.data["fallback"]["event"])
        check("fallback.alert", self.data["fallback"]["alert"])

    def test_actions_are_concrete(self) -> None:
        """建议里不能只有“检查一下”这种空话：每条至少 12 个字符。"""
        for k, e in list(self.data["by_subsys"].items()) + list(self.data["by_code"].items()):
            for a in e.get("actions") or ():
                self.assertGreaterEqual(len(a.strip()), 12, f"{k} 的建议太空: {a!r}")


# --------------------------------------------------------------------------- #
# 2. 匹配优先级
# --------------------------------------------------------------------------- #
class TestExplain(unittest.TestCase):
    def setUp(self) -> None:
        self.help = kb.EventHelp.load()

    def test_err_specific_code_wins(self) -> None:
        e = self.help.explain_event(kind="ERR", code=12, ecode=1, codebook_text="CRASH_CHECK")
        self.assertTrue(e.matched)
        self.assertEqual(e.scope, "code")
        self.assertIn("炸机", e.title)

    def test_err_falls_back_to_subsystem(self) -> None:
        e = self.help.explain_event(kind="ERR", code=12, ecode=99)
        self.assertTrue(e.matched)
        self.assertEqual(e.scope, "subsys")
        self.assertIn("CRASH_CHECK", e.title)

    def test_err_unknown_subsystem_uses_kind_entry(self) -> None:
        e = self.help.explain_event(kind="ERR", code=999, ecode=1)
        self.assertTrue(e.matched)
        self.assertEqual(e.scope, "kind")
        self.assertIn("子系统 999", e.note, "退回通用条目时要说明没找到专门条目")

    def test_err_resolved_is_not_treated_as_fault(self) -> None:
        for kwargs in ({"ecode": 0}, {"ecode": 1, "codebook_text": "ERROR_RESOLVED 已恢复"}):
            e = self.help.explain_event(kind="ERR", code=3, **kwargs)
            self.assertEqual(e.scope, "resolved", kwargs)
            self.assertIn("已恢复", e.title)

    def test_subsystem_entry_carries_codebook_note(self) -> None:
        e = self.help.explain_event(kind="ERR", code=11, ecode=3,
                                    codebook_text="GPS（ECode 3 未映射）")
        self.assertIn("码表含义", e.note)
        self.assertIn("GPS", e.note)

    def test_msg_keyword_matching(self) -> None:
        cases = [
            ("PreArm: Compass not calibrated", "PreArm"),
            ("Crash: Disarming: AngErr=171>30", "炸机"),
            ("EKF3 IMU0 vintage variance", "EKF"),
            ("Thrust loss detected", "推力"),
        ]
        for text, expect in cases:
            e = self.help.explain_event(kind="MSG", text=text, decoded=text)
            self.assertTrue(e.matched, text)
            self.assertEqual(e.scope, "msg", text)
            self.assertIn(expect, e.title, text)

    def test_msg_without_keyword_uses_kind_entry(self) -> None:
        e = self.help.explain_event(kind="MSG", text="Frame: QUAD/H")
        self.assertTrue(e.matched)
        self.assertEqual(e.scope, "kind")
        self.assertIn("未匹配到专门条目", e.note)

    def test_alert_prefix_matching(self) -> None:
        e = self.help.explain_alert(rule="vibe_x", title="振动超标（X）", severity="warn")
        self.assertEqual(e.scope, "alert")
        self.assertIn("振动", e.title)
        e2 = self.help.explain_alert(rule="batt_undervolt_crit", title="电压偏低")
        self.assertIn("电压", e2.title)

    def test_real_alert_rule_ids_all_match(self) -> None:
        """知识库前缀必须跟 alerts.py 真实产出的 rule id 对得上（防数据/代码漂移）。"""
        ids = ["vibe_vibex", "vibe_vibey", "vibe_vibez", "vibe_clip", "vibe_axis",
               "batt_undervolt_warn", "batt_undervolt_crit", "batt_pack_low_warn",
               "batt_pack_low_crit", "batt_semantics", "batt_sag_warn", "batt_sag_crit",
               "batt_temp_warn", "batt_temp_crit", "batt_end_drop", "batt_cells_unknown"]
        for rid in ids:
            e = self.help.explain_alert(rule=rid, title="x")
            self.assertTrue(e.matched, f"规则 {rid} 没匹配到知识库条目")
            self.assertEqual(e.scope, "alert", rid)

    def test_plugin_alert_is_explained_as_such(self) -> None:
        e = self.help.explain_alert(rule="plugin:demo:ke_high", title="动能偏高")
        self.assertFalse(e.matched)
        self.assertEqual(e.scope, "plugin")
        self.assertIn("插件", e.summary)
        self.assertIn("动能偏高", e.note)

    def test_unknown_alert_falls_back_but_never_empty(self) -> None:
        e = self.help.explain_alert(rule="zzz_new_rule", title="新规则")
        self.assertFalse(e.matched)
        self.assertEqual(e.scope, "fallback")
        self.assertIn("未收录", e.title)
        self.assertGreaterEqual(len(e.actions), 3)

    def test_unknown_kind_still_returns_content(self) -> None:
        e = self.help.explain_event(kind="WHAT")
        self.assertFalse(e.matched)
        self.assertTrue(e.title and e.summary and e.causes and e.actions)

    def test_entry_as_text_has_all_sections(self) -> None:
        e = self.help.explain_event(kind="ERR", code=12, ecode=1)
        txt = e.as_text()
        for part in ("问题说明：", "可能原因：", "处理建议："):
            self.assertIn(part, txt)
        self.assertGreater(len(txt), 120)


class TestMissingOrBrokenData(unittest.TestCase):
    def test_missing_file_still_explains(self) -> None:
        h = kb.EventHelp.load(Path(tempfile.mkdtemp()) / "nope.json")
        self.assertFalse(h.loaded)
        e = h.explain_event(kind="ERR", code=12, ecode=1)
        self.assertFalse(e.matched)
        self.assertIn("未收录", e.title)
        self.assertGreaterEqual(len(e.actions), 3)
        self.assertTrue(h.explain_alert(rule="vibe_x").actions)

    def test_broken_json_still_explains(self) -> None:
        p = Path(tempfile.mkdtemp()) / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        h = kb.EventHelp.load(p)
        self.assertFalse(h.loaded)
        self.assertTrue(h.explain_event(kind="MSG", text="x").summary)

    def test_empty_entry_fields_are_backfilled(self) -> None:
        h = kb.EventHelp({"by_subsys": {"3": {"title": "只有标题"}}})
        e = h.explain_event(kind="ERR", code=3, ecode=1)
        self.assertIn("只有标题", e.title)
        self.assertTrue(e.summary and e.causes and e.actions)


# --------------------------------------------------------------------------- #
# 3. 弹窗
# --------------------------------------------------------------------------- #
class TestDialog(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.help = kb.EventHelp.load()

    def test_dialog_shows_matched_entry(self) -> None:
        from apmlog.ui.eventhelp import EventHelpDialog
        entry = self.help.explain_event(kind="ERR", code=12, ecode=1,
                                        codebook_text="CRASH_CHECK 炸机检测")
        dlg = EventHelpDialog(entry, subject="ERR 子系统 12", meta="t = 1.0 s",
                              raw="Crash: Disarming")
        try:
            text = dlg.body.toPlainText()
            for part in ("问题说明", "可能原因", "处理建议", "炸机"):
                self.assertIn(part, text)
            self.assertGreater(len(text), 200, "弹窗内容不许是空壳")
            self.assertIn("ERR 子系统 12", dlg.lbl_subject.text())
            self.assertEqual(dlg.lbl_scope.text(), "具体类型")
            self.assertIn("已复制", dlg.plain_text() + "已复制")   # 复制文本可用
        finally:
            dlg.close()

    def test_dialog_marks_fallback_explicitly(self) -> None:
        from apmlog.ui.eventhelp import EventHelpDialog
        entry = self.help.explain_event(kind="WHAT", code=1, ecode=7)
        self.assertFalse(entry.matched)
        dlg = EventHelpDialog(entry, subject="未知类型", meta="", raw="")
        try:
            text = dlg.body.toPlainText()
            self.assertIn("未收录", text)
            self.assertGreater(len(text), 120, "兜底弹窗也不能空白")
            self.assertEqual(dlg.lbl_scope.text(), "未收录")
        finally:
            dlg.close()

    def test_plain_text_includes_context(self) -> None:
        from apmlog.ui.eventhelp import EventHelpDialog
        entry = self.help.explain_event(kind="ERR", code=11, ecode=2)
        dlg = EventHelpDialog(entry, subject="S", meta="M", raw="R")
        try:
            t = dlg.plain_text()
            self.assertIn("S", t)
            self.assertIn("M", t)
            self.assertIn("R", t)
            self.assertIn("处理建议", t)
        finally:
            dlg.close()


# --------------------------------------------------------------------------- #
# 4. 主窗集成：双击触发、不影响原有行为
# --------------------------------------------------------------------------- #
class TestMainWindowWiring(unittest.TestCase):
    def setUp(self) -> None:
        qapp()
        self.tmp = Path(tempfile.mkdtemp(prefix="apmlog_evhelp_"))
        (self.tmp / "plugins").mkdir()
        st.Settings.set_instance(st.Settings(self.tmp / "settings.json"))
        self._p1 = mock.patch.object(mgr_mod, "default_plugin_dir",
                                     lambda: self.tmp / "plugins")
        self._p1.start()
        self.addCleanup(self._p1.stop)
        self._p2 = mock.patch("apmlog.app.MainWindow._views_path",
                              lambda self_: self.tmp / "views.json")
        self._p2.start()
        self.addCleanup(self._p2.stop)
        from apmlog.app import MainWindow
        self.win = MainWindow()
        self.addCleanup(self.win.close)
        self.addCleanup(lambda: st.Settings.set_instance(None))

    def _err_ev(self) -> EventRec:
        return EventRec(t=12.5, kind="ERR", code=12, ecode=1,
                        decoded="CRASH_CHECK 炸机检测（ECode 1: CRASH_CHECK_CRASH）",
                        text="Subsys=12 ECode=1", severity="critical")

    def test_event_table_double_click_emits_eventrec(self) -> None:
        ev = self._err_ev()
        self.win.event_table.set_events([ev])
        shown: list[tuple] = []
        self.win._show_help = lambda entry, **kw: shown.append((entry, kw))   # 别弹模态窗
        got: list[object] = []
        self.win.event_table.helpRequested.connect(got.append)
        item = self.win.event_table.item(0, 2)
        self.win.event_table.itemDoubleClicked.emit(item)
        pump()
        self.assertEqual(len(got), 1)
        self.assertIs(got[0], ev)
        self.assertEqual(len(shown), 1, "双击还会真的把说明弹出来")
        self.assertTrue(shown[0][0].matched)

    def test_single_click_still_seeks(self) -> None:
        self.win.event_table.set_events([self._err_ev()])
        seeks: list[float] = []
        self.win.event_table.seekRequested.connect(seeks.append)
        self.win.event_table.clearSelection()      # 保证 selectRow(0) 真的产生选择变化
        self.win.event_table.setCurrentCell(-1, -1)
        self.win.event_table.selectRow(0)
        pump()
        self.assertEqual(seeks, [12.5], "原有单击定位行为不能变")

    def test_alert_table_double_click_emits_hit(self) -> None:
        hit = AlertHit(rule="vibe_x", title="振动超标（X）", severity="warn",
                       t0=1.0, t1=2.0, value=42.0, threshold=30.0, unit="m/s²",
                       message="X 轴峰值 42.0 m/s²")
        self.win.alert_table.set_alerts([hit])
        shown: list[tuple] = []
        self.win._show_help = lambda entry, **kw: shown.append((entry, kw))   # 别弹模态窗
        got: list[object] = []
        self.win.alert_table.helpRequested.connect(got.append)
        self.win.alert_table.itemDoubleClicked.emit(self.win.alert_table.item(0, 1))
        pump()
        self.assertEqual(len(got), 1)
        self.assertIs(got[0], hit)
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0][0].scope, "alert")

    def test_alert_seek_behaviour_intact(self) -> None:
        hit = AlertHit(rule="batt_undervolt_warn", title="电压偏低", severity="warn",
                       t0=3.0, t1=4.0, value=3.2, threshold=3.3, unit="V", message="")
        self.win.alert_table.set_alerts([hit])
        seeks: list[object] = []
        self.win.alert_table.seekRequested.connect(seeks.append)
        self.win.alert_table.clearSelection()      # 别的用例可能先选中过第 0 行
        self.win.alert_table.setCurrentCell(-1, -1)
        self.win.alert_table.selectRow(0)
        pump()
        self.assertEqual(seeks, [hit])

    def test_open_event_help_matches_the_double_clicked_type(self) -> None:
        captured: list[tuple] = []
        self.win._show_help = lambda entry, **kw: captured.append((entry, kw))
        self.win._open_event_help(self._err_ev())
        self.assertEqual(len(captured), 1)
        entry, kw = captured[0]
        self.assertTrue(entry.matched)
        self.assertEqual(entry.scope, "code")
        self.assertIn("子系统 12", kw["subject"])
        self.assertIn("严重", kw["meta"])

    def test_open_alert_help_matches_rule(self) -> None:
        captured: list[tuple] = []
        self.win._show_help = lambda entry, **kw: captured.append((entry, kw))
        hit = AlertHit(rule="batt_undervolt_crit", title="电压偏低（Volt）", severity="critical",
                       t0=5.0, t1=6.0, value=3.05, threshold=3.1, unit="V",
                       message="单芯 3.05 V 低于严重阈值 3.10 V")
        self.win._open_alert_help(hit)
        entry, kw = captured[0]
        self.assertEqual(entry.scope, "alert")
        self.assertIn("规则 batt_undervolt_crit", kw["subject"])
        self.assertIn("3.05", kw["meta"])

    def test_help_does_not_touch_view_state(self) -> None:
        """弹说明是只读动作：不能改选中的时间窗，也不能重算视图。"""
        self.win._show_help = lambda entry, **kw: None
        before = getattr(self.win, "_win", "unset")
        self.win._open_event_help(self._err_ev())
        self.assertEqual(getattr(self.win, "_win", "unset"), before)

    def test_unknown_type_gets_fallback_not_blank(self) -> None:
        captured: list[tuple] = []
        self.win._show_help = lambda entry, **kw: captured.append((entry, kw))
        # 连事件类型都没见过（不是 ERR/MSG/ARM…）才是真兜底
        self.win._open_event_help(EventRec(t=1.0, kind="ZZZ", text="完全没见过的类型",
                                          decoded="完全没见过的类型", severity="warn"))
        entry, _kw = captured[0]
        self.assertFalse(entry.matched)
        self.assertIn("未收录", entry.title)
        self.assertTrue(entry.title and entry.actions)

    def test_bad_payload_is_ignored_not_crash(self) -> None:
        self.win._show_help = lambda entry, **kw: None
        self.win._open_event_help(None)
        self.win._open_event_help("不是事件")
        self.win._open_alert_help({"rule": "x"})
        self.assertIsNotNone(self.win.centralWidget())


if __name__ == "__main__":
    unittest.main()
