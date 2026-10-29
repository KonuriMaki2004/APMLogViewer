"""统计真实日志里出现的事件类型（给知识库定覆盖范围）。

用法：
    .venv/Scripts/python.exe tools/_scan_events.py <目录> [最多几个日志]
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apmlog.core.store import LogStore                       # noqa: E402


def main() -> int:
    d = Path(sys.argv[1])
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    logs = sorted(d.glob("*.BIN")) + sorted(d.glob("*.bin"))
    print(f"目录 {d}：{len(logs)} 个日志，最多处理 {limit} 个")

    kinds: collections.Counter = collections.Counter()
    errs: collections.Counter = collections.Counter()
    msgs: collections.Counter = collections.Counter()
    sev: collections.Counter = collections.Counter()
    n_ok = 0
    for p in logs[:limit]:
        try:
            st = LogStore.open(p)
        except Exception as exc:                              # noqa: BLE001
            print(f"  跳过 {p.name}: {exc}")
            continue
        n_ok += 1
        for e in st.events():
            kinds[e.kind] += 1
            sev[e.severity] += 1
            if e.kind == "ERR":
                errs[(e.code, e.ecode, e.decoded or e.text)] += 1
            elif e.kind == "MSG":
                msgs[str(e.display)[:70]] += 1
        st.close()

    print(f"成功解析 {n_ok} 份")
    print("\n=== kind 分布 ===")
    for k, v in kinds.most_common():
        print(f"  {k:8s} {v}")
    print("\n=== 严重度分布 ===")
    for k, v in sev.most_common():
        print(f"  {k:9s} {v}")
    print("\n=== ERR（子系统/错误码） ===")
    for (s, c, txt), v in errs.most_common(30):
        print(f"  {v:5d}× subsys={s} ecode={c}  {txt}")
    print("\n=== MSG 原文（去重后按出现次数） ===")
    for t, v in msgs.most_common(40):
        print(f"  {v:5d}× {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
