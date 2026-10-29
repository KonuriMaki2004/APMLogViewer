# -*- coding: utf-8 -*-
"""APMLogViewer 打包工具（可重复执行）。

    .\\.venv\\Scripts\\python.exe packaging\\build.py icon              # 生成 packaging/icon.ico
    .\\.venv\\Scripts\\python.exe packaging\\build.py clean            # 清 build/ dist/ 与打包自检图
    .\\.venv\\Scripts\\python.exe packaging\\build.py build            # icon → PyInstaller → 校验
    .\\.venv\\Scripts\\python.exe packaging\\build.py report          # 体积/警告/数据文件检查
    .\\.venv\\Scripts\\python.exe packaging\\build.py run --log X --png Y --platform offscreen
    .\\.venv\\Scripts\\python.exe packaging\\build.py smoke --rounds 3
    .\\.venv\\Scripts\\python.exe packaging\\build.py all              # icon + clean + build + report

Windows 控制台是 GBK：本脚本第一件事就是把 stdout/stderr 换成 UTF-8。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")      # type: ignore[attr-defined]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
LOGS = PKG / "logs"
APP = "APMLogViewer"
SPEC = PKG / "apmlogviewer.spec"
ICON = PKG / "icon.ico"
WORK = ROOT / "build" / "pyi"
DIST = ROOT / "dist"
OUT_EXE = DIST / APP / f"{APP}.exe"
INTERNAL = DIST / APP / "_internal"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _die(msg: str, code: int = 1) -> None:
    print(f"[build] FAIL: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ========================================================================== 图标 =
def _render_rgb(size: int, ss: int = 4):
    """numpy 矢量化画一个俯视四轴 + 震动波形图标，返回 (size,size,3) uint8 与 alpha。"""
    import numpy as np

    n = size * ss
    step = 2.0 / n
    # 像素中心（含超采样偏移），坐标 [-1,1]，y 向下
    base = -1.0 + step * (np.arange(n) + 0.5)
    gx, gy = np.meshgrid(base, base)

    def rrect(a, b, r):
        """圆角矩形有向距离（负=内）。"""
        qx = np.abs(a) - (b - r)
        qy = np.abs(b * 0 + a * 0)  # placeholder, replaced below
        return qx, qy

    # --- 背景圆角方块 ---------------------------------------------------
    B = 0.97
    R = 0.24
    dx = np.abs(gx) - (B - R)
    dy = np.abs(gy) - (B - R)
    d_bg = np.sqrt(np.maximum(dx, 0.0) ** 2 + np.maximum(dy, 0.0) ** 2) \
        + np.minimum(np.maximum(dx, dy), 0.0) - R
    inside = d_bg < 0.0

    # 竖向深蓝渐变
    t = (gy + 1.0) / 2.0
    top = np.array([13.0, 34.0, 56.0])
    bot = np.array([7.0, 17.0, 30.0])
    rgb = top[None, None, :] * (1 - t)[..., None] + bot[None, None, :] * t[..., None]

    def sdist_seg(px, py, a, b, half):
        """线段的有向距离（到线段中心距离 - half）。"""
        ax, ay = a
        bx, by = b
        vx, vy = bx - ax, by - ay
        wx, wy = px - ax, py - ay
        L2 = vx * vx + vy * vy
        tt = np.clip((wx * vx + wy * vy) / max(L2, 1e-12), 0.0, 1.0)
        cx, cy = ax + tt * vx, ay + tt * vy
        return np.sqrt((px - cx) ** 2 + (py - cy) ** 2) - half

    def ring(px, py, c, r_in, r_out):
        d = np.sqrt((px - c[0]) ** 2 + (py - c[1]) ** 2)
        return np.maximum(r_in - d, d - r_out)

    # --- 四条机臂 + 四个旋翼环 -----------------------------------------
    arms = [(-0.50, -0.50), (0.50, -0.50), (-0.50, 0.50), (0.50, 0.50)]
    cyan = np.array([99.0, 216.0, 255.0])
    dark = np.array([61.0, 111.0, 150.0])
    for c in arms:
        d_arm = sdist_seg(gx, gy, (0.0, 0.0), c, 0.055)
        m = (d_arm < 0.0) & inside
        rgb[m] = dark[None, None, :]
        d_ring = ring(gx, gy, c, 0.175, 0.245)
        m = (d_ring < 0.0) & inside
        rgb[m] = cyan[None, None, :]
        # 桨叶：环内三条短辐条
        ang = np.arctan2(gy - c[1], gx - c[0])
        rad = np.sqrt((gx - c[0]) ** 2 + (gy - c[1]) ** 2)
        blade = (np.abs((ang * 3) % (2 * np.pi) - np.pi) < 0.30) & (rad < 0.175) & (rad > 0.02)
        m = blade & inside
        rgb[m] = (cyan * 0.72)[None, None, :]
    # 中心机体
    d_body = ring(gx, gy, (0.0, 0.0), 0.0, 0.175)
    m = (d_body < 0.0) & inside
    rgb[m] = np.array([232.0, 244.0, 255.0])[None, None, :]
    d_core = ring(gx, gy, (0.0, 0.0), 0.0, 0.075)
    m = (d_core < 0.0) & inside
    rgb[m] = np.array([13.0, 34.0, 56.0])[None, None, :]

    # --- 底部震动波形（橙色）-------------------------------------------
    wy = 0.66 + 0.13 * np.sin(7.5 * np.pi * gx) * np.exp(-((gx * 1.15) ** 4))
    d_wave = np.abs(gy - wy) - 0.045
    m = (d_wave < 0.0) & inside
    rgb[m] = np.array([255.0, 176.0, 78.0])[None, None, :]

    # --- 抗锯齿：超采样均值 --------------------------------------------
    a_in = inside.reshape(size, ss, size, ss).mean(axis=(1, 3))
    img = rgb.reshape(size, ss, size, ss, 3).mean(axis=(1, 3))
    alpha = np.clip(a_in, 0.0, 1.0)
    return (np.clip(img, 0, 255).astype("uint8")), (np.clip(alpha * 255, 0, 255).astype("uint8"))


def _png_bytes(rgb, a) -> bytes:
    """把 RGBA 编成 PNG（无过滤，纯 stdlib+zlib）。"""
    h, w, _ = rgb.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw.extend((int(rgb[y, x, 0]), int(rgb[y, x, 1]), int(rgb[y, x, 2]), int(a[y, x])))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def _bmp_entry(rgb, a, size: int) -> bytes:
    """ICO 里的 32bpp BMP 项（BGRA，自下而上）+ 1bpp AND 掩码。"""
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    px = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            # 预乘到 0 alpha 处也保持颜色，避免黑边
            px.extend((int(rgb[y, x, 2]), int(rgb[y, x, 1]), int(rgb[y, x, 0]), int(a[y, x])))
    row = (size + 31) // 32 * 4
    mask = bytearray()
    for y in range(size - 1, -1, -1):
        r = bytearray(row)
        for x in range(size):
            if a[y, x] == 0:
                r[x // 8] |= 0x80 >> (x % 8)
        mask.extend(r)
    return hdr + bytes(px) + bytes(mask)


def make_icon(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> dict:
    t0 = time.time()
    imgs = {}
    for s in sizes:
        ss = 4 if s <= 64 else 2
        imgs[s] = _render_rgb(s, ss=ss)
    entries = []
    for s in sizes:
        rgb, a = imgs[s]
        data = _png_bytes(rgb, a) if s >= 128 else _bmp_entry(rgb, a, s)
        entries.append((s, data))
    body_len = sum(len(d) for _, d in entries)
    off = 6 + 16 * len(entries)
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    for s, d in entries:
        w = 0 if s >= 256 else s
        h = 0 if s >= 256 else s
        bpp = 32
        out += struct.pack("<BBBBHHII", w, h, 0, 0, 1, bpp, len(d), off)
        off += len(d)
    for _, d in entries:
        out += d
    assert len(out) == off == 6 + 16 * len(entries) + body_len, (len(out), off)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return {"path": str(path), "bytes": len(out), "sizes": list(sizes),
            "seconds": round(time.time() - t0, 2)}


# ========================================================================== 清理 =
def clean(args) -> dict:
    removed = []
    for p in (ROOT / "build", DIST, ROOT / "selftest" / "pkg"):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(str(p))
    if args.all_users_cache:
        print("[build] 拒绝动用户缓存目录（验收要求它留在 %LOCALAPPDATA%）", file=sys.stderr)
    return {"removed": removed}


# ========================================================================= 构建 ===
def build(args) -> dict:
    LOGS.mkdir(parents=True, exist_ok=True)
    print(json.dumps(make_icon(ICON), ensure_ascii=False), flush=True)
    if not VENV_PY.is_file():
        _die(f"找不到 venv python: {VENV_PY}")
    cmd = [str(VENV_PY), "-m", "PyInstaller", "--noconfirm",
           "--workpath", str(WORK), "--distpath", str(DIST),
           str(SPEC)]
    if getattr(args, "use_cache", False):
        cmd.append("--clean")        # 连 PyInstaller 全局缓存一起清，最严格
    t0 = time.time()
    log_path = LOGS / f"build-{time.strftime('%Y%m%d-%H%M%S')}.log"
    with open(log_path, "w", encoding="utf-8", errors="replace") as fp:
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=fp, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace")
    dt = time.time() - t0
    txt = log_path.read_text(encoding="utf-8", errors="replace")
    tail = [ln for ln in txt.splitlines() if ln.strip()][-25:]
    print(f"[build] rc={proc.returncode} 用时 {dt:.1f}s 完整日志 {log_path}", flush=True)
    for ln in tail:
        print("   |", ln, flush=True)
    if proc.returncode != 0 or not OUT_EXE.is_file():
        _die(f"PyInstaller 失败（rc={proc.returncode}），见 {log_path}")
    return {"rc": proc.returncode, "seconds": round(dt, 1), "log": str(log_path)}


# ======================================================================= 校验 ====
def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def report(args) -> dict:
    if not OUT_EXE.is_file():
        _die(f"产物不存在: {OUT_EXE}（先跑 build）")
    total = _dir_bytes(DIST / APP)
    data_ok = {
        "config/alerts.yaml": (INTERNAL / "config" / "alerts.yaml").is_file(),
        "data/codebooks/copter_4x.json": (INTERNAL / "data" / "codebooks" / "copter_4x.json").is_file(),
    }
    # 图标资源有没有进 exe（RT_ICON=3，RT_GROUP_ICON=14）
    icon_res: dict | str = {}
    try:
        import pefile
        pe = pefile.PE(str(OUT_EXE))
        ids = set()
        for e in getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None).entries:
            try:
                ids.add(int(e.id))
            except Exception:
                pass
        icon_res = {"rt_icon(3)": 3 in ids, "rt_group_icon(14)": 14 in ids,
                    "resource_types": sorted(ids)}
        pe.close()
    except Exception as exc:
        icon_res = {"error": repr(exc)}
    biggest = sorted((f for f in (DIST / APP).rglob("*") if f.is_file()),
                     key=lambda f: -f.stat().st_size)[:12]
    warn = sorted(WORK.rglob("warnings-*.log")) + sorted(WORK.rglob("warn-*.txt"))
    warn_lines = []
    if warn:
        txt = warn[-1].read_text(encoding="utf-8", errors="replace")
        warn_lines = [ln for ln in txt.splitlines() if ln.strip()]
    out = {
        "exe_bytes": OUT_EXE.stat().st_size,
        "dist_total_bytes": total,
        "dist_total_mb": round(total / 1048576, 1),
        "data_files": data_ok,
        "icon_resource": icon_res,
        "warnings_log": str(warn[-1]) if warn else None,
        "warnings_lines": len(warn_lines),
        "largest": [{"path": str(f.relative_to(DIST / APP)), "mb": round(f.stat().st_size / 1048576, 2)}
                    for f in biggest],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    if not all(data_ok.values()):
        print("[build] 警告：必需数据文件缺失！", file=sys.stderr)
    return out


# ========================================================================= 跑 ====
def run_packaged(args) -> dict:
    """用打包好的 exe 跑一次（可 offscreen / 真实 windows 平台），stdout 走 APMLOG_LOGFILE。"""
    if not OUT_EXE.is_file():
        _die(f"产物不存在: {OUT_EXE}")
    env = dict(os.environ)
    if args.platform == "offscreen":
        env["QT_QPA_PLATFORM"] = "offscreen"
    else:
        env.pop("QT_QPA_PLATFORM", None)
    if args.tilecache is not None:
        env["APMLOG_TILECACHE"] = args.tilecache
    if args.cache is not None:
        env["APMLOG_CACHE"] = args.cache
    if args.debug_paths:
        env["APMLOG_PKG_DEBUG"] = "1"
    log = LOGS / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{args.platform}.txt"
    LOGS.mkdir(parents=True, exist_ok=True)
    env["APMLOG_LOGFILE"] = str(log)
    env["APMLOG_NO_CONSOLE"] = "1"          # 自动化用文件捕获，不需要控制台
    cmd = [str(OUT_EXE)] + args.exe_args
    t0 = time.time()
    proc = subprocess.run(cmd, env=env, cwd=str(args.cwd or ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=args.timeout)
    dt = time.time() - t0
    txt = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    print(f"[run] platform={args.platform} rc={proc.returncode} wall={dt:.2f}s log={log}")
    print("--- exe 输出（来自 APMLOG_LOGFILE）---")
    print(txt.rstrip() or "(空)")
    if proc.stdout:
        print("--- 管道里的额外 stdout ---")
        print(proc.stdout.rstrip())
    return {"rc": proc.returncode, "wall": round(dt, 2), "log": str(log)}


# ===================================================================== 冒烟 ====
def smoke(args) -> dict:
    """连开连关 GUI（真实 windows 平台），用 WM_CLOSE 走正常 closeEvent，看退出码。"""
    if not OUT_EXE.is_file():
        _die(f"产物不存在: {OUT_EXE}")
    import ctypes

    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    results = []
    for i in range(args.rounds):
        env = dict(os.environ)
        env["APMLOG_NO_CONSOLE"] = "1"
        log = LOGS / f"smoke-{i}-{time.strftime('%H%M%S')}.txt"
        env["APMLOG_LOGFILE"] = str(log)
        t0 = time.time()
        proc = subprocess.Popen([str(OUT_EXE)] + ([args.log] if args.log else []),
                                cwd=str(ROOT), env=env)
        hwnd = _find_window(user32, proc.pid, args.title_prefix, args.find_timeout)
        if hwnd:
            time.sleep(args.settle)
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            try:
                rc = proc.wait(timeout=args.close_timeout)
                killed = False
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
                killed = True
            results.append({"round": i, "hwnd": int(hwnd), "rc": rc, "killed": killed,
                            "wall": round(time.time() - t0, 2), "log": str(log)})
        else:
            proc.kill()
            rc = proc.wait()
            results.append({"round": i, "hwnd": None, "rc": rc, "error": "窗口没找到",
                            "log": str(log)})
        time.sleep(0.5)
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    bad = [r for r in results if r.get("rc") != 0]
    if bad:
        print(f"[smoke] 有 {len(bad)}/{len(results)} 轮退出码非 0", file=sys.stderr)
    return {"rounds": args.rounds, "results": results}


def _find_window(user32, pid: int, prefix: str, timeout: float = 20.0):
    """按进程 id + 标题前缀找可见主窗，轮询到 timeout。"""
    import ctypes
    from ctypes import wintypes

    end = time.time() + timeout
    while time.time() < end:
        found = []
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(h, l):
            if not user32.IsWindowVisible(h):
                return True
            wp = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(wp))
            if wp.value != pid:
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(h, buf, 512)
            if buf.value and buf.value.startswith(prefix):
                found.append(h)
                return False
            return True

        user32.EnumWindows(EnumProc(cb), 0)
        if found:
            return found[0]
        time.sleep(0.1)
    return None


# ==================================================================== GUI 拉起 ===
def gui_check(args) -> dict:
    """真实 windows 平台：带日志启动 → 窗口出现 → 标题正确 → WM_CLOSE 干净退出。

    可选 ``--profile DIR`` 把 LOCALAPPDATA/APPDATA/USERPROFILE 全重定向到一个空目录，
    用来证明产物不依赖开发机的用户目录（cache/views.json 首运自动建，不当内置资源读）。
    """
    if not OUT_EXE.is_file():
        _die(f"产物不存在: {OUT_EXE}")
    import ctypes
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010

    env = dict(os.environ)
    env["APMLOG_NO_CONSOLE"] = "1"
    env.pop("QT_QPA_PLATFORM", None)          # 确保走真实 windows 平台
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"gui-{time.strftime('%H%M%S')}.txt"
    env["APMLOG_LOGFILE"] = str(log)
    profile = None
    if args.profile:
        profile = Path(args.profile).resolve()
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        (profile / "LocalAppData").mkdir(parents=True, exist_ok=True)
        for k in ("LOCALAPPDATA", "APPDATA", "LocalAppData"):
            env[k] = str(profile / "LocalAppData")
        env["USERPROFILE"] = str(profile)
        env["HOME"] = str(profile)
    for k in ("APMLOG_CACHE", "APMLOG_TILECACHE"):
        env.pop(k, None)                       # 不指定缓存目录，就验默认落点

    cmd = [str(OUT_EXE)] + ([str(Path(args.log).resolve())] if args.log else [])
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
    hwnd = _find_window(user32, proc.pid, args.title_prefix, args.find_timeout)
    title, waited = "", 0.0
    if hwnd:
        while waited < args.title_timeout:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            if args.expect_in_title and args.expect_in_title in title:
                break
            if not args.expect_in_title:
                break
            time.sleep(0.2)
            waited = time.time() - t0
    time.sleep(args.settle)
    rc = proc.poll()
    killed = False
    if rc is None and hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        try:
            rc = proc.wait(timeout=args.close_timeout)
        except subprocess.TimeoutExpired:
            proc.kill(); rc = proc.wait(); killed = True
    elif rc is None:
        proc.kill(); rc = proc.wait(); killed = True
    txt = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    tb = "Traceback" in txt or "Error:" in txt and "ModuleNotFound" in txt
    created = []
    if profile:
        created = sorted(str(p.relative_to(profile))
                         for p in profile.rglob("*") if p.is_file())
    out = {"window_found": bool(hwnd), "title": title, "rc": rc, "killed": killed,
           "wall": round(time.time() - t0, 2), "log": str(log),
           "traceback_in_log": tb, "log_bytes": len(txt),
           "profile_created_files": created[:40], "profile_file_count": len(created)}
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    return out


# ======================================================================== CLI ====
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="packaging/build.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("icon")
    p = sub.add_parser("clean")
    p.add_argument("--all-users-cache", action="store_true")

    p = sub.add_parser("build")
    p.add_argument("--use-cache", action="store_true", help="加 --clean（连全局缓存一起重扫，更慢更严格）")

    sub.add_parser("report")

    p = sub.add_parser("run")
    p.add_argument("--log", required=True)
    p.add_argument("--png", default="selftest/pkg/exe.png")
    p.add_argument("--preset", default="vibe")
    p.add_argument("--platform", choices=["offscreen", "windows"], default="offscreen")
    p.add_argument("--cache", default=None)
    p.add_argument("--tilecache", default=None)
    p.add_argument("--debug-paths", action="store_true")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--cwd", default=None)
    p.add_argument("--no-selftest", action="store_true", help="只把 --log 当普通 GUI 参数传")
    p.add_argument("extra", nargs="*", help="额外 exe 参数（含 'cli ...'）")

    p = sub.add_parser("gui")
    p.add_argument("--log", default=None, help="启动时直接加载的日志（不传就是空主窗）")
    p.add_argument("--profile", default=None,
                   help="把 LOCALAPPDATA/USERPROFILE 重定向到这个空目录，验证首运行为")
    p.add_argument("--title-prefix", default="APMLogViewer")
    p.add_argument("--expect-in-title", default="", help="标题里必须出现的子串（如日志文件名）")
    p.add_argument("--find-timeout", type=float, default=40.0)
    p.add_argument("--title-timeout", type=float, default=25.0)
    p.add_argument("--settle", type=float, default=5.0)
    p.add_argument("--close-timeout", type=float, default=30.0)

    p = sub.add_parser("smoke")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--log", default=None, help="启动时直接加载的日志（会拉起地图/瓦片线程）")
    p.add_argument("--settle", type=float, default=6.0)
    p.add_argument("--find-timeout", type=float, default=40.0)
    p.add_argument("--close-timeout", type=float, default=30.0)
    p.add_argument("--title-prefix", default="APMLogViewer")

    sub.add_parser("all")

    a = ap.parse_args(argv)
    if a.cmd == "icon":
        print(json.dumps(make_icon(ICON), ensure_ascii=False), flush=True)
    elif a.cmd == "clean":
        print(json.dumps(clean(a), ensure_ascii=False), flush=True)
    elif a.cmd == "build":
        build(a)
        report(a)
    elif a.cmd == "report":
        report(a)
    elif a.cmd == "run":
        if a.no_selftest:
            a.exe_args = [str(Path(a.log).resolve())] + list(a.extra or [])
        else:
            a.exe_args = ["--selftest", str(Path(a.log).resolve()), "--preset", a.preset,
                          "--png", a.png] + list(a.extra or [])
        run_packaged(a)
    elif a.cmd == "gui":
        gui_check(a)
    elif a.cmd == "smoke":
        smoke(a)
    elif a.cmd == "all":
        clean(argparse.Namespace(all_users_cache=False))
        build(a)
        report(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
