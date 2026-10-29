"""临时验证：原子缓存写入（含两个进程同时冷解析同一份日志的竞态）。"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "selftest" / "fc_dl2" / "00000005.BIN"
CACHE = ROOT / "selftest" / "_atomic_cache"

CHILD = '''
import json, sys, time
sys.path.insert(0, r"__ROOT__")
from apmlog.core.store import LogStore
t0 = time.time()
s = LogStore.open(r"__LOG__", cache_dir=r"__CACHE__")
out = {"pid": __PID__, "secs": round(time.time()-t0, 2), "msgs": s.stats.n_messages,
       "alerts": len(s.alerts()), "events": len(s.events()), "pos_kind": s.stats.pos_kind,
       "distance": round(s.meta["track"]["distance"], 2)}
print("RESULT " + json.dumps(out, ensure_ascii=False))
s.close()
'''.replace("__ROOT__", str(ROOT)).replace("__LOG__", str(LOG)).replace("__CACHE__", str(CACHE))

if CACHE.exists():
    shutil.rmtree(CACHE)
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
t0 = time.time()
import os as _os
env = dict(_os.environ, PYTHONIOENCODING="utf-8")
procs = [subprocess.Popen([PY, "-c", CHILD.replace("__PID__", str(i))], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", env=env)
         for i in (1, 2)]
outs = [p.communicate(timeout=300)[0] for p in procs]
print(f"两个进程同时冷解析耗时 {time.time()-t0:.1f}s，退出码:", [p.returncode for p in procs])
for o in outs:
    got = [ln for ln in o.splitlines() if ln.startswith("RESULT")]
    print("  ", got[-1] if got else "(无 RESULT)\n" + o[-400:])
left = [p.name for p in CACHE.rglob("*") if p.name.endswith(".tmp")]
print("残留 .tmp 文件:", left)
# 再开一次应为缓存命中
t1 = time.time()
from apmlog.core.store import LogStore
s = LogStore.open(str(LOG), cache_dir=str(CACHE))
print(f"二次打开 {time.time()-t1:.2f}s（应≈0.0x）| 报文 {s.stats.n_messages} | 告警 {len(s.alerts())} | 位置 {s.stats.pos_kind}")
s.close()
shutil.rmtree(CACHE)
print("清理完成")
