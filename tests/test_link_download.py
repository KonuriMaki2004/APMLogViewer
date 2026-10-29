# -*- coding: utf-8 -*-
"""tests/test_link_download.py —— 分块下载：逐字节一致性 + 丢块重传 + CRC 坏帧 + 取消。

主断言（任务书要求）：**从 FakeFC 下载回来的字节与真实 .BIN 逐字节相同**。

已确认并据以实现的线语义（docs/LOG_DOWNLOAD_NOTES.md 第 3 节，源自真实 tlog）：
    LOG_DATA   {'id':125,'ofs':0,'count':90,data[90]}，ofs 严格 +90，共 1 290 836 帧
    LOG_REQUEST_DATA {'id':125,'ofs':1207800,'count':180} / {'ofs':2017710,'count':270}
    → ofs 是**字节偏移**、count 是**字节数**
    LOG_ENTRY  {'id':2,'num_logs':131,'last_log_num':131,'time_utc':315532800,'size':915116}
    → 915116 % 90 = 56，尾块是半帧，按 size 截断

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m unittest tests.test_link_download -v
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 真 TCP 回环用例的开关：**默认开启**（不会静默跳过）。
#: 只有在机器极度拥挤、确实需要排除本机网络干扰时才显式关掉：
#:     set APMLOG_LINK_TCP_TESTS=0
TCP_TESTS_ENABLED = os.environ.get("APMLOG_LINK_TCP_TESTS", "1").strip().lower() not in ("0", "off", "no")
TCP_SKIP_REASON = "APMLOG_LINK_TCP_TESTS=0 显式关闭了真 TCP 回环用例（默认运行）"

from apmlog.link import download as dl                   # noqa: E402
from apmlog.link.download import BLOCK_BYTES             # noqa: E402
from apmlog.link.endpoint import MavLinkSession          # noqa: E402
from apmlog.link.fakes import FakeFC, FakeLog            # noqa: E402
from apmlog.link.transport import TcpClientTransport, UdpOutTransport  # noqa: E402

REAL_BIN = Path(r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000167.BIN")


def sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


class LinkCase(unittest.TestCase):
    """公共夹具：临时输出目录 + FakeFC + 会话。"""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="apmlog_dl_"))
        self.created: list[tuple[FakeFC, MavLinkSession]] = []

    def tearDown(self):
        for fc, sess in self.created:
            sess.close()
            fc.stop()
        shutil.rmtree(self.out, ignore_errors=True)

    # -- 工具
    def make_fc(self, logs, **kw) -> FakeFC:
        kw.setdefault("heartbeat_period", 0.15)
        fc = FakeFC(logs, **kw)
        return fc

    def connect_tcp(self, fc: FakeFC) -> MavLinkSession:
        host, port = fc.serve_tcp("127.0.0.1", 0)
        sess = MavLinkSession(TcpClientTransport(host, port), name=f"dl-{fc.num_logs}logs")
        sess.start()
        t0 = time.monotonic()
        while sess.fc_sysid is None and time.monotonic() - t0 < 6:
            time.sleep(0.02)
        self.created.append((fc, sess))
        return sess

    def connect_udp(self, fc: FakeFC) -> MavLinkSession:
        bind, port = fc.serve_udp("127.0.0.1", 0)
        sess = MavLinkSession(UdpOutTransport("127.0.0.1", port), name="dl-udp",
                              send_our_heartbeat=True)
        sess.start()
        t0 = time.monotonic()
        while sess.fc_sysid is None and time.monotonic() - t0 < 8:
            time.sleep(0.02)
        self.created.append((fc, sess))
        return sess

    def assert_identical(self, src: bytes, path: Path, res: dl.DownloadResult, label: str):
        got = path.read_bytes()
        self.assertTrue(res.complete, f"{label}: 下载未完成 -> {res.reason}")
        self.assertEqual(len(got), len(src), f"{label}: 长度不同")
        self.assertEqual(sha(got), sha(src), f"{label}: SHA1 不同")
        self.assertEqual(got, src, f"{label}: 字节不一致")        # ← 逐字节断言

    def run_download(self, sess, log_id, **kw):
        logs = dl.list_logs(sess, timeout=6.0)
        entry = next(x for x in logs if x.log_id == log_id)
        kw.setdefault("blocks_per_request", 60)
        kw.setdefault("window", 4)
        t0 = time.monotonic()
        res = dl.download_log(sess, entry, self.out, **kw)
        return res, time.monotonic() - t0


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestByteExact(LinkCase):
    def test_real_bin_over_tcp(self):
        """★ 真实 2.9MB .BIN 经 FakeFC+TCP 回环，落盘字节必须与原文件完全相同。"""
        if not REAL_BIN.exists():
            self.skipTest(f"真实日志不存在: {REAL_BIN}")
        src = REAL_BIN.read_bytes()
        fc = self.make_fc([FakeLog(167, src, time_utc=315532800)])
        sess = self.connect_tcp(fc)
        res, wall = self.run_download(sess, 167, blocks_per_request=100, window=4)

        final = self.out / "00000167.BIN"
        print(f"\n[BYTE-EXACT 真机日志] size={len(src)} sha1={sha(src)[:16]} "
              f"-> {final.name} sha1={sha(final.read_bytes())[:16] if final.exists() else '-'} "
              f"| {res.summary()} | 墙钟 {wall:.2f}s", flush=True)
        self.assertEqual(res.expected_bytes, len(src))
        self.assertEqual(res.blocks_ok, -(-len(src) // BLOCK_BYTES), "应收到全部块帧")
        self.assertEqual(res.crc_errors, 0)
        self.assertEqual(res.retries, 0, "无故障注入时不应有重传")
        self.assert_identical(src, final, res, "真机 .BIN")
        self.assertLess(wall, 90.0, "回环下载不应慢到异常")

    def test_multi_log_selection(self):
        a = b"A" * 1000 + b"t" * 13
        mid = bytes(range(251)) * 20 + b"\x7f" * 40
        c = b"C" * (BLOCK_BYTES * 4)
        fc = self.make_fc([FakeLog(1, a), FakeLog(2, mid), FakeLog(7, c)])
        sess = self.connect_tcp(fc)
        res, wall = self.run_download(sess, 2)
        print(f"[多日志选择] id=2 size={len(mid)} {res.summary()} {wall:.2f}s", flush=True)
        self.assert_identical(mid, self.out / "00000002.BIN", res, "中间那条日志")
        self.assertFalse((self.out / "00000001.BIN").exists(), "不该顺手下载别的日志")

    def test_tail_and_tiny_logs(self):
        """尾块不足 90B 与小于 90B 的日志（实测 size 非 90 整数倍：915116%90=56）。"""
        cases = {11: b"x" * 44, 12: b"y" * BLOCK_BYTES, 13: b"z" * (BLOCK_BYTES * 3 + 1),
                 14: b"w" * 1}
        fc = self.make_fc([FakeLog(i, d) for i, d in cases.items()])
        sess = self.connect_tcp(fc)
        for i, data in cases.items():
            res, _ = self.run_download(sess, i, blocks_per_request=2)
            self.assert_identical(data, self.out / f"{i:08d}.BIN", res, f"size={len(data)}")
            self.assertEqual(res.blocks_ok, -(-len(data) // BLOCK_BYTES))
        print("[尾块/极小日志] 4 组全部逐字节一致", flush=True)

    def test_udp_byte_exact_with_loss(self):
        src = bytes((i * 37) & 0xFF for i in range(60_000))
        fc = self.make_fc([FakeLog(5, src)], drop_prob=0.02, seed=3)
        sess = self.connect_udp(fc)
        res, wall = self.run_download(sess, 5, blocks_per_request=20, window=2)
        print(f"[UDP 回环+2%丢帧] {res.summary()} 墙钟 {wall:.2f}s fc.drop={fc.stats.dropped_frames}",
              flush=True)
        self.assertGreater(fc.stats.dropped_frames, 0, "故障注入必须真的丢了帧")
        self.assertGreater(res.retries, 0, "丢帧必须触发重传")
        self.assert_identical(src, self.out / "00000005.BIN", res, "UDP 链路")

    def test_rerun_short_circuits(self):
        src = b"r" * (BLOCK_BYTES * 9 + 7)
        fc = self.make_fc([FakeLog(3, src)])
        sess = self.connect_tcp(fc)
        res1, _ = self.run_download(sess, 3)
        self.assertTrue(res1.complete)
        res2, _ = self.run_download(sess, 3)
        self.assertEqual(res2.reason, "already-present")
        self.assertEqual(res2.elapsed, 0.0, "已存在同尺寸文件不应重新传输")
        self.assert_identical(src, self.out / "00000003.BIN", res2, "重复下载短路")


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestLossAndCrc(LinkCase):
    def test_random_drop_retransmit(self):
        src = bytes((i * 91 + 7) & 0xFF for i in range(150_000))
        fc = self.make_fc([FakeLog(21, src)], drop_prob=0.03, seed=42)
        sess = self.connect_tcp(fc)
        res, wall = self.run_download(sess, 21, blocks_per_request=10, timeout_chunk=1.0)
        print(f"[3%随机丢块] 丢{fc.stats.dropped_frames}帧 重传{res.retries}次 "
              f"重复帧{res.frames_dup} {wall:.2f}s -> {res.summary()}", flush=True)
        self.assertGreater(fc.stats.dropped_frames, 10)
        self.assertGreater(res.retries, 0)
        self.assertTrue(res.complete, f"重传后必须补全: {res.reason}")
        self.assert_identical(src, self.out / "00000021.BIN", res, "随机丢块")
        self.assertLess(wall, 60.0, "顺序流即时重传应让丢块代价接近 0")

    def test_corrupt_crc_frames(self):
        src = bytes((i * 13) & 0xFF for i in range(72_000))
        fc = self.make_fc([FakeLog(22, src)], corrupt_prob=0.05, seed=5)
        sess = self.connect_tcp(fc)
        res, wall = self.run_download(sess, 22, blocks_per_request=10, timeout_chunk=1.0)
        st = sess.stats
        print(f"[CRC 破坏] fc.corrupt={fc.stats.corrupted_frames} 链路 crc_errors={st.crc_errors} "
              f"重传{res.retries} {wall:.2f}s", flush=True)
        self.assertGreater(st.crc_errors, 0, "坏帧必须被严格 CRC 检出")
        self.assertGreater(res.retries, 0, "坏帧必须触发重传")
        self.assert_identical(src, self.out / "00000022.BIN", res, "CRC 坏帧链路")

    def test_fc_silence_leaves_partial(self):
        """飞控对 LOG_REQUEST_DATA 完全不应答 → 超时中止、保留断点、不误报完成。"""
        src = b"s" * (BLOCK_BYTES * 40)
        fc = self.make_fc([FakeLog(31, src)], mute_data=True)
        sess = self.connect_tcp(fc)
        res, wall = self.run_download(sess, 31, blocks_per_request=4, timeout_chunk=0.6,
                                      timeout_stall=1.5, max_retries_per_chunk=2)
        print(f"[飞控静音数据] {res.summary()} 墙钟 {wall:.2f}s", flush=True)
        self.assertFalse(res.complete)
        self.assertTrue("重试上限" in res.reason or "stalled" in res.reason,
                        f"应为超时/重试类中止，实际 {res.reason}")
        self.assertTrue((self.out / "00000031.BIN.part").exists())
        self.assertEqual(sess.fc_sysid, 1, "心跳仍应在线")


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestCancel(LinkCase):
    def test_cancel_midway_keeps_part_and_sidecar(self):
        if not REAL_BIN.exists():
            self.skipTest(f"真实日志不存在: {REAL_BIN}")
        src = REAL_BIN.read_bytes()
        fc = self.make_fc([FakeLog(167, src)])
        sess = self.connect_tcp(fc)
        stop_at = 200_000
        flag = {"cancel": False}
        seen = {"max": 0}

        def prog(done, total, rate, msg):
            seen["max"] = max(seen["max"], done)
            if done >= stop_at:
                flag["cancel"] = True

        res, wall = self.run_download(sess, 167, blocks_per_request=60, window=4,
                                      cancel=lambda: flag["cancel"], progress=prog)
        part = self.out / "00000167.BIN.part"
        side = Path(str(part) + ".json")
        print(f"[取消] 进度回调 {res.written_bytes}B -> {res.summary()} 墙钟 {wall:.2f}s", flush=True)
        self.assertFalse(res.complete)
        self.assertEqual(res.reason, "cancelled")
        self.assertTrue(part.exists(), "取消必须保留 .part")
        self.assertTrue(side.exists(), "取消必须保留旁车 json")
        self.assertFalse((self.out / "00000167.BIN").exists(), "未完成不得产出正式文件")
        st = json.loads(side.read_text(encoding="utf-8"))
        self.assertEqual(st["log_id"], 167)
        self.assertEqual(st["size"], len(src))
        self.assertEqual(st["block_bytes"], BLOCK_BYTES)
        self.assertGreater(st["done_bytes"], 0)
        self.assertLess(st["done_bytes"], len(src))
        self.assertEqual(sum(b - a for a, b in st["ranges"]), st["done_bytes"])
        self.assertGreaterEqual(seen["max"], stop_at)


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestProgress(LinkCase):
    def test_progress_rate_and_monotonic(self):
        src = bytes((i * 11) & 0xFF for i in range(300_000))
        fc = self.make_fc([FakeLog(41, src)])
        sess = self.connect_tcp(fc)
        marks: list[tuple[int, int, float]] = []
        res, wall = self.run_download(sess, 41, blocks_per_request=40, window=4,
                                      progress=lambda d, t, r, m: marks.append((d, t, r)))
        dones = [d for d, _t, _r in marks]
        print(f"[进度] 回调 {len(marks)} 次 末次 {marks[-1]} 平均 {res.bytes_per_sec/1024:.0f}KB/s",
              flush=True)
        self.assertEqual(dones, sorted(dones), "进度必须单调不回退")
        self.assertEqual(marks[-1][0], marks[-1][1], "最后一次必须等于总长")
        self.assertGreater(len(marks), 5)
        self.assertGreater(res.bytes_per_sec, 1024)
        self.assertEqual(res.written_bytes, len(src))


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestThreadContainment(LinkCase):
    """回归护栏：客户端中途掉线时，**任何线程都不得把异常抛到线程外**。

    旧行为：``_stream_data`` 里 ``log.error`` 之后 ``raise``，traceback 逃逸出
    ``fakefc-tcp`` 线程 → pytest 报 ``PytestUnhandledThreadExceptionWarning``，
    并把 ``WinError 10054`` 归给当时正在跑的**无关用例**（实测伪装成
    ``TestFrameReader.test_roundtrip_and_garbage_prefix`` 失败）。
    """

    def test_yanking_client_socket_does_not_escape_thread(self):
        data = bytes(range(256)) * 4000                       # 1 024 000B，保证拔线后还在回包
        fc = self.make_fc([FakeLog(77, data)])
        host, port = fc.serve_tcp("127.0.0.1", 0)
        sess = MavLinkSession(TcpClientTransport(host, port), name="yank")
        sess.start()
        self.created.append((fc, sess))
        t0 = time.monotonic()
        while sess.fc_sysid is None and time.monotonic() - t0 < 6:
            time.sleep(0.02)
        self.assertIsNotNone(sess.fc_sysid, "未拿到飞控地址")
        entry = dl.RemoteLog(77, 1, 77, 315532800, len(data))

        seen = {"done": 0}
        box: dict = {}

        def worker():
            try:
                box["res"] = dl.download_log(sess, entry, self.out, blocks_per_request=40, window=1,
                                             timeout_chunk=1.0, timeout_stall=3.0,
                                             max_retries_total=20, resume=False, overwrite=True,
                                             progress=lambda d, t, r, m: seen.__setitem__("done", d))
            except Exception as exc:                        # 客户端侧预期会报错，不能影哿断言
                box["err"] = f"{type(exc).__name__}: {exc}"

        escaped: list = []
        saved = threading.excepthook
        threading.excepthook = lambda args: escaped.append(args)
        th = threading.Thread(target=worker, name="yank-dl", daemon=True)
        th.start()
        try:
            t0 = time.monotonic()                           # .part 是预分配的，只能看实际收到的字节
            while seen["done"] < 20000 and time.monotonic() - t0 < 20 and th.is_alive():
                time.sleep(0.02)
            self.assertGreaterEqual(seen["done"], 20000, f"没开始收字节就测不到并发写：{box.get('err')}")
            # 用 SO_LINGER(1,0) 强制 **RST**：普通 close() 可能让对端的写全部落进
            # OS 发送缓冲而不报错，那样就测不到逃逸（本机实测如此）。
            raw = getattr(sess.transport, "_sock", None)
            if raw is not None:
                try:
                    raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                except OSError:
                    pass
            sess.transport.close()
            th.join(timeout=30)
            time.sleep(0.8)                                 # 给服务线程走到写失败分支
        finally:
            threading.excepthook = saved
            fc.stop()

        self.assertFalse(th.is_alive(), "下载线程应已退出")
        # 先确认这次真的触发了对端写失败（否则下面的断言是空跑）
        self.assertTrue(fc.last_error,
                        "本次没能触发 FakeFC 写失败，测不到逃逸（需调大样本或重试用量）")
        self.assertEqual(escaped, [],
                         "有异常从线程里逃逸（会产生 PytestUnhandledThreadExceptionWarning）："
                         + "; ".join(f"{a.exc_type.__name__}: {a.exc_value}" for a in escaped))
        print(f"\n[线程包被验证] 收到 {seen['done']}B 后 RST 拔线；客户端={box.get('err') or box['res'].reason}；"
              f"FakeFC 记录={fc.last_error[:60]}；逃逸线程异常 {len(escaped)} 条", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
