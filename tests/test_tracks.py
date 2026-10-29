# -*- coding: utf-8 -*-
"""tests/test_tracks.py —— 多轨迹数据层（POS / GPS#每个接收机 / XKF1 / VISP）。

为什么有这批测试：以前 ``reader.build_track()`` 按优先级**只挑一个**位置源，地图永远
只有一条线。真机实测（112 份日志）里 POS（EKF 融合位置）与 GPS（原始 GNSS）逐点最大
偏差 76.5 m，在地图上就是两条明显不同的线——Mission Planner 会同时画。所以：

* 每个可用源必须**各自成一条轨迹**并各自做有效性过滤（POS 干净不等于 GPS 干净）；
* 只有 **POS/GPS** 按接收机实例拆分。XKF1 的 ``C`` 是 EKF 核号（实测恒为 1），
  按它拆会得到语义错误的 ``XKF1#1``；
* XKF1/VISP 是本地 NED，**没有 ORGN 就绝不输出轨迹**——把米当经纬度画是灾难；
* 主轨迹（光标/里程/状态栏跟的那条）语义必须与历史完全一致。

真日志只读，缓存一律写进 ``tempfile``。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_tracks.py -q
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                             # noqa: E402

from apmlog.core import reader as RD                           # noqa: E402
from apmlog.core.model import TrackResult                      # noqa: E402
from apmlog.core.store import LogStore                         # noqa: E402

# 真日志（只读）。缺任何一份就 skip 对应类，不伪造结果。
LOG_A = Path(r"C:\Users\MAKI\Documents\Mission Planner\logs\QUADROTOR\1\2026-10-16 16-59-28.bin")
LOG_B = Path(r"C:\Users\MAKI\Documents\Mission Planner\logs\QUADROTOR\1\2026-10-23 15-37-41.bin")
LOG_C = Path(r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000165.BIN")

_CACHE: dict[str, tuple[LogStore, float, float, str]] = {}    # path -> (store, 冷, 热, tmpdir)


def cached_store(path: Path) -> tuple[LogStore, float, float]:
    """同一份日志只解析一次：冷解析要 8~9 秒，逐用例重跑会把测试拖死。"""
    key = str(path)
    if key not in _CACHE:
        d = tempfile.mkdtemp(prefix="apmlog_tracks_")
        t0 = time.time()
        st = LogStore.open(str(path), cache_dir=d)
        cold = time.time() - t0
        t1 = time.time()
        st2 = LogStore.open(str(path), cache_dir=d)
        hot = time.time() - t1
        st.close()
        _CACHE[key] = (st2, cold, hot, d)
    st, cold, hot, _ = _CACHE[key]
    return st, cold, hot


def tearDownModule() -> None:
    for _st, _c, _h, d in _CACHE.values():
        shutil.rmtree(d, ignore_errors=True)


def sep_meters(a: TrackResult, b: TrackResult) -> float:
    """把 a 插值到 b 的时间戳上，返回两者最大水平间距（米）。"""
    t = np.clip(b.t, a.t[0], a.t[-1])
    lat = np.interp(t, a.t, a.lat)
    lon = np.interp(t, a.t, a.lon)
    kx = 111320.0 * math.cos(math.radians(float(np.mean(lat))))
    return float(np.max(np.hypot((lat - b.lat) * 110574.0, (lon - b.lon) * kx)))


# --------------------------------------------------------------------------- #
@unittest.skipUnless(LOG_A.exists(), f"缺少真日志 {LOG_A.name}")
class TestMultiTrack(unittest.TestCase):
    """核心：一份同时有 POS/GPS/XKF1/VISP 的真日志必须给出多条可选轨迹。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.store, cls.cold, cls.hot = cached_store(LOG_A)
        cls.tracks = cls.store.tracks()

    def test_returns_more_than_one_track(self) -> None:
        self.assertGreaterEqual(len(self.tracks), 2, "POS 与 GPS 应各自成一条")
        keys = [t.key for t in self.tracks]
        self.assertIn("POS", keys)
        self.assertIn("GPS#0", keys)

    def test_primary_comes_first_and_matches_history(self) -> None:
        """主轨迹排第一，且 == 不带参数调用 track() == 旧的 track.npz 语义。"""
        first = self.tracks[0]
        self.assertEqual(first.key, "POS")
        same = self.store.track()
        self.assertEqual(same.key, first.key)
        self.assertEqual(same.n_points, first.n_points)
        self.assertTrue(np.array_equal(same.lat, first.lat))
        self.assertTrue(np.array_equal(same.lon, first.lon))
        self.assertAlmostEqual(same.distance, first.distance, places=3)

    def test_pos_and_gps_are_genuinely_different_lines(self) -> None:
        """本功能存在的意义：两条线不能是同一份数据的复制。"""
        by = {t.key: t for t in self.tracks}
        d = sep_meters(by["POS"], by["GPS#0"])
        print(f"\n  实测 POS 与 GPS#0 最大间距 {d:.1f} m")
        self.assertGreater(d, 20.0, f"POS 与 GPS#0 间距只有 {d:.1f} m，多轨迹没意义")

    def test_every_track_is_well_formed(self) -> None:
        for t in self.tracks:
            with self.subTest(key=t.key):
                self.assertTrue(t.key, "key 不能空")
                self.assertTrue(t.label, "label 不能空（勾选栏直接显示它）")
                self.assertGreaterEqual(t.n_points, 5)
                self.assertEqual(t.kind, "geodetic", "这份日志有 ORGN，本地源应被投影")
                la = t.lat[np.isfinite(t.lat)]
                lo = t.lon[np.isfinite(t.lon)]
                self.assertEqual(la.size, t.n_points, "geodetic 轨迹不该有 NaN 经纬度")
                self.assertTrue(np.all(np.abs(la) <= 90.0), f"{t.key} 纬度越界")
                self.assertTrue(np.all(np.abs(lo) <= 180.0), f"{t.key} 经度越界")
                # 荒谬值哨兵：把本地 NED 的米当经纬度会立刻飞到天上去
                self.assertLess(la.max() - la.min(), 1.0, f"{t.key} 纬度跨度 {la.max()-la.min():.3f}° 不合理")

    def test_gps_track_reports_its_own_validity(self) -> None:
        """每条轨迹各自算有效性，不跟 POS 共用掩码。

        真日志里未必有无锁定点，所以这里只校自洽性；“无锁定点必须被剔”
        的硬断言在合成用例 ``test_gps_without_fix_is_dropped_per_source`` 里。
        """
        g = next(t for t in self.tracks if t.key == "GPS#0")
        idx = json.loads((self.store.cache_dir / "tracks" / "index.json")
                         .read_text(encoding="utf-8"))
        ent = next(e for e in idx if e["key"] == "GPS#0")
        self.assertEqual(ent["n"], g.n_points, "index 与 npz 的行数必须一致")
        self.assertGreater(ent["valid_frac"], 0.0)
        self.assertLessEqual(ent["valid_frac"], 1.0)
        rows = ent["n"] / ent["valid_frac"]
        self.assertGreaterEqual(rows, ent["n"], "总行数不可能小于有效行数")
        self.assertTrue(np.all(np.isfinite(g.lat)))
        # GPS 与 POS 是两个独立源：行数不同（采样率不同）说明没共用掩码
        p = next(t for t in self.tracks if t.key == "POS")
        self.assertNotEqual(p.n_points, g.n_points)

    def test_labels_are_the_chinese_names_the_ui_shows(self) -> None:
        by = {t.key: t.label for t in self.tracks}
        self.assertEqual(by["POS"], "EKF 融合位置 (POS)")
        self.assertEqual(by["GPS#0"], "原始 GNSS (GPS1)")
        self.assertEqual(by["XKF1"], "EKF 状态位置 (XKF1)")
        self.assertEqual(by["VISP"], "视觉位置 (VISP)")

    def test_unknown_key_does_not_raise(self) -> None:
        t = self.store.track("不存在的轨迹")
        self.assertEqual(t.kind, "none")
        self.assertIn("不存在的轨迹", t.note)
        self.assertEqual(t.t.size, 0)

    def test_named_lookup_returns_that_track(self) -> None:
        g = self.store.track("GPS#0")
        self.assertEqual(g.key, "GPS#0")
        self.assertEqual(g.n_points, next(t for t in self.tracks if t.key == "GPS#0").n_points)

    def test_legacy_index_without_tracks_degrades_to_primary(self) -> None:
        """schema=2 旧缓存（没有 tracks 段）不能崩、不能读脏：只能给主轨迹一条。

        正常路径上 ``is_fresh()`` 会因为 schema 不匹配而重建缓存；这里直接拆掉
        index 里的 tracks 段，验证读取侧自己也能安全降级。
        """
        st, _, _ = cached_store(LOG_A)
        import copy
        saved = copy.deepcopy(st._index)
        try:
            del st._index["track"]["tracks"]
            st._tracks = None
            trs = st.tracks()
            self.assertEqual(len(trs), 1, "旧缓存只能降级为一条，不能编造多条")
            self.assertEqual(trs[0].key, "POS", "降级时应保留真实主轨迹键名")
            self.assertEqual(trs[0].n_points, saved["track"]["rows"])
            # 更老的一代缓存连 key/label 都没有 → 给一稳定占位键，不让勾选栏拿到空串
            st._index["track"].pop("key", None)
            st._index["track"].pop("label", None)
            st._track = None
            st._tracks = None
            trs = st.tracks()
            self.assertEqual(len(trs), 1)
            self.assertEqual(trs[0].key, "primary")
            self.assertEqual(trs[0].label, "主轨迹")
        finally:
            st._index = saved
            st._track = None            # 不然被污染的 TrackResult 会泄漏给下一个用例
            st._tracks = None
        self.assertGreater(len(st.tracks()), 1, "恢复后应重新拿到多轨迹")

    def test_missing_child_npz_is_skipped_not_fatal(self) -> None:
        """index 有但子文件丢了：跳过那条，不能把整个面板弄崩。"""
        st, _, _ = cached_store(LOG_A)
        import copy
        saved = copy.deepcopy(st._index)
        try:
            st._index["track"]["tracks"].append(
                {"key": "GONE", "label": "消失的", "n": 10, "valid_frac": 1.0})
            st._tracks = None
            keys = [t.key for t in st.tracks()]
            self.assertNotIn("GONE", keys)
            self.assertIn("POS", keys)
        finally:
            st._index = saved
            st._track = None
            st._tracks = None

    def test_tracks_are_cached_not_reparsed(self) -> None:
        a = self.store.tracks()
        b = self.store.tracks()
        self.assertIs(a, b, "tracks() 必须缓存，不能每次重读 npz")

    def test_cache_layout_on_disk(self) -> None:
        cdir = self.store.cache_dir
        self.assertTrue((cdir / "track.npz").exists(), "主轨迹文件必须继续存在（兼容老路径）")
        idx = json.loads((cdir / "tracks" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([e["key"] for e in idx], [t.key for t in self.tracks])
        self.assertTrue(idx[0]["primary"])
        self.assertFalse(any(e["primary"] for e in idx[1:]))
        for e in idx:
            self.assertTrue((cdir / "tracks" / f"{e['key']}.npz").exists())
            self.assertGreaterEqual(e["n"], 5)
            self.assertIn("valid_frac", e)
            self.assertIn("label", e)

    def test_warm_cache_is_much_faster(self) -> None:
        """给真实数字：冷解析 vs 命中，避免有人把缓存路径改坏还不自知。"""
        print(f"\n  冷解析 {self.cold:.1f}s / 命中 {self.hot:.2f}s")
        self.assertGreater(self.cold, 0.5)
        self.assertLess(self.hot, self.cold / 5)
        st2, _, _ = cached_store(LOG_A)
        self.assertEqual([t.key for t in st2.tracks()], [t.key for t in self.tracks])


# --------------------------------------------------------------------------- #
@unittest.skipUnless(LOG_B.exists(), f"缺少真日志 {LOG_B.name}")
class TestSecondRealLog(unittest.TestCase):
    def test_also_multi(self) -> None:
        st, _, _ = cached_store(LOG_B)
        trs = st.tracks()
        keys = [t.key for t in trs]
        self.assertIn("POS", keys)
        self.assertIn("GPS#0", keys)
        by = {t.key: t for t in trs}
        d = sep_meters(by["POS"], by["GPS#0"])
        print(f"\n  {LOG_B.name} POS 与 GPS#0 最大间距 {d:.1f} m")
        self.assertGreater(d, 5.0)
        st.close()


# --------------------------------------------------------------------------- #
@unittest.skipUnless(LOG_C.exists(), f"缺少真日志 {LOG_C.name}")
class TestLocalOnlyLogStaysLocal(unittest.TestCase):
    """只有 XKF1/VISP（本地 NED）且**没有 ORGN 数据**的日志。

    这种日志必须给 ``kind="local"``（只有 e/n/u，经纬度全 NaN），地图靠 ENU 画形状；
    绝不能把 PN/PE 的米数当成经纬度——那会落到 (0°,0°) 儿内亚湾或直接越界。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.store, cls.cold, cls.hot = cached_store(LOG_C)

    def test_has_local_tracks(self) -> None:
        trs = self.store.tracks()
        self.assertTrue(trs, "只有本地 NED 源时也该有轨迹可看（老行为就是这样的）")
        self.assertTrue(all(t.kind == "local" for t in trs),
                        f"实际 {[(t.key, t.kind) for t in trs]}")

    def test_no_geodetic_nonsense(self) -> None:
        for t in self.store.tracks():
            with self.subTest(key=t.key):
                self.assertEqual(t.n_points, t.lat.size)
                self.assertFalse(np.isfinite(t.lat).any(),
                                 "无 ORGN 却填了经纬度 = 把米当成了度")
                self.assertFalse(np.isfinite(t.lon).any())
                self.assertTrue(np.isfinite(t.e).any() and np.isfinite(t.n).any())

    def test_note_says_local(self) -> None:
        self.assertTrue(any("本地" in t.note or "NED" in t.note for t in self.store.tracks()))


# --------------------------------------------------------------------------- #
class TestSchemaAndKeys(unittest.TestCase):
    """版本号与键名规则：不 bump 会让 store.tracks() 静默少轨迹。"""

    def test_schema_version_is_four(self) -> None:
        """v3→4：无 GPS/无 ORGN 的纯视觉日志改为出本地轨迹，语义变了必须 bump。

        真事故：开发中间快照也把 schema 写成 3 但内容是 kind="none"，
        文件 size/mtime 不变 → is_fresh() 判有效 → 应用永远读不到轨迹。
        """
        self.assertEqual(RD.SCHEMA_VERSION, 4)

    def test_schema_3_cache_is_rejected(self) -> None:
        """上一版语义（含“无轨迹”那个脏中间态）的 schema=3 缓存必须作废重建。"""
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "x.BIN"
            log.write_bytes(b"\x00" * 64)
            idx = {"schema": 3, "log_id": "whatever", "size": 64, "mtime": 0.0,
                   "abs_path": "irrelevant"}
            self.assertFalse(RD.is_fresh(log, idx),
                             "schema=3 必须判过期：它可能是“无轨迹”的中间态写的")

    def test_old_schema_cache_is_rejected(self) -> None:
        """schema=2 的缓存必须判为过期，否则新代码会去读不存在的 tracks/ 子文件。"""
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "x.BIN"
            log.write_bytes(b"\x00" * 64)
            idx = {"schema": 2, "log_id": "whatever", "size": 64, "mtime": 0.0,
                   "abs_path": "irrelevant"}
            self.assertFalse(RD.is_fresh(log, idx))

    def test_current_schema_still_needs_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "x.BIN"
            log.write_bytes(b"\x00" * 64)
            self.assertFalse(RD.is_fresh(log, {"schema": RD.SCHEMA_VERSION,
                                               "log_id": "错", "size": 0, "mtime": 0,
                                               "abs_path": "错"}))

    def test_key_and_label_rules(self) -> None:
        self.assertEqual(RD._track_key("POS", None), "POS")
        self.assertEqual(RD._track_key("GPS", 0), "GPS#0")
        self.assertEqual(RD._track_key("XKF1", 1), "XKF1")     # 核号不参与键名
        self.assertEqual(RD._track_label("GPS", 0), "原始 GNSS (GPS1)")
        self.assertEqual(RD._track_label("GPS", 1), "原始 GNSS (GPS2)")
        self.assertEqual(RD._track_label("GPS", 3), "原始 GNSS (GPS4)")
        self.assertEqual(RD._track_label("VISP", None), "视觉位置 (VISP)")

    def test_only_pos_and_gps_split_by_instance(self) -> None:
        self.assertEqual(RD.INSTANCE_SPLIT, frozenset({"POS", "GPS"}))
        self.assertNotIn("XKF1", RD.INSTANCE_SPLIT)


# --------------------------------------------------------------------------- #
def _gps_buf(rows_per_inst: int = 30, instances: tuple[int, ...] = (0, 1),
             lat0: float = 31.2, lon0: float = 121.5) -> RD._Buf:
    """合成 GPS 缓冲：每个实例一条自己的直线轨迹。"""
    b = RD._Buf()
    for col in ("Lat", "Lng", "Alt", "NSats", "HDop"):
        b.cols[col] = []
    for inst in instances:
        for i in range(rows_per_inst):
            vals = {"Lat": lat0 + 0.00002 * i + 0.0005 * inst,
                    "Lng": lon0 + 0.00002 * i,
                    "Alt": 20.0 + i,
                    "NSats": 12.0,
                    "HDop": 0.8}
            b.add_row(tc=(i + rows_per_inst * inst) * 0.1,
                      t=(i + rows_per_inst * inst) * 0.1,
                      inst=inst, seg=0, vals=vals)
    return b


class TestSyntheticSources(unittest.TestCase):
    """合成数据：双接收机、无 ORGN、GPS 无锁定这些真日志里凑不出来的分支。"""

    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="apmlog_synth_"))
        self.addCleanup(shutil.rmtree, str(self.d), True)

    def _build(self, bufs: dict, types_meta=None) -> dict:
        tm = types_meta or {name: {"rows": b.n} for name, b in bufs.items()}
        return RD.build_tracks(self.d, tm, bufs, [], {})

    def test_dual_receiver_yields_two_gps_tracks(self) -> None:
        meta = self._build({"GPS": _gps_buf(instances=(0, 1))})
        entries = meta["tracks"]
        keys = [e["key"] for e in entries]
        self.assertEqual(keys, ["GPS#0", "GPS#1"], f"实际 {keys}")
        self.assertEqual([e["label"] for e in entries],
                         ["原始 GNSS (GPS1)", "原始 GNSS (GPS2)"])
        self.assertTrue(entries[0]["primary"])
        for k in keys:
            self.assertTrue((self.d / "tracks" / f"{k}.npz").exists())
        # 两个实例的点不能混进同一条线（混了就会跳 0.0005° ≈ 55 m）
        z0 = np.load(self.d / "tracks" / "GPS#0.npz")
        z1 = np.load(self.d / "tracks" / "GPS#1.npz")
        self.assertEqual(z0["lat"].size, 30)
        self.assertEqual(z1["lat"].size, 30)
        self.assertGreater(float(np.max(np.abs(z1["lat"] - z0["lat"]))), 0.0004)

    def test_local_source_without_origin_stays_local(self) -> None:
        """无 ORGN：给 kind="local"，但**经纬度必须全 NaN**（不能把米当度）。"""
        b = RD._Buf()
        for col in ("PN", "PE", "PD"):
            b.cols[col] = []
        for i in range(20):
            b.add_row(tc=i * 0.1, t=i * 0.1, inst=1, seg=0,
                      vals={"PN": float(i), "PE": float(i) * 2, "PD": -5.0})
        meta = self._build({"XKF1": b})
        keys = [e["key"] for e in meta["tracks"]]
        self.assertEqual(keys, ["XKF1"])
        self.assertEqual(meta["kind"], "local", "无 ORGN 时主轨迹必须是 local")
        z = np.load(self.d / "tracks" / "XKF1.npz")
        self.assertFalse(np.isfinite(z["lat"]).any(), "lat 里出现了数值 = 把米当成了度")
        self.assertFalse(np.isfinite(z["lon"]).any())
        self.assertTrue(np.isfinite(z["e"]).all() and np.isfinite(z["n"]).all())

    def test_local_source_with_origin_projects(self) -> None:
        b = RD._Buf()
        for col in ("PN", "PE", "PD"):
            b.cols[col] = []
        for i in range(20):
            b.add_row(tc=i * 0.1, t=i * 0.1, inst=1, seg=0,
                      vals={"PN": float(i), "PE": float(i) * 2, "PD": -5.0})
        o = RD._Buf()
        o.cols["Lat"] = []
        o.cols["Lng"] = []
        o.cols["Alt"] = []
        o.add_row(tc=0.0, t=0.0, inst=-1, seg=0,
                  vals={"Lat": 31.2, "Lng": 121.5, "Alt": 3.0})
        meta = self._build({"XKF1": b, "ORGN": o})
        keys = [e["key"] for e in meta["tracks"]]
        self.assertEqual(keys, ["XKF1"], "XKF1 的 C 是核号，不该出现在键名里")
        z = np.load(self.d / "tracks" / "XKF1.npz")
        self.assertAlmostEqual(float(z["lat"][0]), 31.2, places=5)
        self.assertLess(float(np.max(z["lat"])) - 31.2, 0.01, "投影结果必须贴着原点")
        self.assertGreater(float(np.max(z["lat"])) - 31.2, 1e-6, "全等于原点说明没真的投影")

    def test_gps_without_fix_is_dropped_per_source(self) -> None:
        """无锁定 GPS（NSats=0/HDop=99.99/经纬度 0）整条源都不可用，不能出轨迹。"""
        b = RD._Buf()
        for col in ("Lat", "Lng", "Alt", "NSats", "HDop"):
            b.cols[col] = []
        for i in range(20):
            b.add_row(tc=i * 0.1, t=i * 0.1, inst=0, seg=0,
                      vals={"Lat": 0.0, "Lng": 0.0, "Alt": 0.0,
                            "NSats": 0.0, "HDop": 99.99})
        meta = self._build({"GPS": b})
        self.assertEqual(meta["tracks"], [])
        self.assertIn("GPS", meta["unusable"])

    def test_good_pos_does_not_excuse_bad_gps(self) -> None:
        """POS 干净时 GPS 仍要各自过滤：两条轨迹各算各的有效点。"""
        good = _gps_buf(rows_per_inst=20, instances=(0,))
        for i in range(10):                       # 掺进 10 个无锁定假点
            good.add_row(tc=100.0 + i * 0.1, t=100.0 + i * 0.1, inst=0, seg=0,
                         vals={"Lat": 0.0, "Lng": 0.0, "Alt": 0.0,
                               "NSats": 0.0, "HDop": 99.99})
        meta = self._build({"GPS": good})
        self.assertEqual(len(meta["tracks"]), 1)
        self.assertEqual(meta["tracks"][0]["n"], 20, "假点没被剔掉")
        self.assertAlmostEqual(meta["tracks"][0]["valid_frac"], 20 / 30, places=3)

    def test_build_track_is_a_thin_wrapper(self) -> None:
        """老入口不能退化：它必须仍然返回同一份 meta（含主轨迹顶层字段）。"""
        meta = RD.build_track(self.d, {"GPS": {"rows": 30}},
                              {"GPS": _gps_buf(instances=(0,))}, [], {})
        self.assertEqual(meta["source"], "GPS")
        self.assertEqual(meta["key"], "GPS#0")
        self.assertGreater(meta["rows"], 0)
        self.assertTrue(math.isfinite(meta["distance"]))
        self.assertEqual([e["key"] for e in meta["tracks"]], ["GPS#0"])

    def test_primary_selection_follows_legacy_priority(self) -> None:
        """POS 与 GPS 同时存在时主轨迹必须是 POS（与历史一致，不能因多轨迹而变）。"""
        pos = RD._Buf()
        for col in ("Lat", "Lng", "Alt", "RelHomeAlt"):
            pos.cols[col] = []
        for i in range(20):
            pos.add_row(tc=i * 0.05, t=i * 0.05, inst=-1, seg=0,
                        vals={"Lat": 31.2 + 1e-5 * i, "Lng": 121.5 + 1e-5 * i,
                              "Alt": 20.0, "RelHomeAlt": float(i)})
        meta = self._build({"POS": pos, "GPS": _gps_buf(instances=(0,))})
        keys = [e["key"] for e in meta["tracks"]]
        self.assertEqual(keys[0], "POS")
        self.assertEqual(meta["source"], "POS")
        self.assertTrue(meta["tracks"][0]["primary"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
