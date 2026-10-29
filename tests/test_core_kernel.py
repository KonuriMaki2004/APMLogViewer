"""内核单元测试：model / codebooks / events / alerts（纯逻辑，不读真实日志）。

跑法：``.venv\\Scripts\\python.exe -m pytest tests/test_core_kernel.py -q``
断言只针对**跨版本稳定**的行为：通道键格式、严重度序、炸机三事件同刻归组、
告警阈值来自日志参数、无数据时降级为提示而不抛。
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np

from apmlog.core import alerts as AL
from apmlog.core import events as EV
from apmlog.core.codebooks import Codebook, load_codebook, msg_severity
from apmlog.core.model import (ChannelInfo, ChannelKey, Envelope, EventRec, LogStats,
                               Series, TrackResult, as_key, human_size, severity_at_least,
                               severity_rank)

# --------------------------------------------------------------------------- #


class ModelTest(unittest.TestCase):
    def test_channelkey_roundtrip(self):
        k = ChannelKey("VIBE", "VibeZ", 1)
        self.assertEqual(k.key, "VIBE#1.VibeZ")
        self.assertEqual(k.label, "VIBE[1].VibeZ")
        self.assertEqual(ChannelKey.parse(k.key), k)
        self.assertEqual(ChannelKey.parse("BAT.Volt").inst, None)
        self.assertEqual(ChannelKey.parse("BAT.Volt").key, "BAT.Volt")
        self.assertEqual(k.with_inst(None).key, "VIBE.VibeZ")
        self.assertEqual(k.without_inst().key, "VIBE.VibeZ")
        self.assertEqual(as_key("IMU#2.AccX").type, "IMU")
        self.assertEqual(as_key("IMU#2.AccX").inst, 2)
        self.assertEqual(as_key(k), k)
        with self.assertRaises(ValueError):
            ChannelKey.parse("没有点号也没有#")

    def test_severity_helpers(self):
        ranks = [severity_rank(s) for s in ("info", "hint", "warn", "error", "critical")]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(ranks[0], 0)
        self.assertGreater(severity_rank("critical"), severity_rank("error"))
        self.assertEqual(severity_rank("没见过的等级"), 0)
        self.assertTrue(severity_at_least("error", "warn"))
        self.assertFalse(severity_at_least("info", "warn"))
        self.assertTrue(severity_at_least("warn", None))

    def test_series_basics(self):
        t = np.arange(0.0, 10.0, 1.0)
        v = np.arange(10.0)
        v[3] = math.nan
        s = Series(ChannelKey("A", "B"), t, v, unit="m")
        self.assertEqual(s.n, 10)
        self.assertEqual(s.span(), (0.0, 9.0))
        st = s.stats()
        self.assertIsInstance(st, dict)
        self.assertAlmostEqual(st.get("min", float("nan")), 0.0)
        self.assertAlmostEqual(st.get("max", float("nan")), 9.0)
        sl = s.slice(2.0, 5.0)
        self.assertEqual(sl.n, 4)
        self.assertEqual(float(sl.t[0]), 2.0)
        self.assertEqual(s.label or "ok", s.label or "ok")

    def test_envelope_shape(self):
        t = np.linspace(0, 8, 9)
        vmin = np.arange(9.0)
        vmax = vmin + 2
        mean = vmin + 1
        e = Envelope(ChannelKey("VIBE", "VibeZ"), t, vmin, vmax, mean,
                     np.full(9, 100), unit="m/s^2")
        self.assertEqual(e.t.size, 9)
        self.assertTrue(np.all(e.vmin <= e.vmean))
        self.assertTrue(np.all(e.vmean <= e.vmax))
        self.assertEqual(e.unit, "m/s^2")

    def test_track_result_helpers(self):
        n = 100
        t = np.arange(float(n))
        tr = TrackResult(kind="geodetic", t=t, lat=31.0 + t / 1e5, lon=121.0 + t / 1e5,
                         alt=np.arange(float(n)), e=np.arange(float(n)), n=np.zeros(n),
                         u=np.zeros(n), speed=np.full(n, 3.0),
                         src=np.zeros(n, np.int8), src_names=["GPS"],
                         origin=(31.0, 121.0, 0.0), distance=100.0, max_speed=3.0)
        self.assertEqual(tr.n_points, n)
        self.assertTrue(tr.has_geodetic)
        e0, e1, n0, n1 = tr.bounds_enu()          # (e_min, e_max, n_min, n_max)
        self.assertEqual((e0, e1), (0.0, 99.0))
        self.assertEqual((n0, n1), (0.0, 0.0))
        lo, hi = tr.alt_range()
        self.assertEqual((lo, hi), (0.0, 99.0))
        self.assertLessEqual(tr.resample(10).n_points, 10)
        self.assertEqual(tr.source_counts()["GPS"], n)
        local = TrackResult(kind="local", t=t, lat=np.zeros(n), lon=np.zeros(n),
                            alt=np.zeros(n), e=np.zeros(n), n=np.arange(float(n)),
                            u=np.arange(float(n)), speed=np.zeros(n),
                            src=np.zeros(n, np.int8), src_names=["VISP"])
        self.assertFalse(local.has_geodetic)
        self.assertEqual(local.kind, "local")

    def test_eventrec_display(self):
        e = EventRec(t=322.45, kind="ERR", code=12, ecode=1, text="Subsys=12 ECode=1（未映射）",
                     severity="critical")
        self.assertIn("Subsys=12", e.display)
        self.assertGreater(e.rank, 0)
        d = e.as_dict()
        self.assertAlmostEqual(float(d["t"]), 322.45)
        self.assertEqual(d["severity"], "critical")

    def test_misc_helpers(self):
        self.assertIn("B", human_size(12))
        self.assertIn("MB", human_size(3_500_000))
        self.assertEqual(LogStats(duration=64.5).duration_str, "01:04")
        self.assertEqual(LogStats(duration=644.59).duration_str, "10:45")


# --------------------------------------------------------------------------- #


class CodebookTest(unittest.TestCase):
    def test_empty_codebook_is_safe(self):
        cb = Codebook({})
        self.assertIsNone(cb.mode_name(2))
        self.assertIsNone(cb.mode_name(None))
        self.assertEqual(cb.err_info(12, 1)[0], None)
        self.assertEqual(cb.ev_text(60)[0], None)
        self.assertIsNone(cb.arm_method(12))
        self.assertIn(cb.motb_flags(3), (None, []))
        self.assertTrue(cb.provenance)

    def test_missing_file_falls_back(self):
        cb = load_codebook("Z:/不可能存在/copter.json")
        self.assertIsNone(cb.mode_name(5))
        self.assertIsInstance(cb.unmapped_report({"mode_numbers": [3]}), dict)

    def test_minimal_tables_decode(self):
        cb = Codebook({
            "mode_numbers": {"0": "STABILIZE", "2": "LOITER", "16": "POSHOLD"},
            "error_subsystems": {"12": {"name": "CRASH", "severity": "critical",
                                       "ecodes": {"1": "Check: Crash Detected"}}},
            "event_ids": {"60": "ARMING_CHECK"},
            "arm_methods": {"12": "RC switch or Mode switch"},
            "arming_checks_bits": {"0": "RC Channels", "1": "Acces"},
            "motb_failflags_bits": {"0": "Deadness", "1": "Bad THO"},
        })
        self.assertEqual(cb.mode_name(2), "LOITER")
        txt, sev = cb.err_info(12, 1)
        self.assertIsNotNone(txt)
        self.assertEqual(sev, "critical")
        self.assertIn("CRASH", str(txt))
        self.assertEqual(cb.ev_text(60)[0], "ARMING_CHECK")
        self.assertEqual(cb.arm_method(12), "RC switch or Mode switch")
        self.assertTrue(cb.arming_checks(3))
        self.assertTrue(cb.motb_flags(3))
        self.assertEqual(len(cb.arming_checks(3) or []), 2)
        # 未知编号仍然是 None（不能瞎猜）
        self.assertIsNone(cb.mode_name(999))
        self.assertEqual(cb.err_info(99, 99)[0], None)


class MsgSeverityTest(unittest.TestCase):
    def test_crash_is_critical(self):
        self.assertEqual(msg_severity("Crash: Disarming: AngErr=171>30"), "critical")

    def test_prearm_is_not_info(self):
        self.assertGreater(severity_rank(msg_severity("PreArm: Battery too low")), 0)

    def test_benign_text_is_info(self):
        self.assertEqual(msg_severity("FltRev 1511f271 ChibiOS: 4f34e217"), "info")
        self.assertEqual(msg_severity("Param space used: 1810/5376"), "info")


# --------------------------------------------------------------------------- #


class EventsTest(unittest.TestCase):
    def test_raw_from_message_kinds(self):
        r = EV.raw_from_message("ERR", {"Subsys": 12, "ECode": 1}, 322.45)
        self.assertEqual((r["t"], r["kind"], r["code"], r["ecode"]), (322.45, "ERR", 12, 1))
        self.assertEqual(r["extra"]["Subsys"], 12)
        self.assertEqual(EV.raw_from_message("EV", {"Id": 60, "Arg": 3}, 1.0)["code"], 60)
        self.assertEqual(EV.raw_from_message("MSG", {"Msg": "hello"}, 2.0)["text"], "hello")
        self.assertIsNone(EV.raw_from_message("MSG", {"Msg": ""}, 2.0))
        self.assertEqual(EV.raw_from_message("ARM", {"ArmState": 0, "Method": 12}, 3.0)["code"], 0)
        self.assertEqual(EV.raw_from_message("MODE", {"Mode": 2}, 0.5)["code"], 2)
        self.assertEqual(EV.raw_from_message("MODE", {"ModeNum": 6}, 0.5)["code"], 6)
        self.assertIsNone(EV.raw_from_message("PARM", {"Name": "X", "Value": 1}, 0.0))
        self.assertIsNone(EV.raw_from_message("FMT", {"Type": 1}, 0.0))

    def test_decode_without_codebook_keeps_raw_numbers(self):
        raws = [EV.raw_from_message("ERR", {"Subsys": 12, "ECode": 1}, 322.45),
                EV.raw_from_message("MODE", {"Mode": 2}, 0.5)]
        evs = EV.decode(raws, Codebook({}))
        self.assertEqual(len(evs), 2)
        err = next(e for e in evs if e.kind == "ERR")
        self.assertIn("12", err.text)
        self.assertIn("未映射", err.text)
        self.assertGreaterEqual(severity_rank(err.severity), severity_rank("error"))
        mode = next(e for e in evs if e.kind == "MODE")
        self.assertIn("2", mode.display)

    def test_crash_triple_is_co_located(self):
        """炸机瞬间 ERR + ARM + MSG + EV 同刻出现：必须归一组，ERR 借用 MSG 判级。"""
        raws = [
            EV.raw_from_message("ERR", {"Subsys": 12, "ECode": 1}, 322.450),
            EV.raw_from_message("ARM", {"ArmState": 0, "Method": 12}, 322.451),
            EV.raw_from_message("MSG", {"Msg": "Crash: Disarming: AngErr=171>30, Accel=0.0<3.0"}, 322.452),
            EV.raw_from_message("EV", {"Id": 20}, 322.460),
            EV.raw_from_message("MSG", {"Msg": "FltRev 1511f271 ChibiOS: 4f34e217"}, 1.0),
        ]
        evs = EV.decode(raws, Codebook({}))
        crash = EV.find_crash_groups(evs)
        self.assertEqual(len(crash), 1)
        t, text = crash[0]
        self.assertAlmostEqual(t, 322.45, places=2)
        self.assertIn("Crash: Disarming", text)
        err = next(e for e in evs if e.kind == "ERR")
        self.assertEqual(err.severity, "critical")
        self.assertIn("Crash", err.extra.get("co_text", ""))
        groups = EV.group_events(evs)
        self.assertLessEqual(len(groups), len(evs))
        joined = max(groups, key=len)
        self.assertGreaterEqual(len(joined), 3)

    def test_no_crash_when_only_benign_messages(self):
        raws = [EV.raw_from_message("MSG", {"Msg": "Flight time exceed: 320 s"}, 300.0),
                EV.raw_from_message("MODE", {"Mode": 6}, 1.0)]
        self.assertEqual(EV.find_crash_groups(EV.decode(raws, Codebook({}))), [])

    def test_mode_segments_tile_the_timeline(self):
        raws = [EV.raw_from_message("MODE", {"Mode": 2}, 0.0),
                EV.raw_from_message("MODE", {"Mode": 6}, 20.0),
                EV.raw_from_message("MODE", {"Mode": 9}, 55.0)]
        cb = Codebook({"mode_numbers": {"2": "LOITER", "6": "SPORT", "9": "LAND"}})
        segs = EV.mode_segments(EV.decode(raws, cb), t_end=70.0)
        self.assertEqual(len(segs), 3)
        self.assertAlmostEqual(segs[0][0], 0.0)
        self.assertAlmostEqual(segs[-1][1], 70.0)
        self.assertIn("LOITER", segs[0][2])
        for (_, b, _), (c, _, _) in zip(segs, segs[1:]):
            self.assertAlmostEqual(b, c, places=6)

    def test_mode_seq_compresses_repeats(self):
        """同模式重复进入（日志里很常见）不能在“模式序列”里看起来像两种模式。"""
        raws = [EV.raw_from_message("MODE", {"Mode": 2}, 0.0),
                EV.raw_from_message("MODE", {"Mode": 2}, 12.0),
                EV.raw_from_message("MODE", {"Mode": 9}, 30.0),
                EV.raw_from_message("MODE", {"Mode": 2}, 45.0)]
        cb = Codebook({"mode_numbers": {"2": "ALT_HOLD", "9": "LAND"}})
        segs = EV.mode_segments(EV.decode(raws, cb), t_end=60.0, cb=cb)
        self.assertEqual(len(segs), 4)                       # 区间仍按事件逐段保留
        self.assertEqual(EV.compress_mode_seq(segs), ["ALT_HOLD", "LAND", "ALT_HOLD"])
        self.assertEqual(EV.compress_mode_seq([]), [])

    def test_group_tolerance_is_explicit(self):
        evs = [EventRec(t=0.0, kind="MSG", text="a"), EventRec(t=9.0, kind="MSG", text="b")]
        self.assertEqual(len(EV.group_events(evs, tol=0.05)), 2)
        self.assertEqual(len(EV.group_events(evs, tol=50.0)), 1)


# --------------------------------------------------------------------------- #


class _FakeStore:
    """只实现告警引擎用到的最小接口，形状与 LogStore 的 index 元数据一致。"""

    def __init__(self, series_map, types_meta, t_end, params=None, bat_semantics=None):
        self._s = series_map
        self.meta = {"types": types_meta, "t_end": float(t_end), "units": {},
                    "stats": {"duration": float(t_end), "t_start": 0.0, "t_end": float(t_end)},
                    "bat_semantics": bat_semantics or {}}
        self._params = dict(params or {})
        self.stats = LogStats(duration=float(t_end), t_start=0.0, t_end=float(t_end))

    def channel_infos(self):
        out = []
        for key, (t, v) in self._s.items():
            k = ChannelKey.parse(key)
            out.append(ChannelInfo(key=k, count=int(t.size), t0=float(t[0]), t1=float(t[-1]),
                                   vmin=float(np.nanmin(v)), vmax=float(np.nanmax(v))))
        return out

    def series(self, key, t0=None, t1=None, max_points=None, drop_nan=False):
        k = as_key(key)
        t, v = self._s[k.key]
        m = np.ones(t.size, bool)
        if t0 is not None:
            m &= t >= t0
        if t1 is not None:
            m &= t <= t1
        if max_points and t.size > max_points:
            step = int(np.ceil(t.size / max_points))
            t, v = t[::step], v[::step]
        return Series(k, t, v)

    def envelope(self, key, t0=None, t1=None, buckets=2000):
        s = self.series(key, t0, t1)
        return Envelope(as_key(key), s.t, s.v, s.v, s.v, np.ones(s.n), unit=s.unit)

    def params(self):
        return {k: (v, None) for k, v in self._params.items()}

    def param(self, name, default=None):
        got = self._params.get(name)
        return default if got is None else float(got)


def _vibe_meta(n):
    return {"VIBE": {"rows": n, "insts": [0],
                     "cols": {c: {"count": n} for c in ("TimeUS", "VibeX", "VibeY", "VibeZ", "Clip")}}}


class VibrationAlertTest(unittest.TestCase):
    def setUp(self):
        # 本类验的是"三档"机制，不该被产品默认阈值牵着走：
        # 2026-10-24 产品默认改成 30/60（界面可调），而这里的合成数据是按 15/30 设计的
        # （刺激 18 落在 [warn,crit)、40 达到严重）。显式钉死，改默认不再连带改测试。
        self.cfg = dict(AL.load_config(None)["vibration"])
        self.cfg.update({"rms_warn": 15.0, "rms_crit": 30.0})

    def test_three_levels_on_synthetic_spike(self):
        t = np.arange(0.0, 60.0)
        base = np.full(t.size, 3.0)
        warn_burst = base.copy(); warn_burst[20:24] = 18.0     # 15 ≤ x < 30
        crit_burst = base.copy(); crit_burst[40:46] = 40.0     # ≥ 30
        clip = np.zeros(t.size); clip[40:46] = 5.0
        store = _FakeStore({ChannelKey("VIBE", a, 0).key: (t, c) for a, c in
                            (("VibeX", warn_burst), ("VibeY", crit_burst),
                             ("VibeZ", crit_burst), ("Clip", clip))},
                           _vibe_meta(60), t[-1])
        hits = AL.vibe_alerts(store, self.cfg)
        by_sev = {}
        for h in hits:
            by_sev.setdefault(h.severity, []).append(h)
        self.assertIn("critical", by_sev)
        self.assertIn("warn", by_sev)
        self.assertTrue(all(h.t0 >= 39.0 for h in by_sev["critical"]), [h.message for h in by_sev["critical"]])
        self.assertTrue(all(19.0 <= h.t0 <= 46.0 for h in by_sev["warn"]))
        self.assertTrue(any("振动" in h.title for h in hits))
        self.assertTrue(all(h.source in ("default-ref", "param", "user", "n/a") for h in hits))
        self.assertTrue(all(math.isfinite(h.value) and h.value >= h.threshold
                            for h in hits if h.severity in ("critical", "warn")))
        # 同一时间段不应既报 warn 又报 critical
        for wc in by_sev["critical"]:
            self.assertFalse(any(ww.channel == wc.channel and ww.t0 <= wc.t1 and wc.t0 <= ww.t1
                                 for ww in by_sev["warn"]), f"{wc.title} 与 warn 区间重叠")

    def test_quiet_log_yields_only_axis_hint(self):
        t = np.arange(0.0, 60.0)
        calm = np.full(t.size, 4.0)
        store = _FakeStore({ChannelKey("VIBE", a, 0).key: (t, calm) for a in
                            ("VibeX", "VibeY", "VibeZ")} |
                           {ChannelKey("VIBE", "Clip", 0).key: (t, np.zeros(t.size))},
                          _vibe_meta(60), t[-1])
        hits = AL.vibe_alerts(store, self.cfg)
        self.assertFalse([h for h in hits if severity_rank(h.severity) >= severity_rank("warn")])
        self.assertTrue(any(h.severity == "info" for h in hits))     # 主轴提示仍要给出

    def test_missing_vibe_degrades_to_hint(self):
        store = _FakeStore({}, {}, 60.0)
        hits = AL.vibe_alerts(store, self.cfg)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].severity, "hint")
        self.assertIn("VIBE", hits[0].message)

    def test_intervals_merge_and_min_duration(self):
        t = np.arange(8.0)
        v = np.array([1, 2, 20, 21, 2, 1, 30, 1], float)
        m = AL.mask_over(v, 15.0)
        self.assertEqual(list(np.nonzero(m)[0]), [2, 3, 6])
        self.assertEqual(AL._intervals(t, m, min_dur=0.0, merge_gap=2.0), [(2, 3), (6, 6)])
        self.assertEqual(AL._intervals(t, np.zeros(8, bool), 0.0, 1.0), [])
        self.assertEqual(AL._intervals(np.empty(0), np.empty(0, bool), 0.0, 1.0), [])
        self.assertEqual(AL._intervals(t, m, min_dur=100.0, merge_gap=0.1),
                         [(6, 6)])      # 长段被 min_dur 砍掉，单点段始终保留


class BatteryAlertTest(unittest.TestCase):
    def _store(self, mode="pack", volt_min=44.0):
        t = np.arange(0.0, 100.0)
        volt = np.linspace(50.4, 42.0, t.size)
        voltr = volt - 0.6
        pct = np.linspace(100.0, 5.0, t.size)
        curr = np.full(t.size, 12.0)
        cols = {c: {"count": t.size} for c in ("TimeUS", "Volt", "VoltR", "RemPct", "Curr")}
        params = {"BATT_VOLT_MIN": volt_min, "BATT_CAPACITY": 3300.0,
                  "BATT_MONITOR": 4.0, "BATT_ARM_VOLT": 50.4}
        sem = {"mode": mode, "cells_est": 12, "note": "测试"}
        smap = {ChannelKey("BAT", f, 0).key: (t, v) for f, v in
                (("Volt", volt), ("VoltR", voltr), ("RemPct", pct), ("Curr", curr))}
        return _FakeStore(smap, {"BAT": {"rows": t.size, "insts": [0], "cols": cols}},
                          t[-1], params, sem), t, volt

    def test_default_is_per_cell_3_3_warn_3_1_crit(self):
        """用户定的默认：单芯 <3.3V 警告、<3.1V 严重。这条曲线 4.2→3.5 V/芯，不该报。"""
        store, t, volt = self._store()
        hits = [h for h in AL.batt_alerts(store, AL.load_config(None)["battery"])
                if h.rule.startswith("batt_undervolt")]
        self.assertEqual(hits, [], "3.5 V/芯 还没到 3.3，不该报")

    def test_user_cell_thresholds_drive_the_judgement(self):
        """用户设的单芯阈值生效，且回报的是单芯值（不是整包 42V）。"""
        store, t, volt = self._store()
        cfg = dict(AL.load_config(None)["battery"])
        cfg.update({"cell_warn": 3.6, "cell_crit": 3.55})
        hits = [h for h in AL.batt_alerts(store, cfg, frozenset({"cell_warn", "cell_crit"}))
                if h.rule.startswith("batt_undervolt")]
        self.assertTrue(hits)
        crit = [h for h in hits if h.severity == "critical"]
        self.assertTrue(crit, [(h.threshold, h.value) for h in hits])
        self.assertAlmostEqual(crit[0].threshold, 3.55, places=6)
        self.assertLess(crit[0].value, 3.56)
        self.assertEqual(crit[0].unit, "V/芯")
        self.assertEqual(crit[0].source, "user")
        self.assertIn("12S", crit[0].message, "正文必须说清按几 S 换算的")

    def test_thresholds_follow_cell_count(self):
        """改串数判定必须跟着变——证明没有把串数/阈值写死。"""
        got = {}
        for n in (6, 12):
            store, _, _ = self._store()
            cfg = dict(AL.load_config(None)["battery"])
            cfg.update({"cell_count": n, "cell_warn": 3.6, "cell_crit": 3.55})
            got[n] = [h for h in AL.batt_alerts(
                store, cfg, frozenset({"cell_count", "cell_warn", "cell_crit"}))
                if h.rule.startswith("batt_undervolt")]
        self.assertTrue(got[12], "12S：42V 只剩 3.5 V/芯，应当报警")
        self.assertFalse(got[6], "6S：42V 还有 7.0 V/芯，不该报警")

    def test_param_threshold_used_only_when_cells_unknown(self):
        """自动识别不出串数时才退回 BATT_VOLT_MIN，并提示去手填电芯数。"""
        store, t, volt = self._store()
        with mock.patch.object(AL, "resolve_cells", lambda *a, **k: (None, "none", "无证据")):
            hits = AL.batt_alerts(store, AL.load_config(None)["battery"])
        uv = [h for h in hits if h.rule.startswith("batt_undervolt")]
        self.assertTrue(uv)
        self.assertTrue(any(abs(h.threshold - 44.0) < 1e-6 for h in uv))
        self.assertTrue(all(h.source == "param" for h in uv))
        self.assertIn("batt_cells_unknown", [h.rule for h in hits])

    def test_cell_mode_compares_volts_directly(self):
        """日志记的就是单芯电压时直接比 3.3/3.1，不再除串数。"""
        t = np.arange(0.0, 60.0)
        cell = np.linspace(4.20, 3.10, t.size)
        cols = {c: {"count": t.size} for c in ("TimeUS", "Volt")}
        store = _FakeStore({ChannelKey("BAT", "Volt", 0).key: (t, cell)},
                           {"BAT": {"rows": t.size, "insts": [0], "cols": cols}}, t[-1],
                           {"BATT_VOLT_MIN": 3.2 * 6}, {"mode": "cell", "cells_est": 6})
        hits = [h for h in AL.batt_alerts(store, AL.load_config(None)["battery"])
                if h.rule.startswith("batt_undervolt")]
        self.assertTrue(hits)
        self.assertEqual({round(h.threshold, 6) for h in hits}, {3.3})
        self.assertLess(min(h.value for h in hits), 3.3)
        self.assertGreater(min(h.value for h in hits), 3.0)   # 明显不是被 6 除过
        self.assertTrue(all(h.unit == "V/芯" for h in hits))

    def test_unknown_semantics_skips_undervolt(self):
        store, t, _ = self._store(mode="unknown")
        hits = AL.batt_alerts(store, AL.load_config(None)["battery"])
        self.assertFalse([h for h in hits if h.rule.startswith("batt_undervolt")],
                         [(h.rule, h.message) for h in hits])

    def test_missing_bat_degrades_to_hint(self):
        store = _FakeStore({}, {}, 30.0)
        hits = AL.batt_alerts(store, AL.load_config(None)["battery"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].severity, "hint")


class EvaluateOrderTest(unittest.TestCase):
    def test_sorts_by_severity_then_time(self):
        t = np.arange(0.0, 60.0)
        v = np.full(t.size, 2.0)
        v[10:14] = 18.0
        v[50:56] = 40.0
        store = _FakeStore({ChannelKey("VIBE", a, 0).key: (t, v if a != "Clip" else np.zeros(t.size))
                            for a in ("VibeX", "VibeY", "VibeZ", "Clip")},
                           _vibe_meta(60), t[-1])
        hits = AL.evaluate(store)
        self.assertTrue(hits)
        ranks = [severity_rank(h.severity) for h in hits]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        for a, b in zip(hits, hits[1:]):
            if a.severity == b.severity and math.isfinite(a.t0) and math.isfinite(b.t0):
                self.assertLessEqual(a.t0, b.t0)

    def test_load_config_defaults_are_sane(self):
        cfg = AL.load_config(None)
        v = cfg["vibration"]
        self.assertTrue(v["enabled"])
        self.assertLess(v["rms_warn"], v["rms_crit"])
        self.assertGreater(v["rms_crit"], 0)
        b = cfg["battery"]
        self.assertLess(b["cell_crit"], b["cell_warn"])
        self.assertAlmostEqual(float(b["cell_warn"]), 3.3)
        self.assertAlmostEqual(float(b["cell_crit"]), 3.1)
        # RemPct 已退出判定（用户 2026-10-24），配置里不该再有这两个键
        self.assertNotIn("remain_crit", b)
        self.assertNotIn("remain_warn", b)
        self.assertIsInstance(AL.load_config("Z:/不可能存在/alerts.yaml"), dict)


class CacheSchemaTest(unittest.TestCase):
    """解析/轨迹语义变更后旧缓存必须自动作废（否则批扫描会复用脏里程）。"""

    def test_old_or_missing_schema_is_stale(self):
        from apmlog.core import reader as R
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.BIN"
            p.write_bytes(b"\xa3\x95\x80\x00" + bytes(64))
            size, mtime, log_id = R._fingerprint(p)
            idx = {"log_id": log_id, "size": size, "mtime": mtime,
                   "abs_path": os.path.normcase(str(p.resolve()))}
            self.assertFalse(R.is_fresh(p, dict(idx)), "无 schema 字段的旧缓存应作废")
            self.assertFalse(R.is_fresh(p, {**idx, "schema": 1}), "低版本应作废")
            self.assertFalse(R.is_fresh(p, {**idx, "schema": "乱码"}))
            self.assertTrue(R.is_fresh(p, {**idx, "schema": R.SCHEMA_VERSION}))
            p.write_bytes(p.read_bytes() + b"1")
            self.assertFalse(R.is_fresh(p, {**idx, "schema": R.SCHEMA_VERSION}), "文件变大后应作废")


class TrackQualityTest(unittest.TestCase):
    """位置源选得对不对 + 跳变剔除。真机数据反推出来的规则。"""

    def _buf(self, cols: dict[str, list[float]], insts=None, tcs=None):
        from apmlog.core.reader import _Buf
        b = _Buf()
        n = len(next(iter(cols.values())))
        for i in range(n):
            b.add_row(float(tcs[i]) if tcs is not None else 0.02 * i, float(tcs[i]) if tcs else 0.02 * i,
                      int(insts[i]) if insts is not None else 0, 0,
                      {k: v[i] for k, v in cols.items()})
        return b

    def test_source_valid_rejects_no_fix_gps(self):
        from apmlog.core.reader import _source_valid
        lat = [0.0] * 3 + [38.96] * 3
        lon = [0.0] * 3 + [117.32] * 3
        m = _source_valid("GPS", {"Lat": np.array(lat), "Lng": np.array(lon),
                                  "NSats": np.array([0, 0, 0, 12, 12, 12], float),
                                  "HDop": np.array([99.99, 99.99, 99.99, 0.9, 0.9, 0.9])})
        self.assertEqual(list(m), [False, False, False, True, True, True])
        # 有星数但 HDop 是占位值 → 仍不算
        m2 = _source_valid("GPS", {"Lat": np.array([38.96] * 4), "Lng": np.array([117.32] * 4),
                                   "NSats": np.array([9.0] * 4), "HDop": np.array([0.8, 99.99, 99.99, 5.0])})
        self.assertEqual(list(m2), [True, False, False, True])
        # 星数不够 3 颗不算有效定位
        m3 = _source_valid("GPS", {"Lat": np.array([38.96] * 3), "Lng": np.array([117.32] * 3),
                                   "NSats": np.array([0.0, 2.0, 3.0])})
        self.assertEqual(list(m3), [False, False, True])
        # POS（EKF 位置）没有 NSats 列，只要非零经纬度即可
        self.assertEqual(list(_source_valid("POS", {"Lat": np.array([38.96, 0.0]),
                                                    "Lng": np.array([117.32, 0.0])})), [True, False])
        # 越界坐标
        self.assertEqual(list(_source_valid("GPS", {"Lat": np.array([91.0, 38.9]),
                                                    "Lng": np.array([181.0, 117.3]),
                                                    "NSats": np.array([12.0, 12.0])})), [False, True])

    def test_no_fix_gps_falls_back_to_local_source(self):
        from apmlog.core.reader import build_track
        n = 40
        gps = self._buf({"Lat": [0.0] * n, "Lng": [0.0] * n, "Alt": [-17.0] * n,
                         "NSats": [0.0] * n, "HDop": [99.99] * n})
        xkf1 = self._buf({"PN": [1.0] * n, "PE": [2.0] * n, "PD": [5.0] * n})
        with tempfile.TemporaryDirectory() as td:
            meta = build_track(Path(td), {"GPS": {"rows": n}, "XKF1": {"rows": n}},
                               {"GPS": gps, "XKF1": xkf1}, [], {})
            self.assertEqual(meta["kind"], "local")
            self.assertEqual(meta["source"], "XKF1")
            self.assertEqual(meta["rows"], n)
            self.assertAlmostEqual(meta["distance"], 0.0, places=6)
            self.assertTrue(np.isfinite(meta["max_speed"]) is False or meta["max_speed"] < 1.0)
            with np.load(Path(td) / "track.npz") as z:
                self.assertEqual(z["lat"].size, n)
                self.assertTrue(np.all(np.isnan(z["lat"])))   # 本地模式不编造经纬度

    def test_usable_gps_wins_over_local_when_present(self):
        from apmlog.core.reader import build_track
        n = 30
        lat = [38.96 + i / 1e4 for i in range(n)]
        lon = [117.32] * n
        gps = self._buf({"Lat": lat, "Lng": lon, "Alt": [30.0] * n,
                         "NSats": [12.0] * n, "HDop": [0.9] * n})
        mixed = self._buf({"Lat": [0.0] * 10 + lat, "Lng": [0.0] * 10 + lon,
                           "Alt": [-17.0] * 10 + [30.0] * n,
                           "NSats": [0.0] * 10 + [12.0] * n,
                           "HDop": [99.99] * 10 + [0.9] * n})
        with tempfile.TemporaryDirectory() as td:
            meta = build_track(Path(td), {"GPS": {"rows": n + 10}}, {"GPS": mixed}, [], {})
            self.assertEqual(meta["kind"], "geodetic")
            self.assertEqual(meta["rows"], n)              # 10 个无锁定样本被剔除
            self.assertEqual(meta["available"]["GPS"], n + 10)

    def test_jump_guard_excludes_teleport_from_distance(self):
        from apmlog.core.reader import build_track
        n = 20
        lat = [38.96 + i * 0.00001 for i in range(n)]
        lat[10] = lat[10] + 1.0                            # 一个直接跳 111 km 的点
        pos = self._buf({"Lat": lat, "Lng": [117.32] * n,
                         "Alt": [10.0] * n, "RelHomeAlt": [0.0] * n},
                        tcs=[float(i) for i in range(n)])
        with tempfile.TemporaryDirectory() as td:
            meta = build_track(Path(td), {"POS": {"rows": n}}, {"POS": pos}, [], {})
            self.assertEqual(meta["jumps"], 2)             # 跳进去 + 跳出来各一次
            self.assertLess(meta["distance"], 200.0)       # 不含那 111 km
            self.assertLess(meta["max_speed"], 5.0)
            self.assertIn("位置跳变", meta["axis_note"])

    def test_no_position_source_at_all(self):
        from apmlog.core.reader import build_track
        n = 20
        vibe = self._buf({"VibeX": [1.0] * n})
        with tempfile.TemporaryDirectory() as td:
            meta = build_track(Path(td), {"VIBE": {"rows": n}}, {"VIBE": vibe}, [], {})
            self.assertEqual(meta["kind"], "none")
            self.assertEqual(meta["source"], "")
            with np.load(Path(td) / "track.npz") as z:
                self.assertEqual(z["t"].size, 0)
                self.assertFalse(np.any(np.isfinite(z["lat"])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
