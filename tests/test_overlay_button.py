# -*- coding: utf-8 -*-
"""tests/test_overlay_button.py —— 曲线页「叠加绘制」按钮。

用户 2026-10-24：「在曲线位置加个"叠加绘制"功能」。

要验清的事：
1. 通道树支持多选，且**按显示顺序**给出选中键（选中顺序不可靠）；
2. 对话框：左树预选、搜索过滤、新建泳道至少 2 条、可改成"叠加到已有泳道"；
3. 新建分组 → 一条泳道 + 若干同轴叠加（颜色各不相同、默认显示）；
4. 叠加到已有泳道 → 只加曲线不重建；重建泳道后要能补回来；
5. 视图记忆：分组与"叠加到已有泳道"的曲线要能存/取/清。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                             # noqa: E402
from PySide6 import QtCore, QtWidgets                          # noqa: E402

from apmlog.core.model import ChannelInfo, ChannelKey, Series   # noqa: E402
from apmlog.ui.overlaydialog import (MIN_NEW_CHANNELS, NEW_LANE,   # noqa: E402
                                     OverlayDialog)
from apmlog.ui.seriesplot import OVERLAY_COLORS, LaneSpec, SeriesPlot   # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app, times: int = 6) -> None:
    for _ in range(times):
        app.processEvents()


CHANNELS = [
    ("ATT.Roll", "ATT.Roll"),
    ("ATT.Pitch", "ATT.Pitch"),
    ("ATT.DesRoll", "ATT.DesRoll"),
    ("BARO.Alt", "BARO.Alt"),
    ("RCOU.C1", "RCOU.C1 [#0]"),
    ("RCOU.C5", "RCOU.C5 [#0]"),
]


class TestOverlayDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _dlg(self, lanes=("ATT.Roll",), pre=()):
        d = OverlayDialog(CHANNELS, list(lanes), list(pre))
        d.show()
        pump(self.app)
        self.addCleanup(d.close)
        return d

    def test_preselected_channels_are_checked(self) -> None:
        d = self._dlg(pre=[ChannelKey.parse("ATT.Roll"), ChannelKey.parse("ATT.DesRoll")])
        self.assertEqual(d.checked_keys(), ["ATT.Roll", "ATT.DesRoll"])

    def test_new_lane_needs_at_least_two_channels(self) -> None:
        d = self._dlg(pre=[ChannelKey.parse("ATT.Roll")])
        self.assertEqual(d.target(), NEW_LANE)
        self.assertTrue(d.is_new_lane())
        self.assertFalse(d._ok_enabled(), "只选 1 条时不该让你新建泳道")
        d.list.item(2).setCheckState(QtCore.Qt.CheckState.Checked)   # + ATT.DesRoll
        pump(self.app)
        self.assertTrue(d._ok_enabled())

    def test_switching_target_relaxes_the_requirement(self) -> None:
        d = self._dlg(pre=[ChannelKey.parse("BARO.Alt")])
        d.cb_target.setCurrentIndex(1)                     # 叠加到 ATT.Roll
        pump(self.app)
        self.assertEqual(d.target(), "ATT.Roll")
        self.assertTrue(d._ok_enabled(), "叠到已有泳道，1 条就够")

    def test_search_filters_the_list(self) -> None:
        d = self._dlg(pre=[ChannelKey.parse("ATT.Roll")])
        d.search.setText("rcou")
        pump(self.app)
        self.assertEqual(d.checked_keys(), [], "被过滤掉的不算选中")
        d.search.setText("ATT.D")
        pump(self.app)
        self.assertIn("ATT.DesRoll", [it.text() for it, _k in d._items if not it.isHidden()])

    def test_target_lists_existing_lanes_in_order(self) -> None:
        d = self._dlg(lanes=("ATT.Roll", "BARO.Alt"))
        data = [d.cb_target.itemData(i) for i in range(d.cb_target.count())]
        self.assertEqual(data, [NEW_LANE, "ATT.Roll", "BARO.Alt"])

    def test_min_new_is_two(self) -> None:
        self.assertEqual(MIN_NEW_CHANNELS, 2)


class _FakeStore:
    """只提供 _spec_for_key / _group_lane_specs 需要的 series()。"""

    def __init__(self, series: dict, t0=0.0, t1=100.0):
        self._s = series
        self.meta = {"stats": {"t_start": t0, "t_end": t1}}

    def channel_infos(self):
        out = []
        for key, (t, v) in self._s.items():
            k = ChannelKey.parse(key)
            a = np.asarray(v, float)
            f = a[np.isfinite(a)]
            out.append(ChannelInfo(key=k, count=int(a.size), t0=float(t[0]), t1=float(t[-1]),
                                   vmin=float(f.min()) if f.size else float("nan"),
                                   vmax=float(f.max()) if f.size else float("nan")))
        return out

    def series(self, key, t0=None, t1=None, max_points=None, drop_nan=True):
        k = key if isinstance(key, ChannelKey) else ChannelKey.parse(key)
        t, v = self._s[k.key]
        return Series(k, np.asarray(t, float), np.asarray(v, float),
                      unit="°" if k.type == "ATT" else "")


class TestAppOverlayWiring(unittest.TestCase):
    """MainWindow 侧：分组 → LaneSpec、叠到已有泳道、视图记忆。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        from apmlog.app import MainWindow
        from apmlog.ui import settings as st
        self.dir = tempfile.mkdtemp(prefix="ovb_")
        self._prev = st.Settings._instance
        st.Settings.set_instance(st.Settings(Path(self.dir) / "settings.json"))
        self.addCleanup(lambda: st.Settings.set_instance(self._prev))
        self.w = MainWindow()
        self.addCleanup(self.w.close)
        self._views = Path(self.dir) / "views.json"
        self.w._views_path = lambda: self._views

    def _store(self):
        t = np.linspace(0.0, 100.0, 400)
        return _FakeStore({"ATT.Roll": (t, np.sin(t)),
                           "ATT.DesRoll": (t, np.sin(t) + 1.0),
                           "ATT.DesPitch": (t, np.cos(t)),
                           "BARO.Alt": (t, 30.0 + np.cos(t))})

    def test_spec_for_key_builds_label_and_key(self) -> None:
        self.w.store = self._store()
        spec = self.w._spec_for_key(ChannelKey.parse("ATT.Roll"), 0.0, 100.0)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.label, "ATT.Roll")
        self.assertEqual(spec.key, "ATT.Roll")
        self.assertEqual(spec.unit, "°")

    def test_spec_for_key_without_store_is_none(self) -> None:
        self.w.store = None
        self.assertIsNone(self.w._spec_for_key(ChannelKey.parse("ATT.Roll"), 0.0, 1.0))

    def test_group_becomes_one_lane_with_overlays(self) -> None:
        self.w.store = self._store()
        self.w._groups = [[ChannelKey.parse("ATT.Roll"), ChannelKey.parse("ATT.DesRoll"),
                           ChannelKey.parse("ATT.DesPitch")]]
        lanes = self.w._group_lane_specs((0.0, 100.0))
        self.assertEqual(len(lanes), 1, "一个分组=一条泳道，不是三条")
        head = lanes[0]
        self.assertEqual(head.label, "ATT.Roll")
        self.assertEqual([o.label for o in head.overlays], ["ATT.DesRoll", "ATT.DesPitch"])
        self.assertFalse(any(o.hidden for o in head.overlays), "手动叠加的默认就该显示")
        self.assertEqual([o.color for o in head.overlays],
                         [OVERLAY_COLORS[0], OVERLAY_COLORS[1]], "同一道里的叠加色要区分开")
        self.assertTrue(all(o.unit == "°" for o in head.overlays))

    def test_broken_group_head_is_reported_not_crashing(self) -> None:
        self.w.store = self._store()
        self.w._groups = [[ChannelKey.parse("ATT.DoesNotExist"), ChannelKey.parse("ATT.Roll")]]
        skipped: list[str] = []
        lanes = self.w._group_lane_specs((0.0, 100.0), skipped)
        self.assertEqual(lanes, [])
        self.assertTrue(skipped and "主曲线取数失败" in skipped[0])

    def test_add_group_only_needs_two(self) -> None:
        self.w.store = self._store()
        calls: list[dict] = []
        self.w._apply = lambda **kw: calls.append(kw)
        self.w._save_view = lambda: None
        self.w._add_overlay_group([ChannelKey.parse("ATT.Roll")])
        self.assertEqual(self.w._groups, [], "一条通道不构成叠加")
        self.assertFalse(calls)
        self.w._add_overlay_group([ChannelKey.parse("ATT.Roll"), ChannelKey.parse("ATT.DesRoll")])
        self.assertEqual(len(self.w._groups), 1)
        self.assertTrue(calls)

    def test_add_overlay_to_existing_lane_keeps_lanes(self) -> None:
        self.w.store = self._store()
        t = np.linspace(0.0, 100.0, 400)
        base = LaneSpec(label="ATT.Roll", color=(31, 119, 180), unit="°", t=t, y=np.sin(t))
        self.w.plot.set_lanes([base])
        pump(self.app)
        self.assertEqual(self.w.plot.lane_labels(), ["ATT.Roll"])
        self.w._add_lane_overlays("ATT.Roll", [ChannelKey.parse("ATT.DesPitch")])
        pump(self.app)
        self.assertEqual(len(self.w.plot._lanes), 1, "叠加不该新增泳道")
        self.assertEqual([g.label for g in self.w.plot._lanes[0].ghosts], ["ATT.DesPitch"])
        self.assertEqual(len(self.w.plot._chips), 2, "叠加曲线要有自己的胶囊")
        self.assertEqual(self.w._lane_overlays[0][0], "ATT.Roll")

    def test_add_overlay_to_missing_lane_says_so(self) -> None:
        self.w.store = self._store()
        t = np.linspace(0.0, 100.0, 10)
        self.w.plot.set_lanes([LaneSpec(label="X", color=(1, 2, 3), t=t, y=np.zeros(t.size))])
        pump(self.app)
        self.w._add_lane_overlays("不存在的道", [ChannelKey.parse("ATT.Roll")])
        self.assertIn("没找到泳道", self.w.statusBar().currentMessage())
        self.assertEqual(self.w._lane_overlays, [])

    def test_views_memory_roundtrip(self) -> None:
        self.w.store = self._store()
        self.w._groups = [[ChannelKey.parse("ATT.Roll"), ChannelKey.parse("ATT.DesRoll")]]
        self.w._lane_overlays = [("BARO.Alt", [ChannelKey.parse("ATT.DesPitch")])]
        self.w._save_view()
        data = json.loads(self._views.read_text(encoding="utf-8"))
        self.assertEqual(data["channel_groups"], [["ATT.Roll", "ATT.DesRoll"]])
        self.assertEqual(data["lane_overlays"], [["BARO.Alt", ["ATT.DesPitch"]]])
        # 清空后从磁盘恢复
        self.w._groups = []
        self.w._lane_overlays = []
        self.w._restore_view()
        self.assertEqual([[k.key for k in g] for g in self.w._groups],
                         [["ATT.Roll", "ATT.DesRoll"]])
        self.assertEqual([(l, [k.key for k in ks]) for l, ks in self.w._lane_overlays],
                         [("BARO.Alt", ["ATT.DesPitch"])])

    def test_forget_clears_groups_and_overlays(self) -> None:
        self.w.store = None                     # 只需验证状态被清空
        self.w._groups = [[ChannelKey.parse("ATT.Roll"), ChannelKey.parse("ATT.DesRoll")]]
        self.w._lane_overlays = [("BARO.Alt", [ChannelKey.parse("ATT.Pitch")])]
        self.w._forget_view()
        self.assertEqual(self.w._groups, [])
        self.assertEqual(self.w._lane_overlays, [])

    def test_groups_are_filtered_when_channels_vanish(self) -> None:
        store = _FakeStore({"ATT.Roll": (np.arange(10.0), np.zeros(10))})
        self.w.store = store
        self.w._groups = [[ChannelKey.parse("ATT.Roll"), ChannelKey.parse("VIBE.VibeX")]]
        self.w._lane_overlays = [("ATT.Roll", [ChannelKey.parse("VIBE.VibeX")])]
        self.w._restore_extra_channels()
        self.assertEqual(self.w._groups, [], "只剩一条就不算叠加组")
        self.assertEqual(self.w._lane_overlays, [])


class TestTreeMultiSelect(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def test_selection_mode_is_extended(self) -> None:
        from apmlog.app import _ChannelTreeDock
        d = _ChannelTreeDock()
        self.assertEqual(d.tree.selectionMode(),
                         QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)

    def test_selected_keys_follow_tree_order(self) -> None:
        from apmlog.app import _ChannelTreeDock
        d = _ChannelTreeDock()
        d.set_store(_FakeStore({"ATT.Pitch": (np.arange(5.0), np.zeros(5)),
                                "ATT.Roll": (np.arange(5.0), np.zeros(5))}))
        top = d.tree.topLevelItem(0)
        # 先选 Roll 再选 Pitch，返回顺序应按树里显示顺序（Pitch 在 Roll 前）
        for j in range(top.childCount()):
            top.child(j).setSelected(True)
        keys = d.selected_keys()
        self.assertEqual([k.key for k in keys],
                         [top.child(j).data(0, QtCore.Qt.ItemDataRole.UserRole)
                          for j in range(top.childCount())])


if __name__ == "__main__":
    unittest.main()
