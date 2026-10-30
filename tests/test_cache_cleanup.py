# -*- coding: utf-8 -*-
"""tests/test_cache_cleanup.py —— 解析缓存：占用统计 + 自动清理策略 + 设置持久化 + 界面。

覆盖四层：

1. 纯逻辑（``apmlog.core.cache``）：按天删、按容量删、保留最新一份、触发条件；
2. 设置：默认值、越界夹紧、写盘后重开仍在；
3. 设置界面：控件存在、自定义条件只在“自定义”下可用、清理完刷新占用；
4. 主窗启动：开了自动清理就在事件循环里跑一次，并把过期的缓存删掉。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m pytest tests/test_cache_cleanup.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets                                     # noqa: E402

from apmlog.core import cache as C                                # noqa: E402
from apmlog.ui import settings as st                              # noqa: E402

_APP: QtWidgets.QApplication | None = None
DAY = 86400.0


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 8) -> None:
    for _ in range(times):
        app.processEvents()


def make_entry(root: Path, name: str, size: int, age_days: float, now: float) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "index.json"
    f.write_bytes(b"x" * size)
    ts = now - age_days * DAY
    os.utime(f, (ts, ts))
    os.utime(d, (ts, ts))
    return d


class _TempCache(unittest.TestCase):
    def setUp(self):
        self.app = qapp()
        self.root = Path(tempfile.mkdtemp(prefix="apmlog-cache-"))
        self.now = time.time()
        self._prev_settings = st.Settings._instance
        self.sdir = Path(tempfile.mkdtemp(prefix="apmlog-cset-"))
        st.Settings.set_instance(st.Settings(self.sdir / "settings.json"))

    def tearDown(self):
        st.Settings.set_instance(self._prev_settings)
        pump(self.app, 2)


class TestCleanupLogic(_TempCache):
    def test_scan_counts_entries_files_bytes(self):
        make_entry(self.root, "a", 100, 1, self.now)
        make_entry(self.root, "b", 250, 2, self.now)
        usage = C.scan(self.root)
        self.assertEqual(usage.entries, 2)
        self.assertEqual(usage.files, 2)
        self.assertEqual(usage.bytes, 350)
        self.assertEqual([e.path.name for e in usage.items], ["b", "a"])   # 最久在前

    def test_scan_missing_dir_is_zero(self):
        usage = C.scan(self.root / "nope")
        self.assertEqual((usage.bytes, usage.files, usage.entries), (0, 0, 0))

    def test_age_policy_removes_only_stale(self):
        make_entry(self.root, "old", 100, 30, self.now)
        make_entry(self.root, "fresh", 200, 1, self.now)
        res = C.cleanup(self.root, max_age_days=7, now=self.now)
        self.assertEqual(res["removed_entries"], 1)
        self.assertEqual(res["freed"], 100)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"fresh"})

    def test_size_policy_evicts_lru_and_keeps_newest(self):
        make_entry(self.root, "oldest", 400, 10, self.now)
        make_entry(self.root, "middle", 400, 5, self.now)
        make_entry(self.root, "newest", 400, 0.1, self.now)   # 400*3=1200 > 800
        res = C.cleanup(self.root, max_bytes=800, now=self.now)
        left = sorted(p.name for p in self.root.iterdir())
        self.assertNotIn("oldest", left, "最久没用过的必须先走")
        self.assertIn("newest", left, "最新一份永远保留")
        self.assertLessEqual(res["after"], 800)
        self.assertGreaterEqual(res["removed_entries"], 1)

    def test_size_policy_never_drops_the_only_entry(self):
        make_entry(self.root, "only", 5000, 3, self.now)
        res = C.cleanup(self.root, max_bytes=100, now=self.now)
        self.assertEqual(res["removed_entries"], 0)
        self.assertTrue((self.root / "only").exists(), "唯一一份不该被清掉")

    def test_both_conditions(self):
        make_entry(self.root, "stale", 100, 20, self.now)
        make_entry(self.root, "big1", 900, 1, self.now)
        make_entry(self.root, "big2", 900, 2, self.now)
        res = C.cleanup(self.root, max_age_days=7, max_bytes=1000, now=self.now)
        left = sorted(p.name for p in self.root.iterdir())
        self.assertNotIn("stale", left)
        self.assertLessEqual(res["after"], 1000)

    def test_cleanup_empty_dir_is_noop(self):
        res = C.cleanup(self.root, max_age_days=1, now=self.now)
        self.assertEqual(res["removed_entries"], 0)
        self.assertEqual(res["freed"], 0)


class TestUsedAtReflectsReads(_TempCache):
    """“最后使用时间”必须是“最后一次**读**”，不是“最后一次写缓存”。

    否则一份天天打开、缓存一直有效的日志，会因为在 N 天内没重解析而被当成冷缓存删掉——
    那就违背了“只删长期没再读的冷缓存”这个原则。
    """

    def setUp(self):
        super().setUp()
        # cache_dir_for() 读的是 APMLOG_CACHE，不设就会摸到用户真实缓存目录
        self._env = os.environ.get("APMLOG_CACHE")
        os.environ["APMLOG_CACHE"] = str(self.root)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("APMLOG_CACHE", None)
        else:
            os.environ["APMLOG_CACHE"] = self._env
        super().tearDown()

    def _used_at(self, cdir: Path) -> float:
        for e in C.scan(self.root).items:
            if e.path == cdir:
                return e.used_at
        return 0.0

    def _fake_entry(self) -> tuple[Path, Path]:
        from apmlog.core import reader as rd
        log = self.root / "fake.BIN"
        log.write_bytes(b"\xa3\x95\x80\x80" + b"\x00" * 64)
        cdir, _log_id = rd.cache_dir_for(log)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "index.json").write_text("{}", encoding="utf-8")
        (cdir / "VIBE.npz").write_bytes(b"npz")
        return log, cdir

    def test_touch_cache_refreshes_use_time(self):
        from apmlog.core import reader as rd
        log, cdir = self._fake_entry()
        old = self.now - 30 * DAY
        for f in cdir.iterdir():
            os.utime(f, (old, old))
        self.assertLess(self._used_at(cdir), self.now - 10 * DAY)
        self.assertTrue(rd.touch_cache(log))
        self.assertAlmostEqual(self._used_at(cdir), time.time(), delta=5)

    def test_touch_cache_missing_log_is_false(self):
        from apmlog.core import reader as rd
        self.assertFalse(rd.touch_cache(self.root / "nope.BIN"))

    def test_cache_hit_marks_entry_as_used(self):
        """真走一遍 ``LogStore.open``：命中缓存时必须调 touch_cache，且不重解析。"""
        from apmlog.core import reader as rd
        from apmlog.core import store as core_store
        log, _cdir = self._fake_entry()
        touched: list[str] = []
        with mock.patch.object(rd, "is_fresh", return_value=True), \
                mock.patch.object(rd, "touch_cache", side_effect=lambda p: touched.append(str(p))), \
                mock.patch.object(rd, "build", side_effect=AssertionError("不该重建")):
            store_obj = core_store.LogStore.open(log)
        self.assertEqual(len(touched), 1, "命中缓存却没刷新使用时间")
        self.assertEqual(Path(touched[0]), log)
        self.assertIsNotNone(store_obj.stats)

    def test_read_makes_entry_non_cold(self):
        """读一次就不再是冷缓存：3 天策略不会再删它。"""
        from apmlog.core import reader as rd
        log, cdir = self._fake_entry()
        old = self.now - 10 * DAY
        for f in cdir.iterdir():
            os.utime(f, (old, old))
        res = C.cleanup(self.root, max_age_days=3, now=self.now)
        self.assertEqual(res["removed_entries"], 1, "未读前应属于冷缓存")

        log2, cdir2 = self._fake_entry()
        for f in cdir2.iterdir():
            os.utime(f, (old, old))
        rd.touch_cache(log2)
        res2 = C.cleanup(self.root, max_age_days=3, now=self.now)
        self.assertEqual(res2["removed_entries"], 0, "刚读过的不该被删")


class TestPolicy(_TempCache):
    def _s(self) -> st.Settings:
        return st.Settings.instance()

    def test_presets(self):
        s = self._s()
        s.set("cache_clean_policy", "7d")
        self.assertEqual(C.policy_conditions(s), (7.0, None, 7.0))
        s.set("cache_clean_policy", "3d")
        self.assertEqual(C.policy_conditions(s), (3.0, None, 3.0))
        s.set("cache_clean_policy", "1g")
        age, cap, interval = C.policy_conditions(s)
        self.assertIsNone(age)
        self.assertEqual(cap, 1024 * C.MB)
        self.assertIsNone(interval)

    def test_custom_days_only(self):
        s = self._s()
        s.set("cache_clean_policy", "custom")
        s.set("cache_custom_days", 2)
        s.set("cache_custom_mb", 0)
        self.assertEqual(C.policy_conditions(s), (2.0, None, 2.0))

    def test_custom_mb_only(self):
        s = self._s()
        s.set("cache_clean_policy", "custom")
        s.set("cache_custom_days", 0)
        s.set("cache_custom_mb", 300)
        self.assertEqual(C.policy_conditions(s), (None, 300 * C.MB, None))

    def test_not_due_when_disabled(self):
        make_entry(self.root, "old", 100, 99, self.now)
        self.assertFalse(C.is_due(self._s(), now=self.now, root=self.root))

    def test_interval_respected(self):
        s = self._s()
        s.set("cache_auto_clean", True)
        s.set("cache_clean_policy", "3d")
        make_entry(self.root, "old", 100, 99, self.now)
        s.set("cache_clean_last_ts", self.now - 1 * DAY)      # 1 天前跑过
        self.assertFalse(C.is_due(s, now=self.now, root=self.root))
        s.set("cache_clean_last_ts", self.now - 4 * DAY)
        self.assertTrue(C.is_due(s, now=self.now, root=self.root))

    def test_size_trigger(self):
        s = self._s()
        s.set("cache_auto_clean", True)
        s.set("cache_clean_policy", "custom")
        s.set("cache_custom_days", 0)
        s.set("cache_custom_mb", 1)                 # 1 MB 上限，便于构造
        make_entry(self.root, "a", 10, 0.1, self.now)
        self.assertFalse(C.is_due(s, now=self.now, root=self.root))
        big = self.root / "a" / "index.json"
        with open(big, "wb") as fh:
            fh.write(b"x" * (2 * C.MB))
        self.assertTrue(C.is_due(s, now=self.now, root=self.root))

    def test_run_if_due_records_timestamp_and_does_not_repeat(self):
        s = self._s()
        s.set("cache_auto_clean", True)
        s.set("cache_clean_policy", "7d")
        make_entry(self.root, "old", 100, 30, self.now)
        res = C.run_if_due(self.root, s, now=self.now)
        self.assertIsNotNone(res)
        self.assertEqual(res["removed_entries"], 1)
        self.assertAlmostEqual(float(s.get("cache_clean_last_ts")), self.now, places=1)
        self.assertEqual(int(s.get("cache_clean_last_freed")), 100)
        self.assertIsNone(C.run_if_due(self.root, s, now=self.now), "同一时刻不该再跑一次")

    def test_force_ignores_schedule(self):
        s = self._s()
        s.set("cache_auto_clean", False)
        make_entry(self.root, "old", 100, 30, self.now)
        res = C.run_if_due(self.root, s, now=self.now, force=True)
        self.assertIsNotNone(res)


class TestSettingsPersistence(_TempCache):
    def test_defaults(self):
        self.assertFalse(st.DEFAULTS["cache_auto_clean"])
        self.assertEqual(st.DEFAULTS["cache_clean_policy"], "7d")

    def test_values_survive_reload(self):
        s = st.Settings.instance()
        s.set("cache_auto_clean", True)
        s.set("cache_clean_policy", "custom")
        s.set("cache_custom_days", 5)
        s.set("cache_custom_mb", 512)
        again = st.Settings(Path(s.path))
        self.assertTrue(again.get("cache_auto_clean"))
        self.assertEqual(again.get("cache_clean_policy"), "custom")
        self.assertEqual(int(again.get("cache_custom_days")), 5)
        self.assertEqual(int(again.get("cache_custom_mb")), 512)

    def test_ranges_clamped(self):
        s = st.Settings.instance()
        s.set("cache_custom_days", 9999)
        s.set("cache_custom_mb", -5)
        self.assertLessEqual(int(s.get("cache_custom_days")), 365)
        self.assertGreaterEqual(int(s.get("cache_custom_mb")), 0)


class TestSettingsUi(_TempCache):
    def setUp(self):
        super().setUp()
        self._env = os.environ.get("APMLOG_CACHE")
        os.environ["APMLOG_CACHE"] = str(self.root)
        make_entry(self.root, "old", 4000, 40, self.now)
        make_entry(self.root, "new", 1000, 0.2, self.now)
        from apmlog.ui.settings import SettingsDialog
        self.dlg = SettingsDialog()

    def tearDown(self):
        self.dlg.close()
        self.dlg.deleteLater()
        pump(self.app, 4)
        if self._env is None:
            os.environ.pop("APMLOG_CACHE", None)
        else:
            os.environ["APMLOG_CACHE"] = self._env
        super().tearDown()

    def _wait_idle(self) -> None:
        for _ in range(200):
            pump(self.app, 2)
            if not self.dlg._janitor.busy():
                pump(self.app, 4)
                return
            time.sleep(0.02)
        self.fail("后台清理一直没结束")

    def test_controls_exist_and_follow_policy(self):
        d = self.dlg
        self.assertIn("cache_auto_clean", d._widgets)
        self.assertIsInstance(d.cb_cache_policy, QtWidgets.QComboBox)
        self.assertFalse(d.sp_cache_days.isEnabled(), "非自定义时自定义条件应置灰")
        d.cb_cache_policy.setCurrentIndex(d.cb_cache_policy.findData("custom"))
        pump(self.app)
        self.assertTrue(d.sp_cache_days.isEnabled())
        self.assertTrue(d.sp_cache_mb.isEnabled())

    def test_usage_label_shows_current_size(self):
        text = self.dlg.lbl_cache.text()
        self.assertIn("当前占用", text)
        self.assertIn("2 份缓存", text)

    def test_enable_and_clean_refreshes_label(self):
        d = self.dlg
        d._widgets["cache_auto_clean"].setChecked(True)     # 触发一次检查（7d）
        pump(self.app)
        if d._janitor.busy():
            self._wait_idle()
        else:
            d._run_cache_clean(force=True)
            self._wait_idle()
        left = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(left, ["new"], "40 天没用的那份应被清掉")
        self.assertIn("当前占用", d.lbl_cache.text())
        self.assertIn("1 份缓存", d.lbl_cache.text())
        self.assertTrue("已清理" in d.lbl_clean.text() or "未开启" in d.lbl_clean.text())

    def test_manual_clean_button_works_when_auto_off(self):
        self.assertFalse(bool(st.Settings.instance().get("cache_auto_clean")))
        self.dlg._run_cache_clean(force=True)
        self._wait_idle()
        self.assertFalse((self.root / "old").exists())


class TestMainWindowAutoClean(_TempCache):
    def test_startup_cleans_when_enabled(self):
        from apmlog.app import MainWindow
        self._env = os.environ.get("APMLOG_CACHE")
        os.environ["APMLOG_CACHE"] = str(self.root)
        make_entry(self.root, "old", 3000, 30, self.now)
        make_entry(self.root, "fresh", 1000, 0.5, self.now)
        s = st.Settings.instance()
        s.set("cache_auto_clean", True)
        s.set("cache_clean_policy", "7d")
        win = MainWindow()
        win._views_path = self.sdir / "views.json"
        try:
            for _ in range(300):
                pump(self.app, 2)
                if not win.cache_janitor.busy() and not (self.root / "old").exists():
                    break
                time.sleep(0.02)
            self.assertFalse((self.root / "old").exists(), "启动时应把过期缓存清掉")
            self.assertTrue((self.root / "fresh").exists())
        finally:
            win.close()
            win.deleteLater()
            pump(self.app, 4)
            if self._env is None:
                os.environ.pop("APMLOG_CACHE", None)
            else:
                os.environ["APMLOG_CACHE"] = self._env

    def test_startup_does_nothing_when_disabled(self):
        from apmlog.app import MainWindow
        self._env = os.environ.get("APMLOG_CACHE")
        os.environ["APMLOG_CACHE"] = str(self.root)
        make_entry(self.root, "old", 3000, 30, self.now)
        win = MainWindow()
        win._views_path = self.sdir / "views.json"
        try:
            pump(self.app, 20)
            time.sleep(0.2)
            pump(self.app, 10)
            self.assertTrue((self.root / "old").exists(), "没开自动清理时不许删东西")
        finally:
            win.close()
            win.deleteLater()
            pump(self.app, 4)
            if self._env is None:
                os.environ.pop("APMLOG_CACHE", None)
            else:
                os.environ["APMLOG_CACHE"] = self._env


if __name__ == "__main__":
    unittest.main(verbosity=2)
