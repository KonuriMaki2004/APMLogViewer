# -*- coding: utf-8 -*-
"""tools/_hw_probe.py —— 真机只读探查（不写任何参数、不发任何会改变状态的命令）。

    .\\.venv\\Scripts\\python.exe -X utf8 tools/_hw_probe.py COM94

做的事：打开串口 → 等心跳 → 打印固件/板型/armed → 请求日志列表 → 打印前几条。
只用 ``LOG_REQUEST_LIST`` / ``PARAM_REQUEST_READ`` 这类只读请求。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from apmlog.link.endpoint import MavLinkSession            # noqa: E402
from apmlog.link.transport import SerialTransport          # noqa: E402


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM94"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
    sess = MavLinkSession(SerialTransport(port, baud), name=f"probe:{port}")
    sess.start()
    try:
        if not sess.wait_for("HEARTBEAT", timeout=8):
            print("没有收到心跳：端口被占用 / 波特率不对 / 不是 MAVLink")
            return 2
        time.sleep(1.2)                                  # 多攒几帧心跳
        print(f"链路：{sess.summary()}")
        for key, hb in sess.heartbeats.items():
            print(f"  心跳 {key[0]}.{key[1]} type={hb.type} autopilot={hb.autopilot} "
                  f"base_mode=0x{hb.base_mode:02x} custom_mode={hb.custom_mode} "
                  f"armed={hb.armed} n={hb.count}")
        print(f"  is_armed() = {sess.is_armed()}")

        # AUTOPILOT_VERSION：版本串 + board_version（board_id 在高 16 位）
        ver = sess.wait_for("AUTOPILOT_VERSION", timeout=3)
        if ver is not None:
            fw = bytes(getattr(ver, "flight_sw_version", b"\x00" * 4)).to_bytes(4, "little")
            major, minor, patch = fw[0], fw[1], (fw[2] << 8) | fw[3]
            bv = int(getattr(ver, "board_version", 0))
            print(f"  固件：{major}.{minor}.{patch}  board_version=0x{bv:08x} "
                  f"(board_id={bv >> 16})  uid={bytes(getattr(ver, 'uid', b'')).hex()}")

        # 日志列表（只读）
        sess.send_log_request_list(start=0, end=0xFFFF)
        entries: dict[int, list] = {}
        deadline = time.monotonic() + 4.0
        queue = sess.listen(["LOG_ENTRY"])
        try:
            while time.monotonic() < deadline:
                try:
                    msg = queue.get(timeout=0.3)
                except Exception:
                    continue
                entries.setdefault(int(msg.num_logs), []).append(msg)
        finally:
            sess.unlisten(queue)
        for num, rows in entries.items():
            rows.sort(key=lambda m: int(m.id))
            print(f"  飞控上共 {num} 条日志；收到 {len(rows)} 条：")
            for m in rows[:6]:
                print(f"    id={int(m.id):3d} size={int(m.size):>9d} "
                      f"time_utc={int(m.time_utc)} last_log_num={int(m.last_log_num)}")
            if rows:
                biggest = max(rows, key=lambda m: int(m.size))
                print(f"    最大：id={int(biggest.id)} size={int(biggest.size)} "
                      f"= {int(biggest.size) / 1e6:.2f} MB")
        st = sess.stats
        print(f"  统计：rx {st.rx_frames} 帧 / tx {st.tx_frames} 帧 / CRC 错 {st.crc_errors}")
    finally:
        sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
