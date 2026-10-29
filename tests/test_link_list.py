# -*- coding: utf-8 -*-
"""tests/test_link_list.py —— LOG_REQUEST_LIST → LOG_ENTRY 聚合、容错与诊断。

字段数值证据（真实 tlog 解出，见 docs/LOG_DOWNLOAD_NOTES.md）：
    LOG_ENTRY {'id':1,'num_logs':131,'last_log_num':131,'time_utc':315532800,'size':819200}
    LOG_ENTRY {'id':2,'num_logs':131,'last_log_num':131,'time_utc':315532800,'size':915116}
本测试同时用**真实 tlog 的数值**构造线上帧，再用 FakeFC 走 TCP 回环验证聚合。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m unittest tests.test_link_list -v
"""
from __future__ import annotations

import os
import queue
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 真 TCP 回环用例开关：**默认开启**，只有显式设 APMLOG_LINK_TCP_TESTS=0 才跳过（不静默跳过）。
TCP_TESTS_ENABLED = os.environ.get("APMLOG_LINK_TCP_TESTS", "1").strip().lower() not in ("0", "off", "no")
TCP_SKIP_REASON = "APMLOG_LINK_TCP_TESTS=0 显式关闭了真 TCP 回环用例（默认运行）"
sys.path.insert(0, str(ROOT))

from pymavlink.dialects.v20 import ardupilotmega as D   # noqa: E402

from apmlog.link import download as dl                  # noqa: E402
from apmlog.link.download import BLOCK_BYTES, RemoteLog  # noqa: E402
from apmlog.link.endpoint import MavLinkSession          # noqa: E402
from apmlog.link.fakes import FakeFC, FakeLog            # noqa: E402
from apmlog.link.transport import LinkError, TcpClientTransport  # noqa: E402


class _Port:
    def __init__(self):
        self.b = bytearray()

    def write(self, d):
        self.b.extend(d)
        return len(d)

    def flush(self):
        pass


def wire_messages(frames: list[tuple[str, dict]]) -> list:
    """把要发的消息真正打包上线再解回来，得到带正确类型的消息对象。"""
    w = _Port()
    m = D.MAVLink(w, 1, 1)
    for fn, kw in frames:
        getattr(m, fn)(**kw)
    dec = D.MAVLink(_Port(), 1, 1)
    return [x for x in (dec.parse_buffer(bytes(w.b)) or [])]


class StubSession:
    """只用来测试聚合逻辑的假会话（不碰网络）。"""

    def __init__(self, msgs: list, *, connected: bool = True, fail_send: bool = False) -> None:
        self.msgs = msgs
        self.connected = connected
        self.fail_send = fail_send
        self.queues: list[queue.Queue] = []
        self.sent_list = 0

    def listen(self, names, *, maxsize: int = 0) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue()
        names = set(names)
        for m in self.msgs:
            if m.get_type() in names:
                q.put(m)
        self.queues.append(q)
        return q

    def unlisten(self, q) -> None:
        if q in self.queues:
            self.queues.remove(q)

    def send_log_request_list(self, *, start: int = 0, end: int = 0xFFFF) -> None:
        if self.fail_send:
            raise LinkError("尚未识别到飞控心跳（HEARTBEAT），无法发送定向请求")
        self.sent_list += 1

    @property
    def stats(self):
        class _S:
            @staticmethod
            def as_dict():
                return {"rx_frames": 0, "crc_errors": 0}
        return _S()


class TestRemoteLogModel(unittest.TestCase):
    """RemoteLog 的字段换算：RTC 占位、块数、尾块、文件名。"""

    def test_tlog_values(self):
        m = wire_messages([("log_entry_send", dict(id=2, num_logs=131, last_log_num=131,
                                                   time_utc=315532800, size=915116))])[0]
        e = RemoteLog.from_message(m)
        self.assertEqual((e.log_id, e.num_logs, e.last_log_num, e.time_utc, e.size),
                         (2, 131, 131, 315532800, 915116))
        self.assertEqual(e.blocks, 10168)                       # ceil(915116/90)
        self.assertEqual(e.tail_bytes, 915116 % 90)             # 56
        self.assertFalse(e.has_rtc, "315532800=1980 是实测占位值，不能当创建时间")
        self.assertIsNone(e.created)
        self.assertEqual(e.name, "00000002.BIN")
        self.assertIn("RTC未设置", e.label())

    def test_real_rtc(self):
        e = RemoteLog(log_id=125, num_logs=131, last_log_num=131,
                      time_utc=1791359555, size=30244860)
        self.assertTrue(e.has_rtc)
        self.assertIsNotNone(e.created)
        self.assertEqual(e.created.year, 2026)
        self.assertEqual(e.blocks, 336054)


class TestListAggregation(unittest.TestCase):
    def test_complete_list_fast(self):
        msgs = wire_messages([("log_entry_send", dict(id=i, num_logs=4, last_log_num=4,
                                                      time_utc=315532800, size=1000 + i))
                              for i in (3, 1, 4, 2)])
        s = StubSession(msgs)
        d: dict = {}
        t0 = time.monotonic()
        logs = dl.list_logs(s, timeout=5.0, diagnostics=d)
        self.assertEqual([x.log_id for x in logs], [1, 2, 3, 4], "必须按 id 排序")
        self.assertEqual([x.size for x in logs], [1001, 1002, 1003, 1004])
        self.assertEqual(d["reason"], "complete")
        self.assertTrue(d["complete"])
        self.assertLess(time.monotonic() - t0, 1.0, "收齐应立即返回")
        self.assertEqual(s.sent_list, 1)

    def test_partial_tolerated(self):
        """期望 5 条只来 2 条：返回已收到的，并在诊断里说明不完整，不能抛异常。"""
        msgs = wire_messages([("log_entry_send", dict(id=i, num_logs=5, last_log_num=5,
                                                      time_utc=315532800, size=90))
                              for i in (1, 2)])
        s = StubSession(msgs)
        d: dict = {}
        logs = dl.list_logs(s, timeout=0.7, diagnostics=d)
        self.assertEqual(len(logs), 2)
        self.assertEqual(d["expected"], 5)
        self.assertEqual(d["received"], 2)
        self.assertFalse(d["complete"])
        self.assertEqual(d["reason"], "timeout")

    def test_duplicate_entries_deduped(self):
        msgs = wire_messages([("log_entry_send", dict(id=5, num_logs=1, last_log_num=5,
                                                      time_utc=315532800, size=111))] * 3)
        s = StubSession(msgs)
        logs = dl.list_logs(s, timeout=1.0)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].size, 111)

    def test_send_failure_propagates_with_diagnostics(self):
        s = StubSession([], fail_send=True)
        d: dict = {}
        with self.assertRaises(LinkError):
            dl.list_logs(s, timeout=1.0, diagnostics=d)
        self.assertIn("LOG_REQUEST_LIST", d["error"])

    def test_statustext_ignored_in_list(self):
        msgs = wire_messages([
            ("statustext_send", dict(severity=D.MAV_SEVERITY_INFO, text=b"hello" + bytes(45), id=0)),
            ("log_entry_send", dict(id=1, num_logs=1, last_log_num=1, time_utc=1, size=90)),
        ])
        logs = dl.list_logs(StubSession(msgs), timeout=1.0)
        self.assertEqual(len(logs), 1)


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestListOverTcp(unittest.TestCase):
    """FakeFC TCP 回环：列表数量、编号、size 与源字节数严格一致。"""

    def setUp(self):
        a = bytes(range(251)) * 7 + b"\xff" * 31          # 长度非 90 整数倍
        b = b"Q" * 90 * 5
        c = b"Z" * (BLOCK_BYTES * 2 + 44)
        self.fc = FakeFC([FakeLog(1, a), FakeLog(2, b), FakeLog(9, c)], heartbeat_period=0.15)
        host, port = self.fc.serve_tcp("127.0.0.1", 0)
        self.sess = MavLinkSession(TcpClientTransport(host, port), name="list-tcp")
        self.sess.start()
        t0 = time.monotonic()
        while self.sess.fc_sysid is None and time.monotonic() - t0 < 6:
            time.sleep(0.02)
        self.assertIsNotNone(self.sess.fc_sysid, "TCP 回环应识别到飞控")

    def tearDown(self):
        self.sess.close()
        self.fc.stop()

    def test_full_list(self):
        d: dict = {}
        logs = dl.list_logs(self.sess, timeout=6.0, diagnostics=d)
        self.assertEqual([x.log_id for x in logs], [1, 2, 9])
        self.assertEqual([x.size for x in logs], [1788, 450, 224],
                         "LOG_ENTRY.size 应等于源字节数（1788=251*7+31，450=90*5，224=90*2+44）")
        self.assertEqual([len(self.fc.data_of(x.log_id)) for x in logs],
                         [x.size for x in logs], "LOG_ENTRY.size 必须等于源日志字节数")
        self.assertTrue(all(x.num_logs == 3 and x.last_log_num == 9 for x in logs))
        self.assertEqual(d["reason"], "complete")
        self.assertEqual(self.fc.stats.list_requests, 1)

    def test_range_filter(self):
        logs = dl.list_logs(self.sess, timeout=6.0, start=2, end=9)
        self.assertEqual([x.log_id for x in logs], [2, 9])

    def test_muted_fc_returns_empty(self):
        """飞控完全不应答 → 空列表 + 明确诊断，而不是异常。"""
        fc2 = FakeFC([FakeLog(1, b"x" * 100)], mute_list=True, heartbeat_period=0.15)
        host, port = fc2.serve_tcp("127.0.0.1", 0)
        s2 = MavLinkSession(TcpClientTransport(host, port), name="muted")
        s2.start()
        try:
            t0 = time.monotonic()
            while s2.fc_sysid is None and time.monotonic() - t0 < 6:
                time.sleep(0.02)
            d: dict = {}
            logs = dl.list_logs(s2, timeout=0.8, diagnostics=d)
            self.assertEqual(logs, [])
            self.assertEqual(d["received"], 0)
            self.assertIn(d["reason"], ("timeout", "link_lost"))
            self.assertFalse(d["complete"])
        finally:
            s2.close()
            fc2.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
