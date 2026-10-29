# -*- coding: utf-8 -*-
"""tests/test_link_endpoint.py —— 目标解析 / 分帧 CRC / 会话心跳（无硬件，本机回环）。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m unittest tests.test_link_endpoint -v
"""
from __future__ import annotations

import hashlib
import queue
import socket
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pymavlink.dialects.v20 import ardupilotmega as D   # noqa: E402

from apmlog.link.audit import AuditTransport              # noqa: E402
from apmlog.link.endpoint import (                       # noqa: E402
    Endpoint, FrameReader, MavLinkSession, open_transport, parse_target,
)
from apmlog.link.fakes import FakeFC, FakeLog            # noqa: E402
from apmlog.link.transport import (                       # noqa: E402
    LinkError, TcpClientTransport, Transport, UdpOutTransport,
)


class _Port:
    def __init__(self):
        self.b = bytearray()

    def write(self, d):
        self.b.extend(d)
        return len(d)

    def flush(self):
        pass


def encode_frames(frames: list[tuple[str, dict]]) -> bytes:
    """用 pymavlink 打包若干帧，返回原始字节。"""
    w = _Port()
    m = D.MAVLink(w, 1, 1)
    for name, kw in frames:
        getattr(m, name)(**kw)
    return bytes(w.b)


class TestTargetParsing(unittest.TestCase):
    """目标串语法必须覆盖串口/COM、tcp、tcpsrv、udp、udpout 与裸 COM5。"""

    def test_all_schemes(self):
        cases = [
            ("serial:COM5?baud=57600", dict(kind="serial", device="COM5", baud=57600)),
            ("serial:COM7", dict(kind="serial", device="COM7", baud=57600)),
            ("serial:/dev/ttyACM0?baud=115200", dict(kind="serial", device="/dev/ttyACM0", baud=115200)),
            ("COM5", dict(kind="serial", device="COM5", baud=57600)),
            ("COM12?baud=921600" if False else "COM12", dict(kind="serial", device="COM12", baud=57600)),
            ("tcp:192.168.2.1:5760", dict(kind="tcp", host="192.168.2.1", port=5760)),
            ("192.168.2.1:5760", dict(kind="tcp", host="192.168.2.1", port=5760)),
            ("tcp:localhost:5760", dict(kind="tcp", host="localhost", port=5760)),
            ("tcpsrv:5760", dict(kind="tcpsrv", bind="0.0.0.0", port=5760)),
            ("tcpsrv:0.0.0.0:5760", dict(kind="tcpsrv", bind="0.0.0.0", port=5760)),
            ("tcpsrv:127.0.0.1:0", dict(kind="tcpsrv", bind="127.0.0.1", port=0)),
            ("udp:14550", dict(kind="udp", bind="0.0.0.0", port=14550)),
            ("udp:0.0.0.0:14550", dict(kind="udp", bind="0.0.0.0", port=14550)),
            ("udpout:192.168.2.1:14550", dict(kind="udpout", host="192.168.2.1", port=14550)),
            ("pipe:selftest", dict(kind="pipe")),
        ]
        for text, want in cases:
            ep = parse_target(text)
            self.assertIsInstance(ep, Endpoint, text)
            self.assertEqual(ep.kind, want["kind"], text)
            if "device" in want:
                self.assertEqual(ep.device, want["device"], text)
            if "baud" in want:
                self.assertEqual(ep.baud, want["baud"], text)
            if "host" in want:
                self.assertEqual(ep.host, want["host"], text)
            if "port" in want:
                self.assertEqual(ep.port, want["port"], text)
            if "bind" in want:
                self.assertEqual(ep.bind, want["bind"], text)
        # 裸串口 + 显式默认波特率
        self.assertEqual(parse_target("COM3", default_baud=115200).baud, 115200)

    def test_rejects_garbage(self):
        for bad in ("", "   ", "nonsense", "tcp:onlyhost", "udp:notaport", "serial:", "COM?"):
            with self.assertRaises(ValueError, msg=bad):
                parse_target(bad)
        # 无协议头的 host:port 故意解释为 TCP 客户端（语法糖，不是错误）
        self.assertEqual(parse_target("xyz:1").kind, "tcp")

    def test_serial_transport_reports_missing_pyserial(self):
        """本机 venv 未装 pyserial —— 必须给出可执行的安装提示而不是静默失败。"""
        try:
            import serial  # type: ignore  # noqa: F401
            self.skipTest("pyserial 已安装，跳过缺失依赖分支")
        except ImportError:
            pass
        ep = parse_target("serial:COM9?baud=57600")
        with self.assertRaises(LinkError) as cm:
            open_transport(ep)
        self.assertIn("pyserial", str(cm.exception))

    def test_pipe_transport_pair(self):
        a = open_transport(parse_target("pipe:unittest"))
        b = open_transport.last_pair
        b.write(b"hello")
        self.assertEqual(a.read(5, timeout=1.0), b"hello")
        a.close()
        b.close()


class TestFrameReader(unittest.TestCase):
    """分帧 + 严格 CRC + 垃圾字节容忍（含 Mission Planner tlog 的 8B 大端时间戳）。"""

    def test_roundtrip_and_garbage_prefix(self):
        raw = encode_frames([
            ("heartbeat_send", dict(type=2, autopilot=3, base_mode=0, custom_mode=0, system_status=4,
                                    mavlink_version=3)),
            ("log_entry_send", dict(id=167, num_logs=131, last_log_num=131, time_utc=315532800, size=2945024)),
            ("log_request_data_send", dict(target_system=255, target_component=190, id=167, ofs=1207800, count=1800)),
        ])
        r = FrameReader()
        # 前面撒 7 字节垃圾（其中不含 magic），再加一个 8B 大端 µs 时间戳
        junk = bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
        ts = int(time.time() * 1e6).to_bytes(8, "big")
        frames = list(r.feed(junk + ts + raw))
        self.assertEqual(len(frames), 3)
        self.assertEqual(r.crc_errors, 0)
        self.assertGreaterEqual(r.dropped_bytes, len(junk))

    def test_corrupt_crc_is_counted_and_dropped(self):
        raw = bytearray(encode_frames([
            ("log_data_send", dict(id=167, ofs=0, count=3, data=[1, 2, 3] + [0] * 87)),
        ]))
        raw[-3] ^= 0xFF                             # 破坏 CRC 低位
        r = FrameReader()
        self.assertEqual(list(r.feed(bytes(raw))), [])
        self.assertEqual(r.crc_errors, 1)
        self.assertEqual(r.rx_frames, 0)

    def test_incremental_split_across_reads(self):
        raw = encode_frames([
            ("log_entry_send", dict(id=1, num_logs=2, last_log_num=2, time_utc=0, size=1000)),
            ("log_request_end_send", dict(target_system=255, target_component=190)),
        ])
        r = FrameReader()
        frames: list[bytes] = []
        for i in range(0, len(raw), 7):             # 每次只喂 7 字节
            frames.extend(r.feed(raw[i:i + 7]))
        self.assertEqual(len(frames), 2)

    def test_v1_frames_are_parsed(self):
        """v1(0xFE) 帧同样要能解 —— 真实链路里两种线格式都可能出现。"""
        w = _Port()
        m = D.MAVLink(w, 1, 1)
        m.heartbeat_send(0, 0, 0, 0, 0, 3, force_mavlink1=True)
        r = FrameReader()
        frames = list(r.feed(bytes(w.b)))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], 0xFE)
        msg = (D.MAVLink(_Port(), 1, 1).parse_buffer(frames[0]) or [None])[0]
        self.assertIsNotNone(msg)
        self.assertEqual(msg.get_type(), "HEARTBEAT")


class _StubTransport(Transport):
    """只发送不收的传输（用于统计发送行为）。"""

    def __init__(self):
        super().__init__()
        self.sent = bytearray()

    def read(self, n=1, timeout=None):
        time.sleep(min(timeout or 0.05, 0.05))
        return b""

    def _write(self, data):
        self.sent.extend(data)
        return len(data)

    def close(self):
        self._closed = True


class TestSessionOverPipe(unittest.TestCase):
    """会话：心跳识别、STATUSTEXT、定向请求目标、断链状态。"""

    def setUp(self):
        self.src = Path(r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000167.BIN")
        if not self.src.exists():
            self.logbytes = bytes(range(256)) * 40
        else:
            self.logbytes = self.src.read_bytes()
        self.fc = FakeFC([FakeLog(log_id=167, data=self.logbytes)], statustext=("APMLOG fake fc up",))
        self.client = self.fc.serve_pipe("sess")
        self.sess = MavLinkSession(self.client, name="pipe-sess")
        self.sess.start()

    def tearDown(self):
        self.sess.close()
        self.fc.stop()

    def test_heartbeat_identifies_fc(self):
        t0 = time.monotonic()
        while self.sess.fc_sysid is None and time.monotonic() - t0 < 5:
            time.sleep(0.02)
        self.assertEqual(self.sess.fc_sysid, 1, "应从 autopilot=3/compid=1 的心跳识别飞控")
        self.assertEqual(self.sess.fc_compid, 1)
        self.assertEqual(self.sess.state, "ready")
        hb = self.sess.heartbeats[(1, 1)]
        self.assertEqual(hb.autopilot, D.MAV_AUTOPILOT_ARDUPILOTMEGA)
        self.assertEqual(hb.type, D.MAV_TYPE_QUADROTOR)
        self.assertFalse(hb.armed)
        self.assertGreaterEqual(hb.count, 1)
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_statustext_captured(self):
        t0 = time.monotonic()
        while not self.sess.statustexts and time.monotonic() - t0 < 5:
            time.sleep(0.02)
        self.assertIn("APMLOG fake fc up", self.sess.statustexts)

    def test_target_ids_raise_without_fc(self):
        t = _StubTransport()
        s = MavLinkSession(t, name="stub")
        s.start()
        with self.assertRaises(LinkError):
            s.target_ids()
        s.close()

    def test_requests_target_the_fc(self):
        t0 = time.monotonic()
        while self.sess.fc_sysid is None and time.monotonic() - t0 < 5:
            time.sleep(0.02)
        self.sess.send_log_request_list()
        self.sess.send_log_request_data(167, 0, 90)
        deadline = time.monotonic() + 5
        while (self.fc.stats.list_requests < 1 or self.fc.stats.data_requests < 1) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.fc.stats.list_requests, 1)
        self.assertEqual(self.fc.stats.data_requests, 1)
        kinds = [k for k, _ in self.fc.request_log]
        self.assertEqual(kinds, ["LOG_REQUEST_LIST", "LOG_REQUEST_DATA"])
        self.assertEqual(self.fc.request_log[1][1], (167, 0, 90))

    def test_link_lost_state_callback(self):
        seen = []
        self.sess.on_state(lambda st, detail: seen.append(st))
        t0 = time.monotonic()
        while self.sess.fc_sysid is None and time.monotonic() - t0 < 5:
            time.sleep(0.02)
        self.fc.stop()                                # 假飞控下线
        self.client.close()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and self.sess.state not in ("lost", "closed"):
            time.sleep(0.05)
        self.assertIn(self.sess.state, ("lost", "closed"), f"状态转移未发生: {seen}")

    def test_listen_unlisten(self):
        q = self.sess.listen(["HEARTBEAT"])
        m = None
        deadline = time.monotonic() + 5
        while m is None and time.monotonic() < deadline:
            try:
                m = q.get(timeout=0.2)
            except queue.Empty:
                continue
        self.assertIsNotNone(m)
        self.assertEqual(m.get_type(), "HEARTBEAT")
        self.sess.unlisten(q)
        self.assertNotIn(q, self.sess._listeners.get("HEARTBEAT", []))

    def test_diagnostics_and_summary(self):
        t0 = time.monotonic()
        while self.sess.fc_sysid is None and time.monotonic() - t0 < 5:
            time.sleep(0.02)
        d = self.sess.diagnostics()
        self.assertEqual(d["fc"]["sysid"], 1)
        self.assertEqual(d["state"], "ready")
        self.assertIn("stats", d)
        s = self.sess.summary()
        self.assertIn("pipe-sess", s)
        self.assertIn("rx ", s)


class TestSessionOverUdp(unittest.TestCase):
    """UDP 回环：GCS 先发包，飞控才能学到对端；之后心跳识别与统计。"""

    def test_udp_heartbeat_and_stats(self):
        fc = FakeFC([FakeLog(log_id=1, data=b"x" * 450)], heartbeat_period=0.15)
        bind, port = fc.serve_udp("127.0.0.1", 0)
        sess = MavLinkSession(UdpOutTransport("127.0.0.1", port), name="udp-test",
                              send_our_heartbeat=True)
        sess.start()
        try:
            t0 = time.monotonic()
            while sess.fc_sysid is None and time.monotonic() - t0 < 6:
                time.sleep(0.02)
            self.assertEqual(sess.fc_sysid, 1, "UDP 上应学到飞控地址")
            self.assertLess(time.monotonic() - t0, 6.0)
            time.sleep(0.6)                          # 等几个心跳周期
            st = sess.stats
            self.assertGreater(st.rx_frames, 3)
            self.assertEqual(st.crc_errors, 0)
            sess.send_log_request_list()
            deadline = time.monotonic() + 5
            while fc.stats.list_requests < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(fc.stats.list_requests, 1)
        finally:
            sess.close()
            fc.stop()


class _SinkTransport(Transport):
    """把写出的字节留在内存里，读侧按预置队列回放（给审计层做单测）。"""

    def __init__(self) -> None:
        super().__init__()
        self.tx = bytearray()
        self.pending = bytearray()

    def read(self, n: int = 1, timeout=None) -> bytes:
        out = bytes(self.pending[:n])
        del self.pending[:n]
        return out

    def _write(self, data: bytes) -> int:
        self.tx.extend(data)
        return len(data)

    def close(self) -> None:
        self._closed = True


class TestTxAudit(unittest.TestCase):
    """只读审计：本端 tx 逐 msgid 计数，写操作/控制类必须被拦下。"""

    def _audit(self, frames: list[tuple[str, dict]]) -> tuple[AuditTransport, _SinkTransport]:
        sink = _SinkTransport()
        a = AuditTransport(sink)
        w = _Port()
        m = D.MAVLink(w, 1, 1)
        for name, kw in frames:
            getattr(m, name)(**kw)
        a.write(bytes(w.b))
        return a, sink

    def test_readonly_session_passes(self):
        a, _ = self._audit([
            ("heartbeat_send", dict(type=2, autopilot=3, base_mode=0, custom_mode=0,
                                    system_status=4, mavlink_version=3)),
            ("log_request_list_send", dict(target_system=1, target_component=1, start=0, end=65535)),
            ("log_request_data_send", dict(target_system=1, target_component=1, id=167, ofs=0, count=1800)),
            ("log_request_data_send", dict(target_system=1, target_component=1, id=167, ofs=1800, count=1800)),
        ])
        snap = a.snapshot()
        self.assertEqual(snap.counts.get("HEARTBEAT"), 1)
        self.assertEqual(snap.counts.get("LOG_REQUEST_LIST"), 1)
        self.assertEqual(snap.counts.get("LOG_REQUEST_DATA"), 2)
        self.assertEqual(snap.forbidden, {}, f"不该有禁发命中：{snap.forbidden}")
        self.assertTrue(snap.is_read_only())
        self.assertIn("只读判定：通过", snap.table())

    def test_param_set_is_flagged(self):
        a, _ = self._audit([
            ("param_set_send", dict(target_system=1, target_component=1, param_id=b"FRAME_XY",
                                    param_value=1.0, param_type=9)),
            ("command_long_send", dict(target_system=1, target_component=1, confirmation=0,
                                       command=400, param1=1.0, param2=0.0, param3=0.0,
                                       param4=0.0, param5=0.0, param6=0.0, param7=0.0)),
        ])
        snap = a.snapshot()
        self.assertEqual(snap.counts.get("PARAM_SET"), 1)
        self.assertEqual(snap.counts.get("COMMAND_LONG"), 1)
        self.assertIn("PARAM_SET", snap.forbidden)
        self.assertIn("COMMAND_LONG", snap.forbidden)
        self.assertFalse(snap.is_read_only())
        self.assertIn("不通过", snap.table())

    def test_rx_side_passthrough_and_tally(self):
        sink = _SinkTransport()
        a = AuditTransport(sink)
        sink.pending = bytearray(encode_frames([
            ("log_entry_send", dict(id=4, num_logs=5, last_log_num=5, time_utc=315532800, size=446464)),
            ("heartbeat_send", dict(type=2, autopilot=3, base_mode=81, custom_mode=0,
                                    system_status=4, mavlink_version=3)),
        ]))
        got = a.read_at_least(64, time.monotonic() + 2)
        self.assertGreater(len(got), 0)
        rx = a.rx_summary()
        self.assertEqual(rx.get("LOG_ENTRY"), 1)
        self.assertEqual(rx.get("HEARTBEAT"), 1)
        self.assertEqual(a.snapshot().counts, {}, "读侧不应被计入发送")


if __name__ == "__main__":
    unittest.main(verbosity=2)
