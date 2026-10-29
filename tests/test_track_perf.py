"""大日志下的交互性能：折线抽稀/切块 + 瓦片请求不许阻塞 UI 线程。

背景（用户 2026-10-24，69 MB / 170 万条报文日志）：
* 一份日志 4 条轨迹、11 万段折线，整屏重绘 615 ms，拖地图一顿一顿；
* 真凶之一是瓦片请求：`QNetworkAccessManager.get()` 是**同步**调用（实测 19 ms/次），
  老的 `_pump()` 在一个 while 里连发 6 个，一次滚动就在 UI 线程吞掉 100 ms 以上，
  实测事件循环最大停顿 608 ms。

这里把两条约束钉死：
1. 折线必须按屏幕距离抽稀、并把每条 path 切块（否则 Qt 无法按包围盒裁掉视口外的段）；
2. 瓦片请求**每轮事件循环只能发一个**，且并发上限照旧生效。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                                    # noqa: E402
from PySide6 import QtCore, QtWidgets                                 # noqa: E402

from apmlog.core.model import TrackResult                             # noqa: E402
from apmlog.ui.trackmap import TrackMapWidget                         # noqa: E402


def qapp() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(app, ms: int = 80) -> None:
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        app.processEvents()
        QtCore.QThread.msleep(5)


def turn(app, n: int = 1) -> None:
    """精确跑 n 轮事件循环（0ms 定时器一轮只发一个瓦片请求）。"""
    for _ in range(n):
        app.processEvents()
        QtCore.QThread.msleep(2)


def big_track(n: int = 40000) -> TrackResult:
    """一条来回折返的长航迹（模拟实测里 6 万点的 VISP）。"""
    t = np.linspace(0.0, 900.0, n)
    k = np.sin(t / 30.0)
    lat = 38.96 + 0.004 * k
    lon = 117.31 + 0.006 * np.cos(t / 45.0)
    return TrackResult(kind="geodetic", t=t, lat=lat, lon=lon,
                       alt=np.linspace(20, 120, n),
                       e=(lon - lon[0]) * 85000.0, n=(lat - lat[0]) * 111000.0,
                       u=np.linspace(20, 120, n), speed=np.full(n, 6.0),
                       src=np.zeros(n, dtype=np.int8), src_names=["POS"],
                       origin=(lat[0], lon[0], 0.0), home=(lat[0], lon[0], 0.0),
                       distance=3000.0, max_speed=9.0)



def _paths(layer):
    """该层里的折线图元（层里还混着轨迹名称标签，计数时要排除）。"""
    return [it for it in layer.items if isinstance(it, QtWidgets.QGraphicsPathItem)]

class TrackCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def _map(self):
        m = TrackMapWidget(source_name="osm")
        m.resize(1000, 700)
        m.show()
        pump(self.app, 60)
        self.addCleanup(lambda: (m.close(), pump(self.app, 20)))
        return m

    @staticmethod
    def _elements(layer) -> int:
        return sum(it.path().elementCount() for it in layer.items if hasattr(it, "path"))


class TestThinMask(TrackCase):
    def test_keeps_first_and_last(self) -> None:
        xs = np.arange(10, dtype=float)
        ys = np.zeros(10)
        keep = TrackMapWidget._thin_mask(xs, ys, 3.0)
        self.assertTrue(keep[0])
        self.assertTrue(keep[-1])

    def test_respects_min_distance(self) -> None:
        m = self._map()
        xs = np.arange(0, 1000, 1.0)          # 每点相距 1
        ys = np.zeros_like(xs)
        keep = m._thin_mask(xs, ys, 10.0)     # 步长 10
        kept = xs[keep]
        self.assertTrue(len(kept) < 120, f"抽稀后仍有 {len(kept)} 点")
        # 末点是**强制保留**的，所以最后一段可能短于步长；其余必须满足
        gaps = np.diff(kept)[:-1]
        self.assertTrue(np.all(gaps >= 10.0 - 1e-9), f"有间隔小于步长：{gaps.min()}")

    def test_empty_and_single_are_safe(self) -> None:
        m = self._map()
        self.assertEqual(m._thin_mask(np.empty(0), np.empty(0), 2.0).size, 0)
        one = m._thin_mask(np.array([1.0]), np.array([1.0]), 2.0)
        self.assertEqual(int(one.sum()), 1)

    def test_duplicate_points_collapse(self) -> None:
        m = self._map()
        xs = np.zeros(100)
        ys = np.zeros(100)
        keep = m._thin_mask(xs, ys, 2.0)
        self.assertLessEqual(int(keep.sum()), 2, "全同点只该留首尾")


class TestChunkedDrawing(TrackCase):
    def test_long_track_is_chunked_and_thinned(self) -> None:
        m = self._map()
        m.set_track(big_track(40000))
        pump(self.app, 120)
        n_items = len(list(m._layer_track.items))
        n_el = self._elements(m._layer_track)
        self.assertGreater(n_items, 5, "这么长的轨迹必须切块，否则每帧全量描边")
        self.assertLess(n_el, 40000 * 0.6, f"抽稀没生效：{n_el} 元素")
        for it in m._layer_track.items:
            self.assertLessEqual(it.path().elementCount(), m.CHUNK_SEGMENTS + 2,
                                 "单条 path 段数超过切块上限")

    def test_secondary_track_is_not_one_monolithic_path(self) -> None:
        """回归：副轨迹以前整条一个 path，Qt 无法裁剪 → 一帧 500 ms。"""
        m = self._map()
        a = big_track(30000)
        b = big_track(30000)
        b.lat = b.lat + 0.002
        m.set_tracks([a, b])
        pump(self.app, 150)
        items = list(_paths(m._layer_track2))
        self.assertGreater(len(items), 3, "副轨迹也要切块")
        for it in items:
            self.assertLessEqual(it.path().elementCount(), m.CHUNK_SEGMENTS + 2)

    def test_zoom_in_schedules_a_rebuild(self) -> None:
        m = self._map()
        m.set_track(big_track(5000))
        pump(self.app, 100)
        # 载入时已经按最终缩放抽稀过，不该有待重建
        m._rebuild.stop()
        built = m._built_k
        self.assertGreater(built, 0)
        m._maybe_rebuild_tracks()
        self.assertFalse(m._rebuild.isActive(), "没放大就不该重建")
        m.view.scale(2.0, 2.0)                       # 放大一倍，抽稀步长不够细了
        m._maybe_rebuild_tracks()
        self.assertTrue(m._rebuild.isActive(), "放大后要安排一次重建")

    def test_zoom_out_never_rebuilds(self) -> None:
        m = self._map()
        m.set_track(big_track(5000))
        pump(self.app, 100)
        m._rebuild.stop()
        m.view.scale(0.4, 0.4)
        m._maybe_rebuild_tracks()
        self.assertFalse(m._rebuild.isActive(), "缩小只会更密，不需要重建")

    def test_load_leaves_no_pending_rebuild(self) -> None:
        """回归：以前先画后 fit，_built_k 立刻过期 → 载入 400ms 后自己又重建一次（多一次卡顿）。"""
        m = self._map()
        m.set_tracks([big_track(8000), big_track(8000)])
        pump(self.app, 60)
        self.assertFalse(m._rebuild.isActive(),
                         "载入完成后不该还挂着一次重建")


class FakeReply:
    """给 TileStore 当 QNetworkReply 的替身：只需要可哈希。"""


class _Sig:
    def disconnect(self, *_a) -> None:
        pass


class FakeMgr:
    """替身管理器：只记录 get 调用，永不完成（这样在途数量才能稳定观察）。"""

    finished = _Sig()

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def get(self, req):
        self._sink.append(req.url().toString())
        return FakeReply()


class TilePumpCase(unittest.TestCase):
    """瓦片请求必须每轮事件循环只发一个。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = qapp()

    def setUp(self) -> None:
        import tempfile
        from apmlog.ui._tiles import BUILTIN_SOURCES, TileStore
        self.dir = tempfile.mkdtemp(prefix="tiles_")
        self.store = TileStore(cache_dir=self.dir, max_concurrent=6)
        self.store.set_online(True)
        self.src = BUILTIN_SOURCES[0]
        self.calls: list[str] = []
        self.store._mgr = FakeMgr(self.calls)     # type: ignore[assignment]
        self.addCleanup(self.store.shutdown)

    def _request_many(self, n: int = 10) -> None:
        for i in range(n):
            self.store.request(self.src, 15, 100 + i, 200)

    def test_requests_are_spread_over_event_turns(self) -> None:
        self._request_many(10)
        self.assertEqual(len(self.calls), 0, "request() 内部不许同步发请求")
        turn(self.app)                        # 让 0ms 定时器跑一轮
        self.assertEqual(len(self.calls), 1, f"一轮只该发一个，实际 {len(self.calls)}")
        for expect in (2, 3, 4):
            turn(self.app)
            self.assertEqual(len(self.calls), expect)

    def test_concurrency_cap_still_applies(self) -> None:
        self._request_many(10)
        for _ in range(12):
            turn(self.app)
        self.assertEqual(len(self.calls), 6, "在途上限 6，不能无限发")

    def test_cancel_stops_pending_pumps(self) -> None:
        self._request_many(10)
        turn(self.app)
        n0 = len(self.calls)
        self.store.cancel_all()
        for _ in range(5):
            turn(self.app)
        self.assertEqual(len(self.calls), n0, "取消后不该再发新请求")

    def test_shutdown_is_safe_with_a_pending_pump(self) -> None:
        self._request_many(4)
        self.store.shutdown()
        for _ in range(5):
            turn(self.app)                    # 迟到的定时器不能炸
        self.assertEqual(len(self.calls), 0)

    def test_duplicate_requests_are_deduped(self) -> None:
        for _ in range(3):
            self.store.request(self.src, 15, 100, 200)
        for _ in range(4):
            turn(self.app)
        self.assertEqual(len(self.calls), 1, "同一张瓦片只该请求一次")


if __name__ == "__main__":
    unittest.main()
