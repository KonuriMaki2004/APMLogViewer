# -*- coding: utf-8 -*-
"""tests/test_timeline_click.py —— 底部时间线的点击查事件、空状态、跟随主题。

这里以前一个测试都没有，所以「左键点时间线直接抛 AttributeError」藏了很久：
PySide6 没有 QtCore.QtMouseButton 这种扁平枚举名，写错后 pythonw 下 stderr 没人看，
表现就是"点了没反应"。第 1 组用例专门钉住这条路径。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_timeline_click.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets                       # noqa: E402

from apmlog.ui import settings as st                               # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 25) -> None:
    for _ in range(times):
        app.processEvents()


def click_at(w: QtWidgets.QWidget, x: float, y: float = 40.0,
             mods: QtCore.Qt.KeyboardModifier = QtCore.Qt.KeyboardModifier.NoModifier) -> None:
    """真发一对鼠标按下/抬起事件（不走模态菜单，菜单本身单独测）。"""
    for typ, btn in ((QtCore.QEvent.Type.MouseButtonPress, QtCore.Qt.MouseButton.LeftButton),
                     (QtCore.QEvent.Type.MouseButtonRelease, QtCore.Qt.MouseButton.NoButton)):
        ev = QtGui.QMouseEvent(typ, QtCore.QPointF(x, y), QtCore.QPointF(x, y),
                               QtCore.QPoint(500, 700), btn, btn, mods)
        QtWidgets.QApplication.sendEvent(w, ev)
    pump(qapp())


EVENTS = [
    {"t": 10.0, "kind": "EV", "severity": "info", "display": "进入 AUTO"},
    {"t": 12.0, "kind": "ERR", "severity": "error", "display": "GPS 丢失"},
    {"t": 50.0, "kind": "MSG", "severity": "warn", "display": "震动偏高"},
]


class _WindowCase(unittest.TestCase):
    """共用一个临时 settings + 一个 MainWindow，绝不碰用户真实设置文件。"""

    def setUp(self) -> None:
        self.app = qapp()
        st.install_tip_style(self.app)
        self._dir = tempfile.TemporaryDirectory(prefix="apmlog_timeline_")
        self.addCleanup(self._dir.cleanup)
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self._dir.name) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        from apmlog.app import MainWindow
        self.w = MainWindow()
        self.w.resize(1200, 800)
        self.w.show()
        pump(self.app)
        self.tl = self.w.timeline
        self.notes: list[str] = []
        self.chosen: list[object] = []
        self.tl.notice.connect(self.notes.append)
        self.tl.eventChosen.connect(self.chosen.append)

    def tearDown(self) -> None:
        self.w.close()
        pump(self.app, 5)
        self.w.deleteLater()
        pump(self.app, 5)

    def load(self, events: list[dict] | None = None, span: tuple[float, float] = (0.0, 100.0)) -> None:
        self.tl.set_span(*span)
        self.tl.set_events(EVENTS if events is None else events)

    def no_popup(self) -> list[float]:
        """把真模态弹出换成记录器（测试里绝不能开模态菜单）。"""
        seen: list[float] = []
        self.tl._popup_events = lambda t, g: seen.append(float(t))   # type: ignore[method-assign]
        return seen


class _Hover:
    """只用来直接驱动 hoverMoveEvent（避开 PySide6 已标废弃的 QHoverEvent 构造重载）。"""

    def __init__(self, pos: QtCore.QPointF) -> None:
        self._pos = pos
        self.accepted = False

    def position(self) -> QtCore.QPointF:
        return self._pos

    def accept(self) -> None:
        self.accepted = True


class TestClickReacts(unittest.TestCase):
    """最基础的一条：左键点击必须能跑完，不能抛异常。"""

    def test_left_click_does_not_raise(self) -> None:
        app = qapp()
        tl = _make_timeline(app)
        self.addCleanup(tl.deleteLater)
        tl.set_span(0.0, 100.0)
        got: list[float] = []
        tl.timeChanged.connect(got.append)
        tl._popup_events = lambda t, g: None                         # type: ignore[assignment]
        for typ, btn in ((QtCore.QEvent.Type.MouseButtonPress, QtCore.Qt.MouseButton.LeftButton),
                         (QtCore.QEvent.Type.MouseButtonRelease, QtCore.Qt.MouseButton.NoButton)):
            ev = QtGui.QMouseEvent(typ, QtCore.QPointF(60, 40), QtCore.QPointF(60, 40),
                                   QtCore.QPoint(60, 40), btn, btn,
                                   QtCore.Qt.KeyboardModifier.NoModifier)
            QtWidgets.QApplication.sendEvent(tl, ev)          # 写错枚举时这里直接 AttributeError
        pump(app)
        self.assertEqual(len(got), 1, "左键按下必须发一次 timeChanged")
        self.assertAlmostEqual(got[0], tl._t_of(60, tl.width()), places=6)

    def test_right_click_is_not_treated_as_seek(self) -> None:
        app = qapp()
        tl = _make_timeline(app)
        self.addCleanup(tl.deleteLater)
        tl.set_span(0.0, 100.0)
        got: list[float] = []
        tl.timeChanged.connect(got.append)
        ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonPress, QtCore.QPointF(60, 40),
                               QtCore.QPointF(60, 40), QtCore.QPoint(60, 40),
                               QtCore.Qt.MouseButton.RightButton, QtCore.Qt.MouseButton.RightButton,
                               QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(tl, ev)
        pump(app)
        self.assertEqual(got, [])


def _make_timeline(app: QtWidgets.QApplication) -> "object":
    from apmlog.app import MiniTimeline
    tl = MiniTimeline()
    tl.resize(1000, 96)
    tl.show()
    pump(app, 5)
    return tl


class TestEmptyState(_WindowCase):
    def test_starts_empty_and_says_so(self) -> None:
        self.assertTrue(self.tl.is_empty())
        seen = self.no_popup()
        click_at(self.tl, 300)
        self.assertEqual(seen, [], "没日志时不该弹事件菜单")
        self.assertIn("还没加载日志", self.notes[-1])

    def test_empty_painting_does_not_draw_fake_ticks(self) -> None:
        img = self.tl.grab().toImage()
        self.assertFalse(img.isNull())
        # 假刻度是 "0s/1s"，空状态只该有一句提示；底色用“跟主题”来钉住
        base = self.tl.palette().color(QtGui.QPalette.ColorRole.Base)
        self.assertEqual(img.pixelColor(2, 2).name(), base.name(),
                         "时间线底色必须跟主题（以前写死白色，深色界面下就是一块白）")

    def test_hover_events_are_subscribed(self) -> None:
        """hoverMoveEvent 只有开了 WA_Hover 才会被投递，否则悬停看事件永远没反应。"""
        self.assertTrue(self.tl.testAttribute(QtCore.Qt.WidgetAttribute.WA_Hover))

    def test_set_span_ends_empty_state(self) -> None:
        self.load()
        self.assertFalse(self.tl.is_empty())


class TestEventMenu(_WindowCase):
    def test_click_near_events_opens_menu(self) -> None:
        self.load()
        seen = self.no_popup()
        click_at(self.tl, self.tl._x_of(12.0, self.tl.width()))
        self.assertEqual([round(x, 2) for x in seen], [12.0])

    def test_menu_lists_events_by_distance(self) -> None:
        self.load()
        menu = self.tl.event_menu(12.0)
        self.addCleanup(menu.deleteLater)
        items = [a for a in menu.actions() if isinstance(a.data(), dict)]
        self.assertEqual([a.data()["display"] for a in items], ["GPS 丢失", "进入 AUTO"],
                         "按离点击时刻的远近排，不是按时间排")
        self.assertIn("±5s", menu.actions()[0].text())
        self.assertIn("2 条事件", menu.actions()[0].text())
        self.assertFalse(menu.actions()[0].isEnabled(), "标题行只能看不能点")

    def test_menu_texts_carry_severity_and_offset(self) -> None:
        self.load()
        menu = self.tl.event_menu(12.0)
        self.addCleanup(menu.deleteLater)
        texts = [a.text() for a in menu.actions() if isinstance(a.data(), dict)]
        self.assertTrue(any("错误" in t and "+0.00s" in t for t in texts), texts)
        self.assertTrue(any("信息" in t and "-2.00s" in t for t in texts), texts)

    def test_no_events_nearby_returns_none_and_notices(self) -> None:
        self.load()
        menu = self.tl.event_menu(90.0)
        self.assertIsNone(menu)
        self.assertIn("没有事件", self.notes[-1])

    def test_ctrl_click_only_seeks(self) -> None:
        self.load()
        seen = self.no_popup()
        got: list[float] = []
        self.tl.timeChanged.connect(got.append)
        click_at(self.tl, self.tl._x_of(12.0, self.tl.width()),
                 mods=QtCore.Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(seen, [], "Ctrl+单击只定位，不打扰")
        self.assertEqual(len(got), 1)

    def test_long_text_is_trimmed(self) -> None:
        self.load([{"t": 5.0, "kind": "MSG", "severity": "warn", "display": "长" * 300}])
        menu = self.tl.event_menu(5.0)
        self.addCleanup(menu.deleteLater)
        line = next(a.text() for a in menu.actions() if isinstance(a.data(), dict))
        self.assertLess(len(line), 140, "菜单宽度不能被一条超长事件撑爆")
        self.assertTrue(line.endswith("…"), line[-8:])


class TestMenuResultDispatch(_WindowCase):
    def test_choosing_event_emits_it(self) -> None:
        self.load()
        menu = self.tl.event_menu(12.0)
        self.addCleanup(menu.deleteLater)
        pick = next(a for a in menu.actions() if isinstance(a.data(), dict) and a.data()["t"] == 12.0)
        self.tl.handle_menu_result(pick, 12.0, QtCore.QPoint())
        self.assertEqual(len(self.chosen), 1)
        self.assertEqual(self.chosen[0]["display"], "GPS 丢失")

    def test_jump_action_emits_empty_dict(self) -> None:
        self.load()
        menu = self.tl.event_menu(12.0)
        self.addCleanup(menu.deleteLater)
        pick = next(a for a in menu.actions() if a.data() is False)
        self.tl.handle_menu_result(pick, 12.0, QtCore.QPoint())
        self.assertEqual(self.chosen, [{}])

    def test_overflow_action_also_goes_to_table(self) -> None:
        many = [{"t": 50.0 + i * 0.01, "kind": "EV", "severity": "info", "display": f"e{i}"}
                for i in range(70)]
        self.load(many)
        menu = self.tl.event_menu(50.0)
        self.addCleanup(menu.deleteLater)
        over = next(a for a in menu.actions() if a.text().startswith("…另有"))
        self.assertIn("10 条", over.text(), over.text())
        self.tl.handle_menu_result(over, 50.0, QtCore.QPoint())
        self.assertEqual(self.chosen, [{}], "装不下的时候得把用户送到事件页")

    def test_window_choice_switches_and_reopens(self) -> None:
        self.load()
        menu = self.tl.event_menu(12.0)
        self.addCleanup(menu.deleteLater)
        sub = next(a.menu() for a in menu.actions() if a.menu())
        checked = [a.text() for a in sub.actions() if a.isChecked()]
        self.assertEqual(checked, ["± 5 秒"], "默认窗口要在子菜单里勾出来")
        seen = self.no_popup()
        one = next(a for a in sub.actions() if a.data() == 1.0)
        self.tl.handle_menu_result(one, 12.0, QtCore.QPoint(300, 300))
        pump(self.app, 40)
        self.assertAlmostEqual(self.tl.click_window(), 1.0)
        self.assertEqual([round(x, 2) for x in seen], [12.0], "换完窗口就地重开，别让用户再点一次")
        m2 = self.tl.event_menu(12.0)
        self.addCleanup(m2.deleteLater)
        self.assertEqual([a.data()["display"] for a in m2.actions() if isinstance(a.data(), dict)],
                         ["GPS 丢失"], "±1s 里只剩最近那条")

    def test_none_result_is_noop(self) -> None:
        self.load()
        self.tl.handle_menu_result(None, 12.0, QtCore.QPoint())
        self.assertEqual(self.chosen, [])
        self.assertAlmostEqual(self.tl.click_window(), 5.0)


class TestEventTableRowLookup(_WindowCase):
    def test_row_for_exact_and_fallback(self) -> None:
        R = type("R", (), {})
        recs = []
        for e in EVENTS:
            o = R()
            o.t, o.kind, o.severity, o.display = e["t"], e["kind"], e["severity"], e["display"]
            recs.append(o)
        self.w.event_table.set_events(recs)
        self.assertEqual(self.w.event_table.row_for(12.0, "ERR", "GPS 丢失"), 1)
        self.assertEqual(self.w.event_table.row_for(12.0, "XXX", "对不上"), 1, "精确不中就退到只认时间")
        self.assertEqual(self.w.event_table.row_for(float("nan")), -1)
        self.assertEqual(self.w.event_table.row_for(999.0), -1)

    def test_main_window_jumps_to_row(self) -> None:
        R = type("R", (), {})
        recs = []
        for e in EVENTS:
            o = R()
            o.t, o.kind, o.severity, o.display = e["t"], e["kind"], e["severity"], e["display"]
            recs.append(o)
        self.w.event_table.set_events(recs)
        self.load()
        self.w._on_timeline_event({"t": 50.0, "kind": "MSG", "display": "震动偏高"})
        self.assertEqual(self.w.tabs.tabText(self.w.tabs.currentIndex()), "事件")
        self.assertEqual(self.w.event_table.currentRow(), 2)
        self.assertIn("第 3 行", self.w.statusBar().currentMessage())
        self.w._on_timeline_event({})
        self.assertEqual(self.w.tabs.tabText(self.w.tabs.currentIndex()), "事件")
        self.w._on_timeline_event(None)
        self.w._on_timeline_event({"t": float("nan"), "kind": "EV", "display": "x"})


class TestTimelineStillHoverable(_WindowCase):
    def test_hover_badge_still_shows_tip(self) -> None:
        self.load()
        self.tl.set_regions([])
        self.tl.resize(1000, 96)
        pump(self.app, 6)
        self.tl.grab()                                   # 触发 paintEvent 重建命中表
        self.assertTrue(self.tl._badge_hits, "warn/error 应该画出感叹号徽标")
        hit = next((b for b in self.tl._badge_hits if any("震动偏高" in ln for ln in b[2])), None)
        self.assertIsNotNone(hit, [b[2] for b in self.tl._badge_hits])
        cx, cy, lines = hit
        self.assertTrue(any("震动偏高" in ln for ln in lines), lines)
        with mock.patch("apmlog.app.show_tip") as shown:
            self.tl.hoverMoveEvent(_Hover(QtCore.QPointF(cx, cy)))
        self.assertTrue(shown.called, "悬停徽标仍要弹提示")
        self.assertIn("震动偏高", shown.call_args[0][1], "提示内容还是那簇事件")

    def test_hover_off_badge_hides_tip(self) -> None:
        self.load()
        self.tl.resize(1000, 96)
        pump(self.app, 6)
        self.tl.grab()
        with mock.patch("apmlog.app.show_tip") as shown, \
                mock.patch.object(QtWidgets.QToolTip, "isVisible", staticmethod(lambda: True)), \
                mock.patch.object(QtWidgets.QToolTip, "hideText") as hidden:
            self.tl.hoverMoveEvent(_Hover(QtCore.QPointF(5.0, 90.0)))
        self.assertFalse(shown.called)
        self.assertTrue(hidden.called, "从徽标上移开要收掉提示")

    def test_leave_hides_a_sticky_tip(self) -> None:
        hidden: list[int] = []
        self.load()
        with mock.patch.object(QtWidgets.QToolTip, "isVisible", staticmethod(lambda: True)), \
                mock.patch.object(QtWidgets.QToolTip, "hideText",
                                  staticmethod(lambda: hidden.append(1))):
            QtWidgets.QApplication.sendEvent(self.tl, QtCore.QEvent(QtCore.QEvent.Type.Leave))
            pump(self.app, 5)
        self.assertEqual(hidden, [1], "提示可以“不自动消失”，离开时就必须收掉")
