# -*- coding: utf-8 -*-
"""tests/test_link_params.py —— 参数**只读**检测层 + 严格只读护栏（FakeFC 内存管道）。

覆盖：
* 按名读取（含类型）、参数不存在、飞控不应答 → timeout、链路断 → link_lost、发送异常 → send_failed
* 链路上哗哗刷 PARAM_VALUE 时仍按名字命中（别的 sysid 的冒名应答必须被忽略）
* ``classify`` 真值表（表驱动，纯函数）：``suggest`` 只在 disabled 时非空
* ``probe_log_write_config`` 跑完 ``fc.writes == []`` 且 ``fc.stats.param_sets == 0``
* **防回退守卫**：写 API 必须不存在、源码里不许出现参数写指令、
  人为注入一条参数写帧时必须被 AuditTransport 抓进 ``forbidden`` 且 FakeFC 不接受不存值

不碰串口/网络，单条 sleep <1s，整文件秒级完成。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_link_params.py -q
"""
from __future__ import annotations

import dataclasses
import inspect
import queue
import sys
import time
import unittest
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pymavlink.dialects.v20 import ardupilotmega as D    # noqa: E402

from apmlog.link import audit as A                       # noqa: E402
from apmlog.link import params as P                      # noqa: E402
from apmlog.link.audit import AuditTransport, TxAudit, audit_summary  # noqa: E402
from apmlog.link.endpoint import MavLinkSession          # noqa: E402
from apmlog.link.fakes import FakeFC                     # noqa: E402
from apmlog.link.params import (                         # noqa: E402
    DEFAULT_LOG_BACKEND_TYPE, DEFAULT_LOG_BITMASK, LOG_BACKEND_TYPE, LOG_BITMASK,
    LogWriteConfig, ParamValue, classify,
)
from apmlog.link.transport import (                      # noqa: E402
    LinkError, MemoryTransport, Transport,
)

HB_WAIT = 5.0          # 等首个心跳的上限（FakeFC heartbeat_period=0.1，实际 <0.3s）
DISABLED_PARAMS = {"LOG_BACKEND_TYPE": 0.0, "LOG_BITMASK": 0.0, "LOG_DISARMED": 0.0}


# --------------------------------------------------------------------------- #
# 测试用的传输装饰器
# --------------------------------------------------------------------------- #


class _InjectedTransport(Transport):
    """透传内层，同时**持续注入一段预先打包好的噪声帧**。

    用来模拟“另一个 GCS 正在拉全量参数”：噪声夹在真数据之前递上去，两边的
    PARAM_VALUE 都会到达本端，以此验证读取是**按名字过滤**而不是“取第一条”。
    """

    def __init__(self, inner: Transport, noise: bytes = b"", *, repeat: bool = True) -> None:
        super().__init__()
        self.inner = inner
        self.name = f"inject+{inner.name}"
        self.repeat = repeat
        self._template = bytearray(noise)
        self._noise = bytearray(noise)

    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        data = self.inner.read(n, timeout)            # 先取真实字节，保证不饿死飞控应答
        if self._noise:
            chunk = bytes(self._noise[:max(1, n)])
            del self._noise[:max(1, n)]
            if not self._noise and self.repeat:
                self._noise = bytearray(self._template)
            return chunk + data
        return data

    def _write(self, data: bytes) -> int:
        return self.inner.write(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.inner.close()


class _BlackHoleWriteTransport(Transport):
    """读正常、写永远失败：用来验证 send_failed 分支（链路本身还"连着"）。"""

    def __init__(self, inner: Transport) -> None:
        super().__init__()
        self.inner = inner
        self.name = f"blackhole+{inner.name}"

    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        return self.inner.read(n, timeout)

    def _write(self, data: bytes) -> int:
        raise LinkError("模拟发送失败（写通道故障）")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.inner.close()


class _SinkTransport(Transport):
    """只收集写出的字节，不产生任何真实 IO（audit 用例用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.b = bytearray()
        self.pending = bytearray()
        self.name = "sink"

    def read(self, n: int = 1, timeout: Optional[float] = None) -> bytes:
        if self.pending:
            out = bytes(self.pending[:n])
            del self.pending[:n]
            return out
        return b""

    def _write(self, data: bytes) -> int:
        self.b.extend(data)
        return len(data)

    def close(self) -> None:
        self._closed = True


def _pack_frames(src_sys: int, src_comp: int, frames: list[tuple[str, dict]]) -> bytes:
    """用 pymavlink 从指定 (sysid, compid) 打包若干帧，用于伪造噪声/越权写帧。"""

    class _Port:
        def __init__(self):
            self.b = bytearray()

        def write(self, d):
            self.b.extend(d)
            return len(d)

        def flush(self):
            pass

    w = _Port()
    m = D.MAVLink(w, src_sys, src_comp)
    for name, kw in frames:
        getattr(m, name)(**kw)
    return bytes(w.b)


def _pv(name: str, value: float, ptype: int = int(D.MAV_PARAM_TYPE_INT32),
        count: int = 3, index: int = 0) -> tuple[str, dict]:
    return ("param_value_send", dict(param_id=P._encode_param_id(name), param_value=float(value),
                                     param_type=int(ptype), param_count=int(count),
                                     param_index=int(index)))


def _param_set(name: str = LOG_BITMASK, value: float = DEFAULT_LOG_BITMASK) -> tuple[str, dict]:
    """一条**人为构造**的参数写帧：本端代码已无此入口，只用来证明护栏还能咬人。"""
    return ("param_set_send", dict(target_system=1, target_component=1,
                                   param_id=P._encode_param_id(name), param_value=float(value),
                                   param_type=int(D.MAV_PARAM_TYPE_INT32)))


def _cmd_long() -> tuple[str, dict]:
    return ("command_long_send", dict(target_system=1, target_component=1, confirmation=0,
                                      command=400, param1=1.0, param2=0.0, param3=0.0,
                                      param4=0.0, param5=0.0, param6=0.0, param7=0.0))


def _hb() -> tuple[str, dict]:
    return ("heartbeat_send", dict(type=8, autopilot=8, base_mode=0, custom_mode=0,
                                   system_status=4, mavlink_version=3))


# --------------------------------------------------------------------------- #
# 公共夹具
# --------------------------------------------------------------------------- #


class _FcCase(unittest.TestCase):
    """FakeFC + 内存管道会话；不碰串口/网络。"""

    def setUp(self):
        self.fc: Optional[FakeFC] = None
        self.sess: Optional[MavLinkSession] = None

    def tearDown(self):
        if self.sess is not None:
            self.sess.close()
        if self.fc is not None:
            self.fc.stop()

    def boot(self, *, params=None, answer_params=True, wrap=None) -> MavLinkSession:
        self.fc = FakeFC([(1, b"\x00" * 100)], heartbeat_period=0.1, params=params,
                         answer_params=answer_params)
        client = self.fc.serve_pipe("params")
        transport = wrap(client) if wrap is not None else client
        self.sess = MavLinkSession(transport, name="param-test")
        self.sess.start()
        t0 = time.monotonic()
        while self.sess.fc_sysid is None and time.monotonic() - t0 < HB_WAIT:
            time.sleep(0.02)
        self.assertIsNotNone(self.sess.fc_sysid, "FakeFC 心跳未在预期时间内被识别")
        return self.sess


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


class TestReadParam(_FcCase):

    def test_read_by_name_with_type(self):
        self.boot()
        pv = P.read_param(self.sess, LOG_BITMASK, timeout=2.0)
        self.assertTrue(pv.found)
        self.assertEqual(pv.name, LOG_BITMASK)
        self.assertEqual(pv.value, 180222.0)
        self.assertEqual(pv.param_type, int(D.MAV_PARAM_TYPE_INT32), "应带上飞控报回的真实类型")
        self.assertEqual(pv.reason, "")
        self.assertEqual(self.fc.reads, [LOG_BITMASK])

    def test_lowercase_name_still_matches(self):
        """线上是大小写不敏感匹配：用户/UI 传小写也必须能命中。"""
        self.boot()
        pv = P.read_param(self.sess, "log_bitmask", timeout=2.0)
        self.assertTrue(pv.found, pv.reason)
        self.assertEqual(pv.value, 180222.0)

    def test_missing_param_not_found(self):
        """参数不存在时飞控不回，本端必须 found=False 而不是抛异常。"""
        self.boot()
        pv = P.read_param(self.sess, "SDCARD_OPTIONS", timeout=0.4)
        self.assertFalse(pv.found)
        self.assertIsNone(pv.value)
        self.assertEqual(pv.reason, "timeout")

    def test_mute_params_gives_timeout(self):
        self.boot(answer_params=False)
        pv = P.read_param(self.sess, LOG_BACKEND_TYPE, timeout=0.3)
        self.assertFalse(pv.found)
        self.assertEqual(pv.reason, "timeout")

    def test_link_lost_on_closed_transport(self):
        self.boot()
        self.sess.close()
        pv = P.read_param(self.sess, LOG_BITMASK, timeout=1.0)
        self.assertFalse(pv.found)
        self.assertEqual(pv.reason, "link_lost")

    def test_send_failed_does_not_raise(self):
        self.boot(wrap=_BlackHoleWriteTransport)
        pv = P.read_param(self.sess, LOG_BITMASK, timeout=0.4)
        self.assertFalse(pv.found)
        self.assertTrue(pv.reason.startswith("send_failed: "), pv.reason)

    def test_no_heartbeat_raises_link_error(self):
        """"还没识别到飞控心跳"必须原样抛 LinkError，UI 要区分这个。"""
        _a, b = MemoryTransport.create_pair("nohb")
        sess = MavLinkSession(b, name="no-fc")
        sess.start()
        try:
            with self.assertRaises(LinkError) as cm:
                P.read_param(sess, LOG_BITMASK, timeout=0.3)
            self.assertIn("心跳", str(cm.exception))
        finally:
            sess.close()

    def test_read_params_returns_early_when_all_hit(self):
        """两个名字都命中就立即收窗：耗时远小于 timeout，而不是把窗口跑满。"""
        self.boot()
        t0 = time.monotonic()
        got = P.read_params(self.sess, (LOG_BACKEND_TYPE, LOG_BITMASK), timeout=3.0)
        took = time.monotonic() - t0
        self.assertTrue(got[LOG_BACKEND_TYPE].found and got[LOG_BITMASK].found)
        self.assertLess(took, 1.0, f"全部命中应立即返回（{took:.2f}s）")

    def test_read_params_shares_one_window(self):
        """串行等两个不存在参数会耗 2x timeout；共用一个应答窗口只会耗 1x。"""
        self.boot()
        t0 = time.monotonic()
        got = P.read_params(self.sess, ("NOPE_A", LOG_BITMASK, "NOPE_B"), timeout=0.6)
        took = time.monotonic() - t0
        self.assertEqual(set(got), {"NOPE_A", LOG_BITMASK, "NOPE_B"}, "缺的键也要在结果里")
        self.assertTrue(got[LOG_BITMASK].found)
        self.assertFalse(got["NOPE_A"].found and got["NOPE_B"].found)
        self.assertLess(took, 1.1, f"两个缺参不应叠加两次超时（{took:.2f}s）")

    def test_read_by_index_supported(self):
        """FakeFC 也要支持 param_index>=0 的按序读（真实 GCS 全量拉参数就这样发）。"""
        self.boot()
        sysid, compid = self.sess.target_ids()
        q = self.sess.listen(["PARAM_VALUE"])
        try:
            self.sess._send("param_request_read_send", sysid, compid,
                            P._encode_param_id(""), 1)
            m = q.get(timeout=2.0)
        finally:
            self.sess.unlisten(q)
        self.assertEqual(P._decode_param_id(m.param_id), LOG_BITMASK)
        self.assertEqual(float(m.param_value), 180222.0)

    def test_read_by_index_out_of_range_is_ignored(self):
        self.boot()
        sysid, compid = self.sess.target_ids()
        q = self.sess.listen(["PARAM_VALUE"])
        try:
            self.sess._send("param_request_read_send", sysid, compid,
                            P._encode_param_id(""), 99)
            with self.assertRaises(queue.Empty):
                q.get(timeout=0.4)
        finally:
            self.sess.unlisten(q)

    def test_noise_on_link_is_filtered_by_name(self):
        """别的 GCS 正在拉全量参数 + 别的 sysid 的冒名应答，也必须按名字命中。"""
        noise = _pack_frames(1, 1, [
            _pv("FRAME_MAX_LEN", 12.0),
            _pv("AHRS_TRIM_X", -0.01, int(D.MAV_PARAM_TYPE_REAL32)),
            _pv("LOG_DISARMED", 0.0),
        ]) + _pack_frames(7, 1, [                        # 另一台机的“LOG_BITMASK=0”：必须被忽略
            _pv(LOG_BITMASK, 0.0),
        ]) + _pack_frames(1, 1, [
            _pv("GPS_TYPE", 5.0), _pv("SCHEMA_VERSION", 10.0),
        ])
        self.boot(wrap=lambda inner: _InjectedTransport(inner, noise))
        pv = P.read_param(self.sess, LOG_BITMASK, timeout=2.5)
        self.assertTrue(pv.found, pv.reason)
        self.assertEqual(pv.value, 180222.0, "绝不能取第一条 PARAM_VALUE，必须按名字过滤")
        self.assertEqual(pv.param_type, int(D.MAV_PARAM_TYPE_INT32))


# --------------------------------------------------------------------------- #
# classify 真值表（suggest = (参数名, 当前值或 None, 建议值)）
# --------------------------------------------------------------------------- #

FOUND_BACKEND = ParamValue(LOG_BACKEND_TYPE, True, 1.0, 6)
FOUND_BACKEND_OFF = ParamValue(LOG_BACKEND_TYPE, True, 0.0, 6)
FOUND_BACKEND_LEGACY = ParamValue(LOG_BACKEND_TYPE, True, 2.0, 6)   # bit0=0（只有非文件后端）
FOUND_MASK_OK = ParamValue(LOG_BITMASK, True, 180222.0, 6)
FOUND_MASK_ZERO = ParamValue(LOG_BITMASK, True, 0.0, 6)
MISS_BACKEND = ParamValue(LOG_BACKEND_TYPE, False, None, 0, "timeout")
MISS_MASK = ParamValue(LOG_BITMASK, False, None, 0, "timeout")

# 关键不变式：**只有 disabled 才允许带 suggest**（unknown 一律不给写入建议）
_CLASSIFY_CASES = [
    ("两个都正常", FOUND_BACKEND, FOUND_MASK_OK, "enabled", ()),
    ("后端 bit0=0", FOUND_BACKEND_OFF, FOUND_MASK_OK, "disabled",
     ((LOG_BACKEND_TYPE, 0.0, DEFAULT_LOG_BACKEND_TYPE),)),
    ("后端只有非文件位", FOUND_BACKEND_LEGACY, FOUND_MASK_OK, "disabled",
     ((LOG_BACKEND_TYPE, 2.0, DEFAULT_LOG_BACKEND_TYPE),)),
    ("bitmask=0", FOUND_BACKEND, FOUND_MASK_ZERO, "disabled",
     ((LOG_BITMASK, 0.0, DEFAULT_LOG_BITMASK),)),
    ("两个都坏（backend 在前）", FOUND_BACKEND_OFF, FOUND_MASK_ZERO, "disabled",
     ((LOG_BACKEND_TYPE, 0.0, DEFAULT_LOG_BACKEND_TYPE),
      (LOG_BITMASK, 0.0, DEFAULT_LOG_BITMASK))),
    ("后端缺失但 bitmask 非 0", MISS_BACKEND, FOUND_MASK_OK, "enabled", ()),
    ("后端缺失且 bitmask=0", MISS_BACKEND, FOUND_MASK_ZERO, "disabled",
     ((LOG_BITMASK, 0.0, DEFAULT_LOG_BITMASK),)),
    ("bitmask 缺失但后端 bit0=1", FOUND_BACKEND, MISS_MASK, "unknown", ()),
    ("bitmask 缺失且后端 bit0=0", FOUND_BACKEND_OFF, MISS_MASK, "disabled",
     ((LOG_BACKEND_TYPE, 0.0, DEFAULT_LOG_BACKEND_TYPE),)),
    ("两个都缺", MISS_BACKEND, MISS_MASK, "unknown", ()),
]


class TestClassify(unittest.TestCase):
    """纯函数真值表：逐项断言 state 与 suggest（只列真正需要用户手动改的参数）。"""

    def test_truth_table(self):
        for label, backend, bitmask, want_state, want_suggest in _CLASSIFY_CASES:
            with self.subTest(case=label):
                cfg = classify(backend, bitmask)
                self.assertEqual(cfg.state, want_state, cfg.detail)
                self.assertEqual(tuple(cfg.suggest), tuple(want_suggest), cfg.detail)
                self.assertIs(cfg.backend, backend)
                self.assertIs(cfg.bitmask, bitmask)
                self.assertTrue(cfg.detail, "detail 不能为空")
                self.assertNotIn("未知错误", cfg.detail)
                self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in cfg.detail),
                                f"detail 要直接给中文用户看：{cfg.detail}")

    def test_suggest_field_shape(self):
        """suggest 三元组必须是 (名字, 当前值或 None, 建议值)，且建议值取实测常量。"""
        cfg = classify(FOUND_BACKEND_OFF, FOUND_MASK_ZERO)
        self.assertTrue(cfg.suggest)
        for item in cfg.suggest:
            self.assertEqual(len(item), 3)
            name, current, target = item
            self.assertIsInstance(name, str)
            self.assertTrue(current is None or isinstance(current, float))
            self.assertIsInstance(target, float)
            # 只列真读到的坏参数，所以当前值一定带得回来（None 只是类型上的余量）
            self.assertIsNotNone(current, cfg.detail)
            self.assertNotEqual(current, target, "不该建议一个和现值相同的参数")
        self.assertEqual([n for n, _, _ in cfg.suggest], [LOG_BACKEND_TYPE, LOG_BITMASK],
                         "顺序必须 backend 在前、bitmask 在后")
        self.assertIn(DEFAULT_LOG_BACKEND_TYPE, [t for _, _, t in cfg.suggest])
        self.assertIn(DEFAULT_LOG_BITMASK, [t for _, _, t in cfg.suggest])

    def test_legacy_backend_missing_reports_unknown_type(self):
        """老固件没有 LOG_BACKEND_TYPE：判 enabled，但 detail 必须写明后端类型未知。"""
        cfg = classify(MISS_BACKEND, FOUND_MASK_OK)
        self.assertEqual(cfg.state, "enabled")
        self.assertIn("未知", cfg.detail)
        self.assertIn(LOG_BACKEND_TYPE, cfg.detail)
        self.assertEqual(cfg.suggest, ())

    def test_unknown_never_suggests_writes(self):
        """unknown / enabled 永远不给建议；只有 disabled 才可能非空。"""
        for backend, bitmask in ((MISS_BACKEND, MISS_MASK), (FOUND_BACKEND, MISS_MASK),
                                 (FOUND_BACKEND, FOUND_MASK_OK), (MISS_BACKEND, FOUND_MASK_OK),
                                 (FOUND_BACKEND_OFF, FOUND_MASK_ZERO)):
            cfg = classify(backend, bitmask)
            if cfg.state != "disabled":
                with self.subTest(state=cfg.state):
                    self.assertEqual(cfg.suggest, ())
                    self.assertEqual(cfg.suggest_note, "")

    def test_disabled_detail_tells_the_user_how_to_act(self):
        """检测到手，但不代手：detail 要说清怎么改，而不是"我来写"。"""
        cfg = classify(FOUND_BACKEND_OFF, FOUND_MASK_ZERO)
        self.assertIn("日志写入未开启", cfg.detail)
        self.assertIn("建议手动设为", cfg.detail)
        self.assertIn("本工具不会代你改参数", cfg.detail)
        self.assertNotIn("已写入", cfg.detail)
        for kw in ("Mission Planner", "MAVProxy", "QGC"):
            self.assertIn(kw, cfg.suggest_note)
        self.assertIn("180222", cfg.suggest_note)

    def test_detail_explains_which_param_was_not_read(self):
        cfg = classify(MISS_BACKEND, MISS_MASK)
        self.assertEqual(cfg.state, "unknown")
        self.assertIn(LOG_BACKEND_TYPE, cfg.detail)
        self.assertIn(LOG_BITMASK, cfg.detail)
        self.assertIn("超时", cfg.detail)


# --------------------------------------------------------------------------- #
# 探测：只读，且审计仍判"严格只读"
# --------------------------------------------------------------------------- #


class TestProbeNeverWrites(_FcCase):

    def test_probe_over_fc_writes_nothing(self):
        """最关键的一条：探测跑完，飞控侧一笔参数写都不该收到。"""
        self.boot(params=DISABLED_PARAMS)
        cfg = P.probe_log_write_config(self.sess, timeout=1.5)
        self.assertEqual(cfg.state, "disabled")
        self.assertEqual(tuple(cfg.suggest),
                         ((LOG_BACKEND_TYPE, 0.0, DEFAULT_LOG_BACKEND_TYPE),
                          (LOG_BITMASK, 0.0, DEFAULT_LOG_BITMASK)))
        self.assertEqual(self.fc.writes, [], "探测绝不发参数写指令")
        self.assertEqual(self.fc.stats.param_sets, 0)
        self.assertIn(LOG_BACKEND_TYPE, self.fc.reads, "读请求确实按名发出去了")
        self.assertEqual(self.fc.params[LOG_BITMASK], 0.0, "探测后飞控参数不该有任何变化")

    def test_probe_tx_stream_is_strictly_read_only(self):
        """同一条链路的 tx 审计：PARAM_REQUEST_READ 属预期内，只读判定必须通过。"""
        self.boot(params=DISABLED_PARAMS, wrap=AuditTransport)
        cfg = P.probe_log_write_config(self.sess, timeout=1.5)
        snap = self.sess.transport.snapshot()
        self.assertEqual(cfg.state, "disabled")
        self.assertEqual(snap.forbidden, {}, f"只读探测不该命中禁发：{snap.forbidden}")
        self.assertTrue(snap.is_read_only(), snap.table())
        self.assertEqual(snap.counts.get("PARAM_REQUEST_READ"), 2, "两个参数各发一条按名读")
        self.assertNotIn("PARAM_REQUEST_READ", snap.unexpected, "已在白名单里")
        self.assertIn("只读判定：通过", snap.table())

    def test_probe_on_muted_link_is_unknown_without_suggestion(self):
        self.boot(answer_params=False)
        cfg = P.probe_log_write_config(self.sess, timeout=0.3)
        self.assertEqual(cfg.state, "unknown")
        self.assertEqual(cfg.suggest, ())
        self.assertEqual(self.fc.writes, [])


# --------------------------------------------------------------------------- #
# 防回退守卫：写能力必须"不存在"，护栏必须还有效
# --------------------------------------------------------------------------- #


class TestNoWriteCapability(unittest.TestCase):
    """这三条是本次决策变更的守门人：谁把写路径偷偷加回来，这里就红。"""

    def test_write_api_symbols_are_gone(self):
        for sym in ("write_param", "apply_log_write_enable", "WriteResult"):
            with self.subTest(sym=sym):
                self.assertFalse(hasattr(P, sym), f"params 里不该还有 {sym}")
                self.assertNotIn(sym, P.__all__)
        self.assertFalse(hasattr(P.LogWriteConfig, "writes"))
        fields = {f.name for f in dataclasses.fields(LogWriteConfig)}
        self.assertEqual(fields, {"state", "backend", "bitmask", "detail", "suggest",
                                  "suggest_note"})

    def test_params_source_contains_no_write_message(self):
        """源码级检查：不许出现参数写指令的发送调用或消息名。"""
        src = inspect.getsource(P)
        self.assertNotIn("param_set_send", src)
        self.assertNotIn("PARAM_SET", src)
        self.assertNotIn("write_param", src)
        # 整个 link 包里除了审计名单，也不该有任何发写指令的调用
        for p in sorted((ROOT / "apmlog" / "link").glob("*.py")):
            text = p.read_text(encoding="utf-8")
            if p.name == "audit.py":
                continue
            self.assertNotIn("param_set_send", text, f"{p.name} 里不该出现参数写调用")

    def test_module_docstring_declares_read_only(self):
        doc = inspect.getdoc(P) or ""
        self.assertIn("只读", doc)
        self.assertIn("PARAM_REQUEST_READ", doc)


class TestAuditStrictlyReadOnly(unittest.TestCase):
    """PARAM_SET 回到**无条件**计入 forbidden；受控写入那套口子必须已经拆掉。"""

    def _snap(self, frames: list[tuple[str, dict]]) -> TxAudit:
        sink = _SinkTransport()
        a = AuditTransport(sink)
        a.write(_pack_frames(255, 190, frames))
        return a.snapshot()

    def test_controlled_write_apis_are_gone(self):
        self.assertFalse(hasattr(A, "CONFIRMED_WRITE_PATTERNS"))
        self.assertFalse(hasattr(TxAudit, "is_controlled"))
        self.assertFalse(hasattr(TxAudit, "controlled_note"))
        self.assertNotIn("controlled_writes", {f.name for f in dataclasses.fields(TxAudit)})
        with self.assertRaises(TypeError):
            AuditTransport(_SinkTransport(), allow_param_set=True)      # type: ignore[call-arg]

    def test_param_set_is_unconditionally_forbidden(self):
        snap = self._snap([_param_set()])
        self.assertIn("PARAM_SET", snap.forbidden)
        self.assertEqual(snap.forbidden["PARAM_SET"], 1)
        self.assertFalse(snap.is_read_only())
        self.assertIn("**不通过**", snap.table())
        self.assertNotIn("受控写入", snap.table())

    def test_command_long_is_forbidden(self):
        snap = self._snap([_cmd_long()])
        self.assertIn("COMMAND_LONG", snap.forbidden)
        self.assertFalse(snap.is_read_only())

    def test_param_request_read_is_allowed(self):
        """上轮定下的白名单条目要留着：只读探测属"预期内"。"""
        self.assertIn("PARAM_REQUEST_READ", A.ALLOWED_PATTERNS)
        frames = [("param_request_read_send", dict(target_system=1, target_component=1,
                                                   param_id=P._encode_param_id(LOG_BITMASK),
                                                   param_index=-1)), _hb()]
        snap = self._snap(frames)
        self.assertEqual(snap.forbidden, {})
        self.assertEqual(snap.unexpected, {}, f"不该被算成意外：{snap.unexpected}")
        self.assertTrue(snap.is_read_only())
        self.assertIn("允许(只读)", snap.table())

    def test_readonly_verdict_wording_restored(self):
        sink = _SinkTransport()
        a = AuditTransport(sink)
        a.write(_pack_frames(255, 190, [_hb()]))
        txt = audit_summary(a)
        self.assertIn("只读判定：通过（无 PARAM_SET / COMMAND_* / FTP / 固件类发送）", txt)

    def test_empty_session_table(self):
        a = AuditTransport(_SinkTransport())
        self.assertIn("未发送任何可解析", a.snapshot().table())


class TestInjectedParamSetIsCaught(_FcCase):
    """端到端证明护栏有效：人为注入一条参数写帧 → 审计抓到、飞控不接受。"""

    def test_injection_is_recorded_flagged_and_not_stored(self):
        before = dict(DISABLED_PARAMS)
        self.boot(params=DISABLED_PARAMS, wrap=AuditTransport)
        frame = _pack_frames(255, 190, [_param_set(LOG_BITMASK, DEFAULT_LOG_BITMASK)])

        q = self.sess.listen(["PARAM_VALUE"])
        got: list[object] = []
        try:
            self.sess.transport.write(frame)        # 绕过本端 API：模拟"若有人偷偷发"
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.4:
                try:
                    got.append(q.get(timeout=0.05))
                except queue.Empty:
                    continue
        finally:
            self.sess.unlisten(q)

        self.assertEqual(got, [], "FakeFC 不该再对参数写请求回声（它不再模拟“可被写”的飞控）")
        # 1) 审计层：抓到并判为非只读
        snap = self.sess.transport.snapshot()
        self.assertIn("PARAM_SET", snap.forbidden)
        self.assertEqual(snap.counts.get("PARAM_SET"), 1)
        self.assertFalse(snap.is_read_only())
        self.assertIn("**不通过**", snap.table())
        self.assertNotIn("受控", snap.table())
        self.assertNotIn("受控", audit_summary(self.sess.transport))
        # 2) 飞控侧：收到并记账，但值没被改
        self.assertEqual(self.fc.writes, [(LOG_BITMASK, DEFAULT_LOG_BITMASK)])
        self.assertEqual(self.fc.stats.param_sets, 1)
        self.assertEqual(self.fc.params, before, "FakeFC 不应再接受任何参数写")
        self.assertEqual(self.fc.params[LOG_BITMASK], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
