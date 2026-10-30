# -*- coding: utf-8 -*-
"""apmlog.core.cache —— 解析缓存的占用统计与自动清理。

缓存目录（``%LOCALAPPDATA%\\APMLogViewer\\cache``，可用 ``APMLOG_CACHE`` 覆盖）下，
**每个子目录 = 一份日志的解析结果**（``index.json`` + 若干 ``.npz``）。清理就是按目录整体删，
不会碰同级的 ``settings.json`` / ``views.json``。

策略模型（三个字段描述一切，界面只是它的三种预设）：

* ``max_age_days`` —— 只删“最后使用时间早于 N 天”的缓存条目；
* ``max_bytes``   —— 超出容量时按“最久没用过优先”删到阈值以下；
* ``interval_days`` —— 每隔 N 天才执行一次（不是每天扫）。

预设：``7d`` = (7, -, 7)；``3d`` = (3, -, 3)；``1g`` = (-, 1 GiB, -)；
``custom`` = 用户自己填的天数/容量（填 0 表示该条件不启用）。

**保留一份最新的**：容量清理永远不会把“最近用过的那一份”删掉，否则会出现
“刚分析完的日志被自己清掉、下次还要重解析”的怪事。
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

MB = 1024 * 1024
DAY = 86400.0

#: 预设策略（键 → 给人看的名字）
POLICIES: dict[str, str] = {
    "7d": "每 7 天清理一次",
    "3d": "每 3 天清理一次",
    "1g": "超过 1 GB 清理一次",
    "custom": "自定义",
}


@dataclass
class CacheEntry:
    """缓存里的一份日志解析结果。"""

    path: Path
    bytes: int = 0
    files: int = 0
    used_at: float = 0.0            # 条目内文件的最新 mtime


@dataclass
class CacheUsage:
    bytes: int = 0
    files: int = 0
    entries: int = 0
    items: list[CacheEntry] = field(default_factory=list)


def scan(root: str | Path) -> CacheUsage:
    """统计缓存占用，并给每个条目算“最后使用时间”。"""
    root = Path(root)
    usage = CacheUsage()
    if not root.is_dir():
        return usage
    try:
        children = list(root.iterdir())
    except OSError:
        log.debug("缓存目录读不了：%s", root, exc_info=True)
        return usage
    for child in children:
        try:
            if child.is_dir():
                files = [f for f in child.rglob("*") if f.is_file()]
                size = 0
                newest = 0.0
                for f in files:
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    size += int(st.st_size)
                    newest = max(newest, float(st.st_mtime))
                usage.items.append(CacheEntry(child, size, len(files), newest))
                usage.bytes += size
                usage.files += len(files)
                usage.entries += 1
            elif child.is_file():
                st = child.stat()
                usage.items.append(CacheEntry(child, int(st.st_size), 1, float(st.st_mtime)))
                usage.bytes += int(st.st_size)
                usage.files += 1
                usage.entries += 1
        except OSError:
            continue
    usage.items.sort(key=lambda e: e.used_at)
    return usage


def policy_conditions(settings: Any) -> tuple[Optional[float], Optional[int], Optional[float]]:
    """把设置翻译成 ``(max_age_days, max_bytes, interval_days)``。"""
    pol = str(settings.get("cache_clean_policy") or "7d")
    if pol == "7d":
        return (7.0, None, 7.0)
    if pol == "3d":
        return (3.0, None, 3.0)
    if pol == "1g":
        return (None, 1024 * MB, None)
    days = max(0, int(settings.get("cache_custom_days") or 0))
    mb = max(0, int(settings.get("cache_custom_mb") or 0))
    return (float(days) if days else None,
            int(mb * MB) if mb else None,
            float(days) if days else None)


def _remove(entry: CacheEntry) -> tuple[int, int]:
    """删掉一个条目，返回 (文件数, 字节数)。删不动就跳过，不抛。"""
    try:
        if entry.path.is_dir():
            shutil.rmtree(entry.path, ignore_errors=True)
        else:
            entry.path.unlink()
    except OSError:
        log.debug("删不掉缓存条目：%s", entry.path, exc_info=True)
        return (0, 0)
    return (entry.files, entry.bytes)


def cleanup(root: str | Path, *, max_age_days: Optional[float] = None,
            max_bytes: Optional[int] = None, now: Optional[float] = None,
            keep_newest: bool = True) -> dict[str, Any]:
    """按条件清理，返回 ``{removed_entries, removed_files, freed, before, after, reason}``。"""
    root = Path(root)
    now = time.time() if now is None else float(now)
    before = scan(root)
    victims: list[CacheEntry] = []
    reasons: list[str] = []

    if max_age_days and max_age_days > 0:
        cutoff = now - float(max_age_days) * DAY
        stale = [e for e in before.items if e.used_at and e.used_at < cutoff]
        if stale:
            reasons.append(f"{max_age_days:g} 天未使用")
        victims.extend(stale)

    if max_bytes is not None and max_bytes >= 0:
        left = [e for e in before.items if e not in victims]
        total = sum(e.bytes for e in left)
        if total > max_bytes:
            # 最久没用过的先删；**永远留一份最新的**（否则刚分析完的日志会被自己清掉）
            order = sorted(left, key=lambda e: e.used_at)
            if keep_newest and order:
                keep = order[-1]
                order = [e for e in order if e is not keep]
            for e in order:
                if total <= max_bytes:
                    break
                victims.append(e)
                total -= e.bytes
            reasons.append(f"超出容量上限 {max_bytes / MB:.0f} MB")

    removed_files = 0
    freed = 0
    done: list[CacheEntry] = []
    for e in victims:
        n, b = _remove(e)
        if n or b or not e.path.exists():
            done.append(e)
        removed_files += n
        freed += b
    after = scan(root)
    if done:
        log.info("缓存清理：删 %d 项 / %d 个文件 / %.1f MB（%s）",
                 len(done), removed_files, freed / MB, "、".join(reasons) or "手动")
    return {
        "removed_entries": len(done),
        "removed_files": removed_files,
        "freed": freed,
        "before": before.bytes,
        "after": after.bytes,
        "reason": "、".join(reasons),
    }


def is_due(settings: Any, usage: Optional[CacheUsage] = None,
           *, now: Optional[float] = None, root: str | Path | None = None) -> bool:
    """现在该不该跑一次清理（只看触发条件，不删东西）。"""
    if not bool(settings.get("cache_auto_clean")):
        return False
    _age, max_bytes, interval = policy_conditions(settings)
    now = time.time() if now is None else float(now)
    if usage is None:
        if root is None:
            return False
        usage = scan(root)
    if max_bytes is not None and usage.bytes > max_bytes:
        return True
    if interval:
        last = float(settings.get("cache_clean_last_ts") or 0.0)
        return (now - last) >= interval * DAY
    return False


def run_if_due(root: str | Path, settings: Any, *, now: Optional[float] = None,
               force: bool = False) -> Optional[dict[str, Any]]:
    """开了自动清理且到点了就清一次；返回结果，没跑返回 ``None``。

    跑完把“上次时间 / 上次释放”写回设置，所以**重启后不会重复清**。
    """
    now = time.time() if now is None else float(now)
    if not force:
        if not bool(settings.get("cache_auto_clean")):
            return None
        if not is_due(settings, now=now, root=root):
            return None
    age, cap, _interval = policy_conditions(settings)
    if force:
        # 手动“立即清理”用当前策略的条件，但不受间隔限制
        pass
    res = cleanup(root, max_age_days=age, max_bytes=cap, now=now)
    try:
        settings.set("cache_clean_last_ts", now)
        settings.set("cache_clean_last_freed", int(res["freed"]))
    except Exception:                                   # noqa: BLE001 设置写失败不影响清理
        log.debug("写回清理时间失败", exc_info=True)
    return res
