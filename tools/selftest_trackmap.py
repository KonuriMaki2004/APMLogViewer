"""selftest_trackmap —— TrackMapWidget 的真实日志集成自测（B 工作流验收）。

不依赖内核 store（内核尚未交付），直接用 pymavlink DFReader 取真实报文：
  A) ``POS``/``GPS``/``ORGN`` → geodetic 轨迹 → 真实瓦片地图（默认 esri_imagery）
  B) ``VISP``(PX,PY,PZ, NED) → local ENU 轨迹（用户的 ZHT 视觉定位日志形态）
  C) 强制离线 + 不可达源 → 降级不得崩

用法（PowerShell）：
    $env:QT_QPA_PLATFORM='offscreen'
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\selftest_trackmap.py [--source esri_imagery]

输出 PNG 到 selftest/，并打印点数、经纬度范围、瓦片统计、耗时。
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                        # noqa: E402
from PySide6 import QtCore, QtWidgets                     # noqa: E402
from pymavlink import DFReader                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apmlog.core.model import TrackResult                 # noqa: E402
from apmlog.ui import _tilemath as tm                     # noqa: E402
from apmlog.ui.trackmap import TrackMapWidget             # noqa: E402

LOG_MP = Path(r"C:\Users\MAKI\Documents\Mission Planner\logs\QUADROTOR\1\2026-10-16 16-59-28.bin")
LOG_ZHT = Path(r"C:\Users\MAKI\Desktop\工作相关\ZHT\测试\测试3\日志\00000165.BIN")
OUT = ROOT / "selftest"


def pump(ms: int) -> None:
    app = QtWidgets.QApplication.instance()
    end = QtCore.QElapsedTimer()
    end.start()
    while end.elapsed() < ms:
        app.processEvents()
        QtCore.QThread.msleep(10)


def wait_tiles(w: TrackMapWidget, max_ms: int = 15000) -> float:
    """等瓦片下完或超时。

    退出条件：连续 15 次轮询（~0.3s）inflight 与 pending 均为 0。
    上一版只要网络掉包连续就来不了 0.5s 空闲，每个用例众然耗满超时。
    """
    t0 = time.perf_counter()
    app = QtWidgets.QApplication.instance()
    stable = 0
    while (time.perf_counter() - t0) * 1000 < max_ms:
        app.processEvents()
        QtCore.QThread.msleep(20)
        if w._tiles.inflight() == 0 and w._tiles.pending() == 0:
            stable += 1
            if stable >= 15:
                break
        else:
            stable = 0
    return (time.perf_counter() - t0) * 1000


def collect(path: Path, types: tuple[str, ...]) -> dict[str, list[dict]]:
    log = DFReader.DFReader_binary(str(path))
    if log is None:
        raise RuntimeError(f"DFReader 打不开: {path}")
    out: dict[str, list[dict]] = {t: [] for t in types}
    while True:
        m = log.recv_msg()
        if m is None:
            break
        t = m.get_type()
        if t in out:
            try:
                out[t].append(m.to_dict())
            except Exception:
                pass
    return out


def build_geodetic(msgs: dict[str, list[dict]]) -> TrackResult:
    pos = [p for p in msgs.get("POS", []) if isinstance(p.get("TimeUS"), int)]
    if not pos:
        raise SystemExit("该日志没有 POS 报文，无法做 geodetic 自测")
    t = np.array([p["TimeUS"] / 1e6 for p in pos], dtype=float)
    lat = np.array([float(p.get("Lat", np.nan)) for p in pos])
    lon = np.array([float(p.get("Lng", np.nan)) for p in pos])
    alt = np.array([float(p.get("RelHomeAlt", p.get("Alt", np.nan))) for p in pos])
    good = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(t)
    t, lat, lon, alt = t[good], lat[good], lon[good], alt[good]
    orgn = msgs.get("ORGN", [{}])[0]
    home = None
    if orgn and isinstance(orgn.get("Lat"), float):
        home = (float(orgn["Lat"]), float(orgn["Lng"]), float(orgn.get("Alt", 0.0)))
    origin = home or (float(lat[0]), float(lon[0]), float(alt[0]))
    e = np.empty(lat.size)
    n = np.empty(lat.size)
    for i, (a, b) in enumerate(zip(lat.tolist(), lon.tolist())):
        e[i], n[i] = tm.enu_from_lonlat(a, b, (origin[0], origin[1]))
    dt = np.gradient(t)
    de, dn = np.gradient(e), np.gradient(n)
    speed = np.hypot(de, dn) / np.maximum(dt, 1e-6)
    gps = msgs.get("GPS", [])
    nsat = [g.get("NSats") for g in gps if isinstance(g.get("NSats"), (int, float))]
    hdop = [g.get("HDop") for g in gps if isinstance(g.get("HDop"), (int, float))]
    tr = TrackResult(
        kind="geodetic", t=t, lat=lat, lon=lon, alt=alt, e=e, n=n, u=alt,
        speed=speed, src=np.zeros(t.size, dtype=np.int8), src_names=["POS"],
        origin=origin, home=home,
        distance=float(np.sum(np.hypot(np.diff(e), np.diff(n)))),
        max_speed=float(np.nanmax(speed)),
        note=f"GPS 星数 {min(nsat) if nsat else '-'}~{max(nsat) if nsat else '-'}  "
             f"HDop {min(hdop) if hdop else '-'}~{max(hdop) if hdop else '-'}",
    )
    return tr


def build_local(msgs: dict[str, list[dict]]) -> TrackResult:
    """VISP 的 PX/PY/PZ 是 NED（X 北、Y 东、Z 下）→ ENU: e=PY, n=PX, u=-PZ。"""
    visp = [v for v in msgs.get("VISP", []) if isinstance(v.get("TimeUS"), int)]
    if not visp:
        raise SystemExit("该日志没有 VISP 报文")
    t = np.array([v["TimeUS"] / 1e6 for v in visp], dtype=float)
    px = np.array([float(v.get("PX", np.nan)) for v in visp])
    py = np.array([float(v.get("PY", np.nan)) for v in visp])
    pz = np.array([float(v.get("PZ", np.nan)) for v in visp])
    perr = np.array([float(v.get("PErr", np.nan)) for v in visp])
    good = np.isfinite(t) & np.isfinite(px) & np.isfinite(py)
    t, px, py, pz, perr = t[good], px[good], py[good], pz[good], perr[good]
    e, n, u = py, px, -pz
    dt = np.gradient(t)
    de, dn = np.gradient(e), np.gradient(n)
    speed = np.hypot(de, dn) / np.maximum(dt, 1e-6)
    return TrackResult(
        kind="local", t=t, lat=np.full(t.size, np.nan), lon=np.full(t.size, np.nan), alt=u,
        e=e, n=n, u=u, speed=speed, src=np.zeros(t.size, dtype=np.int8), src_names=["VISP"],
        note=f"VISP 本地位置误差 PErr {np.nanmin(perr):.1f}~{np.nanmax(perr):.1f} m",
        distance=float(np.sum(np.hypot(np.diff(e), np.diff(n)))),
        max_speed=float(np.nanmax(speed)),
    )


def render(app: QtWidgets.QApplication, name: str, tr: TrackResult, source: str,
           offline: bool = False, events: bool = False) -> dict:
    w = TrackMapWidget(source_name=source, cache_dir=str(ROOT / "selftest" / "_tilecache_st"))
    w.resize(1400, 900)
    w.show()
    pump(150)
    if offline:
        w._tiles.set_online(False)
    t0 = time.perf_counter()
    w.set_track(tr)
    if events and tr.n_points:
        mid = int(tr.n_points * 0.6)
        evs = [{"t": float(tr.t[mid]), "lat": float(tr.lat[mid]) if tr.has_geodetic else None,
                "lon": float(tr.lon[mid]) if tr.has_geodetic else None,
                "severity": "critical", "display": "Crash: Disarming: AngErr=171>30"},
               {"t": float(tr.t[0]), "severity": "info", "display": "ARMED"}]
        w.set_events(evs)
        w.set_regions([{"t0": float(tr.t[max(0, mid - 40)]), "t1": float(tr.t[mid]),
                        "severity": "warn", "title": "振动超标 8.0s"}])
        w.set_time(float(tr.t[mid]))
    t_build = (time.perf_counter() - t0) * 1000
    waited = 0.0
    if not offline:
        waited = wait_tiles(w, 25000)
    pump(200)
    png = OUT / f"{name}.png"
    ok = w.export_png(str(png))
    st = w.state()
    st.update({"png": str(png), "saved": ok, "build_ms": round(t_build, 1),
               "wait_ms": round(waited, 0), "note": tr.note})
    w.close()
    pump(50)
    return st


def main() -> int:
    ap = argparse.ArgumentParser(description="TrackMapWidget 真实日志自测")
    ap.add_argument("--source", default="esri_imagery", help="A 用例的瓦片源名")
    ap.add_argument("--gcj", default="gaode_vector", help="GCJ-02 对照源名")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    OUT.mkdir(parents=True, exist_ok=True)
    app = QtWidgets.QApplication(sys.argv[:1])

    for p in (LOG_MP, LOG_ZHT):
        if not p.exists():
            print(f"[FAIL] 样例日志不存在: {p}")
            return 2

    print("=" * 78)
    print("解析真实日志…")
    t0 = time.perf_counter()
    mp = collect(LOG_MP, ("POS", "GPS", "ORGN"))
    zht = collect(LOG_ZHT, ("VISP", "XKF1"))
    print(f"  DFReader 耗时 {time.perf_counter() - t0:.2f}s  "
          f"POS={len(mp['POS'])} GPS={len(mp['GPS'])} ORGN={len(mp['ORGN'])} VISP={len(zht['VISP'])}")
    tr_geo = build_geodetic(mp)
    tr_loc = build_local(zht)
    print(f"  geodetic: {tr_geo.n_points} 点  lat[{np.nanmin(tr_geo.lat):.6f},{np.nanmax(tr_geo.lat):.6f}]"
          f" lon[{np.nanmin(tr_geo.lon):.6f},{np.nanmax(tr_geo.lon):.6f}]"
          f" 高度[{tr_geo.alt.min():.1f},{tr_geo.alt.max():.1f}] m  里程 {tr_geo.distance:.0f} m  "
          f"最大速度 {tr_geo.max_speed:.1f} m/s")
    print(f"  local   : {tr_loc.n_points} 点  E[{tr_loc.e.min():.1f},{tr_loc.e.max():.1f}] "
          f"N[{tr_loc.n.min():.1f},{tr_loc.n.max():.1f}] U[{tr_loc.u.min():.1f},{tr_loc.u.max():.1f}] m"
          f"  里程 {tr_loc.distance:.0f} m")

    print("=" * 78)
    print(f"A) geodetic + {args.source}")
    print("   ", render(app, "trackmap", tr_geo, args.source, events=True))
    print(f"A2) geodetic + {args.gcj} (GCJ-02 加偏对照)")
    print("   ", render(app, "trackmap_gaode", tr_geo, args.gcj, events=False))
    print("B) local (VISP, 无经纬度)")
    print("   ", render(app, "trackmap_local", tr_loc, args.source, events=True))
    print("C) 强制离线降级")
    print("   ", render(app, "trackmap_offline", tr_geo, args.source, offline=True))
    print("D) 不可达源（真实网络失败）+ 有效轨迹")
    w2 = TrackMapWidget(source_name=args.source, cache_dir=str(OUT / "_tilecache_st"))
    w2.resize(1000, 700)
    w2.show()
    pump(100)
    w2.add_source("broken", "https://invalid-host-for-selftest.local.example/{z}/{x}/{y}.png",
                  "自测用不可达源", datum="WGS84", max_zoom=18)
    w2.set_track(tr_geo)
    w2.set_source("broken")
    pump(6000)
    st2 = w2.state()
    print("   state:", st2)
    print("   占位瓦片数:", len(w2._placeholder_keys), " 失败计数:", st2["tile_stats"]["failed"],
          " 源暂停:", bool(w2._tiles._paused_until.get("broken", 0.0) > 0))
    print("   保存:", w2.export_png(str(OUT / "trackmap_broken_source.png")))
    w2.close()
    pump(100)

    print("E) 空轨迹 / 全 nan 不得崩")
    w = TrackMapWidget(source_name=args.source, cache_dir=str(OUT / "_tilecache_st"))
    w.resize(900, 600)
    w.show()
    pump(100)
    w.add_source("broken", "https://.invalid-host-for-selftest.local/{z}/{x}/{y}.png",
                 "自测用不可达源", datum="WGS84", max_zoom=18)
    w.set_source("broken")
    w.set_track(TrackResult())            # 空数据
    pump(1500)
    print("   空数据 state:", w.state())
    tr_copy = tr_geo.resample(200)
    tr_copy = TrackResult(kind="geodetic", t=tr_copy.t.copy(),
                          lat=np.full(tr_copy.t.size, np.nan), lon=np.full(tr_copy.t.size, np.nan),
                          alt=tr_copy.alt.copy(), e=tr_copy.e.copy(), n=tr_copy.n.copy(),
                          u=tr_copy.u.copy(), speed=tr_copy.speed.copy(),
                          src_names=["POS"])
    w.set_track(tr_copy)
    pump(300)
    print("   全 nan state:", w.state())
    ok = w.export_png(str(OUT / "trackmap_empty.png"))
    print("   保存 empty png:", ok)
    w.close()
    pump(100)

    print("=" * 78)
    print("生成文件:")
    for f in sorted(OUT.glob("trackmap*.png")):
        print(f"   {f.name}  {f.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
