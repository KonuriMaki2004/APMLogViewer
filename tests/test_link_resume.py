# -*- coding: utf-8 -*-
"""tests/test_link_resume.py —— 断点续传：.part + 旁车 json、跨链路/跨参数恢复、旁车失效保护。

续传格式（apmlog/link/download.py 实现）：
    <NNNNNNNN.BIN.part>          预分配到 LOG_ENTRY.size 长度，按 ofs 定位写入
    <NNNNNNNN.BIN.part.json>     {log_id,size,chunk_blocks,block_bytes,ranges[[a,b)...],done_bytes,crc_errors,retries}
恢复条件：log_id 与 size 必须与当前 LOG_ENTRY 一致，否则**忽略旁车重新下载**。

运行：
    .\\.venv\\Scripts\\python.exe -X utf8 -m unittest tests.test_link_resume -v
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 真 TCP 回环用例开关：**默认开启**，只有显式设 APMLOG_LINK_TCP_TESTS=0 才跳过（不静默跳过）。
TCP_TESTS_ENABLED = os.environ.get("APMLOG_LINK_TCP_TESTS", "1").strip().lower() not in ("0", "off", "no")
TCP_SKIP_REASON = "APMLOG_LINK_TCP_TESTS=0 显式关闭了真 TCP 回环用例（默认运行）"
sys.path.insert(0, str(ROOT))

from apmlog.link import download as dl                   # noqa: E402
from apmlog.link.download import BLOCK_BYTES             # noqa: E402
from apmlog.link.endpoint import MavLinkSession          # noqa: E402
from apmlog.link.fakes import FakeFC, FakeLog            # noqa: E402
from apmlog.link.transport import TcpClientTransport     # noqa: E402

REAL_BIN = Path(r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000167.BIN")


def sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


class ResumeCase(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="apmlog_rs_"))
        self.live: list[tuple[FakeFC, MavLinkSession]] = []

    def tearDown(self):
        for fc, sess in self.live:
            try:
                sess.close()
            except Exception:
                pass
            try:
                fc.stop()
            except Exception:
                pass
        shutil.rmtree(self.out, ignore_errors=True)

    def start_link(self, src: bytes, log_id: int = 167, **fc_kw) -> tuple[FakeFC, MavLinkSession]:
        fc = FakeFC([FakeLog(log_id, src)], heartbeat_period=0.15, **fc_kw)
        host, port = fc.serve_tcp("127.0.0.1", 0)
        sess = MavLinkSession(TcpClientTransport(host, port), name="resume")
        sess.start()
        t0 = time.monotonic()
        while sess.fc_sysid is None and time.monotonic() - t0 < 6:
            time.sleep(0.02)
        self.live.append((fc, sess))
        return fc, sess

    def entry_of(self, sess, log_id: int):
        logs = dl.list_logs(sess, timeout=6.0)
        return next(x for x in logs if x.log_id == log_id)

    def kill_link(self, fc: FakeFC, sess: MavLinkSession) -> None:
        """模拟链路中断（拔线/掉电）：先关飞控再关会话。"""
        sess.close()
        fc.stop()
        self.live = [(f, s) for f, s in self.live if s is not sess]


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestResumeAfterCancel(ResumeCase):
    def test_cancel_then_resume_byte_exact(self):
        """★ 中断后续传，最终文件与真实 .BIN 逐字节一致。"""
        if not REAL_BIN.exists():
            self.skipTest(f"真实日志不存在: {REAL_BIN}")
        src = REAL_BIN.read_bytes()
        fc, sess = self.start_link(src)
        flag = {"c": False}
        stop_at = 400_000
        prog = lambda d, t, r, m: flag.__setitem__("c", d >= stop_at)   # noqa: E731

        r1, w1 = self._dl(sess, fc, src, cancel=lambda: flag["c"], progress=prog)
        part = self.out / "00000167.BIN.part"
        side = Path(str(part) + ".json")
        print(f"\n[第 1 段·取消] {r1.summary()} 墙钟 {w1:.2f}s", flush=True)
        self.assertFalse(r1.complete)
        self.assertEqual(r1.reason, "cancelled")
        self.assertTrue(part.exists() and side.exists())
        st1 = json.loads(side.read_text(encoding="utf-8"))
        self.assertEqual(st1["version"], 1)
        self.assertEqual(st1["log_id"], 167)
        self.assertEqual(st1["size"], len(src))
        self.assertGreater(st1["done_bytes"], 0)
        self.assertEqual(sum(b - a for a, b in st1["ranges"]), st1["done_bytes"])
        self.assertEqual(part.stat().st_size, len(src), "part 应预分配到目标长度，支持任意 ofs")

        r2, w2 = self._dl(sess, fc, src)
        final = self.out / "00000167.BIN"
        print(f"[第 2 段·续传] resumed_from={r2.resumed_from} {r2.summary()} 墙钟 {w2:.2f}s", flush=True)
        self.assertEqual(r2.resumed_from, st1["done_bytes"], "续传起点必须来自旁车 ranges")
        self.assertGreater(r2.resumed_from, 0)
        self.assertTrue(r2.complete, r2.reason)
        self.assertEqual(final.read_bytes(), src)                     # ← 逐字节断言
        self.assertEqual(sha(final.read_bytes()), sha(src))
        self.assertFalse(part.exists(), "完成后必须删除 .part")
        self.assertFalse(side.exists(), "完成后必须删除旁车 json")
        self.assertLess(r2.blocks_ok, -(-len(src) // BLOCK_BYTES), "续传不得重传已覆盖的块")

    def _dl(self, sess, fc, src, **kw):
        kw.setdefault("blocks_per_request", 60)
        kw.setdefault("window", 4)
        entry = self.entry_of(sess, 167)
        t0 = time.monotonic()
        res = dl.download_log(sess, entry, self.out, **kw)
        return res, time.monotonic() - t0


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestResumeAfterLinkLoss(ResumeCase):
    def test_link_drop_then_new_link(self):
        """链路断开（FakeFC 下线）后换新链路续传：字节一致 + 断点统计保留。"""
        src = bytes((i * 71 + 3) & 0xFF for i in range(480_000))
        fc, sess = self.start_link(src)
        flag = {"c": False}

        def prog(d, t, r, m):
            if d >= 150_000:
                flag["c"] = True

        r1 = dl.download_log(sess, self.entry_of(sess, 167), self.out,
                             blocks_per_request=40, window=3, cancel=lambda: flag["c"],
                             progress=prog)
        self.assertFalse(r1.complete)
        st1 = json.loads((Path(str(self.out / "00000167.BIN.part") + ".json")).read_text(encoding="utf-8"))

        # 模拟拔线：旧链路彻底关闭
        fc.stop()
        sess.close()
        self.live = []

        # 换新 FakeFC + 新会话（同一份日志内容）
        fc2, sess2 = self.start_link(src, seed=9)
        r2 = dl.download_log(sess2, self.entry_of(sess2, 167), self.out,
                             blocks_per_request=40, window=3)
        print(f"[断链续传] 段1 {r1.written_bytes}B({r1.reason}) → 新链路段2 resumed_from="
              f"{r2.resumed_from} {r2.summary()}", flush=True)
        self.assertEqual(r2.resumed_from, st1["done_bytes"])
        self.assertTrue(r2.complete, r2.reason)
        final = self.out / "00000167.BIN"
        self.assertEqual(final.read_bytes(), src)                     # ← 逐字节断言
        self.assertLess(r2.blocks_ok, -(-len(src) // BLOCK_BYTES))

    def test_partial_range_gap_resume(self):
        """旁车里是**离散区间**（中间有洞）时也必须只补洞。"""
        src = bytes((i * 5) & 0xFF for i in range(90_000))
        fc, sess = self.start_link(src, drop_prob=0.05, seed=11)
        flag = {"c": False}
        r1 = dl.download_log(sess, self.entry_of(sess, 167), self.out,
                             blocks_per_request=5, window=2, timeout_chunk=0.8,
                             cancel=lambda: flag["c"],
                             progress=lambda d, t, r, m: flag.__setitem__("c", d >= 60_000))
        side = Path(str(self.out / "00000167.BIN.part") + ".json")
        if r1.complete:                                               # 丢块少时可能一次就补完
            self.assertTrue(side.exists() is False)
            self.assertEqual((self.out / "00000167.BIN").read_bytes(), src)
            print("[离散洞续传] 第一段已自行补全，无需续传", flush=True)
            return
        st = json.loads(side.read_text(encoding="utf-8"))
        ranges = [(a, b) for a, b in st["ranges"]]
        print(f"[离散洞续传] 段1 {r1.summary()} 区间数={len(ranges)} 重传={st['retries']}", flush=True)
        r2 = dl.download_log(sess, self.entry_of(sess, 167), self.out,
                             blocks_per_request=5, window=2, timeout_chunk=0.8)
        self.assertTrue(r2.complete, r2.reason)
        self.assertEqual((self.out / "00000167.BIN").read_bytes(), src)   # ← 逐字节断言
        self.assertEqual(r2.resumed_from, st["done_bytes"])


@unittest.skipUnless(TCP_TESTS_ENABLED, TCP_SKIP_REASON)
class TestResumeGuardrails(ResumeCase):
    def test_stale_sidecar_size_mismatch_is_ignored(self):
        """LOG_ENTRY.size 变了（飞控上换了日志）→ 旁车必须作废，重新完整下载。"""
        src = bytes(range(200)) * 500                       # 100000B
        fc, sess = self.start_link(src)
        part = self.out / "00000167.BIN.part"
        part.write_bytes(b"\x00" * len(src))
        side = Path(str(part) + ".json")
        side.write_text(json.dumps({
            "version": 1, "log_id": 167, "size": len(src) + 90, "chunk_blocks": 60,
            "block_bytes": BLOCK_BYTES, "ranges": [[0, len(src) + 90]], "done_bytes": len(src) + 90,
            "crc_errors": 0, "retries": 0, "updated": "2026-10-17T00:00:00+00:00", "tag": "synthetic",
        }), encoding="utf-8")

        r = dl.download_log(sess, self.entry_of(sess, 167), self.out, blocks_per_request=40, window=4)
        print(f"[旁车 size 不匹配] {r.summary()} resumed_from={r.resumed_from}", flush=True)
        self.assertEqual(r.resumed_from, 0, "不匹配的旁车必须作废")
        self.assertTrue(r.complete)
        self.assertEqual((self.out / "00000167.BIN").read_bytes(), src)   # ← 逐字节断言

    def test_corrupt_sidecar_json_is_ignored(self):
        src = b"k" * (BLOCK_BYTES * 30 + 9)
        fc, sess = self.start_link(src)
        part = self.out / "00000167.BIN.part"
        part.write_bytes(b"\xff" * len(src))
        Path(str(part) + ".json").write_text("{不是 json", encoding="utf-8")
        r = dl.download_log(sess, self.entry_of(sess, 167), self.out, blocks_per_request=20)
        self.assertEqual(r.resumed_from, 0)
        self.assertTrue(r.complete, r.reason)
        self.assertEqual((self.out / "00000167.BIN").read_bytes(), src)
        print("[旁车损坏] 已忽略并完整重下", flush=True)

    def test_resume_with_different_chunk_size(self):
        """改 blocks_per_request 后续传：区间仍复用，最终一致。"""
        src = bytes((i * 17) & 0xFF for i in range(200_000))
        fc, sess = self.start_link(src)
        flag = {"c": False}
        r1 = dl.download_log(sess, self.entry_of(sess, 167), self.out, blocks_per_request=10,
                             window=2, cancel=lambda: flag["c"],
                             progress=lambda d, t, r, m: flag.__setitem__("c", d >= 80_000))
        self.assertFalse(r1.complete)
        r2 = dl.download_log(sess, self.entry_of(sess, 167), self.out, blocks_per_request=120,
                             window=5)
        print(f"[换 chunk 续传] {r1.written_bytes}B → resumed {r2.resumed_from}B → {r2.summary()}",
              flush=True)
        self.assertEqual(r2.resumed_from, r1.written_bytes)
        self.assertTrue(r2.complete)
        self.assertEqual((self.out / "00000167.BIN").read_bytes(), src)     # ← 逐字节断言

    def test_resume_disabled_restarts(self):
        src = bytes((i * 3) & 0xFF for i in range(60_000))
        fc, sess = self.start_link(src)
        flag = {"c": False}
        r1 = dl.download_log(sess, self.entry_of(sess, 167), self.out, blocks_per_request=10,
                             window=2, cancel=lambda: flag["c"],
                             progress=lambda d, t, r, m: flag.__setitem__("c", d >= 30_000))
        self.assertFalse(r1.complete)
        self.assertTrue(Path(str(self.out / "00000167.BIN.part") + ".json").exists())
        r2 = dl.download_log(sess, self.entry_of(sess, 167), self.out, blocks_per_request=20,
                             window=3, resume=False)
        self.assertEqual(r2.resumed_from, 0, "resume=False 必须从头来")
        self.assertTrue(r2.complete)
        self.assertEqual((self.out / "00000167.BIN").read_bytes(), src)
        print("[禁用续传] 从头重下并成功", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
