# -*- coding: utf-8 -*-
"""tests/test_mavftp.py —— MAVFTP 只读通道（``FILE_TRANSFER_PROTOCOL`` 110）。

覆盖三层：

1. 报文层：``open_ro`` / ``read`` / ``burst_read`` / ``list_dir`` / ``terminate``，
   以及“打不开就抛 :class:`MavFtpError`”；
2. 文件层：``fetch`` 落盘内容逐字节一致、``.part`` 收尾、取消/重试；
3. 链路层：``_LinkWorker._do_download`` 的三种通道选择
   （``ftp`` / ``log`` / ``auto`` 回退），并顺便记录同一条日志两个通道的耗时与吞吐。

全部跑在 FakeFC（假飞控）上——**真机未验证**，见 docs/CONTRACT.md §30。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m pytest tests/test_mavftp.py -q
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtWidgets                                   # noqa: E402

from apmlog.link import ftp as ftpmod                           # noqa: E402
from apmlog.link.download import RemoteLog                      # noqa: E402
from apmlog.link.endpoint import MavLinkSession                 # noqa: E402
from apmlog.link.fakes import FakeFC                            # noqa: E402
from apmlog.link.ftp import MavFtp, MavFtpError                 # noqa: E402
from apmlog.link.transport import TcpClientTransport            # noqa: E402
from apmlog.ui import linkpanel as lp                           # noqa: E402

_APP: QtWidgets.QApplication | None = None


def qapp() -> QtWidgets.QApplication:
    global _APP
    if _APP is None:
        _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


def pump(app: QtWidgets.QApplication, times: int = 3) -> None:
    for _ in range(times):
        app.processEvents()


def blob(n: int, seed: int = 0) -> bytes:
    """确定性伪随机内容（别用随机数，失败要可复现）。"""
    return bytes(((i * 31 + seed) & 0xFF) for i in range(n))


LOG_PATH = "/APM/LOGS/00000000.BIN"   # log_id=0 ↔ 00000000.BIN（真机实测 id 就是编号）


class _FtpBase(unittest.TestCase):
    """起一个 FakeFC + TCP 回环会话。"""

    files: dict[str, bytes] = {}

    def setUp(self):
        self.app = qapp()
        self.dir = Path(tempfile.mkdtemp(prefix="apmlog-ftp-"))
        self.fc = FakeFC([(0, b"")], heartbeat_period=0.1, files=dict(self.files))
        host, port = self.fc.serve_tcp("127.0.0.1", 0)
        self.sess = MavLinkSession(TcpClientTransport(host, port), name="ftp-test")
        self.sess.start()
        self.assertTrue(self.sess.wait_for("HEARTBEAT", timeout=5), "没有等到心跳")
        self.ftp = MavFtp(self.sess, timeout=2.0)

    def tearDown(self):
        try:
            self.sess.close()
        finally:
            self.fc.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        pump(self.app, 2)


class TestFtpProtocol(_FtpBase):
    files = {LOG_PATH: blob(600, 7)}

    def test_open_file_ro_returns_size(self):
        session, size = self.ftp.open_ro(LOG_PATH)
        self.assertIsInstance(session, int)
        self.assertEqual(size, 600)

    def test_read_single_packet(self):
        session, _size = self.ftp.open_ro(LOG_PATH)
        d = self.ftp.read(session, 0)
        self.assertEqual(d["opcode"], ftpmod.OP_ACK)
        self.assertEqual(d["data"], blob(600, 7)[:ftpmod.MAX_DATA])
        d2 = self.ftp.read(session, ftpmod.MAX_DATA)
        self.assertEqual(d2["data"], blob(600, 7)[ftpmod.MAX_DATA:2 * ftpmod.MAX_DATA])

    def test_burst_read_streams_until_burst_complete(self):
        payload = blob(40_000, 11)
        self.fc.files["/APM/LOGS/big.BIN"] = payload
        session, size = self.ftp.open_ro("/APM/LOGS/big.BIN")
        self.assertEqual(size, len(payload))
        got = bytearray()
        offset = 0
        rounds = 0
        while offset < len(payload) and rounds < 20:
            rounds += 1
            packets = self.ftp.burst_read(session, offset)
            self.assertTrue(packets, "burst 没有回包")
            self.assertTrue(packets[-1]["burst_complete"], "最后一包必须带 burst_complete")
            for p in packets:
                self.assertLessEqual(len(p["data"]), ftpmod.MAX_DATA)
                self.assertEqual(p["offset"], offset)
                got += p["data"]
                offset += len(p["data"])
        self.assertEqual(bytes(got), payload)
        self.assertLessEqual(rounds, 4, "40 KB 只该走几轮 burst")

    def test_list_dir_parses_type_and_size(self):
        self.fc.files["/APM/LOGS/sub/00000009.BIN"] = b"12345"
        rows = {r["name"]: r for r in self.ftp.list_dir("/APM/LOGS")}
        self.assertIn("00000000.BIN", rows)
        self.assertEqual(rows["00000000.BIN"]["size"], 600)
        self.assertFalse(rows["00000000.BIN"]["dir"])
        self.assertTrue(rows["sub"]["dir"])

    def test_terminate_closes_server_session(self):
        session, _ = self.ftp.open_ro(LOG_PATH)
        self.assertIn(session, self.fc.ftp_sessions)
        self.ftp.terminate(session)
        self.assertNotIn(session, self.fc.ftp_sessions)

    def test_open_missing_file_raises(self):
        with self.assertRaises(MavFtpError):
            self.ftp.open_ro("/APM/LOGS/nope.BIN")

    def test_read_on_dead_session_raises(self):
        with self.assertRaises(MavFtpError):
            self.ftp.read(99, 0)


class TestFtpFetch(_FtpBase):
    files = {LOG_PATH: blob(200_000, 3)}

    def test_fetch_writes_exact_bytes(self):
        out = self.dir / "00000001.BIN"
        res = self.ftp.fetch(LOG_PATH, out)
        self.assertEqual(res["bytes"], 200_000)
        self.assertGreater(res["rate"], 0.0)
        self.assertEqual(out.read_bytes(), blob(200_000, 3))
        self.assertFalse((self.dir / "00000001.BIN.part").exists(), ".part 必须收尾")

    def test_fetch_reports_progress_monotonically(self):
        seen: list[tuple[int, int]] = []
        self.ftp.fetch(LOG_PATH, self.dir / "p.BIN",
                       progress=lambda d, t, r: seen.append((d, t)))
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_fetch_cancel_leaves_part_file(self):
        out = self.dir / "c.BIN"
        with self.assertRaises(MavFtpError):
            self.ftp.fetch(LOG_PATH, out, cancel=lambda: True)
        self.assertFalse(out.exists())
        self.assertTrue((self.dir / "c.BIN.part").exists())

    def test_fetch_retries_then_succeeds_after_one_bad_burst(self):
        real = self.ftp.burst_read
        calls = {"n": 0}

        def flaky(session, offset):
            calls["n"] += 1
            if calls["n"] == 1:
                raise MavFtpError("模拟丢包")
            return real(session, offset)

        with mock.patch.object(self.ftp, "burst_read", side_effect=flaky):
            res = self.ftp.fetch(LOG_PATH, self.dir / "r.BIN")
        self.assertEqual(res["bytes"], 200_000)


class TestWorkerChannels(unittest.TestCase):
    """_LinkWorker 的通道选择（不起线程，直接同步调用，避免线程噪声）。"""

    LOG_ID = 0

    def setUp(self):
        self.app = qapp()
        self.dir = Path(tempfile.mkdtemp(prefix="apmlog-chan-"))
        self.blob = blob(60_000, 5)
        self.fc = FakeFC([(self.LOG_ID, self.blob)], heartbeat_period=0.1,
                         files={LOG_PATH: self.blob})
        self._workers: list[lp._LinkWorker] = []

    def tearDown(self):
        for w in self._workers:
            try:
                if w.sess is not None:
                    w.sess.close()
            except Exception:                               # noqa: BLE001
                pass
            w.deleteLater()
        self.fc.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        pump(self.app, 3)

    def _worker(self, transport: str) -> lp._LinkWorker:
        w = lp._LinkWorker()
        self._workers.append(w)
        w.set_options(out_dir=str(self.dir), blocks_per_request=120, transport=transport)
        host, port = self.fc.serve_tcp("127.0.0.1", 0)
        w._do_connect(target=f"tcp:{host}:{port}", baud=57600, wait=3.0)
        self.assertIsNotNone(w.sess, "worker 没连上 FakeFC")
        w._known = [RemoteLog(self.LOG_ID, 1, 1, 315532800, len(self.blob))]
        self._notes: list[str] = []
        self._done: list = []
        w.note.connect(self._notes.append)
        w.download_done.connect(self._done.append)
        return w

    def test_ftp_channel_downloads_and_uses_mavftp(self):
        w = self._worker("ftp")
        w._do_download(self.LOG_ID)
        self.assertEqual(len(self._done), 1)
        res = self._done[0]
        self.assertTrue(res.complete, res.reason)
        self.assertEqual(res.written_bytes, len(self.blob))
        self.assertEqual(Path(res.path).read_bytes(), self.blob)
        self.assertIn(ftpmod.OP_OPEN_FILE_RO, self.fc.ftp_ops)
        self.assertIn(ftpmod.OP_BURST_READ_FILE, self.fc.ftp_ops)
        self.assertIn(ftpmod.OP_TERMINATE_SESSION, self.fc.ftp_ops)
        self.assertGreater(res.bytes_per_sec, 0.0)
        print(f"\n  [实测] MAVFTP 通道 {res.written_bytes} B / {res.elapsed:.2f}s "
              f"= {res.bytes_per_sec / 1024:.0f} KB/s")

    def test_log_channel_never_touches_ftp(self):
        w = self._worker("log")
        w._do_download(self.LOG_ID)
        self.assertEqual(self.fc.ftp_ops, [], "选了 LOG_* 就不该发 FILE_TRANSFER_PROTOCOL")
        self.assertTrue(self._done[-1].complete, self._done[-1].reason)
        self.assertEqual(Path(self._done[-1].path).read_bytes(), self.blob)
        print(f"\n  [实测] LOG_* 通道 {self._done[-1].written_bytes} B / "
              f"{self._done[-1].elapsed:.2f}s = {self._done[-1].bytes_per_sec / 1024:.0f} KB/s")

    def test_auto_falls_back_to_log_when_ftp_unavailable(self):
        self.fc.files.clear()                       # 飞控上没有这个 MAVFTP 文件
        w = self._worker("auto")
        w._do_download(self.LOG_ID)
        res = self._done[-1]
        self.assertTrue(res.complete, res.reason)
        self.assertTrue(any("回退" in n for n in self._notes), self._notes)
        self.assertEqual(Path(res.path).read_bytes(), self.blob)

    def test_ftp_only_reports_failure_without_fallback(self):
        self.fc.files.clear()
        w = self._worker("ftp")
        w._do_download(self.LOG_ID)
        res = self._done[-1]
        self.assertFalse(res.complete)
        self.assertIn("MAVFTP", res.reason)


class TestFtpConstants(unittest.TestCase):
    def test_payload_geometry(self):
        self.assertEqual(ftpmod.PAYLOAD_LEN, 251)
        self.assertEqual(ftpmod.DATA_OFF, 12)
        self.assertEqual(ftpmod.HDR_LEN, 12)   # <HBBBBBBI
        self.assertEqual(ftpmod.MAX_DATA, 239)

    def test_mass_storage_command_numbers(self):
        self.assertEqual(ftpmod.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN, 246)
        self.assertEqual(ftpmod.REBOOT_SHUTDOWN_ACTION_REBOOT_TO_MASS_STORAGE, 5)
        self.assertEqual(ftpmod.DEFAULT_LOG_DIR, "/APM/LOGS")

    def test_pymavlink_agrees_with_our_constants(self):
        """上游 pymavlink 里也必须是这两个数（防手抄错）。"""
        from pymavlink.dialects.v20 import ardupilotmega as D
        self.assertEqual(int(D.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN), 246)
        self.assertEqual(int(D.MAV_RESULT_UNSUPPORTED), 3)
        self.assertEqual(int(D.MAV_RESULT_FAILED), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
