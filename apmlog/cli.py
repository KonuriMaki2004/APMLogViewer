"""apmlog CLI —— 内核自检与批量索引入口（GUI 之外也能用）。

用法示例::

    python -m apmlog.cli scan "C:\\path\\to\\logs" --json
    python -m apmlog.cli stats  00000165.BIN
    python -m apmlog.cli channels 00000165.BIN --type VIBE
    python -m apmlog.cli series 00000165.BIN --channel "VIBE[1].VibeX" --csv out.csv
    python -m apmlog.cli events 00000165.BIN --kinds ERR,MSG,ARM --around 322.45
    python -m apmlog.cli alerts 00000165.BIN
    python -m apmlog.cli track  2026-10-16.bin --csv track.csv
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from apmlog.core.model import ChannelKey, human_size
from apmlog.core.store import LogStore

BIN_EXT = (".bin",)


def _find_logs(target: str | Path) -> list[Path]:
    p = Path(target)
    if p.is_dir():
        return sorted((f for f in p.rglob("*") if f.suffix.lower() in BIN_EXT and f.is_file()),
                      key=lambda f: f.stat().st_mtime)
    return [p] if p.exists() else []


def _open(path: Path, args: argparse.Namespace) -> LogStore:
    def prog(fr: float, msg: str) -> None:
        if not args.quiet:
            print(f"  [{fr * 100:5.1f}%] {msg}", end="\r" if args.no_tty else "", flush=True,
                  file=sys.stderr)
    st = LogStore.open(path, cache_dir=args.cache, progress=None if args.quiet else prog,
                       force=args.refresh, codebook_path=args.codebook)
    if not args.quiet and not args.no_tty:
        print(file=sys.stderr)
    return st


def cmd_scan(args: argparse.Namespace) -> int:
    logs: list[Path] = []
    for t in args.paths:
        logs.extend(_find_logs(t))
    if not logs:
        print("未找到 .BIN 日志", file=sys.stderr)
        return 2
    rows: list[dict[str, Any]] = []
    t0 = time.time()
    for p in logs:
        try:
            st = _open(p, args)
            s = st.stats
            al = [] if args.no_alerts else st.alerts(args.rules)
            rows.append({
                "name": st.name, "path": str(p), "size": s.size_bytes,
                "duration": s.duration, "firmware": s.firmware, "frame": s.frame_type,
                "types": s.n_types, "messages": s.n_messages, "params": s.n_params,
                "events": s.n_events, "pos_kind": st.meta.get("track", {}).get("kind", "none"),
                "pos_source": st.meta.get("track", {}).get("source", ""),
                "max_alt": s.max_alt, "distance": s.distance,
                "crashes": s.crashes, "alerts": [a.as_dict() for a in al],
                "warnings": s.warnings,
            })
            if not args.json:
                print(_line(rows[-1]))
        except Exception as exc:
            rows.append({"name": p.name, "path": str(p), "error": f"{type(exc).__name__}: {exc}"})
            print(f"{p.name:<34} 解析失败: {type(exc).__name__}: {exc}")
    dt = time.time() - t0
    if args.json:
        print(json.dumps({"total": len(logs), "seconds": round(dt, 2), "items": rows},
                         ensure_ascii=False, indent=1))
    else:
        ok = [r for r in rows if "error" not in r]
        print(f"\n合计 {len(ok)}/{len(rows)} 成功，索引耗时 {dt:.1f}s"
              f"（{sum(r['size'] for r in ok) / max(dt, 1e-6) / 1e6:.1f} MB/s）")
    return 0


def _line(r: dict[str, Any]) -> str:
    dur = r.get("duration", float("nan"))
    return (f"{r['name']:<30} {human_size(r.get('size', 0)):>9} "
            f"{(f'{dur:.0f}s' if math.isfinite(dur) else '--'):>7} "
            f"{r.get('types', 0):>4}种 {r.get('messages', 0):>8}条 "
            f"位置={r.get('pos_kind', ''):<9}{r.get('pos_source', ''):<6} "
            f"告警{len(r.get('alerts', [])) if r.get('alerts') is not None else '-'} "
            f"{'★炸机' if r.get('crashes') else ''}")


def cmd_stats(args: argparse.Namespace) -> int:
    st = _open(Path(args.path), args)
    s = st.stats
    print(json.dumps({"file": st.name, "stats": s.as_dict(), "track": st.meta.get("track"),
                      "bat_semantics": st.meta.get("bat_semantics"),
                      "segments": st.meta.get("segments"), "codebook": st.cb.provenance,
                      "cache": str(st.cache_dir), "built_in_s": st.meta.get("seconds")},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_channels(args: argparse.Namespace) -> int:
    st = _open(Path(args.path), args)
    for c in st.channel_infos(args.type):
        rng = ""
        if args.exact:
            try:
                s = st.channel_range(c.key)
                rng = f" min={s['min']:.3f} max={s['max']:.3f} mean={s['mean']:.3f} rms={s['rms']:.3f}"
            except Exception as exc:
                rng = f" (量程不可用: {exc})"
        elif math.isfinite(c.vmin):
            rng = f" min={c.vmin:.3f} max={c.vmax:.3f}"
        print(f"{c.key.label:<28} n={c.count:<8} rate={c.rate:6.1f}Hz unit={c.unit or '-':<6}{rng}")
    return 0


def cmd_series(args: argparse.Namespace) -> int:
    st = _open(Path(args.path), args)
    key = ChannelKey.parse(args.channel)
    s = st.series(key, args.t0, args.t1, args.max_points)
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            fh.write("t_s,%s\n" % key.label)
            for a, b in zip(s.t, s.v):
                fh.write(f"{a:.6f},{b if math.isfinite(b) else ''}\n")
        print(f"已写 {args.csv}（{s.n} 行）")
    else:
        print(json.dumps({"channel": key.label, "n": s.n, "unit": s.unit, **s.stats()},
                         ensure_ascii=False, indent=1))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    st = _open(Path(args.path), args)
    kinds = [k.strip().upper() for k in args.kinds.split(",")] if args.kinds else None
    t0 = t1 = None
    if args.around is not None:
        t0, t1 = args.around - args.window, args.around + args.window
    evs = st.events(kinds=kinds, severity_min=args.severity, t0=t0, t1=t1)
    for e in evs:
        print(f"{e.t:9.3f}s {e.kind:<5} {e.severity:<8} {e.display}")
    print(f"共 {len(evs)} 条")
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    st = _open(Path(args.path), args)
    hits = st.alerts(args.rules)
    for h in hits:
        print(f"{h.severity:<9}{h.title:<26} "
              f"t={'' if not math.isfinite(h.t0) else f'{h.t0:.1f}-{h.t1:.1f}s':<16}"
              f"值={h.value if math.isfinite(h.value) else '-':>8} 阈={h.threshold if math.isfinite(h.threshold) else '-':>8} "
              f"[{h.source}] {h.message}")
    print(f"\n共 {len(hits)} 条（震动+电量规则族，阈值出处见 [] ）")
    if args.json:
        print(json.dumps([h.as_dict() for h in hits], ensure_ascii=False, indent=1))
    return 0


def cmd_track(args: argparse.Namespace) -> int:
    st = _open(Path(args.path), args)
    tr = st.track()
    print(f"kind={tr.kind} source={tr.src_names} points={tr.n_points} "
          f"distance={tr.distance} max_speed={tr.max_speed} note={tr.note}")
    if tr.has_geodetic:
        print(f"lat {tr.lat.min():.6f}..{tr.lat.max():.6f} lon {tr.lon.min():.6f}..{tr.lon.max():.6f}")
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            fh.write("t_s,lat,lon,alt,east,north,up,speed\n")
            for i in range(tr.n_points):
                fh.write(",".join(f"{v:.5f}" for v in (tr.t[i], tr.lat[i], tr.lon[i], tr.alt[i],
                                                       tr.e[i], tr.n[i], tr.u[i], tr.speed[i])) + "\n")
        print(f"已写 {args.csv}")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    """连接/下载入口，实现在 apmlog.link（H 工作流交付后可用）。"""
    try:
        from apmlog.link import cli_main  # type: ignore
    except Exception as exc:
        print(f"link 模块尚未就绪或导入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return cli_main(args)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="apmlog", description="ArduCopter DataFlash .BIN 日志分析内核 CLI")
    ap.add_argument("--cache", help="缓存目录（默认 %%LOCALAPPDATA%%\\APMLogViewer\\cache）")
    ap.add_argument("--codebook", help="码表 JSON 路径")
    ap.add_argument("--refresh", action="store_true", help="强制重建缓存")
    ap.add_argument("--quiet", action="store_true", help="不打印进度")
    ap.add_argument("--no-tty", action="store_true", help="进度换行输出（管道友好）")
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="批量索引并摘要")
    p.add_argument("paths", nargs="+")
    p.add_argument("--json", action="store_true")
    p.add_argument("--rules", help="告警配置 yaml")
    p.add_argument("--no-alerts", action="store_true")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("stats", help="单份日志概要")
    p.add_argument("path")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("channels", help="列出数值通道")
    p.add_argument("path")
    p.add_argument("--type", help="只看某类报文，如 VIBE")
    p.add_argument("--exact", action="store_true", help="逐通道精确算量程（慢）")
    p.set_defaults(fn=cmd_channels)

    p = sub.add_parser("series", help="取一条通道")
    p.add_argument("path")
    p.add_argument("--channel", required=True)
    p.add_argument("--t0", type=float)
    p.add_argument("--t1", type=float)
    p.add_argument("--max-points", type=int, default=200000)
    p.add_argument("--csv")
    p.set_defaults(fn=cmd_series)

    p = sub.add_parser("events", help="事件时间线")
    p.add_argument("path")
    p.add_argument("--kinds", help="逗号分隔，如 ERR,MSG,ARM")
    p.add_argument("--severity", choices=("info", "hint", "warn", "error", "critical"))
    p.add_argument("--around", type=float, help="只看该时刻附近")
    p.add_argument("--window", type=float, default=5.0)
    p.set_defaults(fn=cmd_events)

    p = sub.add_parser("alerts", help="震动/电量告警")
    p.add_argument("path")
    p.add_argument("--rules", help="告警配置 yaml")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_alerts)

    p = sub.add_parser("track", help="位置轨迹")
    p.add_argument("path")
    p.add_argument("--csv")
    p.set_defaults(fn=cmd_track)

    p = sub.add_parser("link", help="MAVLink 连接/下载（由 apmlog.link 提供）")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_link)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    # Windows 默认控制台编码可能是 GBK，重定向时统一强制 UTF-8 避免乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
