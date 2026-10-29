"""_tiles —— XYZ 瓦片源定义、异步抓取、磁盘缓存、内存 LRU、失败退避与离线降级。

实测连通性（本机网络，2026-10-17，取日志真实位置 天津 38.9617576/117.3196055）：
- tile.openstreetmap.org   HTTP 200，但**不同 z/x/y 全部返回同一张 6987 B 占位图**
  （sha1 恒为 0cfb5f4431）→ 被 OSM 官方限流。保留在源列表并标注，**不作默认源**。
- tile.openstreetmap.de    HTTP 200，19419 B，内容真实 → 默认矢量源。
- server.arcgisonline.com (World Imagery)  HTTP 200，19221/20744 B JPEG，内容真实 → 默认影像源。
- webrd0{1-4}/webst0{1-4}.is.autonavi.com (高德矢量/影像)  HTTP 200，内容真实；datum=GCJ02。
- a.basemaps.cartocdn.com  HTTP 200，内容真实。
- a.tile.openstreetmap.fr  HTTP 404（URL 规则不同，未纳入）。

线程模型：全部走 QNetworkAccessManager 异步 + Qt 信号，回调只在 GUI 线程执行，
绝不从后台线程改场景。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from PySide6 import QtCore, QtGui, QtNetwork
import shiboken6 as _sb

from . import _tilemath as tm

log = logging.getLogger(__name__)

Key = tuple[str, int, int, int]      # (source, z, x, y)

USER_AGENT = "APMLogViewer/0.1 (offline flight-log analysis tool)"


# --------------------------------------------------------------------------- #
# 源定义
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TileSource:
    """一个 XYZ 瓦片源。"""

    name: str
    url_template: str
    attribution: str
    datum: str = "WGS84"              # 'WGS84' | 'GCJ02'
    kind: str = "vector"              # 'vector' | 'imagery'
    min_zoom: int = 1
    max_zoom: int = 19
    subdomains: tuple[str, ...] = ()

    def format_url(self, z: int, x: int, y: int, rot: int = 0) -> str:
        """按 z/x/y 生成 URL，处理 {s} 子域轮询；越界返回空串。"""
        n = tm.tile_count(z)
        if not (self.min_zoom <= z <= self.max_zoom and 0 <= x < n and 0 <= y < n):
            return ""
        u = self.url_template
        if "{s}" in u and self.subdomains:
            u = u.replace("{s}", self.subdomains[rot % len(self.subdomains)])
        elif "{s}" in u:
            u = u.replace("{s}", "a")
        return u.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))

    def clamp_zoom(self, z: float) -> int:
        return int(max(self.min_zoom, min(self.max_zoom, z)))


BUILTIN_SOURCES: tuple[TileSource, ...] = (
    TileSource(
        name="osm_de",
        url_template="https://tile.openstreetmap.de/{z}/{x}/{y}.png",
        attribution="© OpenStreetMap contributors（德国镜像，WGS-84）",
        datum="WGS84", kind="vector", max_zoom=18,
    ),
    TileSource(
        name="esri_imagery",
        url_template="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attribution="© Esri, Maxar, Earthstar Geographics（WGS-84 影像）",
        datum="WGS84", kind="imagery", max_zoom=19,
    ),
    TileSource(
        name="gaode_vector",
        url_template="https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
        attribution="© 高德 Gaode 矢量（GCJ-02，已自动加偏）",
        datum="GCJ02", kind="vector", max_zoom=18,
        subdomains=("1", "2", "3", "4"),
    ),
    TileSource(
        name="gaode_imagery",
        url_template="https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
        attribution="© 高德 Gaode 影像（GCJ-02，已自动加偏）",
        datum="GCJ02", kind="imagery", max_zoom=18,
        subdomains=("1", "2", "3", "4"),
    ),
    TileSource(
        name="carto_light",
        url_template="https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        attribution="© OpenStreetMap contributors © CARTO（本机实测返回同一占位图，慎用）",
        datum="WGS84", kind="vector", max_zoom=20,
    ),
    TileSource(
        name="osm_standard",
        url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attribution="© OpenStreetMap contributors（本机实测被返回占位图，慎用）",
        datum="WGS84", kind="vector", max_zoom=19,
    ),
)

DEFAULT_SOURCE = "esri_imagery"
DEFAULT_VECTOR_SOURCE = "osm_de"


def default_cache_dir() -> str:
    """瓦片磁盘缓存根目录，可用 APMLOG_TILECACHE 覆盖（绝不写用户日志目录）。"""
    env = os.environ.get("APMLOG_TILECACHE")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "APMLogViewer", "tiles")


def _now() -> float:
    return time.monotonic()


# --------------------------------------------------------------------------- #
# 瓦片仓库
# --------------------------------------------------------------------------- #


class TileStore(QtCore.QObject):
    """内存 LRU + 磁盘缓存 + 受限并发网络抓取 + 失败退避。

    信号:
        tile_ready(source, z, x, y, pixmap)  pixmap.isNull() 表示该瓦片不可用（需画占位）
    """

    tile_ready = QtCore.Signal(str, int, int, int, QtGui.QPixmap)

    def __init__(self, cache_dir: Optional[str] = None, max_concurrent: int = 6,
                 mem_capacity: int = 600, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._cache_dir = cache_dir or default_cache_dir()
        self._max_concurrent = max(1, int(max_concurrent))
        self._mem_capacity = max(16, int(mem_capacity))
        self._mem: OrderedDict[Key, QtGui.QPixmap] = OrderedDict()
        self._sources: dict[str, TileSource] = {s.name: s for s in BUILTIN_SOURCES}
        self._queue: list[Key] = []
        self._queued: set[Key] = set()
        self._inflight: dict[QtNetwork.QNetworkReply, Key] = {}
        self._failed: dict[Key, float] = {}
        self._paused_until: dict[str, float] = {}
        self._streak: dict[str, int] = {}
        self._rot: dict[str, int] = {}
        self._fingerprint: dict[str, str] = {}
        self._warned_placeholder: set[str] = set()
        self._stopping = False
        self._pump_pending = False          # 已排了一个 0ms 定时器等下一轮发请求
        self._online: Optional[bool] = None
        self._mgr = QtNetwork.QNetworkAccessManager(self)
        try:
            self._mgr.setCache(None)                 # 我们自己管缓存，避免双份内存
        except Exception:                            # pragma: no cover - 版本差异容错
            pass
        self._mgr.finished.connect(self._on_finished)
        self.stats = {"mem_hit": 0, "disk_hit": 0, "fetched": 0, "failed": 0,
                      "skipped": 0, "queued": 0, "network": 0}

    # -------------------------------------------------------------- 配置
    @property
    def cache_dir(self) -> str:
        return self._cache_dir

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def register_source(self, src: TileSource) -> None:
        self._sources[src.name] = src

    def source(self, name: str) -> Optional[TileSource]:
        return self._sources.get(name)

    def set_online(self, online: Optional[bool]) -> None:
        """None=自动判定，False=强制离线（自测用），True=强制在线。"""
        self._online = online
        if online is False:
            self.cancel_all()
            log.info("瓦片仓库切换到强制离线模式")

    @property
    def online(self) -> bool:
        if self._online is not None:
            return self._online
        try:
            info = QtNetwork.QNetworkInformation.instance()
            reach = info.networkReachability()
            if reach == QtNetwork.QNetworkInformation.NetworkReachability.NotReachable:
                return False
            if reach == QtNetwork.QNetworkInformation.NetworkReachability.ReachableViaLocalAreaNetwork:
                return True
        except Exception:                            # pragma: no cover - 平台无后端
            pass
        return True

    # -------------------------------------------------------------- 缓存层
    def _disk_path(self, key: Key) -> str:
        src, z, x, y = key
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", src)
        return os.path.join(self._cache_dir, safe, str(z), str(x), f"{y}.img")

    def _load_disk(self, key: Key) -> Optional[QtGui.QPixmap]:
        p = self._disk_path(key)
        try:
            if not os.path.isfile(p) or os.path.getsize(p) == 0:
                return None
        except OSError:
            return None
        pm = QtGui.QPixmap(p)
        if pm.isNull():
            try:
                os.remove(p)                         # 坏缓存自愈
            except OSError:
                pass
            return None
        return pm

    def _save_disk(self, key: Key, data: bytes) -> None:
        p = self._disk_path(key)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, p)
        except OSError as exc:
            log.debug("瓦片写盘失败 %s: %s", p, exc)

    def _mem_put(self, key: Key, pm: QtGui.QPixmap) -> None:
        self._mem[key] = pm
        self._mem.move_to_end(key)
        while len(self._mem) > self._mem_capacity:
            self._mem.popitem(last=False)

    def cached(self, key: Key) -> tuple[Optional[QtGui.QPixmap], str]:
        """只查缓存不发请求。返回 (pixmap, 来源 'mem'|'disk'|'')。"""
        pm = self._mem.get(key)
        if pm is not None and not pm.isNull():
            self._mem.move_to_end(key)
            self.stats["mem_hit"] += 1
            return pm, "mem"
        pm = self._load_disk(key)
        if pm is not None and not pm.isNull():
            self._mem_put(key, pm)
            self.stats["disk_hit"] += 1
            return pm, "disk"
        return None, ""

    # -------------------------------------------------------------- 请求
    def request(self, src: TileSource, z: int, x: int, y: int) -> Optional[QtGui.QPixmap]:
        """请求一张瓦片：命中缓存同步返回并发 tile_ready；否则排队返回 None。"""
        self.register_source(src)
        key = (src.name, int(z), int(x), int(y))
        pm, _ = self.cached(key)
        if pm is not None:
            self.tile_ready.emit(*key, pm)
            return pm
        if not self.online:
            self.stats["skipped"] += 1
            self.tile_ready.emit(*key, QtGui.QPixmap())
            return None
        now = _now()
        if self._paused_until.get(src.name, 0.0) > now:
            self.stats["skipped"] += 1
            return None                              # 源级暂停，静默跳过
        if key in self._queued or key in self._inflight.values():
            return None                              # 去重
        t_fail = self._failed.get(key, 0.0)
        if t_fail and now - t_fail < 300.0:
            self.stats["skipped"] += 1
            return None                              # 单张失败退避
        self._queued.add(key)
        self._queue.append(key)
        self.stats["queued"] += 1
        self._pump()
        return None

    def _pump(self) -> None:
        """请求调度入口：**不平共发**，而是每轮事件循环只发一个。

        为什么：``QNetworkAccessManager.get()`` 是**同步**调用，实测一次可达 19 ms
        （首次还有代理/ DNS 预热）。以前在一个 ``while`` 里连发 ``max_concurrent`` 个，
        一次滚动就在 UI 线程吞掉 100 ms 以上，整个窗口跟着卡——实测事件循环
        最大停顿 608 ms（用户 2026-10-24 报“大日志下移动地图非常卡”）。
        改成用 0 ms 定时器一次发一个：单轮停顿从“6×N”降到“1×N”，
        并发上限仍由 ``_inflight`` 控制，吞吐不受影响。
        """
        if self._stopping or self._pump_pending:
            return
        if not self._queue or len(self._inflight) >= self._max_concurrent:
            return
        self._pump_pending = True
        QtCore.QTimer.singleShot(0, self._pump_step)

    def _pump_step(self) -> None:
        """发一个请求，剩下的留到下一轮事件循环。"""
        self._pump_pending = False
        if self._stopping:
            return
        if not self._queue or len(self._inflight) >= self._max_concurrent:
            return
        key = self._queue.pop(0)
        self._queued.discard(key)
        src = self._sources.get(key[0])
        if src is not None:
            self._rot[src.name] = self._rot.get(src.name, 0) + 1
            url = src.format_url(key[1], key[2], key[3], self._rot[src.name])
            if not url:
                self._mark_failed(key, reason="越界 URL")
            else:
                try:
                    req = QtNetwork.QNetworkRequest(QtCore.QUrl(url))
                    req.setHeader(QtNetwork.QNetworkRequest.KnownHeaders.UserAgentHeader,
                                  USER_AGENT)
                    req.setTransferTimeout(8000)
                    self._inflight[self._mgr.get(req)] = key
                    self.stats["network"] += 1
                except Exception as exc:             # pragma: no cover - API 版本差异兜底
                    log.warning("瓦片请求构造失败: %s", exc)
                    self._mark_failed(key, reason=f"construct:{exc}")
        self._pump()                                     # 下一轮继续

    def cancel_all(self) -> None:
        """切源/关窗前中止全部在途请求并清队列。"""
        self._pump_pending = False
        for reply in list(self._inflight):
            try:
                if _sb.isValid(reply):
                    reply.abort()
            except Exception:
                pass
            self._inflight.pop(reply, None)
        self._queued.clear()
        self._queue.clear()

    def suspect(self, name: str) -> bool:
        """该源是否被识别为“不同瓦片返回同一内容”（限流/占位图）。"""
        return name in self._warned_placeholder

    def suspect_sources(self) -> set:
        return set(self._warned_placeholder)

    def clear_failures(self, source: Optional[str] = None) -> None:
        """手动“重试全部瓦片”时清除失败记录与源级暂停。"""
        if source is None:
            self._failed.clear()
            self._paused_until.clear()
            self._streak.clear()
        else:
            self._failed = {k: v for k, v in self._failed.items() if k[0] != source}
            self._paused_until.pop(source, None)
            self._streak.pop(source, None)

    def shutdown(self) -> None:
        """确定性收尾：中止在途请求、断开回调、清队列与内存。

        没这一步时，进程/解释器退出阶段 QNetworkReply 与 TileStore 的析构顺序
        不定，实测会在退出时触发段错误（反复建销组件的测试尤其明显）。
        可重复调用。
        """
        if self._stopping:
            return
        self._stopping = True
        self.cancel_all()
        try:
            self._mgr.finished.disconnect(self._on_finished)
        except (RuntimeError, TypeError):
            pass
        self._inflight.clear()
        self._queued.clear()
        self._queue.clear()
        self._mem.clear()

    def inflight(self) -> int:
        return len(self._inflight)

    def pending(self) -> int:
        return len(self._queue) + len(self._queued)

    # -------------------------------------------------------------- 回调
    def _on_finished(self, reply: QtNetwork.QNetworkReply) -> None:
        # finished 可能在底层 C++ reply（甚至 TileStore 自身）已被回收后才投递：
        # 切源 abort、关窗、嵌套事件循环都会造成这种竞态。PySide6 6.12 实测会抛
        # "already deleted" / "Signal source has been deleted"，两处都必须护栏。
        if not _sb.isValid(self) or not _sb.isValid(reply):
            return
        try:
            self._handle_finished(reply)
        except RuntimeError as exc:
            log.debug("丢弃迟到的瓦片回调: %s", exc)

    def _handle_finished(self, reply: QtNetwork.QNetworkReply) -> None:
        key = self._inflight.pop(reply, None)
        err = reply.error()
        data = bytes(reply.readAll()) if err == QtNetwork.QNetworkReply.NetworkError.NoError else b""
        try:
            http = int(reply.attribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0)
        except Exception:
            http = 0
        try:
            reply.deleteLater()
        except RuntimeError:
            pass
        if key is None:
            return
        name, z, x, y = key
        if err == QtNetwork.QNetworkReply.NetworkError.OperationCanceledError:
            # 自己 cancel_all() 掉发的取消，不能计为“源失败”，否则会误暂停。
            return
        if err != QtNetwork.QNetworkReply.NetworkError.NoError or not data:
            self._mark_failed(key, reason=f"{getattr(err, 'name', err)}/HTTP{http}")
            self._pump()
            return
        pm = QtGui.QPixmap()
        if not pm.loadFromData(data):
            self._mark_failed(key, reason="decode")
            self._pump()
            return
        self._detect_placeholder(name, data, z, x, y)
        self._streak[name] = 0
        self._failed.pop(key, None)
        self._save_disk(key, data)
        self._mem_put(key, pm)
        self.stats["fetched"] += 1
        self.tile_ready.emit(name, z, x, y, pm)
        self._pump()

    def _detect_placeholder(self, name: str, data: bytes, z: int, x: int, y: int) -> None:
        """识别“不同瓦片返回同一张图”的限流症状，只告警一次。"""
        fp = hashlib.sha1(data).hexdigest()[:10]
        prev = self._fingerprint.get(name)
        if prev is None:
            self._fingerprint[name] = fp
        elif prev == fp and name not in self._warned_placeholder:
            self._warned_placeholder.add(name)
            log.warning("瓦片源 %s 对 z=%d/x=%d/y=%d 返回与之前完全相同的内容(sha1=%s)，"
                        "疑似被限流或返回占位图，建议在地图上切换到其它源", name, z, x, y, fp)

    def _mark_failed(self, key: Key, reason: str = "") -> None:
        name = key[0]
        self._failed[key] = _now()
        self.stats["failed"] += 1
        streak = self._streak.get(name, 0) + 1
        self._streak[name] = streak
        if streak >= 4:
            self._paused_until[name] = _now() + 60.0
            self._streak[name] = 0
            self._queue = [k for k in self._queue if k[0] != name]
            log.warning("瓦片源 %s 连续失败 %d 次（最近原因 %s），暂停 60s 并降级为占位显示",
                        name, streak, reason)
        self.tile_ready.emit(*key, QtGui.QPixmap())


__all__ = ["TileSource", "TileStore", "BUILTIN_SOURCES", "DEFAULT_SOURCE",
           "DEFAULT_VECTOR_SOURCE", "default_cache_dir", "USER_AGENT"]
