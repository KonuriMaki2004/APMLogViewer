# -*- coding: utf-8 -*-
"""tests/test_logconfig_ui.py —— “飞控未开启日志写入”提示框（**只读**版）。

这里守两件事：

1. 提示框必须把判定依据和**手动开启方法**讲清楚，用户照着做就能自己开；
2. 界面上不存在任何能写飞控参数的通道——以前那个“推一下就下发”的滑块已按只读
   原则整体删除，下面的反向守卫会盯住它别被加回来。

弹窗用真控件测，但不进 ``exec()``：面板把“造弹窗”和“弹出来”拆成两个方法，
否则模态循环会把测试挂死（MiniTimeline 的菜单踩过同一个坑）。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_logconfig_ui.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets                       # noqa: E402
from PySide6.QtTest import QTest                                   # noqa: E402

from apmlog.ui import theme as T                                   # noqa: E402
from apmlog.ui.logconfig import LogWriteDialog, guidance_text      # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 12) -> None:
    for _ in range(times):
        app.processEvents()


def settle(app: QtWidgets.QApplication, seconds: float = 0.25) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def click(w: QtWidgets.QWidget, app: QtWidgets.QApplication) -> None:
    """真实鼠标按下+抬起。

    坑：release 事件的 **button()** 仍要是 LeftButton（“哪个键引起的这个事件”），
    只有 buttons()（当前按住的键）才置 NoButton；弄混了 QAbstractButton 不认这是点击。
    """
    c = QtCore.QPoint(int(w.width() / 2), int(w.height() / 2))
    g = QtCore.QPoint(w.mapToGlobal(c))
    L, N = QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.NoButton
    for kind, button, buttons in ((QtCore.QEvent.Type.MouseButtonPress, L, L),
                                  (QtCore.QEvent.Type.MouseButtonRelease, L, N)):
        ev = QtGui.QMouseEvent(kind, QtCore.QPointF(c), QtCore.QPointF(g),
                               button, buttons, QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(w, ev)
    pump(app)


# ---------------------------------------------------------------- 假数据（鸭子类型）
class _PV:
    def __init__(self, name, found=True, value=None, reason=""):
        self.name, self.found, self.value, self.reason = name, found, value, reason
        self.param_type = 6


class _Cfg:
    """对应 apmlog.link.params.LogWriteConfig：suggest = (名字, 当前值, 建议值)。"""

    def __init__(self, backend=None, bitmask=None, suggest=(), state="disabled",
                 detail="飞控当前不会把日志写到 SD 卡，所以飞控上的日志列表是空的。",
                 suggest_note="建议值取自你这块飞控自己的历史日志 PARAM 块"):
        self.state = state
        self.backend = backend or _PV("LOG_BACKEND_TYPE", True, 0.0)
        self.bitmask = bitmask or _PV("LOG_BITMASK", True, 0.0)
        self.suggest = tuple(suggest)
        self.detail = detail
        self.suggest_note = suggest_note


DISABLED = _Cfg(suggest=(("LOG_BACKEND_TYPE", 0.0, 1.0), ("LOG_BITMASK", 0.0, 180222.0)))


# ---------------------------------------------------------------- 提示框内容
class TestLogWriteDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _mk(self, cfg=None) -> LogWriteDialog:
        d = LogWriteDialog(cfg or DISABLED)
        d.show()
        settle(self.app, 0.1)
        self.addCleanup(d.deleteLater)
        return d

    def test_title_states_the_problem(self) -> None:
        texts = [l.text() for l in self._mk().findChildren(QtWidgets.QLabel)]
        self.assertIn("飞控未开启日志写入功能", texts)

    def test_shows_both_parameters_with_values(self) -> None:
        body = " ".join(l.text() for l in self._mk().findChildren(QtWidgets.QLabel))
        self.assertIn("LOG_BACKEND_TYPE", body)
        self.assertIn("LOG_BITMASK", body)
        self.assertIn("0", body, "当前值要摊开显示，不能只说“没开”")

    def test_guidance_lists_manual_steps(self) -> None:
        txt = self._mk().txt.toPlainText()
        for key in ("Mission Planner", "QGroundControl", "MAVProxy",
                    "LOG_BACKEND_TYPE", "LOG_BITMASK", "SD", "重启"):
            self.assertIn(key, txt, f"处理方法里缺了 {key}")

    def test_guidance_promises_not_to_write(self) -> None:
        txt = self._mk().txt.toPlainText()
        self.assertIn("不会替你修改任何参数", txt)
        self.assertIn("只读", txt)

    def test_guidance_appends_suggested_targets(self) -> None:
        txt = self._mk().txt.toPlainText()
        self.assertIn("180222", txt, "建议值要具体到数字，不能只说“改为非 0”")
        self.assertIn("当前 0 → 建议 180222", txt)
        self.assertIn("建议值取自你这块飞控自己的历史日志", txt)

    def test_no_suggest_still_gives_general_steps(self) -> None:
        d = self._mk(_Cfg(state="unknown", suggest=()))
        self.assertIn("Mission Planner", d.txt.toPlainText())

    def test_unreadable_param_is_reported_honestly(self) -> None:
        cfg = _Cfg(bitmask=_PV("LOG_BITMASK", False, None, "timeout"), suggest=())
        body = " ".join(l.text() for l in self._mk(cfg).findChildren(QtWidgets.QLabel))
        self.assertIn("读不到", body)
        self.assertIn("飞控未应答", body, "不能把“没读到”糊成“值为 0”")

    def test_text_is_selectable_and_copyable(self) -> None:
        d = self._mk()
        flags = d.detail.textInteractionFlags()
        self.assertTrue(flags & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        QtWidgets.QApplication.clipboard().setText("")
        d.btn_copy.click()
        got = QtWidgets.QApplication.clipboard().text()
        self.assertIn("Mission Planner", got)
        self.assertEqual(got, guidance_text(DISABLED))

    def test_opt_in_checkbox_copies_on_close(self) -> None:
        d = self._mk()
        QtWidgets.QApplication.clipboard().setText("")
        d.chk_clip.setChecked(True)
        d.btn_close.click()
        self.assertIn("LOG_BITMASK", QtWidgets.QApplication.clipboard().text())

    def test_enter_closes(self) -> None:
        d = self._mk()
        QTest.keyClick(d, QtCore.Qt.Key.Key_Return)
        pump(self.app)
        self.assertFalse(d.isVisible())

    def test_readonly_view_has_no_action_button(self) -> None:
        """按钮只允许“复制/知道了”：出现任何像“开启/写入/应用”的按钮就是越界。"""
        d = self._mk()
        labels = [b.text() for b in d.findChildren(QtWidgets.QAbstractButton)]
        self.assertTrue(labels, "至少要有按钮")
        for t in labels:
            for bad in ("开启", "写入", "应用", "下发", "确定", "是"):
                self.assertNotIn(bad, t, f"按钮“{t}”暗示本工具会写飞控参数")

    def test_no_write_channel_left_on_the_dialog(self) -> None:
        for gone in ("switch", "enableRequested", "report", "set_busy", "altitude"):
            self.assertFalse(hasattr(LogWriteDialog, gone), f"{gone} 应该已经随写路径删除")
        self.assertFalse(hasattr(LogWriteDialog, "clicked_enable"))

    def test_switch_widget_module_is_gone(self) -> None:
        """滑块控件是为写参数而生的；它回来了就说明有人偷偷恢复了写通道。"""
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("apmlog.ui._switch"),
                          "apmlog/ui/_switch.py 应已删除")

    def test_guidance_text_is_readable_on_both_themes(self) -> None:
        for spec in ("light", "dark"):
            T.apply(self.app, T.spec_for(spec))
            pump(self.app)
            d = self._mk(_Cfg(bitmask=_PV("LOG_BITMASK", False, None, "timeout"), suggest=()))
            labels = [l for l in d.findChildren(QtWidgets.QLabel) if l.styleSheet()]
            self.assertTrue(labels, f"{spec} 下“读不到”提示应当带自定义颜色")
            for l in labels:
                # 不假定写样式时“color:” 后面有没有空格
                hexc = l.styleSheet().split("color:")[1].split(";")[0].strip()
                self.assertTrue(hexc.startswith("#") and len(hexc) == 7,
                                f"{spec} 下颜色必须是十六进制而不是 str(QColor)")
                base = self.app.palette().color(QtGui.QPalette.ColorRole.Window).name()
                self.assertGreaterEqual(T.contrast(hexc, base), 4.5,
                                        f"{spec} 主题下“读不到”提示对比不足：{hexc} on {base}")
        T.apply(self.app, T.spec_for("system"))
        pump(self.app)


# ---------------------------------------------------------------- 面板接线
class TestPanelWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_lc_")
        self.addCleanup(self._dir.cleanup)
        from apmlog.ui.linkpanel import LinkPanel
        self.p = LinkPanel()
        self.p.resize(980, 720)
        self.p.show()
        pump(self.app, 20)
        self.addCleanup(self._kill)
        self.shown: list[LogWriteDialog] = []

        def _fake_show(dlg: LogWriteDialog) -> int:
            self.shown.append(dlg)
            self.p._logcfg_dialog = dlg          # 模拟模态仍在屏上
            return 0

        self.p.show_logconfig_dialog = _fake_show                   # type: ignore[assignment]

    def _kill(self) -> None:
        for w in list(self.shown):
            try:
                w.close()
                w.deleteLater()
            except RuntimeError:
                pass
        try:
            self.p.worker.quit()
            self.p.worker.wait(3000)
        except Exception:
            pass
        self.p.close()
        pump(self.app, 6)
        self.p.deleteLater()
        pump(self.app, 6)

    def test_probe_button_exists_and_needs_a_link(self) -> None:
        self.assertTrue(hasattr(self.p, "btn_logcfg"))
        self.assertFalse(self.p.btn_logcfg.isEnabled(), "没连飞控时不该能点检测")
        self.assertIn("不写参数", self.p.btn_logcfg.toolTip())

    def test_disabled_state_opens_the_guidance_dialog(self) -> None:
        self.p._on_logconfig(DISABLED)
        pump(self.app)
        self.assertEqual(len(self.shown), 1)
        self.assertIsInstance(self.shown[0], LogWriteDialog)

    def test_enabled_unknown_and_none_do_not_open_a_dialog(self) -> None:
        self.p._on_logconfig(_Cfg(state="enabled", suggest=()))
        self.p._on_logconfig(_Cfg(state="unknown", suggest=()))
        self.p._on_logconfig(None)
        pump(self.app)
        self.assertEqual(self.shown, [], "只有明确未开启才该弹窗")

    def test_second_probe_while_open_does_not_stack_dialogs(self) -> None:
        first = self.p.build_logconfig_dialog(DISABLED)
        self.addCleanup(first.deleteLater)
        self.p._logcfg_dialog = first
        self.p._show_logconfig_dialog(DISABLED)
        self.assertEqual(self.shown, [], "已经提示过就不该再弹一个")

    def test_reference_is_dropped_when_the_dialog_closes(self) -> None:
        self.p._on_logconfig(DISABLED)
        self.shown[0].deleteLater()
        pump(self.app)
        self.assertIsNone(self.p._logcfg_dialog)
        self.p._on_logconfig(DISABLED)
        self.assertEqual(len(self.shown), 2, "旧的关了，新的要能弹出来")

    def test_panel_and_worker_have_no_write_path(self) -> None:
        """反向守卫：整条写参数的通道必须不存在。"""
        w = self.p.worker
        for gone in ("param_write_done", "_do_enable_log"):
            self.assertFalse(hasattr(type(w), gone), f"worker 上不该再有 {gone}")
            self.assertFalse(hasattr(w, gone))
        for gone in ("_on_param_write", "_on_enable_requested", "_ask_enable_log"):
            self.assertFalse(hasattr(self.p, gone), f"面板上不该再有 {gone}")
        src = Path("apmlog/ui/linkpanel.py").read_text(encoding="utf-8")
        for token in ("PARAM_SET", "param_set", "apply_log_write_enable", "write_param",
                      "enable_log", "allow_param_set"):
            self.assertNotIn(token, src, f"linkpanel.py 里还留着 {token}")

    def test_command_table_has_no_write_command(self) -> None:
        import inspect
        import apmlog.ui.linkpanel as LP
        src = inspect.getsource(LP._LinkWorker.run)
        self.assertIn('"probe"', src)
        for gone in ('"enable_log"', '"write_param"', '"param_set"'):
            self.assertNotIn(gone, src, f"命令表里还留着 {gone}")

    def test_worker_probe_without_session_is_a_note_not_a_crash(self) -> None:
        notes: list[str] = []
        w = self.p.worker
        w.note.connect(notes.append)
        w.sess = None
        w._do_probe()
        self.assertTrue(any("连接" in n for n in notes), notes)


# ---------------------------------------------------------------- 端到端（真链路）
class TestEndToEndWithFakeFC(unittest.TestCase):
    """真链路端到端：FakeFC 关掉日志 → 面板连上 → 自动检测 → 弹提示。

    QThread、TCP 回环、MAVLink 编解码、参数按名读取全是真的；只有弹窗的模态循环
    用真实控件代替。核心断言是**全程没写过飞控一个字节**。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_e2e_")
        self.addCleanup(self._dir.cleanup)
        from apmlog.ui.linkpanel import LinkPanel
        self.p = LinkPanel(out_dir=self._dir.name)
        self.p.show()
        pump(self.app, 20)
        self.shown: list[LogWriteDialog] = []

        def _fake_show(dlg):
            self.shown.append(dlg)
            self.p._logcfg_dialog = dlg
            return 0

        self.p.show_logconfig_dialog = _fake_show                   # type: ignore[assignment]
        self.fc = None
        self.addCleanup(self._kill)

    def _kill(self) -> None:
        try:
            self.p.worker.quit()
            self.p.worker.wait(3000)
        except Exception:
            pass
        if self.fc is not None:
            try:
                self.fc.stop()
            except Exception:
                pass
        self.p.close()
        pump(self.app, 8)

    def _wait(self, pred, secs: float = 15.0) -> bool:
        end = time.time() + secs
        while time.time() < end:
            pump(self.app, 3)
            time.sleep(0.02)
            try:
                if pred():
                    return True
            except Exception:
                return False
        return False

    def _connect(self, fc) -> list:
        self.fc = fc
        host, port = fc.serve_tcp("127.0.0.1", 0)
        got: list = []
        self.p.worker.logconfig_ready.connect(got.append)
        self.p.worker.post("connect", target=f"tcp:{host}:{port}", baud=57600)
        self.assertTrue(self._wait(lambda: got), "连上后应自动完成日志写入检测")
        return got

    def test_disabled_fc_is_detected_and_never_written_to(self) -> None:
        from apmlog.link.fakes import FakeFC
        fc = FakeFC([], heartbeat_period=0.1,
                    params={"LOG_BACKEND_TYPE": 0.0, "LOG_BITMASK": 0.0})
        got = self._connect(fc)
        cfg = got[0]
        self.assertEqual(cfg.state, "disabled")
        self.assertEqual(cfg.suggest,
                         (("LOG_BACKEND_TYPE", 0.0, 1.0), ("LOG_BITMASK", 0.0, 180222.0)))
        # 最关键：光是“连上 + 检测 + 弹提示”不能改飞控
        self.assertEqual(fc.writes, [], "探测阶段发了 PARAM_SET！")
        self.assertEqual(fc.stats.param_sets, 0)
        self.assertEqual(fc.params["LOG_BACKEND_TYPE"], 0.0)
        self.assertEqual(fc.params["LOG_BITMASK"], 0.0)
        self.assertGreaterEqual(fc.stats.param_reads, 2, "应真的按名读过两个参数")

    def test_the_dialog_the_user_sees_actually_lists_the_steps(self) -> None:
        from apmlog.link.fakes import FakeFC
        fc = FakeFC([], heartbeat_period=0.1,
                    params={"LOG_BACKEND_TYPE": 0.0, "LOG_BITMASK": 0.0})
        got = self._connect(fc)
        dlg = self.p.build_logconfig_dialog(got[0])
        dlg.show()
        settle(self.app, 0.1)
        self.addCleanup(dlg.deleteLater)
        body = dlg.txt.toPlainText()
        self.assertIn("180222", body)
        self.assertIn("Mission Planner", body)
        # 用户就算什么都不做，也只是关掉窗口——没有任何写通道可点
        self.assertEqual(fc.writes, [])
        dlg.btn_close.click()
        pump(self.app)
        self.assertEqual(fc.writes, [], "关掉提示框也不该触发任何写入")
        self.assertEqual(fc.params["LOG_BITMASK"], 0.0)

    def test_enabled_fc_does_not_bother_the_user(self) -> None:
        from apmlog.link.fakes import FakeFC
        fc = FakeFC([], heartbeat_period=0.1)          # 默认就是“已开启”那套参数
        got = self._connect(fc)
        self.assertEqual(got[0].state, "enabled")
        self.assertEqual(self.shown, [], "已开启就不该弹窗打扰")
        self.assertEqual(fc.writes, [])

    def test_silent_fc_is_unknown_not_disabled(self) -> None:
        """完全不回参数的飞控：判 unknown，不得据此建议写入。"""
        from apmlog.link.fakes import FakeFC
        fc = FakeFC([], heartbeat_period=0.1, answer_params=False)
        got = self._connect(fc)
        self.assertEqual(got[0].state, "unknown")
        self.assertEqual(got[0].suggest, ())
        self.assertEqual(self.shown, [], "判不准就不要弹窗")
        self.assertEqual(fc.writes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
